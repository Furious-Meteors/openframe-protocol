"""
Real spike test: a REAL pyrtmp-backed server (our own create_server/
bind_ingest) receiving a REAL publish from REAL ffmpeg. Slow by design —
spawns a real ffmpeg subprocess — same as every other transport's own
spike tests in this repo's culture.
"""
from __future__ import annotations

import asyncio
import shutil

import pytest
from openframe.core.inbound import CloseReason
from openframe.core.media import MediaFrame, MediaKind
from openframe.protocol.media import bind_ingest, create_server

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not available")


class _RecordingHandler:
    def __init__(self) -> None:
        self.opened = False
        self.frames: list[MediaFrame] = []
        self.closed_with: CloseReason | None = None

    async def on_open(self, session) -> None:
        self.opened = True

    async def on_event(self, session, event) -> None:
        if isinstance(event, MediaFrame):
            self.frames.append(event)

    async def on_close(self, session, reason: CloseReason) -> None:
        self.closed_with = reason


async def _noop() -> None:
    pass


async def test_real_ffmpeg_publish_produces_classified_media_frames():
    handler = _RecordingHandler()

    async with create_server(
        address="127.0.0.1",
        port=0,
        configure=lambda registry: bind_ingest(registry, "live", handler_factory=lambda: handler),
        on_startup=_noop,
        on_shutdown=_noop,
        manage_telemetry=False,
    ) as server:
        port = server._openframe_bound_port  # type: ignore[attr-defined]

        proc = await asyncio.create_subprocess_exec(
            FFMPEG,
            "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15",
            "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=44100",
            "-t", "3",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-pix_fmt", "yuv420p", "-g", "15",
            "-c:a", "aac", "-b:a", "64k",
            "-f", "flv",
            f"rtmp://127.0.0.1:{port}/live/test-stream",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=40)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise

        # Let cleanup() actually run after ffmpeg's own disconnect.
        for _ in range(200):
            if handler.closed_with is not None:
                break
            await asyncio.sleep(0.05)

        if proc.returncode != 0:
            raise AssertionError(
                f"ffmpeg exited {proc.returncode}\nstdout={stdout.decode(errors='replace')}\n"
                f"stderr={stderr.decode(errors='replace')}"
            )

    assert handler.opened, "on_open was never called"
    assert handler.frames, "no MediaFrames were received at all"

    video_config = [f for f in handler.frames if f.kind is MediaKind.VIDEO and f.is_config]
    audio_config = [f for f in handler.frames if f.kind is MediaKind.AUDIO and f.is_config]
    assert video_config, "no video config (AVCDecoderConfigurationRecord) frame received"
    assert audio_config, "no audio config (AudioSpecificConfig) frame received"

    first_config_index = handler.frames.index(video_config[0])
    keyframes_after_config = [
        f for f in handler.frames[first_config_index + 1 :] if f.kind is MediaKind.VIDEO and f.is_keyframe
    ]
    assert keyframes_after_config, "no keyframe received after the first config frame"

    assert handler.closed_with is CloseReason.COMPLETED, (
        f"expected COMPLETED (cleanup() always defaults to it — see ingest.py's own "
        f"module docstring finding 4), got {handler.closed_with!r}"
    )
