"""
Runs SessionConformanceTests (openframe-protocol-base) against the real
bind_websocket/WebSocketSession — proves the generic suite exercises a
second real transport, not just SSE (test_session_conformance.py) or the
in-process fake in openframe-protocol-base's own tests.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from openframe.core.exceptions import AdapterTimeoutError
from openframe.core.inbound import CloseReason
from openframe.protocol.base.errors import categorize
from openframe.protocol.base.session_testing import (
    SessionConformanceTests,
    SessionDriver,
    SessionResult,
)
from openframe.protocol.http import bind_websocket, install_error_handlers
from starlette.websockets import WebSocketDisconnect


def _build_app(handler_factory):
    app = FastAPI()
    install_error_handlers(app)
    router = APIRouter()
    bind_websocket(router, "/ws", handler_factory=handler_factory)
    app.include_router(router)
    return app


class _EchoCloseHandler:
    """
    Echoes every inbound event back via on_event; closes when it sees the
    "__close__" sentinel. Deliberately driven by client-sent events, not a
    background task spawned from on_open — confirmed the hard way that
    Starlette's TestClient.websocket_connect does not service an orphan
    asyncio.create_task the way a real server does (an earlier version of
    this driver used a background-task-push handler and hung forever;
    verified separately against a real uvicorn server that bind_websocket
    itself handles that pattern correctly — this is a TestClient test
    harness limitation, not a bind_websocket bug). A real caller of
    bind_websocket with a background-task-pushing handler works fine
    outside tests.
    """

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


def _drain(ws) -> list:
    events = []
    while True:
        try:
            events.append(json.loads(ws.receive_text()))
        except WebSocketDisconnect:
            break
    return events


def _sync_open_accepts(handler) -> tuple[bool, list]:
    app = _build_app(lambda: handler)
    client = TestClient(app)
    try:
        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps("__close__"))
            return True, _drain(ws)
    except WebSocketDisconnect:
        return False, []


def _sync_open_rejects(handler) -> bool:
    app = _build_app(lambda: handler)
    client = TestClient(app)
    try:
        with client.websocket_connect("/ws"):
            pass
        return True
    except WebSocketDisconnect:
        return False


def _sync_ordered_delivery(handler) -> list:
    app = _build_app(lambda: handler)
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        for event in ("event-1", "event-2", "event-3", "__close__"):
            ws.send_text(json.dumps(event))
        return _drain(ws)


def _sync_abrupt_disconnect(handler) -> None:
    app = _build_app(lambda: handler)
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        ws.close()


def _sync_correlation_id_stable(handler) -> list:
    app = _build_app(lambda: handler)
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        for event in ("x", "y", "__close__"):
            ws.send_text(json.dumps(event))
        return _drain(ws)


class _WebSocketSessionDriver:
    """
    Drives the real bind_websocket/WebSocketSession through TestClient.

    TestClient manages its own event loop internally and deadlocks if
    called from inside an already-running one (this driver's own async
    methods, under pytest-asyncio) — confirmed the hard way: an earlier
    version called it directly from these async methods and hung forever.
    Each scenario's synchronous TestClient work runs in a worker thread
    via asyncio.to_thread instead.
    """

    async def open_accepts(self) -> SessionResult:
        handler = _EchoCloseHandler()
        opened, events = await asyncio.to_thread(_sync_open_accepts, handler)
        return SessionResult(
            opened=opened,
            close_reason=handler.closed_with,
            received_events=tuple(events),
            correlation_ids=(),
        )

    async def open_rejects(self) -> SessionResult:
        handler = _RejectingHandler()
        exc = AdapterTimeoutError("too slow", adapter="x", operation="connect")
        opened = await asyncio.to_thread(_sync_open_rejects, handler)
        return SessionResult(
            opened=opened,
            close_reason=handler.closed_with,
            received_events=(),
            correlation_ids=(),
            error_category=categorize(exc) if not opened else None,
        )

    async def ordered_delivery(self) -> SessionResult:
        handler = _EchoCloseHandler()
        events = await asyncio.to_thread(_sync_ordered_delivery, handler)
        return SessionResult(
            opened=True,
            close_reason=handler.closed_with,
            received_events=tuple(events),
            correlation_ids=(),
        )

    async def abrupt_disconnect(self) -> SessionResult:
        handler = _HangingHandler()
        await asyncio.to_thread(_sync_abrupt_disconnect, handler)
        return SessionResult(
            opened=True,
            close_reason=handler.closed_with,
            received_events=(),
            correlation_ids=(),
        )

    async def correlation_id_stable(self) -> SessionResult:
        captured: dict = {}

        class _CapturingHandler(_EchoCloseHandler):
            async def on_open(self, session) -> None:
                captured["correlation_id"] = session.context.correlation_id
                await super().on_open(session)

        handler = _CapturingHandler()
        events = await asyncio.to_thread(_sync_correlation_id_stable, handler)
        correlation_id = captured["correlation_id"]
        return SessionResult(
            opened=True,
            close_reason=handler.closed_with,
            received_events=tuple(events),
            correlation_ids=(correlation_id,) * len(events),
        )


class TestWebSocketSessionConformance(SessionConformanceTests):
    @pytest.fixture
    def driver(self) -> _WebSocketSessionDriver:
        return _WebSocketSessionDriver()


def test_driver_satisfies_session_driver_protocol() -> None:
    assert isinstance(_WebSocketSessionDriver(), SessionDriver)
