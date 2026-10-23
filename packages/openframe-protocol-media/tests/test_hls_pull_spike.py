"""
The real spike this module's whole design is built on, re-run as a test:
real `ffmpeg` produces a genuinely live/rotating HLS playlist on disk
(`-f hls -hls_time 2 -hls_list_size 5 -hls_flags delete_segments`), served
over a real local HTTP server via this package's own `bind_hls_delivery`
(reading straight off the directory ffmpeg is writing into), polled by the
real `pull_hls` binding exactly as a production caller would use it. Slow
and spawns real infrastructure by design — same culture as
`test_ingest_spike.py` (RTMP ingest's own real-ffmpeg spike) and every
other transport's own spike test in this repo.

Proves end to end: segments arrive in strictly increasing sequence order,
zero duplicate sequences, at least a handful of real segments are seen
across the poll window, and a clean `ffmpeg` stop (producing a real
`#EXT-X-ENDLIST`) closes the session with `CloseReason.COMPLETED`.
"""
from __future__ import annotations

import asyncio
import contextlib
import shutil
import signal
import tempfile
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from openframe.core.inbound import CloseReason
from openframe.protocol.media import bind_hls_delivery, pull_hls
from uvicorn import Config, Server

FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="real ffmpeg not installed")


class _DirectoryHandler:
    """QueryHandler[MediaRequest, MediaObject] reading straight off a real directory ffmpeg is writing live HLS output into."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    async def execute(self, query, context=None):
        from openframe.protocol.media.types import MediaObject

        file_path = self._directory / query.path
        # ffmpeg deletes rotated-out segments live (-hls_flags
        # delete_segments) -- a real race between the manifest listing a
        # segment and us reading it is possible in principle; not observed
        # in practice at this poll rate, and a genuine miss here would
        # simply surface as a real fetch failure (a true-to-life signal
        # for a real pull client, not a test artifact to hide).
        body = file_path.read_bytes()
        return MediaObject(body=body, total_size=len(body))


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


async def _run_origin_server(directory: Path):
    router = APIRouter()
    handler = _DirectoryHandler(directory)
    bind_hls_delivery(router, handler_dep=lambda: handler)
    app = FastAPI()
    app.include_router(router)
    config = Config(app=app, host="127.0.0.1", port=0, log_level="error")
    server = Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.01)
    return server, task


async def test_real_ffmpeg_live_hls_pulled_in_order_with_clean_endlist():
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        playlist_path = directory / "playlist.m3u8"

        # Same ffmpeg invocation style confirmed in this session's own real
        # spikes: a genuinely live/rotating playlist (sliding window of 5,
        # 2s segments, deleting rotated-out segments as it goes), driven
        # from a synthetic test source so this test needs no external
        # input device.
        ffmpeg_proc = await asyncio.create_subprocess_exec(
            FFMPEG, "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15",
            "-f", "lavfi", "-i", "sine=frequency=1000",
            "-c:v", "libx264", "-preset", "ultrafast", "-g", "30",
            "-c:a", "aac",
            "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
            "-hls_flags", "delete_segments",
            str(playlist_path),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            # Wait for ffmpeg to produce the first real playlist + segment.
            for _ in range(200):
                if playlist_path.exists() and any(directory.glob("*.ts")):
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("real ffmpeg never produced an initial playlist/segment")

            server, server_task = await _run_origin_server(directory)
            try:
                port = server.servers[0].sockets[0].getsockname()[1]
                url = f"http://127.0.0.1:{port}/playlist.m3u8"

                handler = _RecordingHandler()
                async with pull_hls(url, handler_factory=lambda: handler, poll_interval=1.0, max_stale_polls=15):
                    # Real rotation window -- run long enough to see several
                    # real rotations before cleanly stopping ffmpeg (45s,
                    # within the task's own documented 30-45s window).
                    await asyncio.sleep(40)
                    # Clean stop -> ffmpeg writes a real #EXT-X-ENDLIST.
                    ffmpeg_proc.send_signal(signal.SIGINT)
                    await asyncio.wait_for(ffmpeg_proc.wait(), timeout=15)
                    for _ in range(300):
                        if handler.closed_with is not None:
                            break
                        await asyncio.sleep(0.1)
            finally:
                server.should_exit = True
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(server_task, timeout=5)
        finally:
            if ffmpeg_proc.returncode is None:
                ffmpeg_proc.kill()
                await ffmpeg_proc.wait()

    sequences = [s.sequence for s in handler.segments]
    assert sequences == sorted(sequences), "segments must arrive in strictly increasing sequence order"
    assert len(sequences) == len(set(sequences)), "no duplicate sequence numbers"
    assert len(sequences) >= 10, (
        f"expected at least 10 real segments across a ~40s window at a 2s hls_time "
        f"(roughly matching what ffmpeg actually produced), got {len(sequences)}"
    )
    assert handler.closed_with is CloseReason.COMPLETED, (
        "a clean ffmpeg stop produces a real #EXT-X-ENDLIST, which must map to COMPLETED"
    )
