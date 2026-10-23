"""
openframe/protocol/media/flow.py
====================================
`MediaFlowController` — transport-neutral, back-pressure-aware buffering
between a binary media producer (today: `pyrtmp`'s RTMP callbacks, via
`ingest.py`) and whatever consumes `MediaFrame`s downstream (a packager,
a recording sink, ...). Deliberately knows nothing about RTMP, pyrtmp, or
any other transport — every type this module imports is either stdlib or
`openframe.core.media`, so a hypothetical future RTSP/SRT/WebRTC ingest
binding can reuse this unchanged.

**Why a `collections.deque` + `asyncio.Event`, not `asyncio.Queue`** —
`asyncio.Queue.put_nowait` can't do byte-budgeted hysteresis (it only
counts items, not `len(frame.payload)`), can't do keyframe-aware eviction
(there's no way to inspect or remove an item already in the queue other
than draining it entirely), and can't back-evict already-queued items
from the tail when entering a shedding episode. `offer()` also must stay
synchronous and non-blocking — it's called directly from `pyrtmp`'s
`on_video_message`/`on_audio_message` callbacks, which are not places a
`await queue.put()` backpressure-blocks usefully (there's no awaiting
caller on that side to suspend; the thing that needs to pause is the
*reader*, via `pause_reading()`, not this coroutine). A plain `deque`
gives direct, synchronous access to both ends, which is exactly what
byte-budget tracking, eviction, and back-eviction all need.

Hysteresis, hard-ceiling overflow handling (per `DropPolicy`), and the
config-frame invariant (`is_config=True` frames are kept under every
policy, with zero exceptions — dropping one can make an entire stream
undecodable with no recovery short of a new session) are implemented
here. See each `DropPolicy` member and `MediaFlowController.offer`'s own
docstring for the exact shedding/resumption state machine.

Dependency order:
    protocol/media/flow → openframe.core.media (MediaFrame, MediaKind),
    openframe.core.telemetry (get_meter)
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from enum import StrEnum

from openframe.core.media import MediaFrame, MediaKind
from openframe.core.telemetry import get_meter

__all__ = [
    "BackPressureExceeded",
    "DropPolicy",
    "FlowConfig",
    "FlowStats",
    "MediaFlowController",
    "MediaGap",
]

_logger = logging.getLogger(__name__)


class DropPolicy(StrEnum):
    """
    What `MediaFlowController` does when the hard ceiling
    (`FlowConfig.max_buffered_bytes`/`max_buffered_duration_ms`) is
    exceeded by an incoming frame. Hysteresis (the high/low watermark
    pause/resume signal) is unaffected by which policy is configured —
    these only govern what happens once hysteresis alone hasn't been
    enough to keep the buffer bounded (a consumer that's fallen far
    enough behind, or a producer bursting faster than `pause_reading()`
    can react).
    """

    NEVER = "never"
    """
    Never drop a frame. `offer()` returns `False` and the controller
    enters a permanent fault state once the ceiling is exceeded — the
    *consumer* (inside `__anext__`) raises `BackPressureExceeded` the
    next time it's asked for an item, since `offer()` itself is
    synchronous and has no async context worth raising into; the
    consumer is the side actually waiting and able to act on the
    failure.
    """

    DROP_VIDEO_UNTIL_KEYFRAME = "drop_video_until_keyframe"
    """
    The default, sensible policy: shed non-config video interframes
    until a fresh keyframe arrives and the buffer has drained back to
    the low watermark. Audio and config frames are always kept.
    """

    DROP_ALL_UNTIL_KEYFRAME = "drop_all_until_keyframe"
    """
    A pathological-memory escape hatch, NOT a sensible default: sheds
    audio too while shedding (config frames are still always kept, with
    no exception). Only reach for this when even audio can't be kept
    without exceeding available memory — the resulting stream will have
    an audible gap, not just a visual one.
    """


class BackPressureExceeded(Exception):
    """
    Raised from inside `MediaFlowController.__anext__` (never from
    `offer()`) when `DropPolicy.NEVER`'s hard ceiling has been exceeded.
    """


@dataclass(frozen=True, slots=True)
class MediaGap:
    """
    Sentinel marking the end of one shedding episode, carrying the
    timestamp of the resumption keyframe plus that episode's totals.
    Yielded into the same async iteration stream as real `MediaFrame`s
    (`__aiter__` yields `MediaFrame | MediaGap`) — a consumer tells the
    two apart via `isinstance`. Deliberately its own type, not a bare
    `MediaFrame` with `kind=MediaKind.METADATA`: a gap is a fact about
    the *stream*, not a unit of media, and overloading `MediaKind.METADATA`
    for it would make a consumer's `isinstance(frame, MediaFrame)` check
    return `True` for something that isn't actually decodable media.
    """

    resumed_at_timestamp_ms: int
    frames_dropped: int
    bytes_dropped: int


@dataclass(frozen=True, slots=True)
class FlowConfig:
    """
    Tunables for one `MediaFlowController`. The byte-watermark defaults
    (8 MiB high / 2 MiB low / 32 MiB ceiling) are the spike's provisional
    defaults, not proven-optimal numbers — `ffmpeg` survived real 2s and
    5s server-side `pause_reading()` pauses with zero errors at ordinary
    bitrates during the spike, which validates the *mechanism*, not these
    specific thresholds. Tune per deployment bitrate.
    """

    high_watermark_bytes: int = 8_388_608
    low_watermark_bytes: int = 2_097_152
    max_buffered_bytes: int = 33_554_432
    max_buffered_duration_ms: int | None = 4_000
    policy: DropPolicy = DropPolicy.DROP_VIDEO_UNTIL_KEYFRAME


@dataclass
class FlowStats:
    """Running counters for one `MediaFlowController`'s lifetime."""

    buffered_bytes: int = 0
    frames_offered: int = 0
    frames_dropped_overflow: int = 0
    frames_dropped_shed: int = 0
    bytes_dropped_overflow: int = 0
    bytes_dropped_shed: int = 0
    pause_events: int = 0
    resume_events: int = 0
    shed_episodes: int = 0
    currently_shedding: bool = False
    currently_paused: bool = False


