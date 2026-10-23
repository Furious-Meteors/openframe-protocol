"""
Runs SessionConformanceTests (openframe-protocol-base) against the real
bind_session/SseSession — proves the generic suite actually exercises a
real transport, not just the in-process fake in
openframe-protocol-base's own tests/test_session_testing.py.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from openframe.core.exceptions import AdapterTimeoutError
from openframe.protocol.base.errors import categorize
from openframe.protocol.base.session_testing import (
    SessionConformanceTests,
    SessionDriver,
    SessionResult,
)
from openframe.protocol.http import bind_session, install_error_handlers


def _build_app(handler_factory):
    app = FastAPI()
    install_error_handlers(app)
    router = APIRouter()
    bind_session(router, "/stream", handler_factory=handler_factory)
    app.include_router(router)
    return app


def _parse_data_lines(raw: str) -> list:
    import json

    events = []
    for line in raw.split("\n"):
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: "):]))
    return events


class _SendThenCloseHandler:
    def __init__(self, events: list) -> None:
        self._events = events
        self.closed_with = None

    async def on_open(self, session) -> None:
        async def _produce():
            for event in self._events:
                await session.send(event)
                await asyncio.sleep(0)
            await session.close()

        asyncio.create_task(_produce())

    async def on_event(self, session, event) -> None:
        pass

    async def on_close(self, session, reason) -> None:
        self.closed_with = reason


class _RejectingHandler:
    def __init__(self) -> None:
        self.closed_with = None

    async def on_open(self, session) -> None:
        raise AdapterTimeoutError("too slow", adapter="x", operation="connect")

    async def on_event(self, session, event) -> None:
        pass

    async def on_close(self, session, reason) -> None:
        self.closed_with = reason


class _HangingHandler:
    def __init__(self) -> None:
        self.closed_with = None

    async def on_open(self, session) -> None:
        pass

    async def on_event(self, session, event) -> None:
        pass

    async def on_close(self, session, reason) -> None:
        self.closed_with = reason


class _SseSessionDriver:
    """Drives the real bind_session/SseSession through an in-process ASGI client."""

    async def open_accepts(self) -> SessionResult:
        handler = _SendThenCloseHandler([])
        app = _build_app(lambda: handler)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/stream")
        return SessionResult(
            opened=resp.status_code == 200,
            close_reason=handler.closed_with,
            received_events=tuple(_parse_data_lines(resp.text)),
            correlation_ids=(),
        )

    async def open_rejects(self) -> SessionResult:
        handler = _RejectingHandler()
        app = _build_app(lambda: handler)
        exc = AdapterTimeoutError("too slow", adapter="x", operation="connect")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/stream")
        return SessionResult(
            opened=resp.status_code == 200,
            close_reason=handler.closed_with,
            received_events=(),
            correlation_ids=(),
            error_category=categorize(exc) if resp.status_code != 200 else None,
        )

    async def ordered_delivery(self) -> SessionResult:
        handler = _SendThenCloseHandler(["event-1", "event-2", "event-3"])
        app = _build_app(lambda: handler)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/stream")
        return SessionResult(
            opened=True,
            close_reason=handler.closed_with,
            received_events=tuple(_parse_data_lines(resp.text)),
            correlation_ids=(),
        )

    async def abrupt_disconnect(self) -> SessionResult:
        import uvicorn

        handler = _HangingHandler()
        app = _build_app(lambda: handler)
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical")
        server = uvicorn.Server(config)
        server_task = asyncio.create_task(server.serve())
        try:
            for _ in range(200):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            port = server.servers[0].sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /stream HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n")
            await writer.drain()
            await reader.readuntil(b"\r\n\r\n")
            writer.close()
            await writer.wait_closed()
            for _ in range(200):
                if handler.closed_with is not None:
                    break
                await asyncio.sleep(0.01)
        finally:
            server.should_exit = True
            await server_task

        return SessionResult(
            opened=True,
            close_reason=handler.closed_with,
            received_events=(),
            correlation_ids=(),
        )

    async def correlation_id_stable(self) -> SessionResult:
        app = _build_app(lambda: _SendThenCloseHandler(["x", "y"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/stream")
        events = _parse_data_lines(resp.text)
        correlation_id = resp.headers["x-correlation-id"]
        return SessionResult(
            opened=True,
            close_reason=None,
            received_events=tuple(events),
            correlation_ids=(correlation_id,) * len(events),
        )


class TestSseSessionConformance(SessionConformanceTests):
    @pytest.fixture
    def driver(self) -> _SseSessionDriver:
        return _SseSessionDriver()


def test_driver_satisfies_session_driver_protocol() -> None:
    assert isinstance(_SseSessionDriver(), SessionDriver)
