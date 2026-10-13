"""
openframe/protocol/base/testing.py
=====================================
`TransportDriver` + `TransportConformanceTests` — the one conformance
suite every inbound `openframe-protocol-*` binding proves itself against.

Incubating here, not in `openframe-core`, per draft 8 section 4's own
rule: core's existing suites (`core.testing.contracts.inbound`) are
shape-only (does `execute()` have the right signature) — they know
nothing about transports, error mapping, or correlation ids. This suite
is promoted to `openframe.core.testing` only after HTTP *and* gRPC both
pass it for real (the same rule-of-three discipline already applied to
`SessionHandler`).

Design choice: every assertion goes through `DriverResult`, a normalised
shape every transport's own driver translates its real response into —
an HTTP status code and a gRPC status code mean nothing to each other,
but `ErrorCategory.NOT_FOUND` means the same thing regardless of which
wire format produced it. This is what actually keeps the suite
transport-agnostic rather than secretly HTTP-shaped.

No top-level pytest import — importable without pytest installed,
matching every `openframe.core.testing` contract module's own convention.

Dependency order:
    protocol/base/testing → protocol/base/errors
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

from openframe.protocol.base.errors import ErrorCategory

__all__ = [
    "DriverResult",
    "TransportDriver",
    "TransportConformanceTests",
    "UNEXPECTED_ERROR_SECRET",
    "RemoteClientDriver",
    "RemoteClientConformanceTests",
]

# Shared between this module's own assertions and every driver
# implementation's `call_unexpected_error()` — the driver rigs its
# handler to raise an exception carrying this exact string, and the
# conformance test asserts it is absent from the caller-visible body.
# A real driver-cause string, not a placeholder — if this ever shows up
# in a response body, the leak is real, not a test artefact.
UNEXPECTED_ERROR_SECRET = "driver-internal-cause-must-never-reach-a-caller"


@dataclass(frozen=True)
class DriverResult:
    """
    One call's outcome, normalised to a shape every transport can produce.

    Attributes:
        category:       `None` on success. An `ErrorCategory` member on
                        failure — the caller-facing classification, not
                        a transport-specific status code.
        body:           The decoded response body (a dict) on success,
                        or the decoded error envelope on failure.
        correlation_id: Extracted from wherever the transport carries it
                        (an HTTP response header, a gRPC trailer, ...).
                        `None` only if the driver genuinely couldn't find
                        one — a real conformance failure if assertions
                        don't later catch it, not an expected state.
        retryable:      Only meaningful when `category` is not `None`.
    """

    category: ErrorCategory | None
    body: Mapping[str, Any]
    correlation_id: str | None
    retryable: bool | None = None


@runtime_checkable
class TransportDriver(Protocol):
    """
    What a transport's own test module implements to run this suite.

    Each `call_*` method exercises one bound handler scenario end-to-end
    (through the real binder, not by calling a handler method directly)
    and returns the outcome as a `DriverResult`. How a driver rigs its
    handler to produce each scenario (which fake, which `raises=`) is the
    driver's own business — `SpyCommandHandler(raises=...)`/
    `SpyQueryHandler(raises=...)` from `openframe.core.testing` cover
    every case below.
    """

    async def call_create(self) -> DriverResult:
        """A command that succeeds — e.g. creating an entity."""
        ...

    async def call_get_existing(self) -> DriverResult:
        """A query that finds its target and returns a non-empty result."""
        ...

    async def call_get_missing(self) -> DriverResult:
        """A query whose target doesn't exist — must categorise NOT_FOUND."""
        ...

    async def call_list(self) -> DriverResult:
        """A query returning a collection — proves the binder isn't single-entity-only."""
        ...

    async def call_invalid_input(self) -> DriverResult:
        """A request whose payload fails input validation — must categorise INVALID."""
        ...

    async def call_timeout(self) -> DriverResult:
        """The handler raises `AdapterTimeoutError` — must categorise TIMEOUT, retryable."""
        ...

    async def call_connection_error(self) -> DriverResult:
        """The handler raises `AdapterConnectionError` — must categorise UNAVAILABLE, retryable."""
        ...

    async def call_unexpected_error(self) -> DriverResult:
        """
        The handler raises a bare, non-`OpenFrameError` exception carrying
        `UNEXPECTED_ERROR_SECRET` in its message — must categorise
        INTERNAL, and the secret must not appear in `body`.
        """
        ...

    async def call_with_traceparent(self, traceparent: str) -> DriverResult:
        """A successful call carrying a real W3C `traceparent` header."""
        ...

    async def call_with_correlation_header(self, value: str) -> DriverResult:
        """A successful call carrying a correlation-id header, no traceparent."""
        ...

    async def call_with_no_trace_context(self) -> DriverResult:
        """A successful call with neither header — correlation_id must still be generated."""
        ...


