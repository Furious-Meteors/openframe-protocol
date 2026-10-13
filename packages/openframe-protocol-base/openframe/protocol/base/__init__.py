"""
openframe.protocol.base
==========================
Shared, transport-neutral mechanics every `openframe-protocol-*` binding
builds on: error categorisation (`errors.py`), `RequestContext`
construction (`context.py`), and the inbound conformance suite
(`testing.py`). No transport dependencies — a binding package depends on
this one, never the reverse.

.. stability: experimental
"""
from __future__ import annotations

from openframe.protocol.base.context import build_request_context
from openframe.protocol.base.errors import (
    ErrorCategory,
    ErrorEnvelope,
    build_envelope,
    categorize,
    raise_for_envelope,
)
from openframe.protocol.base.testing import (
    UNEXPECTED_ERROR_SECRET,
    DriverResult,
    RemoteClientConformanceTests,
    RemoteClientDriver,
    TransportConformanceTests,
    TransportDriver,
)

__all__ = [
    "ErrorCategory",
    "ErrorEnvelope",
    "categorize",
    "build_envelope",
    "raise_for_envelope",
    "build_request_context",
    "DriverResult",
    "TransportDriver",
    "TransportConformanceTests",
    "UNEXPECTED_ERROR_SECRET",
    "RemoteClientDriver",
    "RemoteClientConformanceTests",
]
