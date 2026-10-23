"""
openframe/protocol/base/session.py
=====================================
`open_session` + `run_opened_session` (and `run_session`, the convenience
wrapper combining both) — the one open/dispatch/error/close state machine
every `SessionHandler`-based transport's `server.py` reuses, instead of
hand-rolling it per transport. The session-lifecycle counterpart to
`create_app`/`create_server` (M1c) — same motivation (real, demonstrated
duplication avoided before it happens, not a hypothetical abstraction),
applied one layer lower.

**Why `open_session` is a separate function, not just the first half of
`run_session` inline** — found by actually tracing through Starlette's
`StreamingResponse`, not assumed: `StreamingResponse.stream_response`
sends the `http.response.start` ASGI message (status code + headers)
*before* it ever requests the body generator's first item. If
`on_open`'s accept/reject decision ran *inside* that generator, a reject
would arrive too late — the 200 status would already be on the wire,
with no way to turn it into the 4xx/5xx `install_error_handlers` would
otherwise produce. The same timing issue applies to a WebSocket's own
`accept()`/handshake. So a transport must call `open_session` *before*
constructing its `StreamingResponse`/completing its handshake, and only
proceed to `run_opened_session` (inside the generator / after the
handshake) once `open_session` has confirmed the handler accepted.

Two different failure points, two different wire treatments — found
while designing this, not assumed:

- **`on_open` rejecting** happens *before* anything is committed — no
  response headers sent yet (SSE), no handshake accepted yet (WebSocket).
  `run_session` therefore lets that exception **propagate out** to the
  caller (after still firing `on_close`/`session.close()` for contract
  compliance — see `SessionHandler.on_close`'s own docstring: it fires
  "however the session ended", including an `on_open` failure) so the
  transport's own `server.py` maps it through the exact same
  `categorize`/`build_envelope` → status-code path `install_error_handlers`
  already uses for a plain request/response failure — a reject should
  look like any other mapped pre-stream error, not a different shape.
- **`on_event` raising mid-session** happens *after* the stream/handshake
  already committed — there is no status code left to change. This is
  sent **in-band**, via `session.send()`, as a plain dict shaped
  `{"error": {"code", "message", "retryable", "correlation_id"}}` —
  `build_envelope`'s own fields — regardless of the session's `TOut`,
  since a transport's wire format (JSON, for both transports this repo
  builds today) already serialises a plain dict alongside whatever `TOut`
  normally produces.

`asyncio.CancelledError` (not `Exception` — it subclasses `BaseException`
and is deliberately not caught by the `except Exception` branch below) is
treated as `CloseReason.CLIENT_DISCONNECT`, then re-raised — standard
asyncio cancellation hygiene: a cancellation is reported, never swallowed.
A transport that needs to distinguish `SERVER_SHUTDOWN` from a genuine
client disconnect (both commonly surface as cancellation to the task
running `run_session`) does so by catching the re-raised
`CancelledError` in its own shutdown path and calling
`handler.on_close(session, CloseReason.SERVER_SHUTDOWN)` itself — outside
`run_session`, which only knows about one connection, never server-wide
shutdown.

Dependency order:
    protocol/base/session → protocol/base/errors, openframe.core.inbound
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import TypeVar

from openframe.core.inbound import CloseReason, Session, SessionHandler
from openframe.protocol.base.errors import build_envelope

__all__ = ["open_session", "run_opened_session", "run_session", "send_error_event"]

_logger = logging.getLogger(__name__)

TIn = TypeVar("TIn")
TOut = TypeVar("TOut")


async def open_session(handler: SessionHandler[TIn, TOut], session: Session[TOut]) -> None:
    """
    Call `on_open(session)` — the accept/reject half of a session's
    lifecycle, meant to run *before* a transport commits anything to the
    wire (before constructing a `StreamingResponse`, before completing a
    WebSocket handshake).

    If `on_open` raises (a reject, or a genuine failure), `on_close`/
    `session.close()` still fire — `on_close`'s own contract says
    "however the session ended", including an `on_open` failure — but the
    exception then **propagates to the caller**, since nothing has been
    committed to the wire yet. The caller (the transport's own
    `server.py`) is expected to let this propagate into its normal
    request/response error handling (`install_error_handlers`'s
    `categorize`/`build_envelope` path), exactly as any other
    request/response failure.

    On success, returns normally — the session is open and ready for
    `run_opened_session`.
    """
    try:
        await handler.on_open(session)
    except asyncio.CancelledError:
        await handler.on_close(session, CloseReason.CLIENT_DISCONNECT)
        await session.close(CloseReason.CLIENT_DISCONNECT)
        raise
    except Exception:
        await handler.on_close(session, CloseReason.ERROR)
        await session.close(CloseReason.ERROR)
        raise


async def run_opened_session(
    handler: SessionHandler[TIn, TOut],
    session: Session[TOut],
    inbound_events: AsyncIterator[TIn],
) -> None:
    """
    Run the dispatch/error/close half of a session's lifecycle, assuming
    `open_session` has already succeeded for *session*.

    Iterates `inbound_events`, calling `on_event(session, event)` for
    each, in order. `inbound_events` simply never yields for a
    one-directional transport (SSE) — the loop executes zero times, a
    valid lifecycle, not a special case this function needs to detect.

    Any exception from `on_event` (other than `CancelledError`) is sent
    **in-band** to the client via `session.send()` as a mapped error
    envelope — the stream already committed by this point (that's the
    whole reason `open_session` ran first), so there is no status code
    left to change — and the session closes with `CloseReason.ERROR`.
    This exception does NOT propagate — the session itself absorbs it and
    closes gracefully.

    `asyncio.CancelledError` is reported as `CloseReason.CLIENT_DISCONNECT`
    and always re-raised — standard cancellation hygiene, never
    swallowed. A transport that needs to distinguish `SERVER_SHUTDOWN`
    from a genuine client disconnect (both commonly surface as
    cancellation to the task running this function) does so by catching
    the re-raised `CancelledError` in its own shutdown path and calling
    `handler.on_close(session, CloseReason.SERVER_SHUTDOWN)` itself —
    outside this function, which only knows about one connection, never
    server-wide shutdown.

    `on_close(session, reason)` and `session.close(reason)` are always
    called exactly once, in a `finally` block, however the loop ended.
    """
    reason = CloseReason.COMPLETED
    try:
        async for event in inbound_events:
            await handler.on_event(session, event)
    except asyncio.CancelledError:
        reason = CloseReason.CLIENT_DISCONNECT
        raise
    except Exception as exc:
        reason = CloseReason.ERROR
        await send_error_event(session, exc)
    finally:
        await handler.on_close(session, reason)
        await session.close(reason)


async def run_session(
    handler: SessionHandler[TIn, TOut],
    session: Session[TOut],
    inbound_events: AsyncIterator[TIn],
) -> None:
    """
    Convenience wrapper: `open_session` followed by `run_opened_session`.

    Correct for a transport that has no "commit point" distinction to
    worry about (e.g. an in-process caller, as every test in this
    package's own `tests/test_session.py` is), or where the handshake has
    already been fully committed by the time this is called. A transport
    like SSE or WebSocket, which must decide accept/reject *before*
    committing to the wire, calls `open_session` and `run_opened_session`
    separately instead — see this module's own docstring for why.
    """
    await open_session(handler, session)
    await run_opened_session(handler, session, inbound_events)


async def send_error_event(session: Session, exc: BaseException) -> None:
    """
    Map *exc* through `build_envelope` and send it in-band as a plain
    dict. Never raises — a session already failing is not made worse by
    a failure to report the failure; logged instead.

    Public (not `run_opened_session`-only) since a push-only transport
    (SSE) whose handler drives its own background production — rather
    than responding to dispatched inbound events — has no `on_event` call
    for `run_opened_session` to wrap, and so maps its own background
    task's failures through this directly instead. See
    `openframe.protocol.http.sse`'s own module docstring for why it
    doesn't route through `run_opened_session` at all.
    """
    envelope = build_envelope(exc, correlation_id=session.context.correlation_id)
    try:
        await session.send(
            {
                "error": {
                    "code": envelope.code,
                    "message": envelope.message,
                    "retryable": envelope.retryable,
                    "correlation_id": envelope.correlation_id,
                }
            }
        )
    except Exception:
        _logger.warning(
            "failed to send error event to session %s", session.context.correlation_id,
            exc_info=True,
        )
