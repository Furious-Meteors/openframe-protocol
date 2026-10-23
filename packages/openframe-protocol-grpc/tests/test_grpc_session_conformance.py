"""
Runs SessionConformanceTests (openframe-protocol-base) against the real
bind_service_stream/GrpcSession — proves the generic suite exercises a
third real transport, after SSE and WebSocket in openframe-protocol-http.
"""
from __future__ import annotations

import asyncio
import json

import grpc
import pytest
from openframe.core.exceptions import AdapterTimeoutError
from openframe.core.inbound import CloseReason
from openframe.protocol.base.session_testing import (
    SessionConformanceTests,
    SessionDriver,
    SessionResult,
)
from openframe.protocol.grpc import bind_service_stream, generic_pb2, generic_pb2_grpc


class _EchoCloseHandler:
    """Echoes every inbound event back via on_event; closes when it sees the "__close__" sentinel."""

    def __init__(self) -> None:
        self.closed_with: CloseReason | None = None

    async def on_open(self, session) -> None:
        pass

    async def on_event(self, session, event) -> None:
        if event == "__close__":
            await session.close()
            return
        await session.send(event)

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


class _HangingHandler:
    def __init__(self) -> None:
        self.closed_with: CloseReason | None = None

    async def on_open(self, session) -> None:
        pass

    async def on_event(self, session, event) -> None:
        pass

    async def on_close(self, session, reason) -> None:
        self.closed_with = reason


class _CapturingHandler(_EchoCloseHandler):
    def __init__(self) -> None:
        super().__init__()
        self.correlation_id: str | None = None

    async def on_open(self, session) -> None:
        self.correlation_id = session.context.correlation_id
        await super().on_open(session)


async def _build_server(handler_factory) -> tuple[grpc.aio.Server, int]:
    server = grpc.aio.server()
    bind_service_stream(server, "echo", handler_factory=handler_factory)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    return server, port


async def _req_gen(*payloads, hold_open: bool = False):
    for payload in payloads:
        yield generic_pb2.GenericRequest(json_payload=json.dumps(payload))
    if hold_open:
        await asyncio.sleep(10)


async def _collect(server, port, handler, *payloads, hold_open=False):
    try:
        channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
        stub = generic_pb2_grpc.GenericServiceStub(channel)
        call = stub.CallStream(
            _req_gen(*payloads, hold_open=hold_open), metadata=(("x-openframe-route", "echo"),)
        )
        events = []
        async for resp in call:
            events.append(json.loads(resp.json_payload))
        await channel.close()
        return True, events, None
    except grpc.aio.AioRpcError as exc:
        return False, [], categorize_exc_from_status(exc)
    finally:
        await server.stop(None)


def categorize_exc_from_status(exc: grpc.aio.AioRpcError):
    from openframe.protocol.base.errors import ErrorCategory

    _STATUS_TO_CATEGORY = {
        grpc.StatusCode.NOT_FOUND: ErrorCategory.NOT_FOUND,
        grpc.StatusCode.INVALID_ARGUMENT: ErrorCategory.INVALID,
        grpc.StatusCode.DEADLINE_EXCEEDED: ErrorCategory.TIMEOUT,
        grpc.StatusCode.UNAVAILABLE: ErrorCategory.UNAVAILABLE,
        grpc.StatusCode.INTERNAL: ErrorCategory.INTERNAL,
    }
    return _STATUS_TO_CATEGORY.get(exc.code())


class _GrpcSessionDriver:
    """Drives the real bind_service_stream/GrpcSession through a real grpc.aio channel."""

    async def open_accepts(self) -> SessionResult:
        handler = _EchoCloseHandler()
        server, port = await _build_server(lambda: handler)
        opened, events, _ = await _collect(server, port, handler, "__close__")
        return SessionResult(
            opened=opened,
            close_reason=handler.closed_with,
            received_events=tuple(events),
            correlation_ids=(),
        )

    async def open_rejects(self) -> SessionResult:
        handler = _RejectingHandler()
        server, port = await _build_server(lambda: handler)
        opened, events, error_category = await _collect(server, port, handler, "x")
        return SessionResult(
            opened=opened,
            close_reason=handler.closed_with,
            received_events=tuple(events),
            correlation_ids=(),
            error_category=error_category,
        )

    async def ordered_delivery(self) -> SessionResult:
        handler = _EchoCloseHandler()
        server, port = await _build_server(lambda: handler)
        opened, events, _ = await _collect(
            server, port, handler, "event-1", "event-2", "event-3", "__close__"
        )
        return SessionResult(
            opened=opened,
            close_reason=handler.closed_with,
            received_events=tuple(events),
            correlation_ids=(),
        )

    async def abrupt_disconnect(self) -> SessionResult:
        handler = _HangingHandler()
        server, port = await _build_server(lambda: handler)
        try:
            channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
            stub = generic_pb2_grpc.GenericServiceStub(channel)
            # Held, not awaited directly — see test_bind_service_stream.py's
            # own identical comment: grpc.aio's call object owns the RPC's
            # lifetime, so dropping the reference would cancel it early.
            call = stub.CallStream(  # noqa: F841
                _req_gen(hold_open=True), metadata=(("x-openframe-route", "echo"),)
            )
            await asyncio.sleep(0.2)
            await channel.close()
            for _ in range(100):
                if handler.closed_with is not None:
                    break
                await asyncio.sleep(0.02)
        finally:
            await server.stop(None)
        return SessionResult(
            opened=True,
            close_reason=handler.closed_with,
            received_events=(),
            correlation_ids=(),
        )

    async def correlation_id_stable(self) -> SessionResult:
        handler = _CapturingHandler()
        server, port = await _build_server(lambda: handler)
        opened, events, _ = await _collect(server, port, handler, "x", "y", "__close__")
        correlation_id = handler.correlation_id
        return SessionResult(
            opened=opened,
            close_reason=handler.closed_with,
            received_events=tuple(events),
            correlation_ids=(correlation_id,) * len(events),
        )


class TestGrpcSessionConformance(SessionConformanceTests):
    @pytest.fixture
    def driver(self) -> _GrpcSessionDriver:
        return _GrpcSessionDriver()


def test_driver_satisfies_session_driver_protocol() -> None:
    assert isinstance(_GrpcSessionDriver(), SessionDriver)
