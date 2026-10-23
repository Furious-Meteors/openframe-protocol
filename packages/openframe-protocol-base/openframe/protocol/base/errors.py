"""
openframe/protocol/base/errors.py
====================================
Transport-neutral error categorisation — the single source of truth every
`openframe-protocol-*` binding maps through, instead of each transport
hand-writing its own isinstance ladder over `OpenFrameError` subclasses.

`categorize()` is keyed on `exc.code` (a plain string, e.g.
"adapter.not_found"), not on `isinstance` checks against concrete classes —
this means a *new* `AdapterError` subclass from any downstream package,
with a code this module has never seen, still gets a sane `INTERNAL`
fallback instead of silently escaping unmapped. `ErrorCategory` itself is
NOT added to `openframe-core` — draft 8's own rule-of-three discipline
applies here exactly as it did to `Capability`/`Communication`: promote to
core only once at least two transports (HTTP + gRPC) show real
isinstance-ladder pain from re-deriving this table themselves.

Dependency order:
    protocol/base/errors → openframe.core.exceptions (read-only; never
    raises or re-exports core exception classes itself)
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum

from openframe.core.exceptions import (
    AdapterConnectionError,
    AdapterNotFoundError,
    AdapterQueryError,
    AdapterTimeoutError,
    OpenFrameError,
)

__all__ = [
    "ErrorCategory",
    "ErrorEnvelope",
    "build_envelope",
    "categorize",
    "raise_for_envelope",
]


class ErrorCategory(Enum):
    """
    Closed, transport-neutral bucket an error falls into.

    Every `openframe-protocol-*` binding maps exactly these five values to
    its own wire representation (HTTP status, gRPC status code, an MCP
    tool-error shape, ...) — see draft 8 section 5.2's table. Adding a
    sixth value here is a decision with the same weight as adding a
    `Capability` member: don't, until a real transport needs a
    distinction none of these five already draw.
    """

    NOT_FOUND = "not_found"
    INVALID = "invalid"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    INTERNAL = "internal"


# Code → category. Keyed on the plain `domain.kind` string `OpenFrameError.code`
# carries as data (see openframe.core.exceptions.codes.ErrorCode's own docstring)
# rather than on the exception *class*, so a downstream package's own
# `"ai.rate_limited"`-style code can be added here later without this module
# needing to import that package's exception classes at all.
_CODE_TO_CATEGORY: dict[str, ErrorCategory] = {
    "adapter.not_found": ErrorCategory.NOT_FOUND,
    "adapter.timeout": ErrorCategory.TIMEOUT,
    "adapter.connection": ErrorCategory.UNAVAILABLE,
    "plugin.not_found": ErrorCategory.NOT_FOUND,
}

# Retryability fallback for codes this module doesn't recognise at all
# (a non-OpenFrameError, or an OpenFrameError whose code isn't in the table
# above) — checked by exception *type* only as a last resort, since an
# unrecognised code has no `retryable` semantics to trust.
_RETRYABLE_TYPES = (AdapterConnectionError, AdapterTimeoutError)
_NOT_FOUND_TYPES = (AdapterNotFoundError,)


def categorize(exc: BaseException) -> ErrorCategory:
    """
    Map any exception to its transport-neutral category.

    Args:
        exc: The exception a binding caught — an `OpenFrameError` subclass,
             a `pydantic.ValidationError`, or anything else (a bare bug).

    Returns:
        The matching `ErrorCategory`. Falls back to `INTERNAL` for
        anything this module has no specific mapping for — including a
        brand-new `AdapterError` subclass from a package this module has
        never heard of. Never raises.
    """
    # pydantic's ValidationError (and FastAPI's RequestValidationError,
    # which wraps one) is the one validation-error source core itself has
    # none of — checked by class name string, not an isinstance import, so
    # this module has zero pydantic/FastAPI dependency of its own.
    if type(exc).__name__ in ("ValidationError", "RequestValidationError"):
        return ErrorCategory.INVALID

    if isinstance(exc, OpenFrameError):
        category = _CODE_TO_CATEGORY.get(exc.code)
        if category is not None:
            return category
        # Unrecognised OpenFrameError code — fall back to the exception's
        # own `retryable` flag/type rather than guessing blindly.
        if isinstance(exc, _NOT_FOUND_TYPES):
            return ErrorCategory.NOT_FOUND
        if isinstance(exc, _RETRYABLE_TYPES) or exc.retryable:
            return ErrorCategory.UNAVAILABLE
        return ErrorCategory.INTERNAL

    return ErrorCategory.INTERNAL


@dataclass(frozen=True)
class ErrorEnvelope:
    """
    The wire-level error body shape every transport serialises, in its own
    format, from the same four fields — see draft 8 section 5.2.

    Attributes:
        code:           `OpenFrameError.code` if the source was one,
                        else a generic `"internal.error"`.
        message:        Safe to show the caller. Never the raw `str(exc)`
                        for an `INTERNAL`-category error — see
                        `build_envelope`'s own docstring for why.
        retryable:      Drives `Retry-After` (HTTP) and equivalent retry
                        hints on other transports.
        correlation_id: Threaded through so a caller can quote it back
                        when reporting an issue.
    """

    code: str
    message: str
    retryable: bool
    correlation_id: str


def build_envelope(exc: BaseException, *, correlation_id: str | None = None) -> ErrorEnvelope:
    """
    Build the wire-safe error envelope for *exc*.

    Deliberately does **not** leak internals in the message for an
    `INTERNAL`-category error (draft 8's own stated rule) — the real
    `str(exc)` goes to logs and the current span via
    `openframe.core.telemetry.record_error`, never into the response body,
    since an `AdapterError.__str__` includes the driver cause and a stray
    driver error message is exactly the kind of internal detail a 500
    body should never carry.

    Args:
        exc:             The exception a binding caught.
        correlation_id:  The active request's correlation id. A fresh
                         `uuid4` is generated if the caller has none yet
                         (should not normally happen — bindings build
                         `RequestContext` before the handler can raise).

    Returns:
        An `ErrorEnvelope` ready for the calling transport to serialise.
    """
    category = categorize(exc)
    cid = correlation_id or str(uuid.uuid4())

    if isinstance(exc, OpenFrameError):
        code = exc.code
        retryable = exc.retryable
    else:
        code = "internal.error"
        retryable = False

    if category is ErrorCategory.INTERNAL:
        message = "An internal error occurred."
    elif isinstance(exc, OpenFrameError):
        message = exc.message
    else:
        message = str(exc)

    return ErrorEnvelope(code=code, message=message, retryable=retryable, correlation_id=cid)


# ---------------------------------------------------------------------------
# Client side: reconstruct an exception from a received envelope
# ---------------------------------------------------------------------------

_CATEGORY_TO_EXCEPTION_CLASS: dict[ErrorCategory, type[OpenFrameError]] = {
    ErrorCategory.NOT_FOUND: AdapterNotFoundError,
    ErrorCategory.TIMEOUT: AdapterTimeoutError,
    ErrorCategory.UNAVAILABLE: AdapterConnectionError,
    # INVALID and INTERNAL both reconstruct as AdapterQueryError — core's
    # AdapterError family has no dedicated "the request itself was
    # malformed" subclass (the server side used pydantic's ValidationError
    # directly, which only exists on the server; there is nothing to
    # reconstruct symmetrically on the client). AdapterQueryError is the
    # closest existing fit for both: a failed operation, not retryable by
    # default — documented here as a deliberate choice, not an oversight.
    ErrorCategory.INVALID: AdapterQueryError,
    ErrorCategory.INTERNAL: AdapterQueryError,
}


def raise_for_envelope(category: ErrorCategory, envelope: ErrorEnvelope, *, remote_name: str) -> None:
    """
    Reconstruct and raise the `AdapterError` subclass matching a remote
    call's error envelope — the client-side mirror of `build_envelope`.

    A remote service call is, from the calling code's perspective,
    exactly as fallible as a database call, and should surface through
    the same `AdapterError` family so callers don't need a separate
    catch clause for "this dependency happens to be another service"
    versus "this dependency happens to be a database" — see
    `openframe.core.remote`'s own docstring for the same reasoning
    applied to the port contract itself.

    Args:
        category:    The category the response's status (or whatever the
                     calling transport uses to signal failure) mapped to.
        envelope:    The decoded error envelope from the response body.
        remote_name: Identifies which remote call this was, in the raised
                     exception's own `adapter`/`operation` context — e.g.
                     `"http:GET /items/{item_id}"`. Not the envelope's own
                     `code`, which identifies the *server's* error, not
                     which call on the client side failed.

    Raises:
        Always raises — this function never returns normally. The
        specific `AdapterError` subclass is chosen by `category`;
        `retryable` on the raised instance reflects the envelope's own
        `retryable` flag, which may differ from the subclass's default
        (e.g. a `503` the server marked non-retryable for some reason).
    """
    exc_class = _CATEGORY_TO_EXCEPTION_CLASS[category]
    # AdapterError subclasses' own __init__ only forwards (message,
    # adapter, operation, cause) — confirmed directly, not assumed:
    # retryable/correlation_id are NOT constructor kwargs despite being
    # real OpenFrameError attributes, so they're set after construction.
    exc = exc_class(envelope.message, adapter=remote_name, operation="call")  # type: ignore[call-arg]  # every AdapterError subclass accepts these; exc_class's static type (type[OpenFrameError]) doesn't
    exc.retryable = envelope.retryable
    exc.correlation_id = envelope.correlation_id
    raise exc