class TransportConformanceTests:
    """
    Reusable pytest base class. Subclasses provide one fixture:

    ``driver``
        A fresh instance satisfying `TransportDriver`, wired to a real
        bound handler over the transport under test (e.g. `httpx` against
        an in-process ASGI app for HTTP).

    .. stability: experimental
       New alongside `openframe-protocol-base` itself. Promotion to
       `openframe.core.testing` is gated on two transports passing this
       suite for real, per draft 8's rule-of-three discipline.
    """

    async def test_create_succeeds(self, driver: TransportDriver) -> None:
        result = await driver.call_create()
        assert result.category is None

    async def test_get_existing_succeeds(self, driver: TransportDriver) -> None:
        result = await driver.call_get_existing()
        assert result.category is None
        assert result.body

    async def test_get_missing_is_not_found(self, driver: TransportDriver) -> None:
        result = await driver.call_get_missing()
        assert result.category is ErrorCategory.NOT_FOUND

    async def test_list_succeeds(self, driver: TransportDriver) -> None:
        result = await driver.call_list()
        assert result.category is None

    async def test_invalid_input_is_invalid(self, driver: TransportDriver) -> None:
        result = await driver.call_invalid_input()
        assert result.category is ErrorCategory.INVALID

    async def test_timeout_is_timeout_and_retryable(self, driver: TransportDriver) -> None:
        result = await driver.call_timeout()
        assert result.category is ErrorCategory.TIMEOUT
        assert result.retryable is True

    async def test_connection_error_is_unavailable_and_retryable(
        self, driver: TransportDriver
    ) -> None:
        result = await driver.call_connection_error()
        assert result.category is ErrorCategory.UNAVAILABLE
        assert result.retryable is True

    async def test_unexpected_error_is_internal_and_does_not_leak(
        self, driver: TransportDriver
    ) -> None:
        result = await driver.call_unexpected_error()
        assert result.category is ErrorCategory.INTERNAL
        body_text = str(result.body)
        assert UNEXPECTED_ERROR_SECRET not in body_text, (
            "Driver-internal cause leaked into a caller-visible response body — "
            "an INTERNAL-category error must never expose the real exception message."
        )

    async def test_correlation_id_from_traceparent(self, driver: TransportDriver) -> None:
        traceparent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
        result = await driver.call_with_traceparent(traceparent)
        assert result.correlation_id is not None
        assert result.correlation_id != ""

    async def test_correlation_id_from_header(self, driver: TransportDriver) -> None:
        result = await driver.call_with_correlation_header("test-corr-abc123")
        assert result.correlation_id == "test-corr-abc123"

    async def test_correlation_id_generated_when_absent(self, driver: TransportDriver) -> None:
        result = await driver.call_with_no_trace_context()
        assert result.correlation_id is not None
        assert result.correlation_id != ""


