"""
openframe/protocol/grpc/client.py
=====================================
`GrpcRemoteQueryClient`/`GrpcRemoteCommandClient` — gRPC implementations
of `openframe.core.remote`'s `RemoteQueryClient`/`RemoteCommandClient`,
the gRPC counterpart of `-http`'s `HttpRemoteQueryClient`/
`HttpRemoteCommandClient`.

No `RequestSpec`-equivalent needed — simpler than the HTTP client's
`build_request` hook, not a weaker version of it. gRPC has no path/query/
body split: a request is just one message (`GenericRequest`), so the
handler's input value is JSON-encoded into `json_payload` directly, no
hook required to decide where each piece of it goes.

Lifecycle, per draft 8's D10 (same reasoning as `-http`'s client.py):
no `BasePort`/`PluginRegistry` involvement. Each client owns (or borrows)
a plain `grpc.aio.Channel` and exposes `aclose()` for a caller's own
lifespan to manage directly.

Dependency order:
    protocol/grpc/client → protocol/base (errors, for the reverse mapping), protocol/grpc (generic_pb2*)
"""
from __future__ import annotations

import json
from typing import Any, Generic, TypeVar

from openframe.core.inbound import RequestContext
from openframe.protocol.base.errors import (
    ErrorCategory,
    ErrorEnvelope,
    raise_for_envelope,
)
from openframe.protocol.grpc import generic_pb2, generic_pb2_grpc
from openframe.protocol.grpc.server import ROUTE_METADATA_KEY, _to_jsonable

import grpc

__all__ = ["GrpcRemoteCommandClient", "GrpcRemoteQueryClient"]

TInput = TypeVar("TInput")
TOutput = TypeVar("TOutput")

_STATUS_CODE_TO_CATEGORY: dict[grpc.StatusCode, ErrorCategory] = {
    grpc.StatusCode.NOT_FOUND: ErrorCategory.NOT_FOUND,
    grpc.StatusCode.INVALID_ARGUMENT: ErrorCategory.INVALID,
    grpc.StatusCode.DEADLINE_EXCEEDED: ErrorCategory.TIMEOUT,
    grpc.StatusCode.UNAVAILABLE: ErrorCategory.UNAVAILABLE,
}


class _GrpcRemoteCallMixin:
    """Shared machinery both client classes below use — not public itself."""

    def __init__(
        self,
        *,
        target: str | None = None,
        route: str,
        channel: grpc.aio.Channel | None = None,
        remote_name: str | None = None,
    ) -> None:
        if channel is None and target is None:
            raise ValueError("Either target or an existing channel must be provided.")
        self._channel = channel if channel is not None else grpc.aio.insecure_channel(target)
        self._owns_channel = channel is None
        self._stub = generic_pb2_grpc.GenericServiceStub(self._channel)
        self._route = route
        self._remote_name = remote_name or f"grpc:{route}"

    async def aclose(self) -> None:
        """Close the underlying channel — only if this instance created it."""
        if self._owns_channel:
            await self._channel.close()

    async def _call(self, value: Any, context: RequestContext | None) -> Any:
        metadata: list[tuple[str, str]] = [(ROUTE_METADATA_KEY, self._route)]
        if context is not None:
            metadata.append(("x-correlation-id", context.correlation_id))

        # _to_jsonable (same helper bind_service uses on the way out) —
        # without it, a caller passing a BaseModel (the value every
        # bind_command/bind_query handler built for -http already accepts)
        # fails json.dumps with a bare TypeError instead of being sent.
        request = generic_pb2.GenericRequest(json_payload=json.dumps(_to_jsonable(value)))

        try:
            response = await self._stub.Call(request, metadata=tuple(metadata))
        except grpc.aio.AioRpcError as exc:
            category = _STATUS_CODE_TO_CATEGORY.get(exc.code(), ErrorCategory.INTERNAL)
            details = exc.details() or ""
            try:
                body = json.loads(details)
                error = body.get("error", body)
            except (json.JSONDecodeError, AttributeError):
                error = {}
            envelope = ErrorEnvelope(
                code=error.get("code", "unknown"),
                message=error.get("message", details or f"{self._remote_name} failed with {exc.code()}"),
                retryable=error.get("retryable", False),
                correlation_id=error.get("correlation_id", ""),
            )
            raise_for_envelope(category, envelope, remote_name=self._remote_name)

        if not response.json_payload:
            return None
        return json.loads(response.json_payload)


class GrpcRemoteQueryClient(_GrpcRemoteCallMixin, Generic[TInput, TOutput]):
    """
    `RemoteQueryClient` over gRPC. Satisfies
    `openframe.core.remote.RemoteQueryClient` structurally.

    Args: see `_GrpcRemoteCallMixin.__init__`.
    """

    async def execute(self, query: TInput, context: RequestContext | None = None) -> TOutput:
        return await self._call(query, context)


class GrpcRemoteCommandClient(_GrpcRemoteCallMixin, Generic[TInput]):
    """
    `RemoteCommandClient` over gRPC. Satisfies
    `openframe.core.remote.RemoteCommandClient` structurally.
    """

    async def execute(self, command: TInput, context: RequestContext | None = None) -> None:
        await self._call(command, context)
