"""
Spikes bind_websocket for real — not assumed. Proves: (a) a real
bidirectional exchange works — on_event actually fires, the first time
any session transport in this repo exercises it, (b) on_open rejecting
produces a real failed handshake (not a message after connecting),
(c) an on_event failure sends an in-band error and ends the session
(run_opened_session's own documented behaviour), and (d) a real client
disconnect reaches on_close with CLIENT_DISCONNECT — using
TestClient.websocket_connect, which (unlike httpx.ASGITransport for
streaming HTTP) genuinely simulates the WebSocket protocol in-process,
confirmed directly rather than assumed to work the same way.
"""
from __future__ import annotations

import json

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from openframe.core.exceptions import AdapterTimeoutError
from openframe.core.inbound import CloseReason
from openframe.protocol.http import bind_websocket, install_error_handlers


def _build_app(handler_factory):
    app = FastAPI()
    install_error_handlers(app)
    router = APIRouter()
    bind_websocket(router, "/ws", handler_factory=handler_factory)
    app.include_router(router)
    return app


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


def test_bidirectional_exchange_calls_on_event_in_order() -> None:
    handler = _EchoHandler()
    app = _build_app(lambda: handler)
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps("hello"))
        assert json.loads(ws.receive_text()) == {"echo": "hello"}
        ws.send_text(json.dumps("world"))
        assert json.loads(ws.receive_text()) == {"echo": "world"}
    assert handler.events == ["hello", "world"]


def test_on_open_reject_produces_a_real_failed_handshake() -> None:
    app = _build_app(lambda: _RejectingHandler())
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws"):
            pass


def test_on_open_reject_still_calls_on_close() -> None:
    handler = _RejectingHandler()
    app = _build_app(lambda: handler)
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws"):
            pass
    assert handler.closed_with is CloseReason.ERROR


def test_on_event_failure_sends_in_band_error_then_ends_session() -> None:
    handler = _FailingOnEventHandler()
    app = _build_app(lambda: handler)
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps("trigger"))
        received = json.loads(ws.receive_text())
        assert "error" in received
        assert "driver-internal-secret" not in json.dumps(received), (
            "an INTERNAL-category error must never leak the real exception message"
        )
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()  # the session ends after an on_event failure
    assert handler.closed_with is CloseReason.ERROR


def test_client_disconnect_reaches_on_close() -> None:
    handler = _EchoHandler()
    app = _build_app(lambda: handler)
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps("hello"))
        assert json.loads(ws.receive_text()) == {"echo": "hello"}
        ws.close()
    assert handler.closed_with is CloseReason.CLIENT_DISCONNECT
