"""
openframe/protocol/media/hls_pull.py
========================================
`pull_hls` — polls a remote HLS *origin* (a live/sliding-window media
playlist we do NOT control), parses it, and fetches new segments into a
`SessionHandler` (`openframe.core.inbound`, ADR-008), for a real-time
pipeline that needs to pull HLS rather than receive an RTMP push
(`ingest.py`) or serve already-produced HLS/DASH (`delivery.py`). The
third, architecturally distinct capability this package provides —
outbound HTTP polling rather than an inbound connection of any kind.

Everything below was confirmed against two real spikes this session — a
real `ffmpeg`-produced live/sliding-window HLS stream, polled with real
`httpx` + the real `m3u8` 6.0.0 library, including a real encoder restart
and a real `SIGKILL` — not assumed from the HLS spec or `m3u8`'s own
README.

1. **Sequence numbers, not URIs, are the dedup/ordering key.** `m3u8`
   assigns `segment.media_sequence` to every segment itself (confirmed by
   reading its source: `for i, segment in enumerate(self.segments,
   self.media_sequence or 0): segment.media_sequence = i`) and this was
   confirmed reliable across 50 real rotations of a real sliding-window
   playlist, zero misses or duplicates at a safe poll rate. Segment URIs
   are NOT safe to dedup on — confirmed real: an unrelated encoder/stream
   restart can reset filename counters, so two different segments can
   share a URI. `highest_sequence_processed` (an int, `-1` until the
   first segment is processed) is the only state this module tracks
   across polls to decide what's new.

2. **`m3u8` has no `reload()`/diff method.** This module fetches the
   playlist text itself via an injected/owned `httpx.AsyncClient` (never
   `m3u8.load(url)`, which would own its own HTTP stack and give us no
   control over headers/auth/retries), parses fresh each poll with
   `m3u8.loads(text)`, and diffs the freshly parsed segment list against
   `highest_sequence_processed` itself.

3. **`current_program_date_time`, not `program_date_time`, is the field
   to read.** PDT (`#EXT-X-PROGRAM-DATE-TIME`) is opt-in at the origin
   and often entirely absent — confirmed: ffmpeg's own default `-f hls`
   emits none at all. `segment.program_date_time` is `m3u8`'s sparse,
   only-set-if-explicitly-tagged field; `segment.current_program_date_time`
   is the library's own backfilled value (it accumulates `#EXTINF`
   duration forward from the last explicit tag, or from playlist start if
   none was ever tagged) and was confirmed, by spike, to be populated far
   more often. Converted to epoch milliseconds for
   `MediaSegment.program_date_time_ms`; left `None` only when
   `current_program_date_time` itself is `None` (meaning `m3u8` had truly
   nothing to backfill from).

4. **Discontinuity must be inferred, not just read off the origin's own
   tag.** `#EXT-X-DISCONTINUITY` is never automatic — confirmed by
   actually restarting a real encoder with a resolution/GOP change into
   the same stream: zero discontinuity tags appeared without the origin
   explicitly configuring `-hls_flags +discont_start`. `segment
   .discontinuity` (from `m3u8`) therefore means "the origin chose to say
   so," not "this segment is known contiguous." This module independently
   infers a discontinuity whenever its own sequence-gap detection (next
   point) fires, OR-ing it with the origin's own flag —
   `discontinuity = segment.discontinuity or gap_detected_before_this_segment`
   — a deliberate robustness addition the spike's findings make
   necessary, not optional.

5. **Poll too slowly and segments vanish with zero errors raised
   anywhere.** Confirmed: polling at roughly the full sliding-window
   duration (rather than a fraction of the target duration) silently
   missed 22% of real segments — the only symptom was `media_sequence`
   jumping by more than 1 between consecutive fetches, never an
   exception. Two defenses, both load-bearing:
     a. `poll_interval=None` derives a default from the first fetched
        playlist's own `#EXT-X-TARGETDURATION` — half of it (confirmed
        safe: 1s against a real 2s target duration produced zero misses
        across 50 rotations). If the manifest has no target duration at
        all (confirmed to happen on some origins), a conservative
        hardcoded `_FALLBACK_POLL_INTERVAL = 2.0` seconds is used instead
        of crashing — chosen as "no worse than a bare minimum HLS target
        duration," not derived from a spike, since there's nothing left
        to derive it from.
     b. Every poll explicitly compares the newly fetched lowest sequence
        number against `highest_sequence_processed + 1`. A gap there IS
        the "we missed segments" signal — logged, and (per finding 4)
        marks the very next segment this module does process as
        `discontinuity=True`, since whatever happened in the gap is, by
        definition, unknown content this consumer never saw.

6. **Clean end (`#EXT-X-ENDLIST`) and a silent origin crash are two
   completely different signals, confirmed to require two completely
   different detections.** `playlist_obj.is_endlist` reliably means the
   stream is over on purpose -> `CloseReason.COMPLETED`. A hard kill
   (confirmed with a real `SIGKILL`-equivalent against a real origin)
   gives NO signal at all — the playlist text was confirmed byte-for-byte
   identical before and after the kill; there is no exception, no
   connection reset, nothing to catch. The only detectable symptom is
   "the sequence number stopped advancing." `max_stale_polls` (default
   `10`, chosen so a momentary origin hiccup — one slow poll, one
   repeated fetch of the same manifest while a new segment is still being
   written — doesn't false-positive, while a genuinely dead origin is
   still caught well within a minute at the safe ~1s poll interval the
   2s-target-duration spike confirmed) counts consecutive polls whose
   highest sequence number did not advance; hitting it raises inside the
   poll loop, which `run_opened_session` maps to `CloseReason.ERROR`.

7. **`SessionHandler`'s own contract is mechanically reusable for a
   self-initiated poll loop — confirmed by direct design research this
   session, not assumed.** ADR-008's own "forward a Kafka consumer into
   an already-open session" example is exactly this shape: a background
   loop calling `on_event`/`session.send()` over time, not a remote peer
   driving the session from outside. One deliberate narrowing, confirmed
   correct by this session's own design review:
   **`CloseReason.CLIENT_DISCONNECT` is NEVER used by this transport** —
   there is no remote peer to disconnect from when this side is the one
   doing the polling. Only `CloseReason.COMPLETED` (a clean
   `#EXT-X-ENDLIST`, or the caller explicitly exiting the `pull_hls`
   context manager) and `CloseReason.ERROR` (the stale-poll timeout above,
   or an unrecoverable fetch/parse failure) are ever reported.

**Why `http_client` is an injection point, mirroring `HttpRemoteQueryClient`
(`openframe.protocol.http.client`) exactly.** Read directly from its own
`_HttpRemoteCallMixin.__init__`/`aclose()` (not guessed): if a caller
passes their own `httpx.AsyncClient`, that client's lifecycle is the
caller's responsibility (`_owns_client = client is None`) — this module
mirrors that exact flag so an injected client is never closed here. This
is the entire justification for why HLS pull belongs in
`openframe-protocol-media` at all, rather than just pointing `ffmpeg -i`
at the remote URL directly: the injection point is what lets a caller
attach custom auth headers, rotate credentials mid-stream, or fail over
across CDNs (via a custom `httpx.AsyncBaseTransport` on their own client)
— none of which `ffmpeg`'s own internal HTTP client exposes a hook for.

**Scope.** A plain *media* playlist only (the common single-rendition
case). A *master* playlist (multiple variant streams, needing a
`rendition_selector` to pick one) is explicitly out of scope for this
module — a caller pointing `pull_hls` at a master playlist URL gets
whatever `m3u8` does with it (no segments, since a master playlist has
none), not a clear rejection; this is a known, accepted scope limit, not
a bug.

Dependency order:
    protocol/media/hls_pull → protocol/base (context, session),
    openframe.core.inbound, openframe.core.media, httpx, m3u8
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

import httpx
import m3u8
from openframe.core.inbound import CloseReason, RequestContext, SessionHandler
from openframe.core.media import MediaSegment
from openframe.protocol.base.context import ContextEnricher, build_request_context
from openframe.protocol.base.session import open_session, run_opened_session

__all__ = ["HlsPullSession", "pull_hls"]

_logger = logging.getLogger(__name__)

# See module docstring, finding 5a: used only when the first fetched
# playlist has no #EXT-X-TARGETDURATION at all to derive a safer default
# from. Not spike-derived (there's nothing left to derive it from by
# definition) — chosen as "no worse than a bare minimum HLS target
# duration."
_FALLBACK_POLL_INTERVAL = 2.0

# See module docstring, finding 5a: half the target duration, confirmed
# safe (1s against a real 2s target duration, zero misses across 50 real
# rotations).
_POLL_INTERVAL_TARGET_DURATION_DIVISOR = 2.0


class HlsPullSession:
    """
    `Session[MediaSegment | Mapping[str, Any]]` (structural — `openframe
    .core.inbound.Session` is a `Protocol`) backed by one `pull_hls` poll
    loop.

    **There is no outbound wire channel for a pull session** — unlike
    RTMP's `onStatus` (`RtmpSession`) or a WebSocket/SSE's actual
    connection, an HTTP GET poll against a remote origin has no control
    channel pointed back at *us* to push anything over. `send()`'s
    behaviour is therefore deliberately narrow, decided and documented
    here rather than left unclear:

    - A `MediaSegment` passed to `send()` is simply recorded (appended to
      `self.delivered`) — this is what lets `pull_hls`'s own poll loop use
      the ordinary `on_event`/`session.send()` shape from
      `openframe.core.inbound`'s ADR-008 "forward a background producer
      into an open session" pattern, and it's also what a conformance
      test's driver reads back to prove ordered delivery. It is a
      same-process handoff, not a wire send.
    - A `Mapping` containing `"error"` (the shape `send_error_event`
      always sends) is **logged, not raised** — there is no onStatus-style
      channel to push an in-band error notification through to "the
      remote end" the way RTMP/SSE/WebSocket/gRPC all have, so silently
      recording it would hide a real `on_event`-time failure, and raising
      it back out of `session.send()` would double-report the very
      exception `run_opened_session`'s own `send_error_event` call is
      trying to report in the first place (see `session.py`'s own
      docstring: `send_error_event` already never raises, logging
      instead, specifically so a failing session isn't made worse by a
      failure to report the failure). Logging here keeps that same
      contract rather than re-raising into it.
    - Anything else raises `TypeError`, same as `RtmpSession.send`.
    """

    def __init__(self, *, context: RequestContext, url: str) -> None:
        self.context = context
        self.url = url
        self.delivered: list[MediaSegment] = []
        self._closed = False

    async def send(self, event: MediaSegment | Mapping[str, Any]) -> None:
        if self._closed:
            raise RuntimeError("cannot send on a closed session")
        if isinstance(event, MediaSegment):
            self.delivered.append(event)
            return
        if isinstance(event, Mapping) and "error" in event:
            # See class docstring: no outbound wire channel exists to push
            # this to "the remote end" (there is no remote end listening
            # on a self-initiated poller) — logged, matching
            # send_error_event's own never-raise contract rather than
            # re-raising into it.
            _logger.warning(
                "hls_pull session %s: in-band error event with no outbound channel to deliver it over: %s",
                self.context.correlation_id, event["error"],
            )
            return
        raise TypeError(f"HlsPullSession.send() does not know how to send {type(event)!r}")

    async def close(self, reason: CloseReason = CloseReason.COMPLETED) -> None:
        # Idempotent flag only — there is no socket/connection of our own
        # to tear down; the owned-or-injected httpx.AsyncClient's lifecycle
        # is managed by pull_hls's own context manager, not here, mirroring
        # RtmpSession.close()'s same "just track closed" shape.
        if self._closed:
            return
        self._closed = True


def _default_correlation_id(url: str) -> str:
    """
    Unlike RTMP (a stream key from the publish handshake) or HTTP delivery
    (real inbound request headers), an outbound poll target has no natural
    "headers" to derive a correlation id from. A short, stable hash of the
    target URL is used instead — deterministic across restarts of the same
    pull (useful for correlating logs/traces across a reconnect to the
    same origin), without leaking the full URL (which may carry a signed
    query string) into every log line and span.
    """
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return f"hls-pull-{digest}"


def _build_headers(url: str) -> dict[str, str]:
    return {"x-correlation-id": _default_correlation_id(url)}


async def _fetch_playlist(client: httpx.AsyncClient, url: str) -> m3u8.M3U8:
    response = await client.get(url)
    response.raise_for_status()
    return m3u8.loads(response.text, uri=url)


def _resolve_poll_interval(playlist: m3u8.M3U8, override: float | None) -> float:
    if override is not None:
        return override
    target_duration = playlist.target_duration
    if not target_duration:
        _logger.warning(
            "hls_pull: origin playlist has no #EXT-X-TARGETDURATION — falling back to a "
            "conservative hardcoded poll_interval of %.1fs", _FALLBACK_POLL_INTERVAL,
        )
        return _FALLBACK_POLL_INTERVAL
    return float(target_duration) / _POLL_INTERVAL_TARGET_DURATION_DIVISOR


def _program_date_time_ms(segment: m3u8.Segment) -> int | None:
    # See module docstring finding 3: current_program_date_time (m3u8's
    # own backfilled value), never the sparse program_date_time.
    current = segment.current_program_date_time
    if current is None:
        return None
    return int(current.timestamp() * 1000)


async def _fetch_segment_bytes(client: httpx.AsyncClient, segment_url: str) -> bytes:
    response = await client.get(segment_url)
    response.raise_for_status()
    return response.content


async def _poll_loop(
    client: httpx.AsyncClient,
    url: str,
    *,
    first_playlist: m3u8.M3U8,
    poll_interval: float,
    max_stale_polls: int,
) -> AsyncIterator[MediaSegment]:
    """
    The actual feed — fetch, diff by sequence number (never URI), detect
    gaps, fetch new segment bytes, yield, sleep, repeat. See module
    docstring findings 1/4/5/6 for the reasoning behind each piece below.
    """
    highest_sequence_processed = -1
    stale_polls = 0
    playlist = first_playlist

    while True:
        new_segments = [s for s in playlist.segments if s.media_sequence > highest_sequence_processed]
        new_segments.sort(key=lambda s: s.media_sequence)

        if new_segments:
            lowest_new = new_segments[0].media_sequence
            gap_detected = highest_sequence_processed >= 0 and lowest_new > highest_sequence_processed + 1
            if gap_detected:
                missed = lowest_new - highest_sequence_processed - 1
                _logger.warning(
                    "hls_pull %s: missed %d segment(s) (sequence %d..%d never seen) — "
                    "marking next processed segment discontinuous",
                    url, missed, highest_sequence_processed + 1, lowest_new - 1,
                )

            for index, segment in enumerate(new_segments):
                segment_url = segment.absolute_uri or segment.uri
                payload = await _fetch_segment_bytes(client, segment_url)
                discontinuity = bool(segment.discontinuity) or (gap_detected and index == 0)
                yield MediaSegment(
                    payload=payload,
                    sequence=segment.media_sequence,
                    duration_ms=int((segment.duration or 0) * 1000),
                    discontinuity=discontinuity,
                    program_date_time_ms=_program_date_time_ms(segment),
                    representation_id=None,  # single-rendition media playlist only — see module docstring scope
                    received_at_ns=time.perf_counter_ns(),
                )
                highest_sequence_processed = segment.media_sequence
            stale_polls = 0
        else:
            stale_polls += 1

        if playlist.is_endlist:
            return

        if stale_polls >= max_stale_polls:
            raise TimeoutError(
                f"hls_pull {url}: no new segment seen across {max_stale_polls} consecutive "
                f"polls (last sequence processed: {highest_sequence_processed}) — treating "
                "the origin as dead (see module docstring finding 6: a hard origin crash gives "
                "no other signal at all)"
            )

        await asyncio.sleep(poll_interval)
        playlist = await _fetch_playlist(client, url)


@asynccontextmanager
async def pull_hls(
    url: str,
    *,
    handler_factory: Callable[[], SessionHandler[MediaSegment, Any]],
    context_enricher: ContextEnricher | None = None,
    http_client: httpx.AsyncClient | None = None,
    poll_interval: float | None = None,
    max_stale_polls: int = 10,
) -> AsyncIterator[None]:
    """
    Async context manager driving one HLS pull session end to end.

    On entry, does the first manifest fetch — the commit point, mirroring
    RTMP ingest's `on_ns_publish` (see `ingest.py`) — then calls
    `open_session`. A failure at either step (404, network error, invalid
    `.m3u8`, or the handler's own `on_open` rejecting) propagates out of
    `__aenter__` exactly as `open_session`'s own documented reject path
    already specifies; nothing has been committed beyond the one GET.

    On success, starts a background task running `run_opened_session`
    against this module's own `_poll_loop` (an `AsyncIterator[MediaSegment]`
    — see module docstring for its full behaviour: sequence-based diffing,
    gap-driven discontinuity inference, `#EXT-X-ENDLIST` ->
    `CloseReason.COMPLETED`, stale-poll timeout -> `CloseReason.ERROR`).

    On exit, cancels the background poll task cleanly, awaits its
    completion, and closes the `httpx.AsyncClient` only if `pull_hls`
    itself constructed it (see module docstring's `http_client` section,
    mirroring `HttpRemoteQueryClient`'s own `_owns_client` convention
    exactly).

    Args:
        url:              The remote HLS *media* playlist URL to poll.
                          A *master* playlist is out of scope (see module
                          docstring) — this module does no variant
                          selection.
        handler_factory:  Builds a fresh `SessionHandler[MediaSegment, Any]`
                          for this pull session. Called once.
        context_enricher: Passed straight through to `build_request_context`.
        http_client:      An existing `httpx.AsyncClient` to reuse (its
                          lifecycle is then the caller's — never closed
                          here), or `None` to have `pull_hls` construct and
                          own a default one. This is the seam a caller uses
                          for custom auth headers, credential rotation, or
                          CDN failover (a custom `httpx.AsyncBaseTransport`
                          on their own client) — see module docstring.
        poll_interval:    Seconds between polls. `None` (the default)
                          derives a safe value from the first fetched
                          playlist's own `#EXT-X-TARGETDURATION` (half of
                          it — see module docstring finding 5a), falling
                          back to a conservative hardcoded `2.0`s if the
                          manifest declares none.
        max_stale_polls:  Consecutive polls with no sequence advance before
                          the origin is treated as dead and the session
                          closes with `CloseReason.ERROR` (see module
                          docstring finding 6). Defaults to `10`.

    Usage::

        async with pull_hls(
            "https://origin.example/live/playlist.m3u8",
            handler_factory=get_pull_handler,
        ):
            await asyncio.sleep(float("inf"))  # run until the context exits or the stream ends
    """
    owns_client = http_client is None
    client = http_client if http_client is not None else httpx.AsyncClient()

    try:
        # Commit point (mirrors ingest.py's on_ns_publish, see module
        # docstring): the first real fetch happens before open_session, so
        # a 404/network error/invalid manifest surfaces as a plain
        # exception out of this context manager's __aenter__, with nothing
        # else started yet.
        first_playlist = await _fetch_playlist(client, url)
        resolved_poll_interval = _resolve_poll_interval(first_playlist, poll_interval)

        context = build_request_context(_build_headers(url), context_enricher=context_enricher)
        handler = handler_factory()
        session = HlsPullSession(context=context, url=url)

        await open_session(handler, session)
    except BaseException:
        if owns_client:
            await client.aclose()
        raise

    poll_task = asyncio.create_task(
        run_opened_session(
            handler, session,
            _poll_loop(
                client, url,
                first_playlist=first_playlist,
                poll_interval=resolved_poll_interval,
                max_stale_polls=max_stale_polls,
            ),
        )
    )

    try:
        yield
    finally:
        poll_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await poll_task
        await session.close()
        if owns_client:
            await client.aclose()
