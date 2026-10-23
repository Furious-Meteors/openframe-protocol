"""
The "real proof" suite for MediaFlowController: push ~100MB of fake
video+audio frames through a controller configured with an 8MiB-scale
budget, with a deliberately slow consumer forcing real shedding, and
assert the hard invariants hold under sustained, genuine back-pressure.
No network, no pyrtmp.
"""
from __future__ import annotations

import asyncio

from openframe.core.media import MediaFrame, MediaKind
from openframe.protocol.media.flow import (
    DropPolicy,
    FlowConfig,
    MediaFlowController,
    MediaGap,
)

_VIDEO_FRAME_BYTES = 4096
_AUDIO_FRAME_BYTES = 256
_TOTAL_VIDEO_FRAMES = 20_000  # ~80MB of video
_KEYFRAME_INTERVAL = 50
_AUDIO_EVERY_N_VIDEO = 3


def _build_frames() -> list[MediaFrame]:
    frames: list[MediaFrame] = []
    frames.append(
        MediaFrame(kind=MediaKind.VIDEO, payload=b"\x00" * 64, timestamp_ms=0, is_config=True, codec="h264")
    )
    frames.append(
        MediaFrame(kind=MediaKind.AUDIO, payload=b"\x00" * 16, timestamp_ms=0, is_config=True, codec="aac")
    )
    ts = 0
    for i in range(_TOTAL_VIDEO_FRAMES):
        is_keyframe = (i % _KEYFRAME_INTERVAL) == 0
        frames.append(
            MediaFrame(
                kind=MediaKind.VIDEO,
                payload=b"v" * _VIDEO_FRAME_BYTES,
                timestamp_ms=ts,
                is_keyframe=is_keyframe,
                codec="h264",
            )
        )
        if (i + 1) % _AUDIO_EVERY_N_VIDEO == 0:
            frames.append(
                MediaFrame(kind=MediaKind.AUDIO, payload=b"a" * _AUDIO_FRAME_BYTES, timestamp_ms=ts, codec="aac")
            )
        ts += 33  # ~30fps
    return frames


async def test_sustained_backpressure_never_exceeds_ceiling_and_preserves_invariants():
    frames = _build_frames()
    total_bytes = sum(len(f.payload) for f in frames)
    assert total_bytes > 80_000_000  # confirm this really is a ~100MB-scale run

    config = FlowConfig(
        high_watermark_bytes=8 * 1024 * 1024,
        low_watermark_bytes=2 * 1024 * 1024,
        max_buffered_bytes=16 * 1024 * 1024,
        max_buffered_duration_ms=None,
        policy=DropPolicy.DROP_VIDEO_UNTIL_KEYFRAME,
    )
    # pause()/resume() actually gate the producer here — mirroring what
    # pause_reading()/resume_reading() do for real against a real socket
    # (bytes genuinely stop arriving while paused). Without this, a
    # synthetic producer that ignores the pause signal entirely isn't a
    # faithful stand-in for the real backpressure mechanism: audio/config
    # frames are never dropped by design (see flow.py's own invariant),
    # so nothing short of the real upstream actually pausing keeps the
    # buffer bounded when the consumer is slower than the producer.
    paused = asyncio.Event()

    def _pause() -> None:
        paused.set()

    def _resume() -> None:
        paused.clear()

    controller = MediaFlowController(config=config, pause=_pause, resume=_resume)

    peak_buffered = 0
    output_sequence: list[MediaFrame | MediaGap] = []

    async def producer() -> None:
        nonlocal peak_buffered
        for i, frame in enumerate(frames):
            if paused.is_set():
                while paused.is_set():
                    await asyncio.sleep(0.001)
            controller.offer(frame)
            peak_buffered = max(peak_buffered, controller.stats.buffered_bytes)
            if i % 500 == 0:
                await asyncio.sleep(0)
        controller.feed_eof()

    async def consumer() -> None:
        async for item in controller:
            output_sequence.append(item)
            # Deliberately slow consumer — forces the producer to outrun
            # it and trigger real shedding.
            await asyncio.sleep(0.0002)

    await asyncio.gather(producer(), consumer())

    assert peak_buffered <= config.max_buffered_bytes
    assert controller.stats.pause_events > 0

    # Every video frame immediately following a MediaGap must be a keyframe.
    for idx, item in enumerate(output_sequence):
        if isinstance(item, MediaGap):
            # Find the next MediaFrame after the gap (should be immediate).
            assert idx + 1 < len(output_sequence)
            following = output_sequence[idx + 1]
            assert isinstance(following, MediaFrame)
            assert following.kind is MediaKind.VIDEO
            assert following.is_keyframe is True


async def test_zero_audio_or_config_frames_dropped_under_sustained_shedding():
    frames = _build_frames()
    config = FlowConfig(
        high_watermark_bytes=8 * 1024 * 1024,
        low_watermark_bytes=2 * 1024 * 1024,
        max_buffered_bytes=16 * 1024 * 1024,
        max_buffered_duration_ms=None,
        policy=DropPolicy.DROP_VIDEO_UNTIL_KEYFRAME,
    )
    paused = asyncio.Event()
    controller = MediaFlowController(config=config, pause=paused.set, resume=paused.clear)

    audio_offered = sum(1 for f in frames if f.kind is MediaKind.AUDIO)
    config_offered = sum(1 for f in frames if f.is_config)

    received: list[MediaFrame | MediaGap] = []

    async def producer() -> None:
        for i, frame in enumerate(frames):
            while paused.is_set():
                await asyncio.sleep(0.001)
            controller.offer(frame)
            if i % 500 == 0:
                await asyncio.sleep(0)
        controller.feed_eof()

    async def consumer() -> None:
        async for item in controller:
            received.append(item)
            await asyncio.sleep(0.0002)

    await asyncio.gather(producer(), consumer())

    audio_received = sum(1 for i in received if isinstance(i, MediaFrame) and i.kind is MediaKind.AUDIO)
    config_received = sum(1 for i in received if isinstance(i, MediaFrame) and i.is_config)

    assert audio_received == audio_offered, "DROP_VIDEO_UNTIL_KEYFRAME must never drop an audio frame"
    assert config_received == config_offered, "a config frame must never be dropped under any policy"
    assert controller.stats.pause_events > 0
