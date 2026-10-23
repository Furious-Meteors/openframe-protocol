"""
Real FastAPI app, real `httpx.AsyncClient` over `ASGITransport` (same
pattern as `-http`'s own `tests/test_conformance.py`), bound to a fake
in-memory `QueryHandler` — exercises `bind_hls_delivery`/
`bind_dash_delivery`/`bind_media_delivery` end to end, including the
Range-handling paths real video players actually depend on.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from openframe.protocol.media.delivery import (
    bind_dash_delivery,
    bind_hls_delivery,
    bind_media_delivery,
)
from openframe.protocol.media.types import (
    DASH_PROFILE,
    MediaObject,
    MediaObjectKind,
    MediaRequest,
)

MANIFEST_BODY = b'#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-STREAM-INF:BANDWIDTH=1000000\nhd.m3u8\n'
SEGMENT_BODY = bytes(range(256)) * 40  # 10240 bytes


class _FakeHandler:
    """In-memory fake QueryHandler[MediaRequest, MediaObject]."""

    def __init__(self, objects: dict[str, bytes], *, etag: str | None = None, last_modified=None) -> None:
        self.objects = objects
        self.etag = etag
        self.last_modified = last_modified
        self.calls: list[MediaRequest] = []

    async def execute(self, query: MediaRequest, context=None) -> MediaObject:
        self.calls.append(query)
        full = self.objects[query.path]
        total_size = len(full)
        if query.range is None:
            return MediaObject(
                body=full,
                total_size=total_size,
                content_type=None,
                etag=self.etag if query.kind != MediaObjectKind.MANIFEST else None,
                last_modified=self.last_modified,
            )
        from openframe.protocol.media.ranges import (
            UnsatisfiableRangeError,
            resolve_range,
        )

        try:
            start, end = resolve_range(query.range, total_size)
        except UnsatisfiableRangeError:
            # A real handler need not pre-validate satisfiability itself —
            # it only has to report the real total_size; the binder does
            # its own authoritative resolve_range() after this call
            # returns and turns that into the real 416 (see delivery.py).
            return MediaObject(body=b"", total_size=total_size)
        return MediaObject(
            body=full[start : end + 1],
            total_size=total_size,
            served_range=(start, end),
            etag=self.etag if query.kind != MediaObjectKind.MANIFEST else None,
            last_modified=self.last_modified,
        )


class _MismatchHandler:
    """Returns a served_range that does not match what was requested."""

    async def execute(self, query: MediaRequest, context=None) -> MediaObject:
        full = SEGMENT_BODY
        return MediaObject(body=full[0:10], total_size=len(full), served_range=(0, 9999))


def _build_app(handler) -> FastAPI:
    router = APIRouter()

    def get_handler():
        return handler

    bind_hls_delivery(router, handler_dep=get_handler)
    app = FastAPI()
    app.include_router(router)
    return app


def _build_dash_app(handler) -> FastAPI:
    router = APIRouter()

    def get_handler():
        return handler

    bind_dash_delivery(router, handler_dep=get_handler)
    app = FastAPI()
    app.include_router(router)
    return app


def _client(app: FastAPI) -> AsyncClient:
    # raise_app_exceptions=False: same reasoning as -http's own
    # test_conformance.py — we want the real production behaviour of an
    # unhandled exception becoming a real 500, not a raised traceback in
    # the test itself.
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


@pytest.fixture
def handler() -> _FakeHandler:
    return _FakeHandler(
        {
            "live/playlist.m3u8": MANIFEST_BODY,
            "live/hd/seg-00001.ts": SEGMENT_BODY,
        }
    )


# ---------------------------------------------------------------------------
# 200 full responses
# ---------------------------------------------------------------------------


async def test_full_manifest_response(handler):
    app = _build_app(handler)
    async with _client(app) as client:
        resp = await client.get("/live/playlist.m3u8")
    assert resp.status_code == 200
    assert resp.content == MANIFEST_BODY
    assert resp.headers["content-type"] == "application/vnd.apple.mpegurl"
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.headers["content-length"] == str(len(MANIFEST_BODY))
    assert resp.headers["cache-control"] == "no-cache"
    assert "x-correlation-id" in resp.headers


async def test_full_segment_response(handler):
    app = _build_app(handler)
    async with _client(app) as client:
        resp = await client.get("/live/hd/seg-00001.ts")
    assert resp.status_code == 200
    assert resp.content == SEGMENT_BODY
    assert resp.headers["content-type"] == "video/mp2t"
    assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"


# ---------------------------------------------------------------------------
# 206 partial responses — all three Range forms
# ---------------------------------------------------------------------------


async def test_partial_response_start_end(handler):
    app = _build_app(handler)
    async with _client(app) as client:
        resp = await client.get("/live/hd/seg-00001.ts", headers={"Range": "bytes=0-99"})
    assert resp.status_code == 206
    assert resp.content == SEGMENT_BODY[0:100]
    assert resp.headers["content-range"] == f"bytes 0-99/{len(SEGMENT_BODY)}"
    assert resp.headers["content-length"] == "100"
    assert resp.headers["accept-ranges"] == "bytes"


async def test_partial_response_open_ended(handler):
    app = _build_app(handler)
    total = len(SEGMENT_BODY)
    async with _client(app) as client:
        resp = await client.get("/live/hd/seg-00001.ts", headers={"Range": f"bytes={total - 100}-"})
    assert resp.status_code == 206
    assert resp.content == SEGMENT_BODY[total - 100 :]
    assert resp.headers["content-range"] == f"bytes {total - 100}-{total - 1}/{total}"
    assert resp.headers["content-length"] == "100"


async def test_partial_response_suffix(handler):
    app = _build_app(handler)
    total = len(SEGMENT_BODY)
    async with _client(app) as client:
        resp = await client.get("/live/hd/seg-00001.ts", headers={"Range": "bytes=-100"})
    assert resp.status_code == 206
    assert resp.content == SEGMENT_BODY[total - 100 :]
    assert resp.headers["content-range"] == f"bytes {total - 100}-{total - 1}/{total}"
    assert resp.headers["content-length"] == "100"


# ---------------------------------------------------------------------------
# 416 — the regression test for the -http HTTPException-headers bug
# ---------------------------------------------------------------------------


async def test_unsatisfiable_range_returns_416_with_content_range(handler):
    app = _build_app(handler)
    total = len(SEGMENT_BODY)
    async with _client(app) as client:
        resp = await client.get("/live/hd/seg-00001.ts", headers={"Range": f"bytes={total + 100}-"})
    assert resp.status_code == 416
    # This is the whole point: a plain HTTPException(416, headers=...)
    # would have silently dropped Content-Range (see delivery.py's own
    # docstring on the real -http bug this routes around).
    assert resp.headers["content-range"] == f"bytes */{total}"


async def test_zero_length_suffix_returns_416_before_handler_call(handler):
    app = _build_app(handler)
    async with _client(app) as client:
        resp = await client.get("/live/hd/seg-00001.ts", headers={"Range": "bytes=-0"})
    assert resp.status_code == 416
    # Caught at the pre-handler sentinel-based parse stage (see
    # delivery.py's docstring) since it's total_size-independent.
    assert handler.calls == []


# ---------------------------------------------------------------------------
# HEAD
# ---------------------------------------------------------------------------


async def test_head_request_has_headers_but_no_body(handler):
    app = _build_app(handler)
    async with _client(app) as client:
        resp = await client.head("/live/hd/seg-00001.ts")
    assert resp.status_code == 200
    assert resp.content == b""
    assert resp.headers["content-length"] == str(len(SEGMENT_BODY))
    assert resp.headers["accept-ranges"] == "bytes"


async def test_head_request_with_range(handler):
    app = _build_app(handler)
    async with _client(app) as client:
        resp = await client.head("/live/hd/seg-00001.ts", headers={"Range": "bytes=0-99"})
    assert resp.status_code == 206
    assert resp.content == b""
    assert resp.headers["content-length"] == "100"
    assert resp.headers["content-range"] == f"bytes 0-99/{len(SEGMENT_BODY)}"


# ---------------------------------------------------------------------------
# Unknown suffix -> 404 without calling the handler
# ---------------------------------------------------------------------------


async def test_unknown_suffix_404s_without_calling_handler(handler):
    app = _build_app(handler)
    async with _client(app) as client:
        resp = await client.get("/live/hd/seg-00001.exe")
    assert resp.status_code == 404
    assert handler.calls == []


# ---------------------------------------------------------------------------
# Cache-Control per kind
# ---------------------------------------------------------------------------


async def test_manifest_is_no_cache_segment_is_immutable(handler):
    app = _build_app(handler)
    async with _client(app) as client:
        manifest_resp = await client.get("/live/playlist.m3u8")
        segment_resp = await client.get("/live/hd/seg-00001.ts")
    assert manifest_resp.headers["cache-control"] == "no-cache"
    assert segment_resp.headers["cache-control"] == "public, max-age=31536000, immutable"


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


async def test_cors_headers_on_success(handler):
    app = _build_app(handler)
    async with _client(app) as client:
        resp = await client.get("/live/playlist.m3u8")
    assert resp.headers["access-control-allow-origin"] == "*"
    assert "Range" in resp.headers["access-control-allow-headers"]
    assert "Content-Range" in resp.headers["access-control-expose-headers"]


async def test_cors_headers_on_404(handler):
    app = _build_app(handler)
    async with _client(app) as client:
        resp = await client.get("/no/such/file.exe")
    assert resp.status_code == 404
    assert resp.headers["access-control-allow-origin"] == "*"


async def test_cors_headers_on_options_preflight(handler):
    app = _build_app(handler)
    async with _client(app) as client:
        resp = await client.options("/live/playlist.m3u8")
    assert resp.status_code == 204
    assert resp.headers["access-control-allow-origin"] == "*"
    assert resp.headers["access-control-allow-methods"]
    assert resp.headers["access-control-max-age"] == "86400"


# ---------------------------------------------------------------------------
# Handler-reported range mismatch -> 500
# ---------------------------------------------------------------------------


async def test_served_range_mismatch_is_a_500():
    app = _build_app(_MismatchHandler())
    async with _client(app) as client:
        resp = await client.get("/live/hd/seg-00001.ts", headers={"Range": "bytes=0-99"})
    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# ETag / Last-Modified pass-through
# ---------------------------------------------------------------------------


async def test_etag_and_last_modified_pass_through_for_segments_not_manifests():
    when = datetime(2024, 1, 1, tzinfo=UTC)
    h = _FakeHandler(
        {"live/playlist.m3u8": MANIFEST_BODY, "live/hd/seg-00001.ts": SEGMENT_BODY},
        etag='"abc123"',
        last_modified=when,
    )
    app = _build_app(h)
    async with _client(app) as client:
        manifest_resp = await client.get("/live/playlist.m3u8")
        segment_resp = await client.get("/live/hd/seg-00001.ts")
    assert "etag" not in manifest_resp.headers
    assert segment_resp.headers["etag"] == '"abc123"'
    assert "last-modified" in segment_resp.headers


# ---------------------------------------------------------------------------
# DASH profile + generic bind_media_delivery
# ---------------------------------------------------------------------------


async def test_dash_manifest():
    h = _FakeHandler({"live/stream.mpd": b"<MPD></MPD>"})
    app = _build_dash_app(h)
    async with _client(app) as client:
        resp = await client.get("/live/stream.mpd")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/dash+xml"


async def test_bind_media_delivery_with_explicit_profile():
    router = APIRouter()
    h = _FakeHandler({"live/stream.mpd": b"<MPD></MPD>"})
    bind_media_delivery(router, handler_dep=lambda: h, profile=DASH_PROFILE)
    app = FastAPI()
    app.include_router(router)
    async with _client(app) as client:
        resp = await client.get("/live/stream.mpd")
    assert resp.status_code == 200
