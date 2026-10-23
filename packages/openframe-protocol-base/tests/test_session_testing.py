"""
Proves SessionConformanceTests itself is correct, against a real
in-process driver that runs real SessionHandler instances through the
real run_session()/InMemorySession — not just hand-built SessionResult
objects — so this also doubles as an integration test of run_session
itself, before any real session transport (SSE) exists to run the suite
against.
"""
from __future__ import annotations

import asyncio

import pytest
from openframe.core.exceptions import AdapterTimeoutError
from openframe.core.inbound import RequestContext
from openframe.core.testing import InMemorySession
from openframe.protocol.base.errors import categorize
from openframe.protocol.base.session import run_session
from openframe.protocol.base.session_testing import (
    SessionConformanceTests,
    SessionDriver,
    SessionResult,
)


class _AcceptingHandler:
    async def on_open(self, session) -> None:
        pass

    async def on_event(self, session, event) -> None:
        await session.send(event)

    async def on_close(self, session, reason) -> None:
        pass


class _RejectingHandler:
    async def on_open(self, session) -> None:
        raise AdapterTimeoutError("too slow", adapter="x", operation="connect")

    async def on_event(self, session, event) -> None:
        pass

    async def on_close(self, session, reason) -> None:
        pass


class _DisconnectingHandler:
    async def on_open(self, session) -> None:
        pass

    async def on_event(self, session, event) -> None:
        raise asyncio.CancelledError()

    async def on_close(self, session, reason) -> None:
        pass


async def _empty_events():
    return
    yield  # pragma: no cover


async def _events(*items):
    for item in items:
        yield item


def _new_session() -> InMemorySession:
    return InMemorySession(context=RequestContext(correlation_id="corr-driver-1"))


class _InProcessSessionDriver:
    """Runs real handlers through the real run_session()/InMemorySession."""

    async def open_accepts(self) -> SessionResult:
        session = _new_session()
        await run_session(_AcceptingHandler(), session, _empty_events())
        return SessionResult(
            opened=True,
            close_reason=session.close_reason,
            received_events=tuple(session.sent),
            correlation_ids=(session.context.correlation_id,) if session.sent else (),
        )

    async def open_rejects(self) -> SessionResult:
        session = _new_session()
        exc = AdapterTimeoutError("too slow", adapter="x", operation="connect")
        try:
            await run_session(_RejectingHandler(), session, _empty_events())
        except AdapterTimeoutError:
            pass
        return SessionResult(
            opened=False,
            close_reason=session.close_reason,
            received_events=tuple(session.sent),
            correlation_ids=(),
            error_category=categorize(exc),
        )

    async def ordered_delivery(self) -> SessionResult:
        session = _new_session()
        await run_session(_AcceptingHandler(), session, _events("event-1", "event-2", "event-3"))
        return SessionResult(
            opened=True,
            close_reason=session.close_reason,
            received_events=tuple(session.sent),
            correlation_ids=(session.context.correlation_id,) * len(session.sent),
        )

    async def abrupt_disconnect(self) -> SessionResult:
        session = _new_session()
        try:
            await run_session(_DisconnectingHandler(), session, _events("trigger"))
        except asyncio.CancelledError:
            pass
        return SessionResult(
            opened=True,
            close_reason=session.close_reason,
            received_events=tuple(session.sent),
            correlation_ids=(),
        )

    async def correlation_id_stable(self) -> SessionResult:
        session = _new_session()
        await run_session(_AcceptingHandler(), session, _events("x", "y"))
        return SessionResult(
            opened=True,
            close_reason=session.close_reason,
            received_events=tuple(session.sent),
            correlation_ids=(session.context.correlation_id,) * len(session.sent),
        )


class TestInProcessSessionConformance(SessionConformanceTests):
    @pytest.fixture
    def driver(self) -> _InProcessSessionDriver:
        return _InProcessSessionDriver()


def test_driver_satisfies_session_driver_protocol() -> None:
    assert isinstance(_InProcessSessionDriver(), SessionDriver)
