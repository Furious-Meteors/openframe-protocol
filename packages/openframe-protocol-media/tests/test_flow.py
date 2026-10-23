"""
Unit tests for MediaFlowController — no network, no pyrtmp. Fake
pause/resume callables (plain functions recording call counts) and
hand-constructed MediaFrames exercise the full hysteresis / overflow /
shedding / resumption state machine in isolation.
"""
from __future__ import annotations

import pytest
from openframe.core.media import MediaFrame, MediaKind
from openframe.protocol.media.flow import (
    BackPressureExceeded,
    DropPolicy,
    FlowConfig,
    MediaFlowController,
    MediaGap,
)


def _frame(
    kind: MediaKind = MediaKind.VIDEO,
    *,
    size: int = 1000,
    timestamp_ms: int = 0,
    is_keyframe: bool = False,
    is_config: bool = False,
) -> MediaFrame:
    return MediaFrame(
        kind=kind,
        payload=b"x" * size,
        timestamp_ms=timestamp_ms,
        is_keyframe=is_keyframe,
        is_config=is_config,
    )


class _Callbacks:
    def __init__(self) -> None:
        self.pause_count = 0
        self.resume_count = 0

    def pause(self) -> None:
        self.pause_count += 1

    def resume(self) -> None:
        self.resume_count += 1


def _controller(**config_kwargs) -> tuple[MediaFlowController, _Callbacks]:
    callbacks = _Callbacks()
    config = FlowConfig(**config_kwargs)
    controller = MediaFlowController(config=config, pause=callbacks.pause, resume=callbacks.resume)
    return controller, callbacks


# ---------------------------------------------------------------------------
# Hysteresis
# ---------------------------------------------------------------------------


async def test_hysteresis_pauses_exactly_once_per_crossing():
    controller, cb = _controller(high_watermark_bytes=5000, low_watermark_bytes=1000, max_buffered_bytes=100_000)
    for _ in range(10):
        controller.offer(_frame(size=1000))
    assert cb.pause_count == 1
    assert controller.stats.currently_paused is True

    # Draining below low watermark should resume exactly once.
    drained = 0
    async for item in controller:
        drained += 1
        if controller.stats.buffered_bytes <= 1000:
            break
    assert cb.resume_count == 1


async def test_hysteresis_never_fires_resume_without_pause():
    controller, cb = _controller(high_watermark_bytes=50_000, low_watermark_bytes=1000, max_buffered_bytes=100_000)
    controller.offer(_frame(size=500))
    controller.feed_eof()
    async for _ in controller:
        pass
    assert cb.pause_count == 0
    assert cb.resume_count == 0


# ---------------------------------------------------------------------------
# Config-frame invariant — the most important invariant in the package.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("policy", [DropPolicy.DROP_VIDEO_UNTIL_KEYFRAME, DropPolicy.DROP_ALL_UNTIL_KEYFRAME])
async def test_config_frames_are_NEVER_dropped_under_any_shedding_policy(policy):
    """
    The one invariant that must never be violated: a frame with
    is_config=True is kept under EVERY DropPolicy, with zero exceptions —
    dropping one can make the entire stream undecodable with no recovery
    short of a brand-new session (strictly worse than dropping a keyframe).
    """
    controller, _ = _controller(
        high_watermark_bytes=10_000_000,  # never hysteresis-pause in this test
        low_watermark_bytes=1000,
        max_buffered_bytes=3000,  # force overflow quickly
        policy=policy,
    )
    config_frame = _frame(kind=MediaKind.VIDEO, size=500, is_config=True)
    controller.offer(config_frame)
    # Force shedding with a flood of large interframes.
    for _ in range(20):
        controller.offer(_frame(kind=MediaKind.VIDEO, size=1000))
        # Config frames keep arriving throughout — none may ever be dropped.
        assert controller.offer(_frame(kind=MediaKind.VIDEO, size=10, is_config=True)) is True

    assert controller.stats.frames_dropped_shed > 0  # shedding genuinely happened