_METER_NAME_DEFAULT = "openframe.protocol.media"


class MediaFlowController:
    """
    Buffers `MediaFrame`s between a synchronous producer callback
    (`offer()`) and an async consumer (`__aiter__`), applying hysteresis-
    based back-pressure and, under sustained overflow, a `DropPolicy`.

    Not itself an `AsyncIterator` subclass of anything from
    `openframe.core` — it is a plain async-iterable a `SessionHandler`
    binding hands to `openframe.protocol.base.session.run_opened_session`
    as the `inbound_events` argument.
    """

    def __init__(
        self,
        *,
        config: FlowConfig = FlowConfig(),
        pause: Callable[[], None] | None = None,
        resume: Callable[[], None] | None = None,
        meter_name: str = _METER_NAME_DEFAULT,
    ) -> None:
        self._config = config
        self._pause = pause
        self._resume = resume
        self._deque: deque[MediaFrame | MediaGap] = deque()
        self._stats = FlowStats()
        self._not_empty = asyncio.Event()
        self._eof = False
        self._abort_exc: BaseException | None = None
        self._backpressure_fault = False
        self._shed_start_dropped_frames = 0
        self._shed_start_dropped_bytes = 0

        meter = get_meter(meter_name)
        self._m_frames_dropped = meter.create_counter(
            "media.frames.dropped", unit="1", description="Media frames dropped by MediaFlowController"
        )
        self._m_bytes_ingested = meter.create_counter(
            "media.bytes.ingested", unit="By", description="Media bytes offered to MediaFlowController"
        )
        self._m_buffered_bytes = meter.create_up_down_counter(
            "media.flow.buffered_bytes", unit="By", description="Currently buffered media bytes"
        )
        self._m_pause_events = meter.create_counter(
            "media.flow.pause_events", unit="1", description="Number of times the upstream reader was paused"
        )
        self._m_queue_dwell_ms = meter.create_histogram(
            "media.flow.queue_dwell_ms", unit="ms", description="Time a frame spent buffered before being consumed"
        )
        self._m_shed_duration_ms = meter.create_histogram(
            "media.shed.duration_ms", unit="ms", description="Duration of one shedding episode"
        )
        self._shed_episode_started_ns: int = 0

    @property
    def stats(self) -> FlowStats:
        return self._stats

    # ------------------------------------------------------------------
    # Producer side — synchronous, non-blocking.
    # ------------------------------------------------------------------

    def offer(self, frame: MediaFrame) -> bool:
        """
        Offer *frame* to the buffer. Synchronous and non-blocking —
        callable directly from a `pyrtmp` callback. Returns `False` if
        the frame was dropped (either shed under
        `DROP_VIDEO_UNTIL_KEYFRAME`/`DROP_ALL_UNTIL_KEYFRAME`, or refused
        outright under `DropPolicy.NEVER`'s hard ceiling); `True`
        otherwise.

        Raises:
            RuntimeError: if called after `feed_eof()`/`abort()`.
        """
        if self._eof or self._abort_exc is not None:
            raise RuntimeError("cannot offer a frame on a closed MediaFlowController")

        self._stats.frames_offered += 1
        self._m_bytes_ingested.add(len(frame.payload), {"kind": frame.kind.value})

        is_config = frame.is_config
        is_audio = frame.kind is MediaKind.AUDIO

        # The one invariant that must never be violated, under ANY
        # policy: a config frame (sequence header / AVCDecoderConfigurationRecord
        # / AudioSpecificConfig) is always kept. Dropping one can make
        # the entire stream undecodable with no recovery short of a
        # brand-new session — strictly worse than dropping a keyframe.
        if is_config:
            self._enqueue(frame)
            return True

        if self._stats.currently_shedding:
            keep = is_audio and self._config.policy is DropPolicy.DROP_VIDEO_UNTIL_KEYFRAME
            if keep:
                self._enqueue(frame)
                return True

            is_resuming_keyframe = (
                frame.kind is MediaKind.VIDEO
                and frame.is_keyframe
                and self._stats.buffered_bytes <= self._config.low_watermark_bytes
            )
            if is_resuming_keyframe:
                self._end_shed_episode(frame)
                self._enqueue(frame)
                return True

            self._record_drop(frame, reason="shed")
            return False

        # Not currently shedding: check whether this frame would push us
        # over the hard ceiling.
        if self._would_overflow(frame):
            if self._config.policy is DropPolicy.NEVER:
                self._backpressure_fault = True
                self._record_drop(frame, reason="overflow")
                self._not_empty.set()
                return False

            self._begin_shed_episode()
            # The frame that triggered shedding is itself subject to the
            # same keep/drop rule as any other frame while shedding,
            # EXCEPT a config frame already returned above.
            keep = is_audio and self._config.policy is DropPolicy.DROP_VIDEO_UNTIL_KEYFRAME
            if keep:
                self._enqueue(frame)
                return True
            self._record_drop(frame, reason="shed")
            return False

        self._enqueue(frame)
        return True

    def _would_overflow(self, frame: MediaFrame) -> bool:
        cfg = self._config
        if self._stats.buffered_bytes + len(frame.payload) > cfg.max_buffered_bytes:
            return True
        if cfg.max_buffered_duration_ms is not None and self._deque:
            oldest = self._oldest_frame()
            if oldest is not None:
                span = frame.timestamp_ms - oldest.timestamp_ms
                if span > cfg.max_buffered_duration_ms:
                    return True
        return False

    def _oldest_frame(self) -> MediaFrame | None:
        for item in self._deque:
            if isinstance(item, MediaFrame):
                return item
        return None

    def _newest_frame(self) -> MediaFrame | None:
        for item in reversed(self._deque):
            if isinstance(item, MediaFrame):
                return item
        return None

    def _enqueue(self, frame: MediaFrame) -> None:
        self._deque.append(frame)
        self._stats.buffered_bytes += len(frame.payload)
        self._m_buffered_bytes.add(len(frame.payload))
        if not self._stats.currently_paused and self._stats.buffered_bytes >= self._config.high_watermark_bytes:
            self._stats.currently_paused = True
            self._stats.pause_events += 1
            self._m_pause_events.add(1)
            if self._pause is not None:
                self._pause()
        self._not_empty.set()

    def _record_drop(self, frame: MediaFrame, *, reason: str) -> None:
        size = len(frame.payload)
        if reason == "overflow":
            self._stats.frames_dropped_overflow += 1
            self._stats.bytes_dropped_overflow += size
        else:
            self._stats.frames_dropped_shed += 1
            self._stats.bytes_dropped_shed += size
        self._m_frames_dropped.add(
            1, {"kind": frame.kind.value, "policy": self._config.policy.value, "reason": reason}
        )

    def _begin_shed_episode(self) -> None:
        self._stats.currently_shedding = True
        self._stats.shed_episodes += 1
        self._shed_episode_started_ns = time.perf_counter_ns()
        self._shed_start_dropped_frames = self._stats.frames_dropped_shed
        self._shed_start_dropped_bytes = self._stats.bytes_dropped_shed

        # Back-evict from the tail down to the low watermark, stopping
        # at (not evicting) the first audio or config frame encountered
        # from the tail — those must survive a shedding transition the
        # same as any frame offered *during* shedding.
        while self._deque and self._stats.buffered_bytes > self._config.low_watermark_bytes:
            tail = self._deque[-1]
            if isinstance(tail, MediaGap):
                break
            if tail.is_config or tail.kind is MediaKind.AUDIO:
                break
            self._deque.pop()
            self._stats.buffered_bytes -= len(tail.payload)
            self._m_buffered_bytes.add(-len(tail.payload))
            self._record_drop(tail, reason="shed")

    def _end_shed_episode(self, resuming_keyframe: MediaFrame) -> None:
        frames_dropped = self._stats.frames_dropped_shed - self._shed_start_dropped_frames
        bytes_dropped = self._stats.bytes_dropped_shed - self._shed_start_dropped_bytes
        gap = MediaGap(
            resumed_at_timestamp_ms=resuming_keyframe.timestamp_ms,
            frames_dropped=frames_dropped,
            bytes_dropped=bytes_dropped,
        )
        self._deque.append(gap)
        if self._shed_episode_started_ns:
            duration_ms = (time.perf_counter_ns() - self._shed_episode_started_ns) / 1_000_000
            self._m_shed_duration_ms.record(duration_ms)
        self._stats.currently_shedding = False

    def feed_eof(self) -> None:
        """
        Signal a clean end of stream. The async iterator drains whatever
        is already buffered, then raises `StopAsyncIteration`.
        """
        self._eof = True
        self._not_empty.set()

    def abort(self, exc: BaseException) -> None:
        """
        Signal an abnormal end. The async iterator drains whatever is
        already buffered (same draining behaviour as `feed_eof()`), then
        raises *exc* instead of `StopAsyncIteration` once the buffer is
        empty — a deliberate choice to never silently discard frames
        that already arrived before the abort, even though the abort
        itself means something went wrong upstream.
        """
        self._abort_exc = exc
        self._not_empty.set()

    # ------------------------------------------------------------------
    # Consumer side — async.
    # ------------------------------------------------------------------

    def __aiter__(self) -> AsyncIterator[MediaFrame | MediaGap]:
        return self

    async def __anext__(self) -> MediaFrame | MediaGap:
        while True:
            if self._deque:
                item = self._deque.popleft()
                if isinstance(item, MediaFrame):
                    self._stats.buffered_bytes -= len(item.payload)
                    self._m_buffered_bytes.add(-len(item.payload))
                    if item.received_at_ns:
                        dwell_ms = (time.perf_counter_ns() - item.received_at_ns) / 1_000_000
                        self._m_queue_dwell_ms.record(dwell_ms)
                    if (
                        self._stats.currently_paused
                        and self._stats.buffered_bytes <= self._config.low_watermark_bytes
                    ):
                        self._stats.currently_paused = False
                        self._stats.resume_events += 1
                        if self._resume is not None:
                            self._resume()
                return item
            if self._backpressure_fault:
                raise BackPressureExceeded(
                    f"DropPolicy.NEVER hard ceiling exceeded "
                    f"(max_buffered_bytes={self._config.max_buffered_bytes})"
                )
            if self._abort_exc is not None:
                raise self._abort_exc
            if self._eof:
                raise StopAsyncIteration
            self._not_empty.clear()
            await self._not_empty.wait()
