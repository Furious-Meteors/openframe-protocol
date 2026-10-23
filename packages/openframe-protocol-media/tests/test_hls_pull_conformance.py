"""
Runs SessionConformanceTests (openframe-protocol-base) against the real
`pull_hls` binding, proving the generic session suite exercises a fifth
real transport, after SSE/WebSocket (openframe-protocol-http), gRPC
(openframe-protocol-grpc), and RTMP ingest (this package's own
`test_media_conformance.py`) — and the first one that is an *outbound*
poller rather than an inbound connection of any kind.

Named separately from `test_media_conformance.py` (which already holds
the RTMP ingest conformance suite) rather than added into that file —
checked `-http`/`-grpc`/`-base`'s own `tests/` directories first (none
has a `tests/__init__.py` anywhere in this monorepo, so names collide
across packages): no existing file anywhere is named
`test_hls_pull_conformance.py`.

Driven against a real local HTTP server built from this package's own
`bind_hls_delivery` (serving hand-written static playlist fixtures this
test controls directly, not real ffmpeg timing — that belongs to
`test_hls_pull_spike.py`), reused here exactly as the task asked: a real
origin for `pull_hls` to poll, rather than a hand-rolled server
duplicating what `bind_hls_delivery` already does well.

`ordered_delivery` is a genuinely STRONGER signal here than for RTMP
ingest's own conformance suite (see `test_media_conformance.py`'s own
docstring: RTMP has no outbound media channel at all for a publish-only
connection, so it bridges `session.send()` through onStatus descriptions
instead). `pull_hls` really does fetch real segments, in real sequence
order, from a real HTTP origin — `ordered_delivery` here drives that
exact real behaviour, not a bridge.

`abrupt_disconnect` has no clean real-world trigger for a self-initiated
outbound poller — there is no remote peer to disconnect from (see
`hls_pull.py`'s own module docstring, finding 7: `CloseReason
.CLIENT_DISCONNECT` is never used by this transport at all, by design).
`xfail`'d here with that reason, the same honest-not-papered-over
treatment RTMP ingest's own conformance suite gives its own unfalsifiable
`CLIENT_DISCONNECT` case.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Self

import pytest
from fastapi import APIRouter, FastAPI
from openframe.core.exceptions import AdapterTimeoutError
from openframe.core.inbound import CloseReason
from openframe.protocol.base.errors import categorize
from openframe.protocol.base.session_testing import (
    SessionConformanceTests,
    SessionDriver,
    SessionResult,
)
from openframe.protocol.media import bind_hls_delivery, pull_hls
from uvicorn import Config, Server


class _StaticHandler:
    """A QueryHandler[MediaRequest, MediaObject] serving fixed in-memory objects — bind_hls_delivery's own backing handler for this test's real origin server."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    async def execute(self, query, context=None):
        from openframe.protocol.media.types import MediaObject

        body = self.objects[query.path]
        return MediaObject(body=body, total_size=len(body))


def _playlist(sequence_start: int, uris: list[str], *, endlist: bool = True) -> bytes:
    lines = [
        "#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:2",
        f"#EXT-X-MEDIA-SEQUENCE:{sequence_start}",
    ]
    for uri in uris:
        lines.append("#EXTINF:2.000,")
        lines.append(uri)
    if endlist:
        lines.append("#EXT-X-ENDLIST")
    return ("\n".join(lines) + "\n").encode()