async def test_shedding_preserves_every_audio_frame_under_default_policy():
    controller, _ = _controller(
        high_watermark_bytes=10_000_000,
        low_watermark_bytes=1000,
        max_buffered_bytes=3000,
        policy=DropPolicy.DROP_VIDEO_UNTIL_KEYFRAME,
    )
    controller.offer(_frame(kind=MediaKind.VIDEO, size=1000))  # trigger overflow -> shedding
    audio_offered = 0
    for _ in range(10):
        controller.offer(_frame(kind=MediaKind.VIDEO, size=1000))
        assert controller.offer(_frame(kind=MediaKind.AUDIO, size=50)) is True
        audio_offered += 1

    audio_in_queue = sum(
        1 for item in controller._deque if isinstance(item, MediaFrame) and item.kind is MediaKind.AUDIO
    )
    assert audio_in_queue == audio_offered


async def test_drop_all_until_keyframe_also_sheds_audio():
    controller, _ = _controller(
        high_watermark_bytes=10_000_000,
        low_watermark_bytes=100,
        max_buffered_bytes=500,
        policy=DropPolicy.DROP_ALL_UNTIL_KEYFRAME,
    )
    controller.offer(_frame(kind=MediaKind.VIDEO, size=1000))
    assert controller.stats.currently_shedding is True
    dropped = controller.offer(_frame(kind=MediaKind.AUDIO, size=50))
    assert dropped is False
    assert controller.stats.frames_dropped_shed > 0


# ---------------------------------------------------------------------------
# Resumption requires BOTH keyframe AND below-low-watermark.
# ---------------------------------------------------------------------------


async def test_resumption_requires_keyframe_and_below_low_watermark():
    controller, _ = _controller(
        high_watermark_bytes=10_000_000,
        low_watermark_bytes=500,
        max_buffered_bytes=800,
        policy=DropPolicy.DROP_VIDEO_UNTIL_KEYFRAME,
    )
    controller.offer(_frame(kind=MediaKind.VIDEO, size=1000))  # overflow -> shedding
    assert controller.stats.currently_shedding is True

    # A keyframe while still ABOVE the low watermark must NOT exit shedding.
    controller._stats.buffered_bytes = 10_000  # force "still too full"
    accepted = controller.offer(_frame(kind=MediaKind.VIDEO, size=10, is_keyframe=True, timestamp_ms=1))
    assert accepted is False
    assert controller.stats.currently_shedding is True

    # Now bring it below the low watermark and offer a keyframe — must exit.
    controller._stats.buffered_bytes = 100
    accepted = controller.offer(_frame(kind=MediaKind.VIDEO, size=10, is_keyframe=True, timestamp_ms=2))
    assert accepted is True
    assert controller.stats.currently_shedding is False


async def test_exactly_one_media_gap_per_shedding_episode_with_correct_counts():
    controller, _ = _controller(
        high_watermark_bytes=10_000_000,
        low_watermark_bytes=100,
        max_buffered_bytes=1500,
        policy=DropPolicy.DROP_VIDEO_UNTIL_KEYFRAME,
    )
    controller.offer(_frame(kind=MediaKind.VIDEO, size=1000, timestamp_ms=0))  # triggers overflow
    for i in range(5):
        controller.offer(_frame(kind=MediaKind.VIDEO, size=200, timestamp_ms=i + 1))
    controller._stats.buffered_bytes = 0  # force below low watermark
    controller.offer(_frame(kind=MediaKind.VIDEO, size=10, is_keyframe=True, timestamp_ms=99))
    controller.feed_eof()

    gaps = []
    frames = []
    async for item in controller:
        if isinstance(item, MediaGap):
            gaps.append(item)
        else:
            frames.append(item)

    assert len(gaps) == 1
    assert gaps[0].resumed_at_timestamp_ms == 99
    assert gaps[0].frames_dropped > 0
    assert gaps[0].bytes_dropped > 0


