"""
openframe/protocol/http/client.py
====================================
`HttpRemoteQueryClient`/`HttpRemoteCommandClient` — HTTP implementations
of `openframe.core.remote`'s `RemoteQueryClient`/`RemoteCommandClient`,
for calling another service built on this platform (M1b).

Lifecycle, per draft 8's D10: no `BasePort`/`PluginRegistry` involvement.
Each client owns (or borrows) a plain `httpx.AsyncClient` and exposes
`aclose()` for a caller's own lifespan to manage directly — the same
reasoning that kept `RemoteQueryClient`/`RemoteCommandClient` themselves
out of the `Capability` system in the first place applies to their
lifecycle too: there's no dynamic resolution happening, so there's
nothing for a registry to orchestrate.

Request construction deliberately mirrors `server.py`'s own
`build_input` philosophy rather than guessing how to serialise an
arbitrary input: a `build_request` hook returns exactly which path
params, query params, and JSON body a given call needs. Support exactly
the shapes `items-postgres`'s own routes need (path param only, query
params only, JSON body only) — not a fully generic REST client.

Dependency order:
    protocol/http/client → protocol/base (errors, for the reverse mapping)
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar
from urllib.parse import quote

import httpx
from openframe.core.inbound import RequestContext
from openframe.protocol.base.errors import (
    ErrorCategory,
    ErrorEnvelope,
    raise_for_envelope,
)
from opentelemetry import propagate

__all__ = ["HttpRemoteCommandClient", "HttpRemoteQueryClient", "RequestSpec"]

TInput = TypeVar("TInput")
TOutput = TypeVar("TOutput")

_STATUS_TO_CATEGORY: dict[int, ErrorCategory] = {
    404: ErrorCategory.NOT_FOUND,
    422: ErrorCategory.INVALID,
    504: ErrorCategory.TIMEOUT,
    503: ErrorCategory.UNAVAILABLE,
    # 429 (rate limited) and 502 (bad gateway) are both transient,
    # retry-worthy failures of the remote side, not "this request is
    # malformed" — the same bucket as 503, not the INTERNAL default.
    429: ErrorCategory.UNAVAILABLE,
    502: ErrorCategory.UNAVAILABLE,
}


@dataclass(frozen=True)
class RequestSpec:
    """
    What one call actually sends — returned by a `build_request` hook,
    given the handler's own input value.

    Attributes:
        path_params: Substituted into the client's `path` template via
                     `str.format(**path_params)`, e.g. `{"item_id": "abc"}`
                     for a path of `"/items/{item_id}"`.
        query_params: Sent as the URL query string.
        json_body:    Sent as the request's JSON body. `None` means no
                     body at all (correct for GET, not just "empty dict").
    """

    path_params: dict[str, Any] = field(default_factory=dict)
    query_params: dict[str, Any] = field(default_factory=dict)
    json_body: Any = None


def _default_request_spec(value: Any) -> RequestSpec:
    """
    `build_request=None` default: if the input is falsy/None, send
    nothing; otherwise send it as the JSON body. Covers the common
    "one JSON-body input, no path or query params" case without
    requiring every caller to write a trivial `build_request`.
    """
    if value is None:
        return RequestSpec()
    return RequestSpec(json_body=value)


class _HttpRemoteCallMixin:
    """Shared machinery both client classes below use — not public itself."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        path: str,
        method: str,
        client: httpx.AsyncClient | None = None,
        build_request: Callable[[Any], RequestSpec] | None = None,
        remote_name: str | None = None,
    ) -> None:
        if client is None and base_url is None:
            raise ValueError("Either base_url or an existing client must be provided.")
        if client is not None:
            self._client = client
        else:
            # base_url is guaranteed non-None here (the check above raises
            # otherwise) — asserted so mypy can narrow it, not just for
            # runtime safety.
            assert base_url is not None
            self._client = httpx.AsyncClient(base_url=base_url)
        self._owns_client = client is None
        self._path = path
        self._method = method
        self._build_request = build_request or _default_request_spec
        self._remote_name = remote_name or f"http:{method} {path}"

    async def aclose(self) -> None:
        """Close the underlying httpx client — only if this instance created it."""
        if self._owns_client:
            await self._client.aclose()

    async def _call(self, value: Any, context: RequestContext | None) -> Any:
        spec = self._build_request(value)
        # Path params are substituted into a URL template, not free text —
        # an unescaped value (e.g. containing "/" or "..") would otherwise
        # change the request path FastAPI's own router sees, not just the
        # value inside it.
        path_params = {k: quote(str(v), safe="") for k, v in spec.path_params.items()}
        url = self._path.format(**path_params) if path_params else self._path

        headers: dict[str, str] = {}
        propagate.inject(headers)  # outgoing traceparent — joins the inbound trace, if any is active
        if context is not None:
            headers["X-Correlation-ID"] = context.correlation_id

        try:
            response = await self._client.request(
                self._method, url, params=spec.query_params or None, json=spec.json_body, headers=headers
            )
        except httpx.TimeoutException as exc:
            from openframe.core.exceptions import AdapterTimeoutError

            raise AdapterTimeoutError(
                f"Timed out calling {self._remote_name}", adapter=self._remote_name, operation="call", cause=exc
            ) from exc
        except httpx.TransportError as exc:
            # Catches everything else httpx raises for a failed exchange —
            # ConnectError (already handled before this was broadened),
            # ReadError, WriteError, RemoteProtocolError, ProxyError — all
            # of which previously escaped as a raw httpx exception instead
            # of the AdapterError every caller is told to expect.
            from openframe.core.exceptions import AdapterConnectionError

            raise AdapterConnectionError(
                f"Could not connect for {self._remote_name}", adapter=self._remote_name, operation="call", cause=exc
            ) from exc

        if response.status_code < 400:
            if response.status_code == 204 or not response.content:
                return None
            return response.json()

        category = _STATUS_TO_CATEGORY.get(response.status_code, ErrorCategory.INTERNAL)
        try:
            body = response.json() if response.content else {}
        except (json.JSONDecodeError, ValueError):
            # A proxy's own error page (HTML 502, plain-text 429) isn't
            # this service's envelope — fall back to an empty body rather
            # than letting a JSONDecodeError mask the real HTTP failure.
            body = {}
        error = body.get("error", body) if isinstance(body, dict) else {}
        envelope = ErrorEnvelope(
            code=error.get("code", "unknown"),
            message=error.get("message", f"{self._remote_name} failed with status {response.status_code}"),
            retryable=error.get("retryable", False),
            correlation_id=error.get("correlation_id", ""),
        )
        raise_for_envelope(category, envelope, remote_name=self._remote_name)


