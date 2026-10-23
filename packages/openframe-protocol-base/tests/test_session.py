"""
Tests for openframe.protocol.base.session's run_session — the
open/dispatch/error/close state machine every SessionHandler-based
transport reuses.
"""
from __future__ import annotations

import asyncio

import pytest
from openframe.core.exceptions import AdapterConnectionError
from openframe.core.inbound import CloseReason, RequestContext
from openframe.core.testing import InMemorySession
from openframe.protocol.base.session import (
    open_session,
    run_opened_session,
    run_session,
)


async def _empty_events():
    return
    yield  # pragma: no cover — makes this a generator that never yields


async def _events(*items):
    for item in items:
        yield item


class _Handler:
    """Configurable SessionHandler for testing run_session's state machine."""

    def __init__(self, *, reject_with: BaseException | None = None, fail_on_event: BaseException | None = None):
        self._reject_with = reject_with
        self._fail_on_event = fail_on_event
        self.opened = False
        self.events: list = []
        self.closed_with: CloseReason | None = None
        self.close_calls = 0

    async def on_open(self, session) -> None:
        if self._reject_with is not None:
            raise self._reject_with
        self.opened = True

    async def on_event(self, session, event) -> None:
        if self._fail_on_event is not None:
            raise self._fail_on_event
        self.events.append(event)
        await session.send(event)

    async def on_close(self, session, reason: CloseReason) -> None:
        self.closed_with = reason
        self.close_calls += 1


def _session() -> InMemorySession:
    return InMemorySession(context=RequestContext(correlation_id="corr-1"))


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_run_session_accepts_and_closes_cleanly() -> None:
    handler = _Handler()
    session = _session()
    await run_session(handler, session, _empty_events())
    assert handler.opened
    assert session.closed
    assert session.close_reason is CloseReason.COMPLETED
    assert handler.closed_with is CloseReason.COMPLETED
    assert handler.close_calls == 1


async def test_run_session_delivers_events_in_order() -> None:
    handler = _Handler()
    session = _session()
    await run_session(handler, session, _events("a", "b", "c"))
    assert handler.events == ["a", "b", "c"]
    assert session.sent == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# on_open rejecting — propagates, no in-band send, on_close still fires
# ---------------------------------------------------------------------------


async def test_run_session_reject_propagates_the_exception() -> None:
    handler = _Handler(reject_with=PermissionError("nope"))
    session = _session()
    with pytest.raises(PermissionError):
        await run_session(handler, session, _empty_events())


async def test_run_session_reject_still_calls_on_close() -> None:
    handler = _Handler(reject_with=PermissionError("nope"))
    session = _session()
    with pytest.raises(PermissionError):
        await run_session(handler, session, _empty_events())
    assert handler.closed_with is CloseReason.ERROR
    assert handler.close_calls == 1


async def test_run_session_reject_closes_the_session() -> None:
    handler = _Handler(reject_with=PermissionError("nope"))
    session = _session()
    with pytest.raises(PermissionError):
        await run_session(handler, session, _empty_events())
    assert session.closed
    assert session.close_reason is CloseReason.ERROR


async def test_run_session_reject_sends_nothing_in_band() -> None:
    """A reject never committed to the wire — nothing should be sent."""
    handler = _Handler(reject_with=PermissionError("nope"))
    session = _session()
    with pytest.raises(PermissionError):
        await run_session(handler, session, _empty_events())
    assert session.sent == []


# ---------------------------------------------------------------------------
# on_event failing mid-session — in-band, does NOT propagate
# ---------------------------------------------------------------------------


async def test_run_session_on_event_failure_does_not_propagate() -> None:
    handler = _Handler(fail_on_event=AdapterConnectionError("down", adapter="x", operation="get"))
    session = _session()
    await run_session(handler, session, _events("trigger"))  # must not raise


async def test_run_session_on_event_failure_sends_error_envelope_in_band() -> None:
    handler = _Handler(fail_on_event=AdapterConnectionError("down", adapter="x", operation="get"))
    session = _session()
    await run_session(handler, session, _events("trigger"))
    assert len(session.sent) == 1
    envelope = session.sent[0]["error"]
    assert envelope["code"] == "adapter.connection"
    assert envelope["correlation_id"] == "corr-1"


async def test_run_session_on_event_failure_closes_with_error() -> None:
    handler = _Handler(fail_on_event=RuntimeError("boom"))
    session = _session()
    await run_session(handler, session, _events("trigger"))
    assert session.close_reason is CloseReason.ERROR
    assert handler.closed_with is CloseReason.ERROR


async def test_run_session_on_event_failure_unexpected_error_does_not_leak_message() -> None:
    handler = _Handler(fail_on_event=RuntimeError("driver-internal-secret"))
    session = _session()
    await run_session(handler, session, _events("trigger"))
    envelope = session.sent[0]["error"]
    assert "driver-internal-secret" not in envelope["message"]


# ---------------------------------------------------------------------------
# Cancellation — reported as CLIENT_DISCONNECT, never swallowed
# ---------------------------------------------------------------------------


async def test_run_session_cancellation_during_events_is_client_disconnect_and_reraises() -> None:
    class _CancellingHandler(_Handler):
        async def on_event(self, session, event) -> None:
            raise asyncio.CancelledError()

    handler = _CancellingHandler()
    session = _session()
    with pytest.raises(asyncio.CancelledError):
        await run_session(handler, session, _events("trigger"))
    assert handler.closed_with is CloseReason.CLIENT_DISCONNECT
    assert session.close_reason is CloseReason.CLIENT_DISCONNECT


async def test_run_session_cancellation_during_open_is_client_disconnect_and_reraises() -> None:
    handler = _Handler(reject_with=asyncio.CancelledError())
    session = _session()
    with pytest.raises(asyncio.CancelledError):
        await run_session(handler, session, _empty_events())
    assert handler.closed_with is CloseReason.CLIENT_DISCONNECT
    assert session.close_reason is CloseReason.CLIENT_DISCONNECT


# ---------------------------------------------------------------------------
# open_session / run_opened_session called separately — the shape a
# transport that must decide accept/reject before committing to the wire
# (SSE, WebSocket) actually uses, rather than the combined run_session().
# ---------------------------------------------------------------------------


async def test_open_session_succeeds_without_running_event_loop() -> None:
    handler = _Handler()
    session = _session()
    await open_session(handler, session)
    assert handler.opened
    assert not session.closed, "open_session alone must not close the session"
    assert handler.close_calls == 0, "on_close belongs to run_opened_session, not open_session"


async def test_open_session_reject_propagates_before_any_commit() -> None:
    handler = _Handler(reject_with=PermissionError("nope"))
    session = _session()
    with pytest.raises(PermissionError):
        await open_session(handler, session)
    assert session.closed
    assert session.close_reason is CloseReason.ERROR
    assert handler.close_calls == 1


async def test_run_opened_session_after_successful_open_session() -> None:
    handler = _Handler()
    session = _session()
    await open_session(handler, session)
    await run_opened_session(handler, session, _events("a", "b"))
    assert session.sent == ["a", "b"]
    assert session.close_reason is CloseReason.COMPLETED
    assert handler.close_calls == 1
