"""
openframe/protocol/base/session_testing.py
=============================================
`SessionDriver` + `SessionConformanceTests` — the one conformance suite
every inbound `SessionHandler`-based `openframe-protocol-*` binding
proves itself against. The session-lifecycle counterpart to
`testing.py`'s `TransportDriver`/`TransportConformanceTests`, in its own
module (not added to `testing.py`) since the two suites share no
scenarios — a session's `SessionDriver` methods set up an open
connection and drive its lifecycle, not one call-and-respond exchange.

Incubating here, not in `openframe-core`, mirroring `testing.py`'s own
precedent: `openframe.core.testing.contracts.inbound`'s new
`SessionHandlerContractTests` is shape-only (does the handler have the
right methods) — it knows nothing about transports, in-band error
mapping, or session-lifetime correlation ids. This suite is promoted to
`openframe.core.testing` only after two session transports (SSE,
WebSocket) both pass it for real — the same rule-of-three discipline
`TransportConformanceTests`'s own docstring already applies.

Covers exactly what `openframe-protocol-design.md` section 7 specifies
for sessions: open/reject, ordered delivery, clean close, abrupt
disconnect, and correlation id stability across the session's lifetime.
Back-pressure is deliberately not covered here — it has no
transport-neutral normalised shape the way open/close/delivery/
correlation-id do; a transport-specific back-pressure test belongs in
that transport's own test suite, not this generic one.

No top-level pytest import — importable without pytest installed,
matching `testing.py`'s own convention.

Dependency order:
    protocol/base/session_testing → protocol/base/errors, openframe.core.inbound
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from openframe.core.inbound import CloseReason
from openframe.protocol.base.errors import ErrorCategory

__all__ = ["SessionConformanceTests", "SessionDriver", "SessionResult"]


@dataclass(frozen=True)
class SessionResult:
    """
    One session's outcome, normalised to a shape every session transport
    can produce.

    Attributes:
        opened:          Whether `on_open` accepted the session (returned
                         normally) rather than rejecting it (raised).
        close_reason:    The `CloseReason` the session ended with.
        received_events: Events the client actually received, via
                         `session.send()`, **in the order they arrived**
                         — the thing `test_ordered_delivery` checks.
        correlation_ids: The correlation id associated with each received
                         event — same length as `received_events`. All
                         entries must be identical for a well-behaved
                         session: one `RequestContext` for the whole
                         session, not one per event.
        error_category:  `None` unless the session ended via `on_open`
                         rejecting or an `on_event` exception — the
                         `ErrorCategory` the error mapped to.
    """

    opened: bool
    close_reason: CloseReason | None
    received_events: tuple[Any, ...]
    correlation_ids: tuple[str, ...]
    error_category: ErrorCategory | None = None


@runtime_checkable
class SessionDriver(Protocol):
    """
    What a session transport's own test module implements to run this
    suite. Each method exercises one bound `SessionHandler` scenario end
    to end (through the real session binding, not by calling handler
    methods directly) and returns the outcome as a `SessionResult`.
    """

    async def open_accepts(self) -> SessionResult:
        """A session whose handler accepts, sends nothing, and closes cleanly."""
        ...

    async def open_rejects(self) -> SessionResult:
        """A session whose handler's `on_open` raises — must never open or deliver events."""
        ...

    async def ordered_delivery(self) -> SessionResult:
        """
        A session whose handler sends exactly three events, in order:
        `"event-1"`, `"event-2"`, `"event-3"`. Must arrive in that exact
        order — proves the binding doesn't reorder or buffer out of
        sequence.
        """
        ...

    async def abrupt_disconnect(self) -> SessionResult:
        """
        A session where the client disconnects mid-session (not a clean,
        handler-initiated close). Must close with
        `CloseReason.CLIENT_DISCONNECT`.
        """
        ...

    async def correlation_id_stable(self) -> SessionResult:
        """
        A session whose handler sends at least two events. The
        correlation id must be identical across all of them.
        """
        ...


class SessionConformanceTests:
    """
    Reusable pytest base class. Subclasses provide one fixture:

    ``driver``
        A fresh instance satisfying `SessionDriver`, wired to a real
        bound `SessionHandler` over the session transport under test.

    .. stability: experimental
       New alongside `openframe-protocol-base` itself. Promotion to
       `openframe.core.testing` is gated on two session transports (SSE,
       WebSocket) passing this suite for real.
    """

    async def test_open_accepts(self, driver: SessionDriver) -> None:
        result = await driver.open_accepts()
        assert result.opened
        assert result.close_reason is CloseReason.COMPLETED

    async def test_open_rejects(self, driver: SessionDriver) -> None:
        result = await driver.open_rejects()
        assert not result.opened
        assert result.close_reason is CloseReason.ERROR
        assert result.error_category is not None
        assert not result.received_events, "a rejected session must never deliver events"

    async def test_ordered_delivery(self, driver: SessionDriver) -> None:
        result = await driver.ordered_delivery()
        assert result.received_events == ("event-1", "event-2", "event-3")

    async def test_abrupt_disconnect_closes_with_client_disconnect(self, driver: SessionDriver) -> None:
        result = await driver.abrupt_disconnect()
        assert result.close_reason is CloseReason.CLIENT_DISCONNECT

    async def test_correlation_id_stable_across_session(self, driver: SessionDriver) -> None:
        result = await driver.correlation_id_stable()
        assert len(result.correlation_ids) >= 2
        assert len(set(result.correlation_ids)) == 1, (
            "correlation_id must be identical across every event in one session — "
            "one RequestContext for the session's whole lifetime, not one per event"
        )