class HttpRemoteQueryClient(_HttpRemoteCallMixin, Generic[TInput, TOutput]):
    """
    `RemoteQueryClient` over HTTP. Satisfies
    `openframe.core.remote.RemoteQueryClient` structurally.

    Args: see `_HttpRemoteCallMixin.__init__` — `method` defaults
    naturally to `"GET"` for queries (pass explicitly if a query uses a
    different verb, though that would be unusual).
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        path: str,
        method: str = "GET",
        client: httpx.AsyncClient | None = None,
        build_request: Callable[[TInput], RequestSpec] | None = None,
        remote_name: str | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url, path=path, method=method, client=client,
            build_request=build_request, remote_name=remote_name,
        )

    async def execute(self, query: TInput, context: RequestContext | None = None) -> TOutput:
        return await self._call(query, context)


class HttpRemoteCommandClient(_HttpRemoteCallMixin, Generic[TInput]):
    """
    `RemoteCommandClient` over HTTP. Satisfies
    `openframe.core.remote.RemoteCommandClient` structurally.

    `method` has no default here (unlike the query client) — a command
    is meaningfully POST, PUT, PATCH, or DELETE, and none of those is
    clearly "the" default the way GET is for queries.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        path: str,
        method: str,
        client: httpx.AsyncClient | None = None,
        build_request: Callable[[TInput], RequestSpec] | None = None,
        remote_name: str | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url, path=path, method=method, client=client,
            build_request=build_request, remote_name=remote_name,
        )

    async def execute(self, command: TInput, context: RequestContext | None = None) -> None:
        await self._call(command, context)
