"""
openframe/protocol/http/websocket.py
=======================================
`bind_websocket` — register a WebSocket route backed by a `SessionHandler`
(`openframe.core.inbound`, ADR-008), the second real transport to
implement it (after SSE) and the first to exercise `on_event` for real —
WebSocket is genuinely bidirectional, unlike SSE. A module of `-http`,
not its own package: FastAPI/Starlette's WebSocket support needs no extra
dependency beyond `[server]` (same as `bind_session`) — serving real WS
traffic over uvicorn needs `websockets` or `wsproto` installed, but
that's an operational concern for whoever runs the service, not this
package's own dependency surface (the same relationship uvicorn itself
already has to this package).

**Accept/reject timing, confirmed with a real spike (`TestClient.
websocket_connect`), not assumed** — a WebSocket connection has no
response body/status the way an SSE GET does, so `open_session`'s own
"run `on_open` before committing to the wire" advice maps onto the
`accept()` call, not a `StreamingResponse`. Calling `websocket.close()`
*before* `accept()` sends a real rejection the client sees as a failed
handshake — `websocket_connect` raises on `__enter__` in that case,
confirmed directly. `on_open` therefore always runs before `accept()`; a
handler that wants to push something immediately on accept does so from
a background task it starts inside `on_open` itself (same pattern as
SSE's own handlers), since `WebSocketSession.send()` accepts the
connection on first call if it hasn't happened yet — a handler can
decide to accept without sending anything at all (a listen-only session
waiting on `on_event`).

**`WebSocketDisconnect` is translated to `asyncio.CancelledError`**,
confirmed necessary, not assumed: `run_opened_session`
(`openframe-protocol-base`) only special-cases `asyncio.CancelledError`
as `CloseReason.CLIENT_DISCONNECT` — it has no Starlette dependency of
its own and can't know about `WebSocketDisconnect`. A real client
disconnect during `websocket.receive_text()` raises `WebSocketDisconnect`
to Starlette; `inbound_events()` here re-raises it as `CancelledError`
instead, so `run_opened_session`'s existing disconnect handling applies
unchanged rather than this module needing to duplicate it.

Dependency order:
    protocol/http/websocket → protocol/base/context, protocol/base/session,
    openframe.core.inbound
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Sequence
from typing import Any

from fastapi import APIRouter
from openframe.core.inbound import CloseReason, RequestContext, SessionHandler
from openframe.protocol.base.context import ContextEnricher, build_request_context
from openframe.protocol.base.session import open_session, run_opened_session
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

__all__ = ["WebSocketSession", "bind_websocket"]

_logger = logging.getLogger(__name__)

_CLOSE_CODES: dict[CloseReason, int] = {
    CloseReason.COMPLETED: 1000,
    CloseReason.SERVER_SHUTDOWN: 1001,
    CloseReason.ERROR: 1011,
}


class WebSocketSession:
    """
    `Session[TOut]` backed directly by a Starlette `WebSocket` — no
    intermediate queue needed (unlike `SseSession`), since `send()` can
    just call the real socket's own `send_text()` directly.

    `send()` accepts the connection on first call if it hasn't happened
    yet, so a handler that decides to accept in `on_open` without
    immediately sending anything (a listen-only session) still works —
    the connection is accepted explicitly via :meth:`accept`, called by
    `bind_websocket` right after `on_open` succeeds, not lazily deferred
    to the first `send()`.
    """

    def __init__(self, *, websocket: WebSocket, context: RequestContext) -> None:
        self.context = context
        self._websocket = websocket
        self._closed = False
        self.close_reason: CloseReason | None = None

    async def accept(self) -> None:
        """Accept the connection if not already accepted. Idempotent."""
        if self._websocket.client_state == WebSocketState.CONNECTING:
            await self._websocket.accept()

    async def send(self, event: Any) -> None:
        if self._closed:
            raise RuntimeError("cannot send on a closed session")
        await self.accept()
        await self._websocket.send_text(json.dumps(event, default=str))

    async def close(self, reason: CloseReason = CloseReason.COMPLETED) -> None:
        if self._closed:
            return
        self._closed = True
        self.close_reason = reason
        if self._websocket.client_state == WebSocketState.DISCONNECTED:
            return  # the client is already gone — nothing to send
        try:
            await self._websocket.close(code=_CLOSE_CODES.get(reason, 1000))
        except RuntimeError:
            # Starlette raises if close() races a concurrent disconnect —
            # the connection is ending either way; nothing more to do.
            _logger.debug("websocket already closing for session %s", self.context.correlation_id)


def bind_websocket(
    router: APIRouter,
    path: str,
    *,
    handler_factory: Callable[[], SessionHandler],
    context_enricher: ContextEnricher | None = None,
    allowed_origins: Sequence[str] | None = None,
    max_message_size: int | None = None,
) -> None:
    """
    Register a WebSocket route at *path* backed by a fresh `SessionHandler`
    (built by `handler_factory`, called once per connection — a plain
    callable, same convention as `bind_session`).

    `on_open` runs — and can reject — before `accept()`, so a reject
    becomes a real failed handshake (`websocket.close()` called by
    `open_session` itself via `session.close(CloseReason.ERROR)`), not a
    message sent after the client thinks it's connected.

    `context_enricher`: see `build_request_context`'s own docstring — the
    hook a service uses to set `principal`/`tenant`, same as
    `bind_query`/`bind_command`/`bind_session` accept it.
    `allowed_origins`: if given, a connection whose `Origin` header isn't
    in this list is rejected with a policy-violation close (1008) before
    `on_open` even runs — `None` (the default) performs no check, the
    previous behaviour, since a browser's own same-origin policy isn't a
    substitute for a server-side allowlist but also isn't always wanted
    (a non-browser client sends no `Origin` header at all).
    `max_message_size`: if given, an inbound text frame larger than this
    (in UTF-8 bytes) closes the connection (1009, "message too big")
    instead of being passed to `on_event` — `None` performs no check, the
    previous behaviour, which let an unbounded inbound message reach the
    handler.
    """

    async def endpoint(websocket: WebSocket) -> None:
        if allowed_origins is not None and websocket.headers.get("origin") not in allowed_origins:
            await websocket.close(code=1008)
            return

        context = build_request_context(dict(websocket.headers), context_enricher=context_enricher)
        session = WebSocketSession(websocket=websocket, context=context)
        handler = handler_factory()

        try:
            await open_session(handler, session)
        except Exception:
            # Already closed/rejected and reported via on_close by
            # open_session itself — nothing left to do here.
            return

        await session.accept()

        async def inbound_events():
            try:
                while True:
                    # A handler can close the session itself from inside
                    # on_event (a clean, self-initiated end, not a client
                    # disconnect) — confirmed by testing, not assumed:
                    # without this check, the next receive_text() call
                    # raises WebSocketDisconnected (a different exception
                    # than WebSocketDisconnect below) against the
                    # already-closed socket, which this generator doesn't
                    # otherwise know how to distinguish from a real error.
                    # Ending the generator here instead lets
                    # run_opened_session's loop finish normally —
                    # CloseReason.COMPLETED, matching what actually
                    # happened, not CloseReason.ERROR.
                    if session._closed:
                        return
                    data = await websocket.receive_text()
                    if max_message_size is not None and len(data.encode()) > max_message_size:
                        await session.close(CloseReason.ERROR)
                        await websocket.close(code=1009)
                        return
                    try:
                        yield json.loads(data)
                    except json.JSONDecodeError:
                        yield data
            except WebSocketDisconnect:
                raise asyncio.CancelledError() from None

        try:
            await run_opened_session(handler, session, inbound_events())
        except asyncio.CancelledError:
            pass  # client disconnected — on_close/session.close already ran

    router.add_api_websocket_route(path, endpoint)