async def test_back_eviction_on_entering_shedding_stops_at_audio_or_config():
    controller, _ = _controller(
        high_watermark_bytes=10_000_000,
        low_watermark_bytes=100,
        max_buffered_bytes=1300,
        policy=DropPolicy.DROP_VIDEO_UNTIL_KEYFRAME,
    )
    controller.offer(_frame(kind=MediaKind.VIDEO, size=200, timestamp_ms=0))
    controller.offer(_frame(kind=MediaKind.AUDIO, size=200, timestamp_ms=1))
    controller.offer(_frame(kind=MediaKind.VIDEO, size=200, timestamp_ms=2))
    # This offer pushes over max_buffered_bytes (1300), triggering shedding
    # and back-eviction from the tail.
    controller.offer(_frame(kind=MediaKind.VIDEO, size=1000, timestamp_ms=3))

    remaining_kinds = [
        (item.kind, item.timestamp_ms) for item in controller._deque if isinstance(item, MediaFrame)
    ]
    # The audio frame at timestamp_ms=1 must still be present — back-eviction
    # stops at it rather than evicting through it.
    assert (MediaKind.AUDIO, 1) in remaining_kinds


# ---------------------------------------------------------------------------
# Duration-based overflow, independent of byte-based overflow.
# ---------------------------------------------------------------------------


async def test_duration_based_overflow_triggers_independently_of_bytes():
    controller, _ = _controller(
        high_watermark_bytes=10_000_000,
        low_watermark_bytes=100,
        max_buffered_bytes=10_000_000,  # effectively unlimited by bytes
        max_buffered_duration_ms=100,
        policy=DropPolicy.DROP_VIDEO_UNTIL_KEYFRAME,
    )
    controller.offer(_frame(kind=MediaKind.VIDEO, size=10, timestamp_ms=0))
    assert controller.stats.currently_shedding is False
    # Span now exceeds max_buffered_duration_ms even though bytes are tiny.
    controller.offer(_frame(kind=MediaKind.VIDEO, size=10, timestamp_ms=500))
    assert controller.stats.currently_shedding is True


# ---------------------------------------------------------------------------
# DropPolicy.NEVER
# ---------------------------------------------------------------------------


async def test_drop_policy_never_raises_backpressure_exceeded_from_consumer():
    controller, _ = _controller(
        high_watermark_bytes=10_000_000,
        low_watermark_bytes=100,
        max_buffered_bytes=1000,
        policy=DropPolicy.NEVER,
    )
    controller.offer(_frame(size=500))
    accepted = controller.offer(_frame(size=800))  # overflow
    assert accepted is False

    with pytest.raises(BackPressureExceeded):
        async for _ in controller:
            pass


# ---------------------------------------------------------------------------
# feed_eof / abort / closed-controller RuntimeError
# ---------------------------------------------------------------------------


async def test_feed_eof_drains_then_stops_cleanly():
    controller, _ = _controller()
    controller.offer(_frame(size=10, timestamp_ms=0))
    controller.offer(_frame(size=10, timestamp_ms=1))
    controller.feed_eof()

    collected = [item async for item in controller]
    assert len(collected) == 2


async def test_abort_drains_then_raises_exact_exception():
    controller, _ = _controller()
    controller.offer(_frame(size=10, timestamp_ms=0))
    boom = RuntimeError("boom")
    controller.abort(boom)

    collected = []
    with pytest.raises(RuntimeError) as exc_info:
        async for item in controller:
            collected.append(item)
    assert exc_info.value is boom
    assert len(collected) == 1


async def test_offering_after_eof_raises_runtime_error():
    controller, _ = _controller()
    controller.feed_eof()
    with pytest.raises(RuntimeError):
        controller.offer(_frame())


async def test_offering_after_abort_raises_runtime_error():
    controller, _ = _controller()
    controller.abort(RuntimeError("x"))
    with pytest.raises(RuntimeError):
        controller.offer(_frame())
