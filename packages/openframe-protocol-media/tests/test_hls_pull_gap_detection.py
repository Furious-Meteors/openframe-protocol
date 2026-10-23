"""
Controlled (no real ffmpeg, no real timing dependency) tests of
`pull_hls`'s gap-detection, discontinuity-inference, stale-poll timeout,
and clean-#EXT-X-ENDLIST behaviour — proving the three hardest-to-exercise
spike findings from `hls_pull.py`'s own module docstring (findings 4/5/6)
deterministically, via a tiny hand-rolled `asyncio` HTTP server serving
hand-written playlist fixtures this test fully controls, rather than
depending on real ffmpeg's own timing.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Self

import httpx
import pytest
from openframe.core.inbound import CloseReason
from openframe.protocol.media import pull_hls


class _RecordingHandler:
    def __init__(self) -> None:
        self.segments: list = []
        self.closed_with: CloseReason | None = None
        self.open_error: BaseException | None = None

    async def on_open(self, session) -> None:
        pass

    async def on_event(self, session, event) -> None:
        self.segments.append(event)

    async def on_close(self, session, reason: CloseReason) -> None:
        self.closed_with = reason


class _ScriptedPlaylistServer:
    """
    Serves a scripted sequence of playlist bodies for `GET /playlist.m3u8`
    (one body per call, repeating the last one once the script is
    exhausted) and arbitrary segment bytes for any other path, over a real
    `asyncio` TCP server — real HTTP parsing, real sockets, but fully
    deterministic content under this test's own control.
    """

    def __init__(self, playlists: list[bytes]) -> None:
        self._playlists = playlists
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
            # Drain headers.
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
            path = request_line.decode().split(" ")[1]

            if path == "/playlist.m3u8":
                index = min(self._call_count, len(self._playlists) - 1)
                self._call_count += 1
                body = self._playlists[index]
                content_type = "application/vnd.apple.mpegurl"
            else:
                # Deterministic small segment body derived from the path.
                body = f"segment-bytes:{path}".encode()
                content_type = "video/mp2t"

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
        return f"http://127.0.0.1:{self.port}/playlist.m3u8"


def _playlist(sequence_start: int, uris: list[str], *, endlist: bool = False, target_duration: int = 2) -> bytes:
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{target_duration}",
        f"#EXT-X-MEDIA-SEQUENCE:{sequence_start}",
    ]
    for uri in uris:
        lines.append("#EXTINF:2.000,")
        lines.append(uri)
    if endlist:
        lines.append("#EXT-X-ENDLIST")
    return ("\n".join(lines) + "\n").encode()


# ---------------------------------------------------------------------------
# (a) sequence gap -> next processed segment carries discontinuity=True
# ---------------------------------------------------------------------------


async def test_sequence_gap_marks_next_segment_discontinuous():
    first = _playlist(0, ["seg-0.ts", "seg-1.ts"])
    # Sequence 2 and 3 never appear anywhere -- a real missed-segment gap.
    second = _playlist(4, ["seg-4.ts", "seg-5.ts"], endlist=True)

    async with _ScriptedPlaylistServer([first, second]) as server:
        handler = _RecordingHandler()
        async with pull_hls(
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
    stale = _playlist(0, ["seg-0.ts"])  # same body forever -- never advances

    async with _ScriptedPlaylistServer([stale]) as server:
        handler = _RecordingHandler()
        async with pull_hls(
            server.url, handler_factory=lambda: handler, poll_interval=0.02, max_stale_polls=3
        ):
            for _ in range(500):
                if handler.closed_with is not None:
                    break
                await asyncio.sleep(0.01)

    assert handler.closed_with is CloseReason.ERROR


# ---------------------------------------------------------------------------
# (c) #EXT-X-ENDLIST already present -> clean CloseReason.COMPLETED, fast
# ---------------------------------------------------------------------------


async def test_endlist_present_on_first_fetch_closes_cleanly():
    body = _playlist(0, ["seg-0.ts", "seg-1.ts"], endlist=True)

    async with _ScriptedPlaylistServer([body]) as server:
        handler = _RecordingHandler()
        async with pull_hls(
            server.url, handler_factory=lambda: handler, poll_interval=0.05, max_stale_polls=50
        ):
            for _ in range(200):
                if handler.closed_with is not None:
                    break
                await asyncio.sleep(0.01)

    assert handler.closed_with is CloseReason.COMPLETED
    assert [s.sequence for s in handler.segments] == [0, 1]


# ---------------------------------------------------------------------------
# open_rejects / commit-point behaviour: unreachable origin propagates
# ---------------------------------------------------------------------------


async def test_unreachable_origin_propagates_before_handler_opens():
    handler = _RecordingHandler()
    with pytest.raises(httpx.TransportError):
        async with pull_hls(
            "http://127.0.0.1:1/playlist.m3u8", handler_factory=lambda: handler, poll_interval=0.05
        ):
            pass