# ---------------------------------------------------------------------------
# Client-side conformance — the outbound counterpart, added for M1b
# ---------------------------------------------------------------------------
#
# Deliberately NOT built on DriverResult's normalise-into-data pattern. A
# remote client's execute() doesn't return a category-tagged result on
# failure — it raises a real AdapterError subclass directly (that's the
# whole point of raise_for_envelope: the caller doesn't need to know this
# dependency happens to be a remote service rather than a database). So
# the conformance test for the CLIENT side just calls the driver and
# asserts the real exception type, rather than introducing a second
# normalised-result type that would only exist to immediately throw an
# exception anyway.


@runtime_checkable
class RemoteClientDriver(Protocol):
    """
    What a remote-client implementation's own test module provides to
    run `RemoteClientConformanceTests`. Each `call_*` method exercises
    one real client call end to end (through the real client class, not
    by calling a lower-level method directly) against a real bound
    server on the other end — same rigor as `TransportDriver`, just for
    the opposite direction.
    """

    async def call_success(self) -> Any:
        """A call that succeeds — returns the decoded result."""
        ...

    async def call_not_found(self) -> Any:
        """A call whose target doesn't exist server-side. Must raise, not return."""
        ...

    async def call_timeout(self) -> Any:
        """The server raises `AdapterTimeoutError`. Must raise the same type client-side."""
        ...

    async def call_connection_refused(self) -> Any:
        """A genuine network-level connection failure — not a simulated server error."""
        ...

    async def call_unexpected_error(self) -> Any:
        """
        The server raises a bare, non-`OpenFrameError` exception carrying
        `UNEXPECTED_ERROR_SECRET`. Must raise `AdapterQueryError`
        client-side, with the secret absent from the exception's message.
        """
        ...


class RemoteClientConformanceTests:
    """
    Reusable pytest base class. Subclasses provide one fixture:

    ``driver``
        A fresh instance satisfying `RemoteClientDriver`, wired to a real
        client calling a real bound server over the transport under test.

    .. stability: experimental
       New alongside `openframe-protocol-http`'s `HttpRemoteQueryClient`/
       `HttpRemoteCommandClient`. Promotion to `openframe.core.testing` is
       gated on two transports (HTTP, gRPC) passing this suite for real,
       same rule as `TransportConformanceTests`.
    """

    async def test_call_success_returns_a_value(self, driver: "RemoteClientDriver") -> None:
        result = await driver.call_success()
        assert result is not None

    async def test_call_not_found_raises_adapter_not_found_error(
        self, driver: "RemoteClientDriver"
    ) -> None:
        from openframe.core.exceptions import AdapterNotFoundError

        try:
            await driver.call_not_found()
        except AdapterNotFoundError:
            pass
        else:
            raise AssertionError("expected AdapterNotFoundError to be raised")

    async def test_call_timeout_raises_adapter_timeout_error(
        self, driver: "RemoteClientDriver"
    ) -> None:
        from openframe.core.exceptions import AdapterTimeoutError

        try:
            await driver.call_timeout()
        except AdapterTimeoutError as exc:
            assert exc.retryable is True
        else:
            raise AssertionError("expected AdapterTimeoutError to be raised")

    async def test_call_connection_refused_raises_adapter_connection_error(
        self, driver: "RemoteClientDriver"
    ) -> None:
        from openframe.core.exceptions import AdapterConnectionError

        try:
            await driver.call_connection_refused()
        except AdapterConnectionError:
            pass
        else:
            raise AssertionError("expected AdapterConnectionError to be raised")

    async def test_call_unexpected_error_raises_adapter_query_error_and_does_not_leak(
        self, driver: "RemoteClientDriver"
    ) -> None:
        from openframe.core.exceptions import AdapterQueryError

        try:
            await driver.call_unexpected_error()
        except AdapterQueryError as exc:
            assert UNEXPECTED_ERROR_SECRET not in exc.message
        else:
            raise AssertionError("expected AdapterQueryError to be raised")
