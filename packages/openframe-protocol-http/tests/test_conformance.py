"""
Runs TransportConformanceTests for real, over real HTTP (httpx.AsyncClient
against an in-process FastAPI app) — the first transport to ever pass this
suite. A second transport (gRPC, later) passing it too is what actually
justifies promoting the suite into openframe.core.testing — this file
alone does not.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from openframe.protocol.base.errors import ErrorCategory
from openframe.protocol.base.testing import DriverResult, TransportConformanceTests


class _HttpDriver:
    """TransportDriver over real HTTP, in-process, no network socket."""

    def __init__(self, app) -> None:
        self._app = app
        # raise_app_exceptions=False: without this, httpx's ASGITransport
        # re-raises an unhandled exception into the test itself instead of
        # letting FastAPI's own `@app.exception_handler(Exception)` catch
        # it and return a real 500 — the opposite of what a real uvicorn
        # deployment does. Default is True specifically to surface raw
        # tracebacks during ad hoc testing; this driver wants the real
        # production error-handling path, not that shortcut.
        self._client = AsyncClient(
            transport=ASGITransport(app=self._app, raise_app_exceptions=False),
            base_url="http://test",
        )

    async def _result(self, response) -> DriverResult:
        correlation_id = response.headers.get("x-correlation-id")
        if response.status_code >= 400:
            body = response.json()
            error = body.get("error", body)
            return DriverResult(
                category=self._status_to_category(response.status_code),
                body=body,
                correlation_id=error.get("correlation_id", correlation_id),
                retryable=error.get("retryable"),
            )
        return DriverResult(category=None, body=response.json(), correlation_id=correlation_id)

    @staticmethod
    def _status_to_category(status: int) -> ErrorCategory:
        return {
            404: ErrorCategory.NOT_FOUND,
            422: ErrorCategory.INVALID,
            504: ErrorCategory.TIMEOUT,
            503: ErrorCategory.UNAVAILABLE,
            500: ErrorCategory.INTERNAL,
        }[status]

    async def call_create(self) -> DriverResult:
        return await self._result(await self._client.post("/create", json={"limit": 5}))

    async def call_get_existing(self) -> DriverResult:
        return await self._result(await self._client.get("/existing"))

    async def call_get_missing(self) -> DriverResult:
        return await self._result(await self._client.get("/missing"))

    async def call_list(self) -> DriverResult:
        return await self._result(await self._client.get("/list"))

    async def call_invalid_input(self) -> DriverResult:
        return await self._result(await self._client.post("/invalid", json={"limit": "not-an-int"}))

    async def call_timeout(self) -> DriverResult:
        return await self._result(await self._client.get("/timeout"))

    async def call_connection_error(self) -> DriverResult:
        return await self._result(await self._client.get("/connection-error"))

    async def call_unexpected_error(self) -> DriverResult:
        return await self._result(await self._client.get("/unexpected"))

    async def call_with_traceparent(self, traceparent: str) -> DriverResult:
        return await self._result(
            await self._client.get("/trace-test", headers={"traceparent": traceparent})
        )

    async def call_with_correlation_header(self, value: str) -> DriverResult:
        return await self._result(
            await self._client.get("/trace-test", headers={"X-Correlation-ID": value})
        )

    async def call_with_no_trace_context(self) -> DriverResult:
        return await self._result(await self._client.get("/trace-test"))


class TestHttpConformance(TransportConformanceTests):
    @pytest.fixture
    async def driver(self, fastapi_app):
        driver = _HttpDriver(fastapi_app)
        yield driver
        await driver._client.aclose()


async def test_traceparent_trace_id_actually_propagates(fastapi_app) -> None:
    """
    The base suite's own `test_correlation_id_from_traceparent` only
    asserts non-empty (deliberately transport-agnostic — see its
    docstring). This test is HTTP-specific and checks the real value:
    the correlation id returned must be derived from the exact trace id
    in the traceparent header sent, not a coincidentally-non-empty
    generated uuid4 — caught a real gap where TelemetryMiddleware wasn't
    wired into the toy app, so this assertion would have silently passed
    on fallback-generated ids without ever exercising real extraction.
    """
    driver = _HttpDriver(fastapi_app)
    try:
        trace_id = "0af7651916cd43dd8448eb211c80319c"
        traceparent = f"00-{trace_id}-b7ad6b7169203331-01"
        result = await driver.call_with_traceparent(traceparent)
        assert result.correlation_id == trace_id
    finally:
        await driver._client.aclose()