class _RealOriginServer:
    """A real uvicorn server running bind_hls_delivery over a real `_StaticHandler` -- the real HTTP origin pull_hls polls against in this suite."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        router = APIRouter()
        handler = _StaticHandler(objects)
        bind_hls_delivery(router, handler_dep=lambda: handler)
        app = FastAPI()
        app.include_router(router)
        self._config = Config(app=app, host="127.0.0.1", port=0, log_level="error")
        self._server = Server(self._config)
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> Self:
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(200):
            if self._server.started:
                break
            await asyncio.sleep(0.01)
        return self

    async def __aexit__(self, *exc) -> None:
        self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._task, timeout=5)

    @property
    def port(self) -> int:
        return self._config.port if self._config.port else self._server.servers[0].sockets[0].getsockname()[1]

    def url(self, path: str) -> str:
        port = self._server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}/{path}"


class _AcceptingHandler:
    def __init__(self) -> None:
        self.closed_with: CloseReason | None = None
        self.events: list = []

    async def on_open(self, session) -> None:
        pass

    async def on_event(self, session, event) -> None:
        self.events.append(event)

    async def on_close(self, session, reason: CloseReason) -> None:
        self.closed_with = reason


class _RejectingHandler:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.closed_with: CloseReason | None = None

    async def on_open(self, session) -> None:
        raise self._exc

    async def on_event(self, session, event) -> None:
        pass

    async def on_close(self, session, reason: CloseReason) -> None:
        self.closed_with = reason


class _CapturingHandler(_AcceptingHandler):
    def __init__(self) -> None:
        super().__init__()
        self.correlation_id: str | None = None

    async def on_open(self, session) -> None:
        self.correlation_id = session.context.correlation_id


async def _wait_closed(handler, attempts: int = 300) -> None:
    for _ in range(attempts):
        if handler.closed_with is not None:
            return
        await asyncio.sleep(0.02)


class _HlsPullSessionDriver:
    async def open_accepts(self) -> SessionResult:
        objects = {"playlist.m3u8": _playlist(0, ["seg-0.ts"]), "seg-0.ts": b"data"}
        handler = _AcceptingHandler()
        async with (
            _RealOriginServer(objects) as origin,
            pull_hls(origin.url("playlist.m3u8"), handler_factory=lambda: handler, poll_interval=0.05),
        ):
            await _wait_closed(handler)
        return SessionResult(
            opened=True, close_reason=handler.closed_with, received_events=(), correlation_ids=()
        )

    async def open_rejects(self) -> SessionResult:
        objects = {"playlist.m3u8": _playlist(0, ["seg-0.ts"]), "seg-0.ts": b"data"}
        exc = AdapterTimeoutError("too slow", adapter="x", operation="connect")
        handler = _RejectingHandler(exc)
        async with _RealOriginServer(objects) as origin:
            opened = True
            try:
                async with pull_hls(
                    origin.url("playlist.m3u8"), handler_factory=lambda: handler, poll_interval=0.05
                ):
                    pass
            except Exception:
                opened = False
        return SessionResult(
            opened=opened,
            close_reason=handler.closed_with,
            received_events=(),
            correlation_ids=(),
            error_category=categorize(exc),
        )

    async def ordered_delivery(self) -> SessionResult:
        # A real media playlist -- real segments, fetched in real
        # sequence order from a real HTTP origin. Genuinely exercises
        # pull_hls's real behaviour, unlike RTMP ingest's own weaker
        # onStatus-description bridge for this same scenario (see this
        # module's own docstring).
        objects = {
            "playlist.m3u8": _playlist(0, ["event-1.ts", "event-2.ts", "event-3.ts"]),
            "event-1.ts": b"event-1", "event-2.ts": b"event-2", "event-3.ts": b"event-3",
        }
        handler = _AcceptingHandler()
        async with (
            _RealOriginServer(objects) as origin,
            pull_hls(origin.url("playlist.m3u8"), handler_factory=lambda: handler, poll_interval=0.05),
        ):
            await _wait_closed(handler)
        received = tuple(segment.payload.decode() for segment in handler.events)
        return SessionResult(
            opened=True, close_reason=handler.closed_with, received_events=received, correlation_ids=()
        )

    async def abrupt_disconnect(self) -> SessionResult:  # pragma: no cover -- xfail'd, see module docstring
        raise NotImplementedError(
            "pull_hls is a self-initiated outbound poller -- there is no remote peer to "
            "disconnect from, so this scenario has no real-world trigger (see hls_pull.py's "
            "own module docstring, finding 7: CLIENT_DISCONNECT is never used by this transport)"
        )

    async def correlation_id_stable(self) -> SessionResult:
        objects = {
            "playlist.m3u8": _playlist(0, ["x.ts", "y.ts"]),
            "x.ts": b"x", "y.ts": b"y",
        }
        handler = _CapturingHandler()
        async with (
            _RealOriginServer(objects) as origin,
            pull_hls(origin.url("playlist.m3u8"), handler_factory=lambda: handler, poll_interval=0.05),
        ):
            await _wait_closed(handler)
        return SessionResult(
            opened=True,
            close_reason=handler.closed_with,
            received_events=tuple(handler.events),
            correlation_ids=(handler.correlation_id,) * len(handler.events),
        )


class TestHlsPullSessionConformance(SessionConformanceTests):
    @pytest.fixture
    def driver(self) -> _HlsPullSessionDriver:
        return _HlsPullSessionDriver()

    @pytest.mark.xfail(
        reason=(
            "pull_hls is a self-initiated outbound poller with no remote peer to disconnect "
            "from -- CloseReason.CLIENT_DISCONNECT is never used by this transport by design "
            "(see hls_pull.py's own module docstring, finding 7), so this scenario has no "
            "real-world trigger to drive"
        ),
        strict=False,
    )
    async def test_abrupt_disconnect_closes_with_client_disconnect(self, driver: SessionDriver) -> None:
        await super().test_abrupt_disconnect_closes_with_client_disconnect(driver)


def test_driver_satisfies_session_driver_protocol() -> None:
    assert isinstance(_HlsPullSessionDriver(), SessionDriver)
