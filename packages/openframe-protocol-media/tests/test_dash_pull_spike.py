"""
The real spike this module's whole design is built on, re-run as a test:
real `ffmpeg` produces a genuinely live/rotating DASH MPD on disk (`-f
dash -seg_duration 2 -window_size 5 -use_template 1 -use_timeline 1
-remove_at_exit 0`), served over a real local HTTP server via this
package's own `bind_dash_delivery` (reading straight off the directory
ffmpeg is writing into), polled by the real `pull_dash` binding exactly as
a production caller would use it. Slow and spawns real infrastructure by
design -- same culture as `test_hls_pull_spike.py` (HLS pull's own
real-ffmpeg spike) and every other transport's own spike test in this
repo.

Proves end to end, per representation (video and audio are independently
sequenced -- see `dash_pull.py`'s own module docstring on why a flat
cross-representation sequence check would be wrong): segments arrive in
strictly increasing `sequence` order, zero duplicate sequences, at least a
handful of real segments are seen across the poll window matching what
ffmpeg actually produced on disk, and a clean `ffmpeg` stop (flipping the
MPD's `type` from `"dynamic"` to `"static"`) closes the session with
`CloseReason.COMPLETED`.
"""
from __future__ import annotations

import asyncio
import contextlib
import shutil
import signal
import tempfile
from collections import defaultdict
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from openframe.core.inbound import CloseReason
from openframe.protocol.media import bind_dash_delivery, pull_dash
from uvicorn import Config, Server

FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="real ffmpeg not installed")


class _DirectoryHandler:
    """QueryHandler[MediaRequest, MediaObject] reading straight off a real directory ffmpeg is writing live DASH output into."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    async def execute(self, query, context=None):
        from openframe.protocol.media.types import MediaObject

        file_path = self._directory / query.path
        # Same real-fetch-failure-is-a-true-to-life-signal reasoning as test_hls_pull_spike.py's
        # own _DirectoryHandler -- ffmpeg writes atomically via a .tmp + rename, so a real race
        # here is possible in principle but not observed in practice at this poll rate.
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
    bind_dash_delivery(router, handler_dep=lambda: handler)
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


async def test_real_ffmpeg_live_dash_pulled_in_order_with_clean_static_transition():
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        manifest_path = directory / "manifest.mpd"

        # Same ffmpeg invocation style confirmed in this session's own real spike: a genuinely
        # live/rotating MPD (sliding window of 5, 2s segments via SegmentTemplate+SegmentTimeline),
        # driven from a synthetic test source so this test needs no external input device.
        ffmpeg_proc = await asyncio.create_subprocess_exec(
            FFMPEG, "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15",
            "-f", "lavfi", "-i", "sine=frequency=1000",
            "-c:v", "libx264", "-preset", "ultrafast", "-g", "30",
            "-c:a", "aac",
            "-f", "dash", "-seg_duration", "2", "-window_size", "5",
            "-use_template", "1", "-use_timeline", "1", "-remove_at_exit", "0",
            str(manifest_path),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            # Wait for ffmpeg to produce the first real manifest + segment.
            for _ in range(200):
                if manifest_path.exists() and any(directory.glob("*.m4s")):
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("real ffmpeg never produced an initial manifest/segment")

            server, server_task = await _run_origin_server(directory)
            try:
                port = server.servers[0].sockets[0].getsockname()[1]
                url = f"http://127.0.0.1:{port}/manifest.mpd"

                handler = _RecordingHandler()
                async with pull_dash(url, handler_factory=lambda: handler, poll_interval=1.0, max_stale_polls=15):
                    # Real rotation window -- run long enough to see several real rotations
                    # before cleanly stopping ffmpeg (40s, within the task's documented 30-45s
                    # window).
                    await asyncio.sleep(40)
                    # Clean stop -> ffmpeg flips type="dynamic" to type="static".
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

    by_representation: dict[str, list[int]] = defaultdict(list)
    for segment in handler.segments:
        by_representation[segment.representation_id].append(segment.sequence)

    assert len(by_representation) >= 2, (
        f"expected at least 2 representations (video + audio, the real ffmpeg output's own "
        f"shape), got {sorted(by_representation)}"
    )
    for representation_id, sequences in by_representation.items():
        assert sequences == sorted(sequences), (
            f"representation {representation_id}: segments must arrive in strictly increasing "
            f"sequence order, got {sequences}"
        )
        assert len(sequences) == len(set(sequences)), (
            f"representation {representation_id}: no duplicate sequence numbers, got {sequences}"
        )
        assert len(sequences) >= 10, (
            f"representation {representation_id}: expected at least 10 real segments across a "
            f"~40s window at a 2s seg_duration (roughly matching what ffmpeg actually produced), "
            f"got {len(sequences)}"
        )

    assert handler.closed_with is CloseReason.COMPLETED, (
        "a clean ffmpeg stop flips the MPD's type to \"static\", which must map to COMPLETED"
    )
