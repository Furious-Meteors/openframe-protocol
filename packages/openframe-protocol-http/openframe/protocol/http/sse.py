"""
openframe/protocol/http/sse.py
=================================
`bind_session` — register a Server-Sent Events route backed by a
`SessionHandler` (`openframe.core.inbound`, ADR-008), the first real
transport to implement it. A module of `-http`, not a new package —
per the design spec's own placement decision: SSE has no handshake
beyond a normal HTTP GET, so it belongs with HTTP, not as its own
transport family.

**Does not use `openframe.protocol.base.session.run_opened_session`** —
found while spiking this, not assumed. `run_opened_session` dispatches
inbound events to `on_event`, mapping any exception it raises into an
in-band error. SSE has **no inbound events at all** — `on_event` is
never called (see `SessionHandler.on_event`'s own docstring) — so there
is nothing for `run_opened_session` to dispatch. An SSE handler instead
drives its own background production after `on_open` returns (e.g.
`asyncio.create_task` inside `on_open`, pushing via `session.send()`
until it calls `session.close()`), and is responsible for catching its
own background task's failures and reporting them via the now-public
`openframe.protocol.base.session.send_error_event` directly — the same
mapping `run_opened_session` would have used, just invoked by the
handler's own code instead of a generic dispatch loop with nothing to
dispatch. `open_session` is still used, and still matters: it's the
accept/reject half, and SSE needs that exactly as much as any other
session transport (see that function's own docstring for why it must
run before `StreamingResponse` is constructed, not inside it).

Dependency order:
    protocol/http/sse → protocol/base/context, protocol/base/session,
    openframe.core.inbound
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from openframe.core.inbound import CloseReason, RequestContext, SessionHandler
from openframe.protocol.base.context import build_request_context
from openframe.protocol.base.session import open_session

__all__ = ["SseSession", "bind_session"]

_logger = logging.getLogger(__name__)

_SENTINEL = object()


class SseSession:
    """
    `Session[TOut]` backed by an `asyncio.Queue` — `send()` enqueues,
    the SSE body generator (`_drain`) dequeues and formats each item as
    a `data: {json}\\n\\n` line. `close()` is idempotent and records the
    *first* reason it was called with, in :attr:`close_reason` — the
    transport reads this after the stream ends to call `on_close` with
    the reason the session actually intended, not a generically-guessed
    one.
    """

    def __init__(self, *, context: RequestContext) -> None:
        self.context = context
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._closed = False
        self.close_reason: CloseReason | None = None

    async def send(self, event: Any) -> None:
        if self._closed:
            raise RuntimeError("cannot send on a closed session")
        await self._queue.put(event)

    async def close(self, reason: CloseReason = CloseReason.COMPLETED) -> None:
        if self._closed:
            return
        self._closed = True
        self.close_reason = reason
        await self._queue.put(_SENTINEL)

    async def _drain(self):
        """Yields formatted SSE lines until `close()` enqueues the sentinel."""
        while True:
            item = await self._queue.get()
            if item is _SENTINEL:
                break
            yield f"data: {json.dumps(item, default=str)}\n\n"


def bind_session(
    router: APIRouter,
    path: str,
    *,
    handler_factory: Callable[[], SessionHandler],
) -> None:
    """
    Register a GET route at *path* opening an SSE stream backed by a
    fresh `SessionHandler` (built by `handler_factory`, called once per
    request — a plain callable, not a FastAPI `Depends`, since there is
    no per-request dependency graph need beyond "build me a handler").

    `on_open` runs — and can reject — *before* the `StreamingResponse` is
    constructed, so a reject becomes a normal mapped error response
    (through `install_error_handlers`, wired by `create_app`), not a
    stream that already sent a 200 before failing. See
    `openframe.protocol.base.session.open_session`'s own docstring for
    the full reasoning.
    """

    async def endpoint(request: Request) -> StreamingResponse:
        context = build_request_context(dict(request.headers))
        session = SseSession(context=context)
        handler = handler_factory()

        # Accept/reject before anything is committed to the wire.
        await open_session(handler, session)

        async def event_stream():
            try:
                async for line in session._drain():
                    yield line
            except asyncio.CancelledError:
                # The ASGI framework cancels this generator's task on a
                # real client disconnect — report it, then re-raise
                # (standard cancellation hygiene: never swallow it).
                await session.close(CloseReason.CLIENT_DISCONNECT)
                raise
            finally:
                await handler.on_close(session, session.close_reason or CloseReason.COMPLETED)

        response = StreamingResponse(event_stream(), media_type="text/event-stream")
        # Same convention as bind_query/bind_command — one correlation id
        # for the whole response, set once, not per-event: a session has
        # exactly one RequestContext for its entire lifetime.
        response.headers["X-Correlation-ID"] = context.correlation_id
        return response

    router.add_api_route(path, endpoint, methods=["GET"])
