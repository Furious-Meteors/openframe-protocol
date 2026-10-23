"""
openframe/protocol/base/context.py
=====================================
Builds `openframe.core.inbound.RequestContext` for an inbound transport
binding — the one place that decides what `correlation_id` actually is,
so every transport does it the same way instead of each reinventing it.

`RequestContext` itself is a frozen dataclass in core with no factory
function (confirmed empty — nothing in core constructs one outside its
own tests). `principal`/`tenant` stay `None` here; wiring real auth
through is deferred to `openframe-infra`/security, per draft 8 section 7's
"reserve the hook, don't implement it yet" rule — `context_enricher`
below is that hook.

Dependency order:
    protocol/base/context → openframe.core.inbound, opentelemetry (trace API)
"""
from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping

from openframe.core.inbound import RequestContext
from opentelemetry import propagate, trace

__all__ = ["ContextEnricher", "build_request_context"]

ContextEnricher = Callable[[RequestContext], RequestContext]


def build_request_context(
    headers: Mapping[str, str],
    *,
    correlation_header: str = "x-correlation-id",
    context_enricher: ContextEnricher | None = None,
) -> RequestContext:
    """
    Build a `RequestContext` for one inbound request.

    `correlation_id` resolution order (first match wins):

    1. The trace id from an incoming W3C `traceparent` header, extracted
       directly from *this request's own headers* — not from whatever
       OTel span happens to be ambiently active. This distinction is
       the fix for a real bug found while building this: `TelemetryMiddleware`
       starts a span for *every* request regardless of whether one
       actually propagated in from a caller, so checking
       `trace.get_current_span()` (ambient state) can never tell "a
       trace genuinely arrived" apart from "a fresh local span with no
       upstream caller" — it would always win, making step 2 below
       unreachable in practice. Extracting straight from `headers`
       avoids that: no incoming `traceparent` means no trace id here,
       full stop, regardless of what span is locally active.
    2. The `correlation_header` request header (default
       `X-Correlation-ID`), for a caller that sends its own id without
       a trace context.
    3. A freshly generated `uuid4`, so `correlation_id` is never empty.

    Args:
        headers:            The inbound request's headers. Lookup is
                            case-insensitive — pass whatever mapping the
                            transport already has; most ASGI frameworks'
                            header mappings already are.
        correlation_header: Header name checked in step 2. Override only
                            if a deployment uses a non-default header.
        context_enricher:   Optional hook called with the built context
                            before it's returned, so a caller can attach
                            `principal`/`tenant` once real auth exists.
                            `None` (the default) leaves both unset — this
                            is the reserved-but-unimplemented auth seam
                            draft 8 asks for, not a finished feature.

    Returns:
        A `RequestContext` with a non-empty `correlation_id`.
    """
    extracted = propagate.extract(headers)
    span_context = trace.get_current_span(extracted).get_span_context()
    if span_context.is_valid:
        correlation_id = f"{span_context.trace_id:032x}"
    else:
        candidate = _get_header(headers, correlation_header)
        safe_candidate = candidate if candidate is not None and _is_safe_correlation_id(candidate) else None
        correlation_id = safe_candidate or str(uuid.uuid4())

    context = RequestContext(correlation_id=correlation_id)
    if context_enricher is not None:
        context = context_enricher(context)
    return context


_MAX_CORRELATION_ID_LENGTH = 128


def _is_safe_correlation_id(value: str) -> bool:
    """
    A client-supplied correlation id is reflected straight back into a
    response header and into every log line/span for the request — an
    overly long value is a cheap log-flooding vector, and a value
    containing control characters (CR/LF in particular) risks header
    injection if some caller downstream builds a raw header line from it
    without its own escaping. Neither is trusted: a value failing either
    check falls back to a fresh uuid4 instead of being used as-is.
    """
    return 0 < len(value) <= _MAX_CORRELATION_ID_LENGTH and value.isprintable() and "\n" not in value and "\r" not in value


def _get_header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup — ASGI header mappings vary on casing."""
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None
