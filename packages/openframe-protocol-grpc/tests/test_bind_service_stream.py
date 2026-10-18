"""
Spikes bind_service_stream for real — not assumed. Proves: (a) a real
bidirectional exchange works (on_event actually dispatches, in order),
(b) on_open rejecting produces a clean AioRpcError with zero messages
(the gRPC counterpart of SSE's pre-StreamingResponse / WebSocket's
pre-accept() reject timing), (c) an on_event failure sends an in-band
error and ends the session, and (d) a real client disconnect (channel
close) reaches on_close with CLIENT_DISCONNECT — using a real grpc.aio
server/channel on an ephemeral port, matching every other conformance
test in this repo.
"""
from __future__ import annotations

import asyncio
import json

import grpc
import pytest

from openframe.core.exceptions import AdapterTimeoutError
from openframe.core.inbound import CloseReason
from openframe.protocol.base.session import send_error_event
from openframe.protocol.grpc import bind_service_stream, generic_pb2, generic_pb2_grpc


class _EchoHandler:
    """Accepts, echoes every inbound event back, tracks lifecycle calls."""

    def __init__(self) -> None:
        self.events: list = []
        self.closed_with: CloseReason | None = None

    async def on_open(self, session) -> None:
        pass

    async def on_event(self, session, event) -> None:
        self.events.append(event)
        await session.send({"echo": event})

    async def on_close(self, session, reason) -> None:
        self.closed_with = reason


class _RejectingHandler:
    def __init__(self) -> None:
        self.closed_with: CloseReason | None = None

    async def on_open(self, session) -> None:
        raise AdapterTimeoutError("too slow", adapter="x", operation="connect")

    async def on_event(self, session, event) -> None:
        pass

    async def on_close(self, session, reason) -> None:
        self.closed_with = reason


class _FailingOnEventHandler:
    def __init__(self) -> None:
        self.closed_with: CloseReason | None = None

    async def on_open(self, session) -> None:
        pass

    async def on_event(self, session, event) -> None:
        raise RuntimeError("driver-internal-secret")

    async def on_close(self, session, reason) -> None:
        self.closed_with = reason


class _HangingHandler:
    def __init__(self) -> None:
        self.closed_with: CloseReason | None = None

    async def on_open(self, session) -> None:
        pass

    async def on_event(self, session, event) -> None:
        pass

    async def on_close(self, session, reason) -> None:
        self.closed_with = reason


async def _build_server(handler_factory) -> tuple[grpc.aio.Server, int]:
    server = grpc.aio.server()
    bind_service_stream(server, "echo", handler_factory=handler_factory)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    return server, port


async def _req_gen(*payloads):
    for payload in payloads:
        yield generic_pb2.GenericRequest(json_payload=json.dumps(payload))


async def test_bidirectional_exchange_calls_on_event_in_order() -> None:
    handler = _EchoHandler()
    server, port = await _build_server(lambda: handler)
    try:
        channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
        stub = generic_pb2_grpc.GenericServiceStub(channel)
        call = stub.CallStream(
            _req_gen("hello", "world"), metadata=(("x-openframe-route", "echo"),)
        )
        responses = []
        async for resp in call:
            responses.append(json.loads(resp.json_payload))
            if len(responses) == 2:
                break
        await channel.close()
    finally:
        await server.stop(None)

    assert handler.events == ["hello", "world"]
    assert responses == [{"echo": "hello"}, {"echo": "world"}]


async def test_on_open_reject_produces_clean_error_with_zero_messages() -> None:
    handler = _RejectingHandler()
    server, port = await _build_server(lambda: handler)
    try:
        channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
        stub = generic_pb2_grpc.GenericServiceStub(channel)
        call = stub.CallStream(_req_gen("hello"), metadata=(("x-openframe-route", "echo"),))
        received = []
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            async for resp in call:
                received.append(resp)
        assert exc_info.value.code() == grpc.StatusCode.DEADLINE_EXCEEDED
        assert received == []
        await channel.close()
    finally:
        await server.stop(None)
    assert handler.closed_with is CloseReason.ERROR


async def test_on_event_failure_sends_in_band_error_then_ends_session() -> None:
    handler = _FailingOnEventHandler()
    server, port = await _build_server(lambda: handler)
    try:
        channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
        stub = generic_pb2_grpc.GenericServiceStub(channel)
        call = stub.CallStream(_req_gen("trigger"), metadata=(("x-openframe-route", "echo"),))
        responses = []
        async for resp in call:
            responses.append(json.loads(resp.json_payload))
        await channel.close()
    finally:
        await server.stop(None)

    assert len(responses) == 1
    assert "error" in responses[0]
    assert "driver-internal-secret" not in json.dumps(responses[0]), (
        "an INTERNAL-category error must never leak the real exception message"
    )
    assert handler.closed_with is CloseReason.ERROR


async def test_client_disconnect_reaches_on_close() -> None:
    handler = _HangingHandler()
    server, port = await _build_server(lambda: handler)
    try:
        channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
        stub = generic_pb2_grpc.GenericServiceStub(channel)

        async def req_gen():
            # Never sends anything — request_iterator's very first
            # __anext__() stays pending, so there's no "inbound stream
            # ended" signal to race against the real disconnect.
            await asyncio.sleep(10)
            yield generic_pb2.GenericRequest(json_payload=json.dumps("never"))

        call = stub.CallStream(req_gen(), metadata=(("x-openframe-route", "echo"),))
        await asyncio.sleep(0.2)  # give the server time to reach open_session/accept
        await channel.close()

        for _ in range(100):
            if handler.closed_with is not None:
                break
            await asyncio.sleep(0.02)
    finally:
        await server.stop(None)

    assert handler.closed_with is CloseReason.CLIENT_DISCONNECT
