"""
openframe/protocol/media/delivery.py
========================================
`bind_media_delivery`/`bind_hls_delivery`/`bind_dash_delivery` — register
a FastAPI route that serves already-produced HLS/DASH manifest and
segment files over HTTP with Range support, by calling into a
`QueryHandler` (`openframe.core.inbound`) for each request.

**Scope.** This module serves bytes that already exist somewhere — a
manifest, an init segment, a media segment, a key, a subtitle file.
Transcoding, segmenting, or packaging that content is explicitly out of
scope (a separate ffmpeg-based adapter the wider platform is building
handles that); this is the read side only, the counterpart to this
package's RTMP write side (`ingest.py`).

**Why a `QueryHandler`, not a new port.** "Give me a thing by identity"
is already exactly what `QueryHandler.execute(query, context)` means —
inventing a `MediaSource` port in this package would duplicate that
shape and, worse, would be an *outbound* port (something a handler calls
to reach storage) defined from inside a *protocol* package, which is the
wrong layer per this ecosystem's own intake rule (ADR-006): outbound
ports belong in `openframe-core`'s `ports/outbound/`, not invented ad hoc
wherever a transport binding happens to need one. Binding `QueryHandler`
here keeps this package's own stated philosophy intact — "protocol never
sees a repository" — because it's the handler, not this module, that
decides whether the bytes come from S3, a local mount, or a cache.

**Why this depends on `-base` + fastapi directly, never on
`openframe-protocol-http`.** The seam between this package and `-http`
is a plain `APIRouter` — a service wires `create_app(..., routers=
(api_router, media_router))` itself and gets `install_error_handlers`
for free with zero package-level coupling. Depending on `-http` instead
would pin this package to `-http`'s own release cadence for two
functions it doesn't even own: `build_request_context` already lives in
`-base` (imported directly from there, below), and `create_app`/
`install_error_handlers` are once-per-process assembly a *service* does,
not something a reusable binding module should reach for. It would also
be flatly wrong on its own terms: the ingest half of this very package
needs no ASGI stack at all, so a package-wide dependency on `-http`
would be dead weight for half of what this package does.

**Why the handler slices the range, not this binder.** `MediaRequest
.range` carries the parsed `Range` header all the way down to the
handler; `MediaObject.served_range`/`total_size` come back up for this
module to turn into `Content-Range`/`Content-Length` and to cross-check.
This is a deliberate memory/performance decision, not a layering
preference: a top-rung ABR segment can be on the order of 15MB, and a
real player seeks constantly, making ranged GETs the *common* case, not
the exception. A binder that always fetched the full object and sliced
it itself would materialize the whole segment in memory on every single
seek. Worse, it would make a genuinely range-aware storage read (`S3
GetObject(Range=...)`, `os.pread()`) unreachable from this package
entirely, since only the handler knows which storage backend — and
therefore which native ranged read — is actually in play. See
`types.py`'s own docstrings for the full reasoning.

**Why total_size is learned via a sentinel, not a second handler call.**
`ranges.parse_range_header` takes a `total_size` to validate
satisfiability, but this binder doesn't know the real `total_size` until
*after* the handler responds — and calling the handler twice (once to
learn the size, once for the real ranged body) would mean either
re-fetching the whole object twice or inventing a "just tell me the
size" half-request this package's `MediaRequest`/`MediaObject` contract
doesn't express, both worse than the problem being solved. Instead, the
Range header is first parsed against an effectively-unbounded sentinel
size (`_UNBOUNDED_SIZE`) purely to validate *syntax* and to catch the
one satisfiability check that genuinely doesn't depend on the real size
(a zero-length suffix range, `bytes=-0`, which is never satisfiable
regardless of the object's actual length) — this can only ever produce a
false negative (missing an early 416), never a false positive, since no
real object is anywhere close to `2**62` bytes. The *real* bounds check
against the handler's own `total_size` happens after the handler
responds, via `ranges.resolve_range`, which is where a genuine
out-of-bounds Range (`bytes=9999-` against a 100-byte object) actually
turns into a 416.

**Why a 416 is a raw `Response`, never `HTTPException`.** Read directly
from `openframe.protocol.http.server._handle_http_exception` (the
installed source, not a guess): it builds a `JSONResponse` from a raised
`HTTPException` without passing `headers=exc.headers`, silently dropping
any headers attached to the exception. A 416 response is required by RFC
9110 §14.4 to carry `Content-Range: bytes */<total_size>` — raising
`HTTPException(416, headers=...)` would, if this module's error path
ever ran through that handler (it structurally can't, since this package
never depends on `-http` — see above), lose exactly the one header a 416
exists to carry. Every error path in this module therefore builds and
returns a real `Response` object directly, never `raise`s an
`HTTPException`, specifically so this class of bug is unreachable here
rather than merely avoided by not wiring `-http`'s handlers. That `-http`
bug itself is a separate, future drive-by fix — not part of this
module's job.

**Why unknown suffixes 404 before the handler ever runs.** Guessing a
content type for an unrecognised extension risks serving a manifest as
`application/octet-stream` or a key file as a manifest — silently wrong
in a way a client has no way to detect. `profile.suffixes` is matched
against the request path up front; no match means 404 immediately, and
the handler — which might otherwise happily resolve the path against
real storage — is never even asked.

Dependency order:
    protocol/media/delivery → protocol/media/ranges, protocol/media/types,
    protocol/base/context, fastapi
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from openframe.protocol.base.context import ContextEnricher, build_request_context
from openframe.protocol.media.ranges import (
    UnsatisfiableRangeError,
    parse_range_header,
    resolve_range,
)
from openframe.protocol.media.types import (
    DASH_PROFILE,
    DEFAULT_CACHE,
    DEFAULT_CORS,
    HLS_PROFILE,
    MediaCachePolicy,
    MediaCorsPolicy,
    MediaObject,
    MediaObjectKind,
    MediaProfile,
    MediaRequest,
)

__all__ = ["bind_dash_delivery", "bind_hls_delivery", "bind_media_delivery"]

_logger = logging.getLogger(__name__)

# See this module's own docstring for the full reasoning: no real media
# object is anywhere close to this size, so using it as a stand-in
# `total_size` when parsing the Range header's syntax (before the real
# size is known) can only ever miss an early 416, never falsely trigger
# one.
_UNBOUNDED_SIZE = 1 << 62

_CACHE_ATTR: dict[MediaObjectKind, str] = {
    MediaObjectKind.MANIFEST: "manifest",
    MediaObjectKind.INIT: "init",
    MediaObjectKind.SEGMENT: "segment",
    MediaObjectKind.KEY: "key",
    MediaObjectKind.SUBTITLE: "subtitle",
}


def _cors_headers(cors: MediaCorsPolicy) -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": cors.allow_origin,
        "Access-Control-Allow-Methods": ", ".join(cors.allow_methods),
        "Access-Control-Allow-Headers": ", ".join(cors.allow_headers),
        "Access-Control-Expose-Headers": ", ".join(cors.expose_headers),
        "Access-Control-Max-Age": str(cors.max_age),
    }


def _suffix_of(media_path: str) -> str:
    """`"live/hd/seg-00042.ts"` -> `".ts"`; `"live/playlist"` -> `""`."""
    basename = media_path.rsplit("/", 1)[-1]
    if "." not in basename:
        return ""
    return "." + basename.rsplit(".", 1)[-1]


def _format_http_date(value: datetime) -> str:
    """RFC 9110 `IMF-fixdate` (`Last-Modified`'s wire format)."""
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return format_datetime(aware, usegmt=True)


def bind_media_delivery(
    router: APIRouter,
    path: str = "/{media_path:path}",
    *,
    handler_dep: Callable[..., Any],
    profile: MediaProfile,
    cache: MediaCachePolicy = DEFAULT_CACHE,
    cors: MediaCorsPolicy = DEFAULT_CORS,
    context_enricher: ContextEnricher | None = None,
) -> None:
    """
    Register a GET/HEAD (plus an OPTIONS preflight) route on *router* that
    serves HLS/DASH media objects for the given *profile* by calling into
    a `QueryHandler` resolved via *handler_dep*.

    Args:
        router:      The `APIRouter` to register onto.
        path:        Route path template with exactly one path parameter
                      capturing the full media path, e.g. the default
                      `"/{media_path:path}"` (FastAPI's `:path` converter
                      matches slashes, needed for a nested manifest/segment
                      layout like `live/hd/seg-00042.ts`). This binder
                      reads the single path parameter by position (`next
                      (iter(request.path_params.values()))`), not by the
                      name `media_path`, so a caller can mount this under
                      a differently-named parameter without this module
                      needing to know its name.
        handler_dep: A FastAPI dependency (plain callable or
                      `Depends`-wrapped) resolving to a `QueryHandler
                      [MediaRequest, MediaObject]` instance for this
                      request.
        profile:      `HLS_PROFILE`/`DASH_PROFILE` (or a custom
                      `MediaProfile`) — maps the request path's suffix to
                      a `MediaObjectKind` and default content type.
                      Suffixes not in `profile.suffixes` 404 before the
                      handler is ever called.
        cache:        Per-kind `Cache-Control` values. Defaults to
                      `DEFAULT_CACHE` (manifests `no-cache`, everything
                      else long-lived/immutable — see `types.py`).
        cors:         CORS header values applied to every response this
                      binder produces, success or error, plus the OPTIONS
                      preflight route. Defaults to `DEFAULT_CORS`.
        context_enricher: Passed straight through to
                      `build_request_context` — the hook a service uses to
                      attach `principal`/`tenant`, same as every other
                      binder in this ecosystem.

    Response shape (see this module's own docstring for the design behind
    each of these):

    - Unknown suffix: `404`, no handler call.
    - No Range header, or a Range this server chooses to ignore per RFC
      9110 §14.2 (wrong unit, multi-range, malformed): `200` with
      `Accept-Ranges: bytes` and `Content-Length`.
    - A satisfiable single-range request: `206` with `Accept-Ranges:
      bytes`, `Content-Range: bytes {start}-{end}/{total}`, and
      `Content-Length` set to the range's width.
    - An unsatisfiable range: `416` with `Content-Range: bytes
      */{total}` — returned as a raw `Response`, never via
      `HTTPException` (see this module's docstring for why).
    - `HEAD`: identical status and headers to the `GET` that would have
      produced them, with no body — this binder strips the body itself
      rather than relying on FastAPI/Starlette to do it, so the contract
      holds regardless of ASGI-server-level behaviour (confirmed by this
      package's own test suite either way).
    - A handler-returned `served_range`/body width that doesn't match
      the resolved request range raises `ValueError`, which surfaces as a
      `500` — correctly: this is a storage-layer bug (the handler lied
      about what it served), not a client error.

    `ETag`/`Last-Modified` are included only when the handler actually
    sets them on the returned `MediaObject`; this binder never invents
    either.
    """
    cors_headers = _cors_headers(cors)

    async def endpoint(request: Request, handler: Any = Depends(handler_dep)) -> Response:
        media_path = next(iter(request.path_params.values()), "")
        context = build_request_context(dict(request.headers), context_enricher=context_enricher)
        headers: dict[str, str] = dict(cors_headers)
        headers["X-Correlation-ID"] = context.correlation_id

        suffix = _suffix_of(media_path)
        matched = profile.suffixes.get(suffix)
        if matched is None:
            return Response(status_code=404, headers=headers)
        kind, default_content_type = matched

        try:
            # See module docstring: _UNBOUNDED_SIZE stands in for the real
            # total_size, which isn't known until the handler responds.
            # Sufficient to validate syntax and the one satisfiability
            # check that doesn't depend on the real size (a zero-length
            # suffix range).
            parsed_range = parse_range_header(request.headers.get("range"), _UNBOUNDED_SIZE)
        except UnsatisfiableRangeError:
            return Response(status_code=416, headers=headers)

        media_request = MediaRequest(
            path=media_path, kind=kind, content_type=default_content_type, range=parsed_range
        )
        result: MediaObject = await handler.execute(media_request, context)

        headers["Content-Type"] = result.content_type or default_content_type
        headers["Accept-Ranges"] = "bytes"
        headers["Cache-Control"] = getattr(cache, _CACHE_ATTR[kind])
        if result.etag is not None:
            headers["ETag"] = result.etag
        if result.last_modified is not None:
            headers["Last-Modified"] = _format_http_date(result.last_modified)

        status_code = 200
        if parsed_range is not None:
            if result.total_size is None:
                raise ValueError(
                    f"MediaObject.total_size is required whenever a Range was requested "
                    f"(path={media_path!r}) — the handler must report the full object's size."
                )
            try:
                start, end = resolve_range(parsed_range, result.total_size)
            except UnsatisfiableRangeError:
                headers["Content-Range"] = f"bytes */{result.total_size}"
                # Returned directly, never raised as HTTPException — see
                # module docstring for the -http bug this routes around.
                return Response(status_code=416, headers=headers)

            width = end - start + 1
            if result.served_range is not None and result.served_range != (start, end):
                raise ValueError(
                    f"handler-reported served_range {result.served_range} does not match the "
                    f"resolved request range ({start}, {end}) for path={media_path!r} — "
                    "this is a storage-layer bug, not a client error."
                )
            if isinstance(result.body, (bytes, bytearray)) and len(result.body) != width:
                # An AsyncIterator[bytes] body's total length isn't known
                # upfront without consuming it, which would defeat the
                # point of streaming it — only a concrete bytes body can
                # be validated here.
                raise ValueError(
                    f"handler returned a body of {len(result.body)} bytes but the resolved "
                    f"range ({start}, {end}) is {width} bytes wide for path={media_path!r}."
                )

            headers["Content-Range"] = f"bytes {start}-{end}/{result.total_size}"
            headers["Content-Length"] = str(width)
            status_code = 206
        else:
            if result.total_size is not None:
                headers["Content-Length"] = str(result.total_size)
            elif isinstance(result.body, (bytes, bytearray)):
                headers["Content-Length"] = str(len(result.body))

        if request.method == "HEAD":
            return Response(status_code=status_code, headers=headers)

        if isinstance(result.body, (bytes, bytearray)):
            return Response(content=bytes(result.body), status_code=status_code, headers=headers)
        return StreamingResponse(result.body, status_code=status_code, headers=headers)

    async def preflight(request: Request) -> Response:
        return Response(status_code=204, headers=cors_headers)

    router.add_api_route(path, endpoint, methods=["GET", "HEAD"], response_class=Response)
    router.add_api_route(path, preflight, methods=["OPTIONS"], response_class=Response)


def bind_hls_delivery(
    router: APIRouter,
    path: str = "/{media_path:path}",
    *,
    handler_dep: Callable[..., Any],
    cache: MediaCachePolicy = DEFAULT_CACHE,
    cors: MediaCorsPolicy = DEFAULT_CORS,
    context_enricher: ContextEnricher | None = None,
) -> None:
    """`bind_media_delivery(..., profile=HLS_PROFILE)` — see its docstring for the full contract."""
    bind_media_delivery(
        router,
        path,
        handler_dep=handler_dep,
        profile=HLS_PROFILE,
        cache=cache,
        cors=cors,
        context_enricher=context_enricher,
    )


def bind_dash_delivery(
    router: APIRouter,
    path: str = "/{media_path:path}",
    *,
    handler_dep: Callable[..., Any],
    cache: MediaCachePolicy = DEFAULT_CACHE,
    cors: MediaCorsPolicy = DEFAULT_CORS,
    context_enricher: ContextEnricher | None = None,
) -> None:
    """`bind_media_delivery(..., profile=DASH_PROFILE)` — see its docstring for the full contract."""
    bind_media_delivery(
        router,
        path,
        handler_dep=handler_dep,
        profile=DASH_PROFILE,
        cache=cache,
        cors=cors,
        context_enricher=context_enricher,
    )
