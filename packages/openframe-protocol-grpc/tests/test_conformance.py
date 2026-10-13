"""
Runs TransportConformanceTests for real, over real gRPC (grpc.aio server +
channel, both in the same test process, bound to 127.0.0.1 on an
OS-assigned port) — the second transport to pass this suite, after HTTP.
This is what actually proves the architecture isn't accidentally
HTTP-shaped, not just "more coverage": the suite's assertions are
expressed purely in terms of ErrorCategory/DriverResult, never an HTTP
status code, and this file is the proof that holds for a transport with
no status codes, no headers, and no path/query/body split at all.

grpc.aio has no true in-memory transport (confirmed in the M4 research
spike) — add_insecure_port("127.0.0.1:0") binds a real loopback socket,
same rigor TransportDriver already applies via httpx.ASGITransport for
HTTP (process-local, no network boundary crossed, but a real socket).
"""
from __future__ import annotations

import grpc
import pytest

from openframe.protocol.base.errors import ErrorCategory
from openframe.protocol.base.testing import DriverResult, TransportConformanceTests
from openframe.protocol.grpc import ROUTE_METADATA_KEY
from openframe.protocol.grpc import generic_pb2, generic_pb2_grpc


class _GrpcDriver:
    """TransportDriver over real gRPC, in-process, real loopback socket."""

    def __init__(self, port: int) -> None:
        self._channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
        self._stub = generic_pb2_grpc.GenericServiceStub(self._channel)

    async def aclose(self) -> None:
        """Closes the channel only — the server itself is the grpc_server fixture's responsibility."""
        await self._channel.close()

    async def _result(self, route: str, payload, *, extra_metadata: tuple = ()) -> DriverResult:
        import json

        metadata = ((ROUTE_METADATA_KEY, route),) + extra_metadata
        request = generic_pb2.GenericRequest(json_payload=json.dumps(payload))
        call = self._stub.Call(request, metadata=metadata)
        try:
            response = await call
        except grpc.aio.AioRpcError as exc:
            trailing = dict(exc.trailing_metadata() or ())
            details = exc.details() or ""
            try:
                body = json.loads(details)
            except json.JSONDecodeError:
                body = {"error": {"message": details}}
            error = body.get("error", body)
            return DriverResult(
                category=self._status_to_category(exc.code()),
                body=body,
                correlation_id=error.get("correlation_id") or trailing.get("x-correlation-id"),
                retryable=error.get("retryable"),
            )
        trailing = dict((await call.trailing_metadata()) or ())
        body = json.loads(response.json_payload) if response.json_payload else None
        return DriverResult(category=None, body=body, correlation_id=trailing.get("x-correlation-id"))

    @staticmethod
    def _status_to_category(code: grpc.StatusCode) -> ErrorCategory:
        return {
            grpc.StatusCode.NOT_FOUND: ErrorCategory.NOT_FOUND,
            grpc.StatusCode.INVALID_ARGUMENT: ErrorCategory.INVALID,
            grpc.StatusCode.DEADLINE_EXCEEDED: ErrorCategory.TIMEOUT,
            grpc.StatusCode.UNAVAILABLE: ErrorCategory.UNAVAILABLE,
            grpc.StatusCode.INTERNAL: ErrorCategory.INTERNAL,
        }[code]

    async def call_create(self) -> DriverResult:
        return await self._result("create", {"limit": 5})

    async def call_get_existing(self) -> DriverResult:
        return await self._result("existing", None)

    async def call_get_missing(self) -> DriverResult:
        return await self._result("missing", None)

    async def call_list(self) -> DriverResult:
        return await self._result("list", None)

    async def call_invalid_input(self) -> DriverResult:
        return await self._result("invalid", {"limit": "not-an-int"})

    async def call_timeout(self) -> DriverResult:
        return await self._result("timeout", None)

    async def call_connection_error(self) -> DriverResult:
        return await self._result("connection-error", None)

    async def call_unexpected_error(self) -> DriverResult:
        return await self._result("unexpected", None)

    async def call_with_traceparent(self, traceparent: str) -> DriverResult:
        return await self._result("trace-test", None, extra_metadata=(("traceparent", traceparent),))

    async def call_with_correlation_header(self, value: str) -> DriverResult:
        return await self._result("trace-test", None, extra_metadata=(("x-correlation-id", value),))

    async def call_with_no_trace_context(self) -> DriverResult:
        return await self._result("trace-test", None)


@pytest.fixture
async def driver(grpc_server):
    _server, port = grpc_server
    d = _GrpcDriver(port)
    yield d
    await d.aclose()


class TestGrpcConformance(TransportConformanceTests):
    """
    Proves TransportConformanceTests for real against grpc.aio — the
    second transport. Per draft 8's rule-of-three discipline, this is
    what justifies promoting the suite from openframe-protocol-base into
    openframe.core.testing (not done automatically by this file passing;
    a separate, deliberate step).
    """
