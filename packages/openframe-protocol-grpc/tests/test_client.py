"""
Tests for GrpcRemoteQueryClient — against the SAME real, in-process
grpc.aio server test_conformance.py already built and proved for the
server side. Real loopback socket (grpc.aio has no true in-memory
transport, confirmed in the M4 spike), same rigor as test_client.py's
real-connection-refused test for HTTP.

Also proves RemoteClientConformanceTests (openframe-protocol-base) for
the second time, after HTTP — exactly what PLAN.md's M4 section claims
this build would do.
"""
from __future__ import annotations

import pytest

from openframe.core.exceptions import (
    AdapterConnectionError,
    AdapterNotFoundError,
    AdapterQueryError,
    AdapterTimeoutError,
)
from openframe.protocol.base.testing import RemoteClientConformanceTests
from openframe.protocol.grpc import GrpcRemoteQueryClient


@pytest.fixture
async def running_server(grpc_server):
    _server, port = grpc_server
    return port


async def test_query_client_success(running_server) -> None:
    client = GrpcRemoteQueryClient(target=f"127.0.0.1:{running_server}", route="existing")
    result = await client.execute(None)
    assert result == {"id": "1"}
    await client.aclose()


async def test_query_client_not_found_raises_adapter_not_found_error(running_server) -> None:
    client = GrpcRemoteQueryClient(target=f"127.0.0.1:{running_server}", route="missing")
    with pytest.raises(AdapterNotFoundError):
        await client.execute(None)
    await client.aclose()


async def test_query_client_timeout_raises_adapter_timeout_error(running_server) -> None:
    client = GrpcRemoteQueryClient(target=f"127.0.0.1:{running_server}", route="timeout")
    with pytest.raises(AdapterTimeoutError) as exc_info:
        await client.execute(None)
    assert exc_info.value.retryable is True
    await client.aclose()


async def test_query_client_connection_error_raises_adapter_connection_error(running_server) -> None:
    client = GrpcRemoteQueryClient(target=f"127.0.0.1:{running_server}", route="connection-error")
    with pytest.raises(AdapterConnectionError) as exc_info:
        await client.execute(None)
    assert exc_info.value.retryable is True
    await client.aclose()


async def test_query_client_unexpected_error_raises_adapter_query_error_and_does_not_leak(
    running_server,
) -> None:
    from openframe.protocol.base.testing import UNEXPECTED_ERROR_SECRET

    client = GrpcRemoteQueryClient(target=f"127.0.0.1:{running_server}", route="unexpected")
    with pytest.raises(AdapterQueryError) as exc_info:
        await client.execute(None)
    assert UNEXPECTED_ERROR_SECRET not in exc_info.value.message
    await client.aclose()


async def test_query_client_real_connection_refused_raises_adapter_connection_error() -> None:
    """
    Not a simulated error from a toy handler — an actual real-socket
    connection refusal, proving the grpc.aio.AioRpcError-with-UNAVAILABLE
    except branch in client.py itself is reachable. Port 1 is a real,
    always-unused, always-refusing port on localhost — same technique
    test_conformance.py's HTTP counterpart uses.
    """
    client = GrpcRemoteQueryClient(target="127.0.0.1:1", route="anything")
    with pytest.raises(AdapterConnectionError):
        await client.execute(None)
    await client.aclose()


class _GrpcRemoteClientDriver:
    """
    RemoteClientDriver over GrpcRemoteQueryClient, against the same real
    in-process grpc.aio server test_conformance.py already proved the
    server side with.
    """

    def __init__(self, port: int) -> None:
        self._port = port

    async def call_success(self) -> object:
        client = GrpcRemoteQueryClient(target=f"127.0.0.1:{self._port}", route="existing")
        try:
            return await client.execute(None)
        finally:
            await client.aclose()

    async def call_not_found(self) -> object:
        client = GrpcRemoteQueryClient(target=f"127.0.0.1:{self._port}", route="missing")
        try:
            return await client.execute(None)
        finally:
            await client.aclose()

    async def call_timeout(self) -> object:
        client = GrpcRemoteQueryClient(target=f"127.0.0.1:{self._port}", route="timeout")
        try:
            return await client.execute(None)
        finally:
            await client.aclose()

    async def call_connection_refused(self) -> object:
        client = GrpcRemoteQueryClient(target="127.0.0.1:1", route="anything")
        try:
            return await client.execute(None)
        finally:
            await client.aclose()

    async def call_unexpected_error(self) -> object:
        client = GrpcRemoteQueryClient(target=f"127.0.0.1:{self._port}", route="unexpected")
        try:
            return await client.execute(None)
        finally:
            await client.aclose()


@pytest.fixture
async def driver(running_server):
    return _GrpcRemoteClientDriver(running_server)


class TestGrpcRemoteClientConformance(RemoteClientConformanceTests):
    """
    Proves RemoteClientConformanceTests for the second time, after HTTP
    — two transports both passing the same client-side suite is what
    actually proves raise_for_envelope()'s reconstruction logic isn't
    accidentally HTTP-shaped either.
    """
