"""
Proves bind_service(input_model=...) and the matching output
serialisation — added when wiring a real HTTP-built domain handler
(CreateItem.execute(self, item: Item, ...)) over gRPC for the first time
revealed two real gaps: the decoded json_payload is a plain dict/list
with no casting (unlike FastAPI's body_model, which already validates
before a handler ever sees it), and json.dumps can't serialise a
BaseModel a handler returns on its own.
"""
from __future__ import annotations

import grpc
import pytest
from pydantic import BaseModel

from openframe.protocol.grpc import ROUTE_METADATA_KEY, bind_service, generic_pb2, generic_pb2_grpc


class _Item(BaseModel):
    id: str
    name: str


class _EchoHandler:
    """Returns exactly what it received — proves the cast happened and the type survived."""

    async def execute(self, value, context=None):
        return value


class _CreateItemLikeHandler:
    """Mirrors a real -http-built handler: expects a validated _Item, returns one."""

    async def execute(self, item: _Item, context=None):
        assert isinstance(item, _Item)  # would be a plain dict without input_model casting
        return item


@pytest.fixture
async def grpc_server():
    server = grpc.aio.server()
    bind_service(server, "echo_raw", handler_factory=lambda: _EchoHandler())
    bind_service(
        server, "create_item", handler_factory=lambda: _CreateItemLikeHandler(), input_model=_Item
    )
    bind_service(
        server, "list_items", handler_factory=lambda: _EchoHandler(), input_model=list[_Item]
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    yield server, port
    await server.stop(None)


async def _call(port: int, route: str, payload) -> str:
    import json

    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    stub = generic_pb2_grpc.GenericServiceStub(channel)
    response = await stub.Call(
        generic_pb2.GenericRequest(json_payload=json.dumps(payload)),
        metadata=((ROUTE_METADATA_KEY, route),),
    )
    await channel.close()
    return response.json_payload


async def test_without_input_model_raw_dict_passes_through(grpc_server) -> None:
    _server, port = grpc_server
    result = await _call(port, "echo_raw", {"id": "abc", "name": "Widget"})
    assert result == '{"id": "abc", "name": "Widget"}'


async def test_input_model_casts_dict_into_basemodel_before_execute(grpc_server) -> None:
    _server, port = grpc_server
    result = await _call(port, "create_item", {"id": "abc", "name": "Widget"})
    # The handler asserted isinstance(item, _Item) internally — reaching
    # here with the right JSON back proves the cast AND the serialisation
    # of the returned BaseModel both worked.
    assert result == '{"id": "abc", "name": "Widget"}'


async def test_input_model_casts_list_of_basemodels() -> None:
    server = grpc.aio.server()
    bind_service(server, "list_items", handler_factory=lambda: _EchoHandler(), input_model=list[_Item])
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        result = await _call(port, "list_items", [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}])
        assert result == '[{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]'
    finally:
        await server.stop(None)


async def test_invalid_input_categorises_as_invalid_argument() -> None:
    server = grpc.aio.server()
    bind_service(server, "create_item", handler_factory=lambda: _CreateItemLikeHandler(), input_model=_Item)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
        stub = generic_pb2_grpc.GenericServiceStub(channel)
        import json

        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await stub.Call(
                generic_pb2.GenericRequest(json_payload=json.dumps({"id": "abc"})),  # missing "name"
                metadata=((ROUTE_METADATA_KEY, "create_item"),),
            )
        assert exc_info.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        await channel.close()
    finally:
        await server.stop(None)


async def test_dict_result_containing_basemodels_is_serialised() -> None:
    class _ListLikeHandler:
        async def execute(self, value, context=None):
            return {"items": [_Item(id="a", name="A"), _Item(id="b", name="B")], "total": 2}

    server = grpc.aio.server()
    bind_service(server, "list_items", handler_factory=lambda: _ListLikeHandler())
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        result = await _call(port, "list_items", None)
        import json

        assert json.loads(result) == {
            "items": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}],
            "total": 2,
        }
    finally:
        await server.stop(None)
