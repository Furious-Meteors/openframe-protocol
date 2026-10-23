"""
Runs SessionConformanceTests (openframe-protocol-base) against the real
`pull_dash` binding -- the DASH counterpart to
`test_hls_pull_conformance.py`, same pattern exactly.

Driven against a real local HTTP server built from this package's own
`bind_dash_delivery` (serving hand-written static MPD/segment fixtures
this test controls directly, not real ffmpeg timing -- that belongs to
`test_dash_pull_spike.py`), reused here exactly as the task asked: a real
origin for `pull_dash` to poll, rather than a hand-rolled server
duplicating what `bind_dash_delivery` already does well.

`abrupt_disconnect` has no clean real-world trigger for a self-initiated
outbound poller -- same reasoning as `test_hls_pull_conformance.py`'s own
(see `dash_pull.py`'s own module docstring: this transport never uses
`CloseReason.CLIENT_DISCONNECT` at all, by design, mirroring
`hls_pull.py`'s own finding 7). `xfail`'d here with that reason, the same
honest-not-papered-over treatment.
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
from openframe.protocol.media import bind_dash_delivery, pull_dash
from uvicorn import Config, Server


class _StaticHandler:
    """A QueryHandler[MediaRequest, MediaObject] serving fixed in-memory objects -- bind_dash_delivery's own backing handler for this test's real origin server."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    async def execute(self, query, context=None):
        from openframe.protocol.media.types import MediaObject

        body = self.objects[query.path]
        return MediaObject(body=body, total_size=len(body))


def _mpd(*, mpd_type: str, start_number: int, timeline_s: str) -> bytes:
    dynamic_attrs = (
        'minimumUpdatePeriod="PT2S" suggestedPresentationDelay="PT2S" '
        'availabilityStartTime="2026-10-19T00:00:00.000Z" timeShiftBufferDepth="PT10.0S" '
        if mpd_type == "dynamic" else 'mediaPresentationDuration="PT10.0S" '
    )
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" profiles="urn:mpeg:dash:profile:isoff-live:2011"
     type="{mpd_type}" {dynamic_attrs}minBufferTime="PT4.0S">
  <Period id="0" start="PT0.0S">
    <AdaptationSet id="0" contentType="video">
      <Representation id="0" mimeType="video/mp4" codecs="avc1" bandwidth="100000">
        <SegmentTemplate timescale="1" startNumber="{start_number}" media="seg-$Number%03d$.m4s">
          <SegmentTimeline>
            {timeline_s}
          </SegmentTimeline>
        </SegmentTemplate>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>"""
    return xml.encode()


class _RealOriginServer:
    """A real uvicorn server running bind_dash_delivery over a real `_StaticHandler` -- the real HTTP origin pull_dash polls against in this suite."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        router = APIRouter()
        handler = _StaticHandler(objects)
        bind_dash_delivery(router, handler_dep=lambda: handler)
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


class _DashPullSessionDriver:
    async def open_accepts(self) -> SessionResult:
        objects = {
            "manifest.mpd": _mpd(mpd_type="static", start_number=0, timeline_s='<S t="0" d="1" />'),
            "seg-000.m4s": b"data",
        }
        handler = _AcceptingHandler()
        async with (
            _RealOriginServer(objects) as origin,
            pull_dash(origin.url("manifest.mpd"), handler_factory=lambda: handler, poll_interval=0.05),
        ):
            await _wait_closed(handler)
        return SessionResult(
            opened=True, close_reason=handler.closed_with, received_events=(), correlation_ids=()
        )

    async def open_rejects(self) -> SessionResult:
        objects = {
            "manifest.mpd": _mpd(mpd_type="static", start_number=0, timeline_s='<S t="0" d="1" />'),
            "seg-000.m4s": b"data",
        }
        exc = AdapterTimeoutError("too slow", adapter="x", operation="connect")
        handler = _RejectingHandler(exc)
        async with _RealOriginServer(objects) as origin:
            opened = True
            try:
                async with pull_dash(
                    origin.url("manifest.mpd"), handler_factory=lambda: handler, poll_interval=0.05
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
        # A real MPD -- real segments, fetched in real sequence order from a real HTTP origin.
        objects = {
            "manifest.mpd": _mpd(mpd_type="static", start_number=1, timeline_s='<S t="0" d="1" r="2" />'),
            "seg-001.m4s": b"event-1", "seg-002.m4s": b"event-2", "seg-003.m4s": b"event-3",
        }
        handler = _AcceptingHandler()
        async with (
            _RealOriginServer(objects) as origin,
            pull_dash(origin.url("manifest.mpd"), handler_factory=lambda: handler, poll_interval=0.05),
        ):
            await _wait_closed(handler)
        received = tuple(segment.payload.decode() for segment in handler.events)
        return SessionResult(
            opened=True, close_reason=handler.closed_with, received_events=received, correlation_ids=()
        )

    async def abrupt_disconnect(self) -> SessionResult:  # pragma: no cover -- xfail'd, see module docstring
        raise NotImplementedError(
            "pull_dash is a self-initiated outbound poller -- there is no remote peer to "
            "disconnect from, so this scenario has no real-world trigger (see dash_pull.py's "
            "own module docstring: CLIENT_DISCONNECT is never used by this transport)"
        )

    async def correlation_id_stable(self) -> SessionResult:
        objects = {
            "manifest.mpd": _mpd(mpd_type="static", start_number=1, timeline_s='<S t="0" d="1" r="1" />'),
            "seg-001.m4s": b"x", "seg-002.m4s": b"y",
        }
        handler = _CapturingHandler()
        async with (
            _RealOriginServer(objects) as origin,
            pull_dash(origin.url("manifest.mpd"), handler_factory=lambda: handler, poll_interval=0.05),
        ):
            await _wait_closed(handler)
        return SessionResult(
            opened=True,
            close_reason=handler.closed_with,
            received_events=tuple(handler.events),
            correlation_ids=(handler.correlation_id,) * len(handler.events),
        )


class TestDashPullSessionConformance(SessionConformanceTests):
    @pytest.fixture
    def driver(self) -> _DashPullSessionDriver:
        return _DashPullSessionDriver()

    @pytest.mark.xfail(
        reason=(
            "pull_dash is a self-initiated outbound poller with no remote peer to disconnect "
            "from -- CloseReason.CLIENT_DISCONNECT is never used by this transport by design "
            "(see dash_pull.py's own module docstring), so this scenario has no real-world "
            "trigger to drive"
        ),
        strict=False,
    )
    async def test_abrupt_disconnect_closes_with_client_disconnect(self, driver: SessionDriver) -> None:
        await super().test_abrupt_disconnect_closes_with_client_disconnect(driver)


def test_driver_satisfies_session_driver_protocol() -> None:
    assert isinstance(_DashPullSessionDriver(), SessionDriver)
