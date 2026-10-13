"""
Tests for openframe.protocol.base.errors.

The table test (`test_every_core_exception_class_has_a_category`) is the
one that matters most: it enumerates every real `AdapterError`/
`PluginError` subclass from the installed `openframe-core` and asserts
each maps to *some* category, rather than hand-listing the classes here —
so a new subclass added to core later is automatically covered (or
automatically caught as newly falling through to INTERNAL, which is a
correct, not a surprising, outcome for an unrecognised code).
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from openframe.core.exceptions import (
    AdapterConfigurationError,
    AdapterConnectionError,
    AdapterError,
    AdapterNotFoundError,
    AdapterQueryError,
    AdapterTimeoutError,
)
from openframe.core.exceptions.plugin import (
    AmbiguousCapabilityError,
    DuplicatePluginError,
    PluginError,
    PluginInitializationError,
    PluginNotFoundError,
)
from openframe.protocol.base.errors import (
    ErrorCategory,
    ErrorEnvelope,
    build_envelope,
    categorize,
    raise_for_envelope,
)


# ---------------------------------------------------------------------------
# categorize() — one test per real adapter exception class
# ---------------------------------------------------------------------------


def test_connection_error_is_unavailable() -> None:
    exc = AdapterConnectionError("down", adapter="x", operation="get")
    assert categorize(exc) is ErrorCategory.UNAVAILABLE


def test_query_error_is_internal() -> None:
    exc = AdapterQueryError("bad query", adapter="x", operation="get")
    assert categorize(exc) is ErrorCategory.INTERNAL


def test_not_found_error_is_not_found() -> None:
    exc = AdapterNotFoundError("missing", adapter="x", operation="get")
    assert categorize(exc) is ErrorCategory.NOT_FOUND


def test_configuration_error_is_internal() -> None:
    exc = AdapterConfigurationError("bad config", adapter="x", operation="init")
    assert categorize(exc) is ErrorCategory.INTERNAL


def test_timeout_error_is_timeout() -> None:
    exc = AdapterTimeoutError("too slow", adapter="x", operation="get")
    assert categorize(exc) is ErrorCategory.TIMEOUT


def test_plugin_not_found_is_not_found() -> None:
    exc = PluginNotFoundError("no such plugin", plugin_name="x")
    assert categorize(exc) is ErrorCategory.NOT_FOUND


def test_plugin_initialization_error_is_internal() -> None:
    exc = PluginInitializationError("init failed", plugin_name="x")
    assert categorize(exc) is ErrorCategory.INTERNAL


def test_duplicate_plugin_error_is_internal() -> None:
    exc = DuplicatePluginError("dup", plugin_name="x")
    assert categorize(exc) is ErrorCategory.INTERNAL


def test_ambiguous_capability_error_is_internal() -> None:
    from openframe.core.ports import Capability

    exc = AmbiguousCapabilityError(
        "ambiguous", plugin_name="x", capability=Capability.PERSISTENCE, matches=["a", "b"]
    )
    assert categorize(exc) is ErrorCategory.INTERNAL


# ---------------------------------------------------------------------------
# Table test — every real class, enumerated from core itself
# ---------------------------------------------------------------------------


def _all_adapter_and_plugin_exception_instances() -> list[BaseException]:
    """
    Enumerate every real `AdapterError`/`PluginError` subclass from the
    installed `openframe-core` and construct one instance of each —
    building, not just listing classes, since `AmbiguousCapabilityError`
    needs constructor args none of the others do.
    """
    import openframe.core.exceptions.adapter as adapter_mod
    import openframe.core.exceptions.plugin as plugin_mod
    from openframe.core.ports import Capability

    instances: list[BaseException] = []
    for name in adapter_mod.__all__:
        obj = getattr(adapter_mod, name)
        if isinstance(obj, type) and issubclass(obj, AdapterError) and obj is not AdapterError:
            instances.append(obj("msg", adapter="x", operation="op"))
        elif obj is AdapterError:
            instances.append(obj("msg", adapter="x", operation="op"))

    for name in plugin_mod.__all__:
        obj = getattr(plugin_mod, name)
        if obj is AmbiguousCapabilityError:
            instances.append(
                obj("ambiguous", plugin_name="x", capability=Capability.PERSISTENCE, matches=["a"])
            )
        elif isinstance(obj, type) and issubclass(obj, PluginError):
            instances.append(obj("msg", plugin_name="x"))
    return instances


@pytest.mark.parametrize(
    "instance", _all_adapter_and_plugin_exception_instances(), ids=lambda e: type(e).__name__
)
def test_every_core_exception_class_has_a_category(instance: BaseException) -> None:
    """Every real AdapterError/PluginError instance maps to a category — never raises."""
    category = categorize(instance)
    assert isinstance(category, ErrorCategory)


# ---------------------------------------------------------------------------
# Non-OpenFrameError sources
# ---------------------------------------------------------------------------


class _Model(BaseModel):
    name: str


def test_pydantic_validation_error_is_invalid() -> None:
    try:
        _Model()
    except ValidationError as exc:
        assert categorize(exc) is ErrorCategory.INVALID
    else:
        pytest.fail("expected ValidationError")


def test_bare_exception_is_internal() -> None:
    assert categorize(RuntimeError("boom")) is ErrorCategory.INTERNAL


# ---------------------------------------------------------------------------
# build_envelope() — the message-leak guard
# ---------------------------------------------------------------------------


def test_internal_error_envelope_does_not_leak_driver_cause() -> None:
    cause = RuntimeError("super secret connection string: postgres://user:pw@host")
    exc = AdapterQueryError("insert failed", adapter="postgres", operation="create", cause=cause)
    envelope = build_envelope(exc, correlation_id="corr-1")
    assert "super secret" not in envelope.message
    assert envelope.message == "An internal error occurred."


def test_not_found_error_envelope_keeps_the_safe_message() -> None:
    exc = AdapterNotFoundError("Item 'abc' not found", adapter="postgres", operation="get")
    envelope = build_envelope(exc, correlation_id="corr-2")
    assert envelope.message == "Item 'abc' not found"
    assert envelope.retryable is False
    assert envelope.code == "adapter.not_found"


def test_connection_error_envelope_is_retryable() -> None:
    exc = AdapterConnectionError("unreachable", adapter="postgres", operation="connect")
    envelope = build_envelope(exc, correlation_id="corr-3")
    assert envelope.retryable is True


def test_envelope_generates_correlation_id_when_none_given() -> None:
    envelope = build_envelope(RuntimeError("boom"))
    assert envelope.correlation_id


# ---------------------------------------------------------------------------
# raise_for_envelope() — the client-side reverse mapping
# ---------------------------------------------------------------------------


def test_raise_for_envelope_not_found() -> None:
    from openframe.core.exceptions import AdapterNotFoundError

    envelope = ErrorEnvelope(
        code="adapter.not_found", message="Item 'x' not found", retryable=False, correlation_id="c1"
    )
    with pytest.raises(AdapterNotFoundError) as exc_info:
        raise_for_envelope(ErrorCategory.NOT_FOUND, envelope, remote_name="http:GET /items/{id}")
    assert exc_info.value.message == "Item 'x' not found"
    assert exc_info.value.retryable is False
    assert exc_info.value.correlation_id == "c1"
    assert exc_info.value.adapter == "http:GET /items/{id}"


def test_raise_for_envelope_timeout_is_retryable() -> None:
    from openframe.core.exceptions import AdapterTimeoutError

    envelope = ErrorEnvelope(code="adapter.timeout", message="slow", retryable=True, correlation_id="c2")
    with pytest.raises(AdapterTimeoutError) as exc_info:
        raise_for_envelope(ErrorCategory.TIMEOUT, envelope, remote_name="http:GET /x")
    assert exc_info.value.retryable is True


def test_raise_for_envelope_unavailable_maps_to_connection_error() -> None:
    from openframe.core.exceptions import AdapterConnectionError

    envelope = ErrorEnvelope(code="adapter.connection", message="down", retryable=True, correlation_id="c3")
    with pytest.raises(AdapterConnectionError):
        raise_for_envelope(ErrorCategory.UNAVAILABLE, envelope, remote_name="http:GET /x")


def test_raise_for_envelope_invalid_and_internal_both_map_to_query_error() -> None:
    from openframe.core.exceptions import AdapterQueryError

    for category in (ErrorCategory.INVALID, ErrorCategory.INTERNAL):
        envelope = ErrorEnvelope(code="x", message="bad", retryable=False, correlation_id="c4")
        with pytest.raises(AdapterQueryError):
            raise_for_envelope(category, envelope, remote_name="http:POST /x")


def test_raise_for_envelope_round_trips_through_build_envelope() -> None:
    """
    build_envelope(exc) then raise_for_envelope(category, envelope) on
    the result reconstructs an exception of the same category — proving
    the two functions are genuine inverses, not just independently
    plausible-looking mappings.
    """
    from openframe.core.exceptions import AdapterConnectionError, AdapterNotFoundError

    original = AdapterNotFoundError("missing", adapter="postgres", operation="get")
    category = categorize(original)
    envelope = build_envelope(original, correlation_id="round-trip-1")
    with pytest.raises(AdapterNotFoundError) as exc_info:
        raise_for_envelope(category, envelope, remote_name="http:GET /items/{id}")
    assert exc_info.value.message == "missing"

    original2 = AdapterConnectionError("down", adapter="postgres", operation="connect")
    category2 = categorize(original2)
    envelope2 = build_envelope(original2, correlation_id="round-trip-2")
    with pytest.raises(AdapterConnectionError):
        raise_for_envelope(category2, envelope2, remote_name="http:GET /x")
