"""
openframe.protocol.base
==========================
Shared, transport-neutral mechanics every `openframe-protocol-*` binding
builds on: error categorisation (`errors.py`), `RequestContext`
construction (`context.py`), the inbound request/response conformance
suite (`testing.py`), the session-lifecycle loop (`session.py`), and its
own conformance suite (`session_testing.py`). No transport dependencies —
a binding package depends on this one, never the reverse.

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
from openframe.protocol.base.session import (
    open_session,
    run_opened_session,
    run_session,
    send_error_event,
)
from openframe.protocol.base.session_testing import (
    SessionConformanceTests,
    SessionDriver,
    SessionResult,
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
    "open_session",
    "run_opened_session",
    "run_session",
    "send_error_event",
    "SessionResult",
    "SessionDriver",
    "SessionConformanceTests",
]
