"""
Proves TransportConformanceTests itself is correct, against a trivial
in-memory fake driver — before any real transport binding exists to run
it against. Catches a bug in the suite's own assertions early, rather
than only discovering one later while debugging what looks like an HTTP
binding failure.
"""
from __future__ import annotations

import pytest
from openframe.core.exceptions import AdapterConnectionError, AdapterTimeoutError
from openframe.protocol.base.errors import ErrorCategory, build_envelope
from openframe.protocol.base.testing import (
    UNEXPECTED_ERROR_SECRET,
    DriverResult,
    TransportConformanceTests,
)


class _FakeDriver:
    """Minimal driver: no real transport, just builds DriverResult directly."""

    async def call_create(self) -> DriverResult:
        return DriverResult(category=None, body={"id": "1"}, correlation_id="corr-1")

    async def call_get_existing(self) -> DriverResult:
        return DriverResult(category=None, body={"id": "1"}, correlation_id="corr-1")

    async def call_get_missing(self) -> DriverResult:
        return DriverResult(category=ErrorCategory.NOT_FOUND, body={}, correlation_id="corr-1")

    async def call_list(self) -> DriverResult:
        return DriverResult(category=None, body={"items": []}, correlation_id="corr-1")

    async def call_invalid_input(self) -> DriverResult:
        return DriverResult(category=ErrorCategory.INVALID, body={}, correlation_id="corr-1")

    async def call_timeout(self) -> DriverResult:
        exc = AdapterTimeoutError("slow", adapter="x", operation="get")
        envelope = build_envelope(exc, correlation_id="corr-1")
        return DriverResult(
            category=ErrorCategory.TIMEOUT,
            body={"error": envelope.message},
            correlation_id="corr-1",
            retryable=envelope.retryable,
        )

    async def call_connection_error(self) -> DriverResult:
        exc = AdapterConnectionError("down", adapter="x", operation="get")
        envelope = build_envelope(exc, correlation_id="corr-1")
        return DriverResult(
            category=ErrorCategory.UNAVAILABLE,
            body={"error": envelope.message},
            correlation_id="corr-1",
            retryable=envelope.retryable,
        )

    async def call_unexpected_error(self) -> DriverResult:
        exc = RuntimeError(UNEXPECTED_ERROR_SECRET)
        envelope = build_envelope(exc, correlation_id="corr-1")
        return DriverResult(
            category=ErrorCategory.INTERNAL, body={"error": envelope.message}, correlation_id="corr-1"
        )

    async def call_with_traceparent(self, traceparent: str) -> DriverResult:
        return DriverResult(category=None, body={}, correlation_id="derived-from-trace")

    async def call_with_correlation_header(self, value: str) -> DriverResult:
        return DriverResult(category=None, body={}, correlation_id=value)

    async def call_with_no_trace_context(self) -> DriverResult:
        return DriverResult(category=None, body={}, correlation_id="generated-uuid")


class TestFakeDriverConformance(TransportConformanceTests):
    @pytest.fixture
    def driver(self) -> _FakeDriver:
        return _FakeDriver()


def test_driver_satisfies_transport_driver_protocol() -> None:
    from openframe.protocol.base.testing import TransportDriver

    assert isinstance(_FakeDriver(), TransportDriver)
