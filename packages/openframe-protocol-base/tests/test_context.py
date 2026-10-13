"""
Tests for openframe.protocol.base.context.

Uses core's own `span_exporter` fixture (via conftest's wildcard import of
`openframe.core.testing.fixtures`) to create a real active span, rather
than mocking the OTel trace API — proving `build_request_context` against
the real SDK, not a stand-in for it.
"""
from __future__ import annotations

from opentelemetry import propagate, trace

from openframe.protocol.base.context import build_request_context


def test_correlation_id_from_incoming_traceparent_header(span_exporter) -> None:
    """
    `build_request_context` extracts from the request's own headers, not
    from whatever span happens to be ambiently active — see context.py's
    own docstring for the real bug this distinction fixes (a locally
    auto-started span, with no incoming traceparent, must NOT be treated
    as a propagated trace). Simulate a real incoming traceparent by
    injecting a genuine span's context into a headers dict, the same way
    an actual upstream caller would.
    """
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("upstream-span") as span:
        headers: dict[str, str] = {}
        propagate.inject(headers)
        expected_trace_id = f"{span.get_span_context().trace_id:032x}"

    context = build_request_context(headers=headers)
    assert context.correlation_id == expected_trace_id
    assert context.principal is None
    assert context.tenant is None


def test_ambient_span_with_no_incoming_traceparent_is_not_used() -> None:
    """
    The bug this guards against: a locally-started span (e.g. one
    `TelemetryMiddleware` starts for every request, trace-propagated or
    not) must not be picked up when the request's own headers carry no
    `traceparent` — otherwise an explicit `X-Correlation-ID` header could
    never win, which is exactly what broke before this was fixed.
    """
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("local-only-span"):
        context = build_request_context(headers={"X-Correlation-ID": "explicit-id"})
    assert context.correlation_id == "explicit-id"


def test_correlation_id_from_header_when_no_active_span() -> None:
    context = build_request_context(headers={"X-Correlation-ID": "my-corr-id"})
    assert context.correlation_id == "my-corr-id"


def test_correlation_id_header_lookup_is_case_insensitive() -> None:
    context = build_request_context(headers={"x-correlation-id": "lower-case-id"})
    assert context.correlation_id == "lower-case-id"


def test_correlation_id_generated_when_nothing_available() -> None:
    context = build_request_context(headers={})
    assert context.correlation_id
    assert len(context.correlation_id) > 0


def test_correlation_id_uses_custom_header_name() -> None:
    context = build_request_context(
        headers={"X-Request-ID": "custom-header-id"},
        correlation_header="X-Request-ID",
    )
    assert context.correlation_id == "custom-header-id"


def test_context_enricher_hook_is_applied() -> None:
    from dataclasses import replace

    def enrich(ctx):
        return replace(ctx, correlation_id=f"enriched-{ctx.correlation_id}")

    context = build_request_context(
        headers={"X-Correlation-ID": "base-id"}, context_enricher=enrich
    )
    assert context.correlation_id == "enriched-base-id"
