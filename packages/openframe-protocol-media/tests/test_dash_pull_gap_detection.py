"""
Controlled (no real ffmpeg, no real timing dependency) tests of
`pull_dash`'s gap-detection, discontinuity-inference, stale-poll timeout,
and clean-`type="static"` behaviour -- the DASH counterpart to
`test_hls_pull_gap_detection.py`, proving the hardest-to-exercise spike
findings from `dash_pull.py`'s own module docstring (findings 2/5/6/7/8)
deterministically, via a tiny hand-rolled `asyncio` HTTP server serving
hand-written MPD fixtures this test fully controls.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Self

import httpx
import pytest
from openframe.core.inbound import CloseReason
from openframe.protocol.media import pull_dash


class _RecordingHandler:
    def __init__(self) -> None:
        self.segments: list = []
        self.closed_with: CloseReason | None = None

    async def on_open(self, session) -> None:
        pass

    async def on_event(self, session, event) -> None:
        self.segments.append(event)

    async def on_close(self, session, reason: CloseReason) -> None:
        self.closed_with = reason


class _ScriptedManifestServer:
    """
    Serves a scripted sequence of MPD bodies for `GET /manifest.mpd` (one
    body per call, repeating the last one once the script is exhausted)
    and arbitrary segment bytes for any other path, over a real `asyncio`
    TCP server -- real HTTP parsing, real sockets, fully deterministic
    content under this test's own control.
    """

    def __init__(self, manifests: list[bytes]) -> None:
        self._manifests = manifests
        self._call_count = 0
        self._server: asyncio.base_events.Server | None = None
        self.port = 0

    async def __aenter__(self) -> Self:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await reader.readline()
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
            path = request_line.decode().split(" ")[1]

            if path == "/manifest.mpd":
                index = min(self._call_count, len(self._manifests) - 1)
                self._call_count += 1
                body = self._manifests[index]
                content_type = "application/dash+xml"
            else:
                body = f"segment-bytes:{path}".encode()
                content_type = "video/mp4"

            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: " + content_type.encode() + b"\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + body
            )
            writer.write(response)
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/manifest.mpd"


def _mpd(
    *,
    mpd_type: str = "dynamic",
    start_number: int,
    timeline_s: str,
    minimum_update_period: str = "PT2S",
) -> bytes:
    dynamic_attrs = (
        f'minimumUpdatePeriod="{minimum_update_period}" suggestedPresentationDelay="PT2S" '
        f'availabilityStartTime="2026-10-19T00:00:00.000Z" timeShiftBufferDepth="PT10.0S" '
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


# ---------------------------------------------------------------------------
# (a) sequence gap -> next processed segment carries discontinuity=True
# ---------------------------------------------------------------------------


async def test_sequence_gap_marks_next_segment_discontinuous():
    first = _mpd(start_number=0, timeline_s='<S t="0" d="2" r="1" />')  # numbers 0, 1
    # Numbers 2 and 3 never appear anywhere -- a real missed-segment gap.
    second = _mpd(mpd_type="static", start_number=4, timeline_s='<S t="8" d="2" r="1" />')  # numbers 4, 5

    async with _ScriptedManifestServer([first, second]) as server:
        handler = _RecordingHandler()
        async with pull_dash(
            server.url, handler_factory=lambda: handler, poll_interval=0.05, max_stale_polls=50
        ):
            for _ in range(200):
                if handler.closed_with is not None:
                    break
                await asyncio.sleep(0.02)

    assert handler.closed_with is CloseReason.COMPLETED
    sequences = [s.sequence for s in handler.segments]
    assert sequences == [0, 1, 4, 5]
    discontinuities = {s.sequence: s.discontinuity for s in handler.segments}
    assert discontinuities[0] is False
    assert discontinuities[1] is False
    assert discontinuities[4] is True, "first segment after the gap must be marked discontinuous"
    assert discontinuities[5] is False


# ---------------------------------------------------------------------------
# (b) stale manifest across max_stale_polls -> CloseReason.ERROR
# ---------------------------------------------------------------------------


async def test_stale_manifest_triggers_close_reason_error():
    stale = _mpd(start_number=0, timeline_s='<S t="0" d="2" />')  # same body forever -- never advances

    async with _ScriptedManifestServer([stale]) as server:
        handler = _RecordingHandler()
        async with pull_dash(
            server.url, handler_factory=lambda: handler, poll_interval=0.02, max_stale_polls=3
        ):
            for _ in range(500):
                if handler.closed_with is not None:
                    break
                await asyncio.sleep(0.01)

    assert handler.closed_with is CloseReason.ERROR


# ---------------------------------------------------------------------------
# (c) type="static" already present on first fetch -> clean CloseReason.COMPLETED
# ---------------------------------------------------------------------------


async def test_static_type_on_first_fetch_closes_cleanly():
    body = _mpd(mpd_type="static", start_number=0, timeline_s='<S t="0" d="2" r="1" />')

    async with _ScriptedManifestServer([body]) as server:
        handler = _RecordingHandler()
        async with pull_dash(
            server.url, handler_factory=lambda: handler, poll_interval=0.05, max_stale_polls=50
        ):
            for _ in range(200):
                if handler.closed_with is not None:
                    break
                await asyncio.sleep(0.01)

    assert handler.closed_with is CloseReason.COMPLETED
    assert [s.sequence for s in handler.segments] == [0, 1]


# ---------------------------------------------------------------------------
# dynamic -> static transition mid-poll -> CloseReason.COMPLETED
# ---------------------------------------------------------------------------


async def test_dynamic_to_static_transition_closes_cleanly():
    first = _mpd(start_number=0, timeline_s='<S t="0" d="2" />')
    second = _mpd(mpd_type="static", start_number=0, timeline_s='<S t="0" d="2" r="1" />')

    async with _ScriptedManifestServer([first, second]) as server:
        handler = _RecordingHandler()
        async with pull_dash(
            server.url, handler_factory=lambda: handler, poll_interval=0.05, max_stale_polls=50
        ):
            for _ in range(200):
                if handler.closed_with is not None:
                    break
                await asyncio.sleep(0.02)

    assert handler.closed_with is CloseReason.COMPLETED
    assert [s.sequence for s in handler.segments] == [0, 1]


# ---------------------------------------------------------------------------
# open_rejects / commit-point behaviour: unreachable origin propagates
# ---------------------------------------------------------------------------


async def test_unreachable_origin_propagates_before_handler_opens():
    handler = _RecordingHandler()
    with pytest.raises(httpx.TransportError):
        async with pull_dash(
            "http://127.0.0.1:1/manifest.mpd", handler_factory=lambda: handler, poll_interval=0.05
        ):
            pass
