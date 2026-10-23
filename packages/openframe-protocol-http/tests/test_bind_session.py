"""
Spikes bind_session (SSE) for real — not assumed. Proves: (a) a stream
actually delivers multiple `data:` lines in order over one connection,
(b) on_open rejecting produces a normal mapped HTTP error response
(never a 200 that then fails), (c) a mid-session handler failure
produces an in-band error event rather than crashing the connection,
and (d) a client disconnecting mid-stream reaches on_close with
CLIENT_DISCONNECT — before this module's own implementation is treated
as finished, matching every other transport build in this repo.
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from openframe.core.exceptions import AdapterTimeoutError
from openframe.core.inbound import CloseReason
from openframe.protocol.base.session import send_error_event
from openframe.protocol.http import bind_session, install_error_handlers


def _build_app(handler_factory):
    app = FastAPI()
    install_error_handlers(app)
    router = APIRouter()
    bind_session(router, "/stream", handler_factory=handler_factory)
    app.include_router(router)
    return app


class _ProducingHandler:
    """Accepts, sends events from a background task, then closes."""

    def __init__(self, events, *, fail_after: int | None = None):
        self._events = events
        self._fail_after = fail_after
        self.closed_with: CloseReason | None = None

    async def on_open(self, session) -> None:
        async def _produce():
            try:
                for i, event in enumerate(self._events):
                    if self._fail_after is not None and i == self._fail_after:
                        raise RuntimeError("driver-internal-secret")
                    await session.send(event)
                    await asyncio.sleep(0)
            except Exception as exc:
                await send_error_event(session, exc)
                await session.close(CloseReason.ERROR)
                return
            await session.close()

        asyncio.create_task(_produce())

    async def on_event(self, session, event) -> None:
        raise AssertionError("on_event must never be called for SSE")

    async def on_close(self, session, reason) -> None:
        self.closed_with = reason


class _RejectingHandler:
    async def on_open(self, session) -> None:
        raise AdapterTimeoutError("too slow", adapter="x", operation="connect")

    async def on_event(self, session, event) -> None:
        pass

    async def on_close(self, session, reason) -> None:
        pass


class _HangingHandler:
    """Accepts, then never sends anything and never closes on its own —
    used to prove client disconnect reaches on_close."""

    def __init__(self) -> None:
        self.closed_with: CloseReason | None = None

    async def on_open(self, session) -> None:
        pass

    async def on_event(self, session, event) -> None:
        pass

    async def on_close(self, session, reason) -> None:
        self.closed_with = reason


def _parse_data_lines(raw: str) -> list:
    events = []
    for line in raw.split("\n"):
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: "):]))
    return events


async def test_ordered_delivery_over_one_connection() -> None:
    handler = _ProducingHandler(["event-1", "event-2", "event-3"])
    app = _build_app(lambda: handler)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/stream")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_data_lines(resp.text)
    assert events == ["event-1", "event-2", "event-3"]


async def test_handler_closes_with_completed_reason() -> None:
    handler = _ProducingHandler(["x"])
    app = _build_app(lambda: handler)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.get("/stream")
    assert handler.closed_with is CloseReason.COMPLETED


async def test_on_open_reject_produces_a_mapped_http_error_not_a_broken_stream() -> None:
    app = _build_app(lambda: _RejectingHandler())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/stream")
    assert resp.status_code == 504, "AdapterTimeoutError must categorise TIMEOUT -> 504"
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert body["error"]["code"] == "adapter.timeout"


async def test_mid_stream_failure_sends_in_band_error_event_not_a_broken_connection() -> None:
    handler = _ProducingHandler(["event-1", "event-2", "event-3"], fail_after=1)
    app = _build_app(lambda: handler)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/stream")
    assert resp.status_code == 200, "a mid-stream failure must not change the already-sent status"
    events = _parse_data_lines(resp.text)
    assert events[0] == "event-1"
    assert "error" in events[1]
    assert "driver-internal-secret" not in json.dumps(events[1]), (
        "an INTERNAL-category error must never leak the real exception message"
    )
    assert handler.closed_with is CloseReason.ERROR


async def test_client_disconnect_reaches_on_close() -> None:
    """
    `httpx.ASGITransport` does NOT simulate a real client disconnect —
    confirmed the hard way: an earlier version of this test used it and
    hung forever, since ASGITransport drives the ASGI app in-process with
    no parallel `http.disconnect`-watcher task the way a real server
    (uvicorn) has. A real disconnect needs a real server and a real
    socket close.
    """
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
        assert server.started, "uvicorn server never started"
        port = server.servers[0].sockets[0].getsockname()[1]

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /stream HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n")
        await writer.drain()
        await reader.readuntil(b"\r\n\r\n")  # read past the response headers
        writer.close()
        await writer.wait_closed()

        for _ in range(200):
            if handler.closed_with is not None:
                break
            await asyncio.sleep(0.01)
        assert handler.closed_with is CloseReason.CLIENT_DISCONNECT
    finally:
        server.should_exit = True
        await server_task
