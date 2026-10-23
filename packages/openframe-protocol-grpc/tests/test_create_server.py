"""
Proves `create_server` for real — the gRPC counterpart of `-http`'s
`create_app`. Confirms `configure` runs before start (so `bind_service`
calls can register routes), `on_startup`/`on_shutdown` are awaited, a
degraded-mode exception doesn't prevent the server from starting, and a
non-degraded exception propagates out of the `async with` block.
"""
from __future__ import annotations

import grpc
import pytest
from openframe.core.exceptions import AdapterConnectionError
from openframe.core.testing import SpyQueryHandler
from openframe.protocol.grpc import (
    ROUTE_METADATA_KEY,
    bind_service,
    create_server,
    generic_pb2,
    generic_pb2_grpc,
)


async def test_configure_runs_before_start_and_routes_are_callable() -> None:
    async def noop() -> None:
        pass

    def configure(server):
        bind_service(server, "existing", handler_factory=lambda: SpyQueryHandler(response={"id": "1"}))

    async with create_server(
        address="127.0.0.1:0", configure=configure, on_startup=noop, on_shutdown=noop
    ) as server:
        port = server._openframe_bound_port
        channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
        stub = generic_pb2_grpc.GenericServiceStub(channel)
        response = await stub.Call(
            generic_pb2.GenericRequest(json_payload="null"),
            metadata=((ROUTE_METADATA_KEY, "existing"),),
        )
        assert response.json_payload == '{"id": "1"}'
        await channel.close()


async def test_on_startup_and_on_shutdown_are_awaited() -> None:
    called: list[str] = []

    async def on_startup() -> None:
        called.append("startup")

    async def on_shutdown() -> None:
        called.append("shutdown")

    async with create_server(address="127.0.0.1:0", configure=lambda server: None, on_startup=on_startup, on_shutdown=on_shutdown):
        assert called == ["startup"]
    assert called == ["startup", "shutdown"]


async def test_degraded_mode_exception_does_not_prevent_server_starting() -> None:
    async def on_startup() -> None:
        raise AdapterConnectionError("down", adapter="x", operation="connect")

    async def on_shutdown() -> None:
        pass

    async with create_server(
        address="127.0.0.1:0", configure=lambda server: None, on_startup=on_startup, on_shutdown=on_shutdown
    ) as server:
        assert server is not None  # reaching here means start() succeeded despite the exception


async def test_non_degraded_exception_propagates() -> None:
    async def on_startup() -> None:
        raise RuntimeError("not in the degraded-mode tuple")

    async def on_shutdown() -> None:
        pass

    with pytest.raises(RuntimeError, match="not in the degraded-mode tuple"):
        async with create_server(
            address="127.0.0.1:0", configure=lambda server: None, on_startup=on_startup, on_shutdown=on_shutdown
        ):
            pass
