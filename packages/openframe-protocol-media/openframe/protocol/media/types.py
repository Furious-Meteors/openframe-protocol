"""
openframe/protocol/media/types.py
=====================================
Pure-stdlib vocabulary shared by `ranges.py` and `delivery.py` — no
`fastapi`, no `pyrtmp`, nothing beyond the standard library. This is
deliberate: a service's own handler module (the `QueryHandler`
implementation that actually knows whether a manifest lives on S3, a
local mount, or a cache) needs to be able to write

    def execute(self, query: MediaRequest, context=None) -> MediaObject: ...

without installing the `[delivery]` extra at all. If `MediaRequest`/
`MediaObject` lived in `delivery.py` next to the FastAPI binder, a plain
handler module would drag `fastapi` in transitively just to spell its own
type annotations — exactly the coupling the ingest half of this package
already avoids by keeping `flow.py` free of `pyrtmp`.

Dependency order:
    protocol/media/types → (stdlib only)
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

__all__ = [
    "DASH_PROFILE",
    "DEFAULT_CACHE",
    "DEFAULT_CORS",
    "HLS_PROFILE",
    "MediaCachePolicy",
    "MediaCorsPolicy",
    "MediaObject",
    "MediaObjectKind",
    "MediaProfile",
    "MediaRange",
    "MediaRequest",
]


class MediaObjectKind(StrEnum):
    """
    What kind of file is being requested, independent of container format
    (HLS vs. DASH) — drives both the default `Cache-Control` policy
    (`MediaCachePolicy`) and, indirectly, suffix-to-content-type mapping
    (`MediaProfile.suffixes`) in one place instead of scattering
    "is this a manifest" checks across the binder.
    """

    MANIFEST = "manifest"
    INIT = "init"
    SEGMENT = "segment"
    KEY = "key"
    SUBTITLE = "subtitle"


@dataclass(frozen=True, slots=True)
class MediaRange:
    """
    A parsed (but not yet resolved against a real size) HTTP byte range —
    the three forms RFC 9110 §14.1.2 defines for a single `bytes=` range:

    - `bytes=0-499`   -> `start=0, end=499` (both inclusive)
    - `bytes=500-`    -> `start=500, end=None` ("to the end", end unknown
                          until `total_size` is known)
    - `bytes=-500`    -> `suffix_length=500` ("the last 500 bytes" —
                          `start`/`end` are both unset; which absolute
                          offsets this means depends on `total_size`,
                          which this type deliberately does not carry)

    `start`/`end` and `suffix_length` are mutually exclusive — see
    `ranges.parse_range_header`, the only place that constructs one of
    these from a raw header value.
    """

    start: int | None = None
    end: int | None = None
    suffix_length: int | None = None


@dataclass(frozen=True, slots=True)
class MediaRequest:
    """
    What the binder asks a `QueryHandler` for, by identity: `path` plus
    which byte range (if any) the client wants.

    `range` is carried all the way down to the handler, not sliced by the
    binder after fetching the whole object, for a real memory/performance
    reason: a top-rung ABR segment can be on the order of 15MB, and a
    video player seeks constantly — ranged GETs are the *common* case,
    not an edge case. If the binder always fetched the full object and
    sliced it itself, every single ranged request would materialize the
    whole segment in memory first, discarding most of it. Worse, a
    full-object-only interface makes genuinely range-aware storage reads
    (`S3 GetObject(Range=...)`, `os.pread()`) unreachable from this
    package entirely — the handler is the only layer that knows which
    storage backend it's talking to, so it's the only layer that can
    actually exploit a native ranged read. Passing `range` down keeps
    that door open; the binder only ever does HTTP header arithmetic.
    """

    path: str
    kind: MediaObjectKind
    content_type: str
    range: MediaRange | None = None


@dataclass(frozen=True, slots=True)
class MediaObject:
    """
    What a `QueryHandler` hands back for one `MediaRequest`.

    `total_size`/`served_range` are the other half of the "range goes
    down, size comes back up" split described on `MediaRequest`: the
    handler is the only party that knows the full object's size (it just
    read it, or statted it, or has it cached), and it's the only party
    that knows exactly which bytes it actually served — so both travel
    back up for the binder to turn into `Content-Length`/`Content-Range`
    and to cross-check against what was asked for (see `delivery.py`'s
    own width-mismatch check). `total_size` is required whenever `range`
    was set on the originating `MediaRequest` — there is no way to build
    a correct `Content-Range: bytes start-end/total` header without it.

    `body` may be a plain `bytes` (the common case for a manifest, which
    is always small) or an `AsyncIterator[bytes]` (for a large segment a
    handler wants to stream rather than buffer whole) — `delivery.py`
    only validates `served_range`'s width against the real body length
    when `body` is `bytes`; an iterator's total length isn't known
    upfront without consuming it, which would defeat the point of
    streaming it in the first place.

    `content_type` is optional and, when `None`, the binder falls back to
    `MediaRequest.content_type` (the profile's own suffix-derived
    default) — a handler only needs to override this for something a
    static suffix mapping can't express (e.g. a manifest whose variant
    encoding differs per request).

    `etag`/`last_modified` are passed through only when the handler
    actually sets them, never synthesized by the binder. A segment or
    init file — immutable once named — is a natural fit for both; a live
    manifest typically is not, since serving a stale cached copy at the
    live edge is worse than forcing a revalidation (or, more simply,
    just skipping validators on it altogether so a client never asks).
    This is a per-object judgment call only the handler is positioned to
    make, so the binder never invents a validator on the handler's
    behalf.
    """

    body: bytes | AsyncIterator[bytes]
    total_size: int | None = None
    served_range: tuple[int, int] | None = None
    content_type: str | None = None
    etag: str | None = None
    last_modified: datetime | None = None


@dataclass(frozen=True, slots=True)
class MediaProfile:
    """
    Maps a file suffix to its `MediaObjectKind` and default content type
    for one packaging format (HLS or DASH). The binder looks a request
    path's suffix up here *before* ever calling the handler — an unknown
    suffix is a 404 up front, never a guess (see `delivery.py`).
    """

    name: str
    suffixes: Mapping[str, tuple[MediaObjectKind, str]]


HLS_PROFILE = MediaProfile(
    name="hls",
    suffixes={
        ".m3u8": (MediaObjectKind.MANIFEST, "application/vnd.apple.mpegurl"),
        ".ts": (MediaObjectKind.SEGMENT, "video/mp2t"),
        ".m4s": (MediaObjectKind.SEGMENT, "video/iso.segment"),
        ".mp4": (MediaObjectKind.INIT, "video/mp4"),
        ".key": (MediaObjectKind.KEY, "application/octet-stream"),
        ".vtt": (MediaObjectKind.SUBTITLE, "text/vtt"),
    },
)

DASH_PROFILE = MediaProfile(
    name="dash",
    suffixes={
        ".mpd": (MediaObjectKind.MANIFEST, "application/dash+xml"),
        ".m4s": (MediaObjectKind.SEGMENT, "video/iso.segment"),
        ".cmfv": (MediaObjectKind.SEGMENT, "video/mp4"),
        ".cmfa": (MediaObjectKind.SEGMENT, "audio/mp4"),
        ".mp4": (MediaObjectKind.INIT, "video/mp4"),
    },
)


@dataclass(frozen=True, slots=True)
class MediaCachePolicy:
    """
    Default `Cache-Control` value per `MediaObjectKind`.

    `manifest` defaults to `no-cache` (meaning: always revalidate, don't
    just trust a local copy) because a live-edge HLS/DASH manifest is
    rewritten every time a new segment is produced — an ABR player that
    cached it for even a few seconds would miss the newest segment and
    stall at the live edge. `init`/`segment` default to a one-year
    `immutable` cache instead, because both are written once under a
    name that never changes — a segment URL encodes its own sequence
    number/timestamp, so "segment 42" today and "segment 42" a year from
    now are byte-for-byte the same object by construction; there is
    nothing to revalidate. `key` defaults to `no-store` (a DRM/encryption
    key has no business sitting in any cache, shared or private). `subtitle`
    gets a short-lived `max-age` — it changes less often than a manifest
    but isn't permanently immutable like a segment.
    """

    manifest: str = "no-cache"
    init: str = "public, max-age=31536000, immutable"
    segment: str = "public, max-age=31536000, immutable"
    key: str = "no-store"
    subtitle: str = "public, max-age=3600"


DEFAULT_CACHE = MediaCachePolicy()


@dataclass(frozen=True, slots=True)
class MediaCorsPolicy:
    """
    CORS headers for cross-origin delivery (a player hosted on a
    different origin than the media host, the common case for a CDN-
    fronted deployment).

    `allow_headers` includes `Range` because it is a *request* header — a
    browser refuses to let JavaScript send a non-"simple" header
    cross-origin unless the server's preflight response explicitly
    allows it, and `Range` is exactly the header every seek does.

    `expose_headers` is a different list for a different reason: by
    default a browser hides every response header from JavaScript except
    a small CORS-safelisted set, so even though the server always *sends*
    `Content-Range`/`Content-Length`/`Accept-Ranges`, `hls.js`/`dash.js`
    can't *read* them back cross-origin unless they're named here. Mixing
    these two lists up (e.g. putting `Range` in `expose_headers`, which
    is a request header a client reads from nowhere) is a common and
    confusing CORS mistake this split is meant to make hard to make.
    """

    allow_origin: str = "*"
    allow_methods: tuple[str, ...] = ("GET", "HEAD", "OPTIONS")
    allow_headers: tuple[str, ...] = ("Range",)
    expose_headers: tuple[str, ...] = ("Content-Length", "Content-Range", "Accept-Ranges")
    max_age: int = 86400


DEFAULT_CORS = MediaCorsPolicy()
