"""
openframe/protocol/media/dash_pull.py
========================================
`pull_dash` — the DASH counterpart to `hls_pull.py`: polls a remote DASH
*origin* (a live MPD we do NOT control), parses it, and fetches new
segments into a `SessionHandler` (`openframe.core.inbound`, ADR-008). The
fourth and final planned capability in this package, after RTMP ingest
(push, `ingest.py`), HLS/DASH delivery (serve, `delivery.py`), and HLS
pull ingest (poll, `hls_pull.py`).

Architecturally identical to `hls_pull.py` — same outbound-HTTP-poll
shape, same `Session`/`SessionHandler` wiring, same `http_client`
injection point, same `CloseReason` narrowing (`CLIENT_DISCONNECT` never
used, see `hls_pull.py`'s own module docstring finding 7 for the full
argument, which applies here unchanged: there is no remote peer to
disconnect from when this side is the one polling). The two modules
differ only in manifest format and URL-resolution logic — this docstring
covers only what's DASH-specific; see `hls_pull.py`'s own docstring for
the shared reasoning this module doesn't re-derive.

Everything below was confirmed against a real spike this session — real
`ffmpeg` DASH output (`-f dash -seg_duration 2 -window_size 5
-use_template 1 -use_timeline 1`, a common, realistic live config),
inspected directly, plus this package's own real `bind_dash_delivery` as
the test origin — not assumed from the DASH-IF spec or `mpegdash`'s own
README.

1. **`mpegdash` (pip, 0.4.1) does zero URL resolution of any kind** —
   confirmed by reading its actual installed source
   (`mpegdash/nodes.py`): `SegmentTemplate.media`/`.initialization` and
   `S.t`/`.d`/`.r` are plain strings/ints with no substitution logic
   anywhere in the library. Confirmed live from a real `ffmpeg` run:
   `media="chunk-stream$RepresentationID$-$Number%05d$.m4s"` comes back
   from `mpegdash` byte-for-byte as that raw placeholder string. This
   module hand-writes resolution for **both** `$Number[%0Nd]$` and
   `$Time[%0Nd]$` substitution forms (`_resolve_template_url`), plus
   `$RepresentationID$` (confirmed present in real ffmpeg output for both
   `media=` and `initialization=` template strings) — a resolver that
   only handled one of `$Number$`/`$Time$` would silently do nothing
   useful against the other.

2. **A `SegmentTimeline`'s `<S t= d= r=>` elements must be walked
   per-element, independently — never assumed to be "one `<S>` with
   `r=N-1`".** Confirmed by the real spike: the AUDIO track's timeline
   split into multiple separate `<S>` elements with DIFFERENT `d` values
   each (e.g. `<S t="0" d="88064"/><S d="89088"/><S d="88064"/><S
   d="87584"/>`, from AAC frame-boundary rounding), while the VIDEO track
   happened to produce one clean `<S t="0" d="30720" r="3">`. The
   algorithm this module implements (`_expand_segment_timeline`): iterate
   every `<S>` in document order; track a running `current_time` that
   starts at the first `<S>`'s own `t` (or carries forward from the
   previous `<S>`'s computed end if a later `<S>` omits `t`, which real
   timelines do — confirmed: every `<S>` after the first omits `t` in
   real ffmpeg output); expand each `<S>`'s own `r` (if present and
   non-negative) into `r + 1` consecutive segments, each exactly that
   `<S>`'s own `d` apart, advancing `current_time` by `d` each step; also
   track a running sequential `number` counter (for `$Number$`), starting
   at `SegmentTemplate.start_number` (default `1` if absent, confirmed:
   `mpegdash` leaves it `None` when the attribute is absent, never `1`
   itself). `<S r="-1">` ("repeat until the next `<S>`'s `t`, or end of
   Period" per the DASH spec) was never produced by real ffmpeg output in
   this spike and is explicitly out of scope — treated as a single
   segment (no extra repeats), logged as a warning, not silently wrong.

3. **Everything `mpegdash` hands back untyped needs hand-written
   parsing**, confirmed by reading its source (`SegmentBase.parse`,
   `MPEGDASH.parse`: every duration/dateTime attribute is parsed with
   plain `str`, zero interpretation):
     - `availabilityStartTime`/`publishTime` (ISO-8601 datetime, e.g. a
       real `"2026-10-19T03:04:09.315Z"`) — `datetime.fromisoformat()`
       after replacing a trailing `Z` with `+00:00`, confirmed sufficient
       for real ffmpeg output (`_parse_iso8601_datetime`).
     - `minimumUpdatePeriod`/`suggestedPresentationDelay`/
       `minBufferTime`/`timeShiftBufferDepth`/`maxSegmentDuration`
       (ISO-8601 *durations*, e.g. real `"PT2S"`/`"PT4.0S"`/`"PT10.0S"`)
       — confirmed real ffmpeg output only ever used the simple
       `PT<seconds>S` form, so a small hand-written regex
       (`_parse_iso8601_duration_seconds`) handling exactly that form is
       sufficient; it raises `ValueError` with a clear message on
       anything richer (`PnYnMnD`, hour/minute components) rather than
       silently mis-parsing, since a real production origin other than
       ffmpeg may eventually emit one. No `isodate`-style third-party
       dependency pulled in for this.

4. **Live-edge formula, confirmed against real numbers**:
   `live_edge_seconds = (now_utc - availability_start_time)
   .total_seconds() - suggested_presentation_delay_seconds`
   (`_live_edge_seconds`). The subtraction is load-bearing — without it,
   the computed edge points PAST the last actually-available segment (a
   real 404 risk against a stricter origin than this spike's own
   disk-backed test server). This module does not use it to further trim
   the first fetched manifest's own segment list, though — confirmed by
   the spike, `ffmpeg -window_size N` already prunes the `SegmentTimeline`
   itself down to a live sliding window before this module ever sees it,
   exactly as HLS's own sliding-window media playlist does for
   `hls_pull.py`. So "starting near the live edge" falls out naturally
   from processing everything in the first fetched manifest (the same
   choice `hls_pull.py` makes, see its own docstring finding 1) rather
   than needing a second filtering pass; `_live_edge_seconds` is
   retained because it's independently useful for logging/diagnostics
   (how far behind live is this session right now) and is exercised
   directly by this module's own unit tests, confirmed via real captured
   `availabilityStartTime` values across 8 real fetches to land inside
   the newest available segment's actual time window.

5. **Manifest re-fetch cadence**: `minimumUpdatePeriod` (confirmed equal
   to `-seg_duration` in real output, i.e. `"PT2S"` for a 2s segment
   duration) is the polling interval to use directly — the DASH-native
   analogue of HLS's `#EXT-X-TARGETDURATION`-derived interval
   (`_resolve_poll_interval`). Unlike HLS pull's own halving of the
   target duration, `minimumUpdatePeriod` is already the origin's own
   stated "poll me this often" value (not a segment duration needing a
   safety margin applied), so it's used as-is; `poll_interval=None` falls
   back to a conservative hardcoded `2.0`s only if the manifest declares
   none at all or it fails to parse — same "no worse than a bare minimum"
   reasoning as `hls_pull.py`'s own `_FALLBACK_POLL_INTERVAL`, since
   there's nothing left to derive a better default from.

   **Diffing between fetches is NOT a raw XML/text diff.** Confirmed: a
   representation's `<S r=N>` can re-shape across polls (a segment
   rolling in/out of the live window can re-split one `<S>` into two, or
   merge two into one) even though only one segment actually changed.
   Every poll fully re-runs `_expand_segment_timeline` into concrete
   `(number, time, duration)` tuples and diffs those against a
   per-representation `highest_number_processed` watermark — never the
   raw `<S>` elements themselves (see module docstring finding 2's
   algorithm, re-run from scratch every poll, never incrementally
   patched).

6. **DASH DOES have a clean, reliable end-of-stream signal** — confirmed
   via a real clean `ffmpeg` stop (`SIGINT`): the MPD root's `type`
   attribute flips from `"dynamic"` to `"static"`, `
   mediaPresentationDuration` is set, and every live-only attribute
   (`availabilityStartTime`, `minimumUpdatePeriod`,
   `suggestedPresentationDelay`, `timeShiftBufferDepth`) is dropped from
   the root entirely. `mpd.type == "static"` (re-checked on every poll)
   maps to `CloseReason.COMPLETED` — the direct DASH-native analogue of
   HLS's `#EXT-X-ENDLIST`, and a genuinely *stronger* signal than HLS had
   available (HLS pull's own module docstring finding 6 notes a hard
   kill is indistinguishable from a live stream by playlist content
   alone; the same is true here for DASH, see finding 7, but the clean
   case is unambiguous).

7. **A hard origin crash gives NO manifest signal at all** — same
   "confirmed, not assumed" treatment as `hls_pull.py`'s own SIGKILL
   finding: `type` stays `"dynamic"`, every live attribute stays intact,
   exactly as if the stream were still live, plus a dangling incomplete
   segment file left behind. The same `max_stale_polls`-style
   no-sequence-advance timeout heuristic as `hls_pull.py` is required
   here too, for `CloseReason.ERROR` — there is no DASH-native signal to
   distinguish "stalled" from "dead" in this case, mirroring HLS pull's
   own gap exactly.

8. **DASH has no per-segment discontinuity signal of any kind — not even
   an opt-in one.** Unlike HLS's `#EXT-X-DISCONTINUITY` (origin-settable,
   if weak — see `hls_pull.py`'s own finding 4), the DASH spec has no
   equivalent tag anywhere in `SegmentTimeline`/`SegmentTemplate`. This
   module's own sequence-gap-inferred discontinuity (the same mechanism
   `hls_pull.py` uses as a *robustness addition* on top of the origin's
   own flag) is therefore the ONLY discontinuity signal available here at
   all — a stronger justification for the same mechanism than HLS pull
   ever had.

**Scope decisions** (explicit, not bugs — mirroring `hls_pull.py`'s own
master-playlist exclusion):
  - **Single `Period` only.** A multi-`Period` MPD (ad insertion,
    programme boundaries) is out of scope; only `mpd.periods[0]` is read.
  - **No multi-bitrate rendition selection.** An `AdaptationSet` with
    more than one `Representation` (multi-bitrate ABR) has no
    `rendition_selector` callback here — the first `Representation` is
    used, logged, exactly as `hls_pull.py` leaves a master-playlist's
    variant selection out of scope. Multiple *`AdaptationSet`s* (e.g.
    separate video + audio, confirmed the common case even for a
    "simple" real ffmpeg stream) ARE handled naturally — each yields its
    own independently sequenced/watermarked stream of `MediaSegment`s,
    distinguished by `representation_id`. This is not rendition
    *selection* (choosing one of several alternatives covering the same
    content) — it's simply walking every track the origin actually
    offers, the DASH equivalent of "fetch both the video and the audio."
  - **No init-segment (`initialization=`) fetch.** `$RepresentationID$`
    substitution is implemented for both `media=` and `initialization=`
    template strings (confirmed both appear in real ffmpeg output), but
    this module never fetches the init segment itself — `MediaSegment`
    has no init-segment concept, and demuxing/decoding a CMAF track
    (which needs the init segment) is explicitly a downstream adapter's
    job (`openframe-adapters-media-ffmpeg`, not yet built), the same
    "transcoding stays out of this package" boundary `MediaSegment`'s own
    docstring and ADR-009 already draw for `hls_pull.py`.
  - **`SegmentList`/`SegmentBase` representations are skipped, logged.**
    Only `SegmentTemplate` + `SegmentTimeline` (confirmed real ffmpeg
    output's own shape with `-use_template 1 -use_timeline 1`) is
    resolved. A caller pointing `pull_dash` at an MPD using `SegmentList`
    or plain `SegmentBase` gets no segments from that representation, not
    a crash — a known, accepted scope limit for the same reason
    `hls_pull.py` accepts "no segments from a master playlist" as its own
    scope limit rather than raising.
  - **A production-realism trap this module deliberately does NOT
    accommodate**: the real spike's own disk-backed test handler (built
    with `-remove_at_exit 0`) kept old segment files reachable on disk
    even after they rolled off the live `SegmentTimeline` — a
    `remove_at_exit 0` artifact, not something a real production origin
    (S3 lifecycle policy, CDN TTL, a real packager that actually deletes)
    will do. This module never retains/reuses a resolved URL for a
    segment number that has since rolled off the current
    `SegmentTimeline` on a later fetch — `highest_number_processed` only
    ever moves forward, and a representation's current window is the only
    thing ever considered fetchable.

Dependency order:
    protocol/media/dash_pull → protocol/base (context, session),
    openframe.core.inbound, openframe.core.media, httpx, mpegdash
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

import httpx
from mpegdash.nodes import MPEGDASH, Representation, SegmentTemplate
from mpegdash.parser import MPEGDASHParser
from openframe.core.inbound import CloseReason, RequestContext, SessionHandler
from openframe.core.media import MediaSegment
from openframe.protocol.base.context import ContextEnricher, build_request_context
from openframe.protocol.base.session import open_session, run_opened_session

__all__ = ["DashPullSession", "pull_dash"]

_logger = logging.getLogger(__name__)

# See module docstring finding 5: used only when the manifest declares no
# minimumUpdatePeriod at all, or it fails to parse. Not spike-derived
# (nothing left to derive a better default from) -- mirrors hls_pull.py's
# own _FALLBACK_POLL_INTERVAL exactly, same reasoning.
_FALLBACK_POLL_INTERVAL = 2.0

# See module docstring finding 3: real ffmpeg output only ever used the
# simple PT<seconds>S form (e.g. "PT2S", "PT4.0S") -- this is NOT a
# general ISO-8601 duration parser, and deliberately raises ValueError on
# anything richer (PnYnMnD, hour/minute components) rather than silently
# mis-parsing.
_SIMPLE_DURATION_RE = re.compile(r"^PT(\d+(?:\.\d+)?)S$")

# See module docstring finding 1: $Number%0Nd$ / $Time%0Nd$ zero-padded
# formatted substitution.
_NUMBER_FORMATTED_RE = re.compile(r"\$Number%0(\d+)d\$")
_TIME_FORMATTED_RE = re.compile(r"\$Time%0(\d+)d\$")


def _parse_iso8601_datetime(value: str) -> datetime:
    """
    See module docstring finding 3. `mpegdash` hands back
    availabilityStartTime/publishTime as a raw string -- confirmed real
    ffmpeg output always uses a plain ISO-8601 datetime with a trailing
    `Z` (e.g. "2026-10-19T03:04:09.315Z"), which `datetime.fromisoformat`
    doesn't accept directly pre-3.11 oddities aside; replacing the
    trailing `Z` with `+00:00` was confirmed sufficient.
    """
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(normalized)


def _parse_iso8601_duration_seconds(value: str) -> float:
    """See module docstring finding 3 and `_SIMPLE_DURATION_RE`'s own comment above."""
    match = _SIMPLE_DURATION_RE.match(value.strip())
    if not match:
        raise ValueError(
            f"dash_pull: {value!r} is not a simple PT<seconds>S ISO-8601 duration -- "
            "richer forms (PnYnMnD, hour/minute components) are out of scope (see module "
            "docstring finding 3), no isodate-style dependency is pulled in for this"
        )
    return float(match.group(1))


def _live_edge_seconds(availability_start: datetime, suggested_presentation_delay_seconds: float) -> float:
    """See module docstring finding 4 -- the subtraction is load-bearing."""
    now = datetime.now(UTC)
    return (now - availability_start).total_seconds() - suggested_presentation_delay_seconds


@dataclass(frozen=True, slots=True)
class _SegmentEntry:
    """One concrete, resolved segment position -- the output of `_expand_segment_timeline`."""

    number: int
    time: int  # timescale units, NOT seconds/ms -- see module docstring finding 2
    duration: int  # timescale units, NOT seconds/ms


def _expand_segment_timeline(template: SegmentTemplate) -> list[_SegmentEntry]:
    """
    See module docstring finding 2 for the full algorithm and the real
    audio-track spike finding that makes "one `<S>` with `r=N-1`" an
    unsafe assumption. Re-run in full on every poll (see finding 5) --
    never incrementally patched against a previous result.
    """
    timelines = template.segment_timelines or []
    if not timelines:
        return []
    s_elements = timelines[0].Ss or []
    start_number = template.start_number if template.start_number is not None else 1

    entries: list[_SegmentEntry] = []
    number = start_number
    current_time: int | None = None
    for s in s_elements:
        if s.t is not None:
            current_time = s.t
        if current_time is None:
            raise ValueError(
                "dash_pull: first <S> in a SegmentTimeline has no t= attribute and there is "
                "no previous <S> to carry forward a running time from -- cannot resolve"
            )
        if s.r is not None and s.r < 0:
            # See module docstring finding 2: r=-1 (open-ended repeat) never observed in real
            # ffmpeg output -- out of scope, treated as a single segment, not silently wrong.
            _logger.warning(
                "dash_pull: <S r=%d> (open-ended repeat) is out of scope -- treating as a "
                "single segment, not expanding further",
                s.r,
            )
        repeat_count = s.r if (s.r is not None and s.r >= 0) else 0
        duration = s.d
        for _ in range(repeat_count + 1):
            entries.append(_SegmentEntry(number=number, time=current_time, duration=duration))
            current_time += duration
            number += 1
    return entries


def _resolve_template_url(template_str: str, *, representation_id: str, number: int, time: int) -> str:
    """
    See module docstring finding 1: `mpegdash` does zero substitution of
    its own -- this hand-writes `$RepresentationID$`/`$Number[%0Nd]$`/
    `$Time[%0Nd]$`. `$$` (the spec's own escape for a literal `$`) is
    resolved last, after every other substitution, matching the spec's
    own evaluation order.
    """
    resolved = template_str.replace("$RepresentationID$", representation_id)
    resolved = _NUMBER_FORMATTED_RE.sub(lambda m: str(number).zfill(int(m.group(1))), resolved)
    resolved = resolved.replace("$Number$", str(number))
    resolved = _TIME_FORMATTED_RE.sub(lambda m: str(time).zfill(int(m.group(1))), resolved)
    resolved = resolved.replace("$Time$", str(time))
    return resolved.replace("$$", "$")


def _segment_template_for(representation: Representation, adaptation_set: Any) -> SegmentTemplate | None:
    """A Representation's own SegmentTemplate, falling back to its parent AdaptationSet's (DASH-spec inheritance)."""
    own = representation.segment_templates or []
    if own:
        return own[0]
    inherited = adaptation_set.segment_templates or []
    return inherited[0] if inherited else None


def _iter_representations(mpd: MPEGDASH) -> list[tuple[str, SegmentTemplate]]:
    """
    See module docstring's scope section: single Period only
    (`mpd.periods[0]`), first Representation per AdaptationSet only (no
    multi-bitrate selection), every AdaptationSet handled (video + audio
    both, confirmed the common real-ffmpeg case), SegmentList/SegmentBase
    representations skipped with a warning (SegmentTemplate +
    SegmentTimeline only).
    """
    periods = mpd.periods or []
    if not periods:
        return []
    results: list[tuple[str, SegmentTemplate]] = []
    for adaptation_set in periods[0].adaptation_sets or []:
        representations = adaptation_set.representations or []
        if not representations:
            continue
        if len(representations) > 1:
            _logger.warning(
                "dash_pull: AdaptationSet id=%s has %d Representations (multi-bitrate) -- "
                "no rendition-selection logic in this module's scope (mirrors hls_pull.py's "
                "own master-playlist scope limit) -- using the first Representation only "
                "(id=%s)",
                adaptation_set.id, len(representations), representations[0].id,
            )
        representation = representations[0]
        template = _segment_template_for(representation, adaptation_set)
        if template is None or not template.media:
            _logger.warning(
                "dash_pull: Representation id=%s has no SegmentTemplate+SegmentTimeline "
                "(SegmentList/SegmentBase representations are out of scope) -- skipping",
                representation.id,
            )
            continue
        results.append((representation.id, template))
    return results


def _program_date_time_ms(availability_start: datetime | None, entry: _SegmentEntry, timescale: int) -> int | None:
    """
    DASH's equivalent of HLS pull's `current_program_date_time` -- see
    module docstring's comparison to HLS: `availabilityStartTime` is a
    *required* live-profile MPD attribute, unlike HLS's opt-in
    `#EXT-X-PROGRAM-DATE-TIME` tagging, so this is more reliably
    available here than for `hls_pull.py`. `None` only when the origin's
    `availabilityStartTime` itself failed to parse or was absent (a
    static/VOD-only MPD has no live-profile requirement to provide one).
    """
    if availability_start is None:
        return None
    seconds_offset = entry.time / timescale
    return int((availability_start.timestamp() + seconds_offset) * 1000)


class DashPullSession:
    """
    `Session[MediaSegment | Mapping[str, Any]]` backed by one `pull_dash`
    poll loop -- the DASH counterpart to `HlsPullSession`
    (`hls_pull.py`). Identical `send()`/`close()` design decision and
    reasoning as `HlsPullSession` (no outbound wire channel for a pull
    session; a `MediaSegment` is recorded, a `Mapping` error is logged
    not raised, anything else raises `TypeError`) -- see
    `HlsPullSession`'s own class docstring in `hls_pull.py` for the full
    argument; not re-derived here.
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
            # See HlsPullSession's own docstring in hls_pull.py: logged, not raised -- no
            # outbound wire channel exists to push this to "the remote end" for a
            # self-initiated poller, and send_error_event already never raises itself.
            _logger.warning(
                "dash_pull session %s: in-band error event with no outbound channel to deliver it over: %s",
                self.context.correlation_id, event["error"],
            )
            return
        raise TypeError(f"DashPullSession.send() does not know how to send {type(event)!r}")

    async def close(self, reason: CloseReason = CloseReason.COMPLETED) -> None:
        # Idempotent flag only -- see HlsPullSession.close()'s own docstring, identical reasoning.
        if self._closed:
            return
        self._closed = True


def _default_correlation_id(url: str) -> str:
    """See hls_pull.py's own `_default_correlation_id` -- identical reasoning, `dash-pull-` prefix."""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return f"dash-pull-{digest}"


def _build_headers(url: str) -> dict[str, str]:
    return {"x-correlation-id": _default_correlation_id(url)}


async def _fetch_manifest(client: httpx.AsyncClient, url: str) -> MPEGDASH:
    response = await client.get(url)
    response.raise_for_status()
    # MPEGDASHParser.parse treats any string containing "<MPD" as literal XML rather than a
    # URL/file path to re-open -- confirmed by reading its source (parser.py) -- so passing
    # the already-fetched response text (which always contains "<MPD") never triggers a
    # second, uncontrolled fetch of its own.
    return MPEGDASHParser.parse(response.text)


def _resolve_poll_interval(mpd: MPEGDASH, override: float | None) -> float:
    if override is not None:
        return override
    if not mpd.minimum_update_period:
        _logger.warning(
            "dash_pull: origin MPD has no minimumUpdatePeriod -- falling back to a "
            "conservative hardcoded poll_interval of %.1fs", _FALLBACK_POLL_INTERVAL,
        )
        return _FALLBACK_POLL_INTERVAL
    try:
        return _parse_iso8601_duration_seconds(mpd.minimum_update_period)
    except ValueError:
        _logger.warning(
            "dash_pull: could not parse minimumUpdatePeriod %r -- falling back to a "
            "conservative hardcoded poll_interval of %.1fs",
            mpd.minimum_update_period, _FALLBACK_POLL_INTERVAL,
        )
        return _FALLBACK_POLL_INTERVAL


async def _fetch_segment_bytes(client: httpx.AsyncClient, segment_url: str) -> bytes:
    response = await client.get(segment_url)
    response.raise_for_status()
    return response.content


async def _poll_loop(
    client: httpx.AsyncClient,
    url: str,
    *,
    first_mpd: MPEGDASH,
    poll_interval: float,
    max_stale_polls: int,
) -> AsyncIterator[MediaSegment]:
    """
    The actual feed -- fetch, fully re-expand every representation's
    SegmentTimeline, diff by per-representation watermark (never a raw
    `<S>`/XML diff), detect gaps, fetch new segment bytes, yield, sleep,
    repeat. See module docstring findings 2/5/6/7/8 for the reasoning
    behind each piece below.
    """
    highest_number_processed: dict[str, int] = {}
    stale_polls = 0
    mpd = first_mpd

    while True:
        availability_start: datetime | None = None
        if mpd.availability_start_time:
            try:
                availability_start = _parse_iso8601_datetime(mpd.availability_start_time)
            except ValueError:
                _logger.warning(
                    "dash_pull %s: could not parse availabilityStartTime %r",
                    url, mpd.availability_start_time,
                )

        any_new = False
        for representation_id, template in _iter_representations(mpd):
            timescale = template.timescale or 1
            entries = _expand_segment_timeline(template)
            watermark = highest_number_processed.get(representation_id, -1)
            new_entries = sorted((e for e in entries if e.number > watermark), key=lambda e: e.number)
            if not new_entries:
                continue

            lowest_new = new_entries[0].number
            gap_detected = watermark >= 0 and lowest_new > watermark + 1
            if gap_detected:
                missed = lowest_new - watermark - 1
                _logger.warning(
                    "dash_pull %s representation %s: missed %d segment(s) (number %d..%d "
                    "never seen) -- marking next processed segment discontinuous",
                    url, representation_id, missed, watermark + 1, lowest_new - 1,
                )

            for index, entry in enumerate(new_entries):
                segment_url = urljoin(
                    url,
                    _resolve_template_url(
                        template.media, representation_id=representation_id,
                        number=entry.number, time=entry.time,
                    ),
                )
                payload = await _fetch_segment_bytes(client, segment_url)
                # See module docstring finding 8: DASH has no origin-side discontinuity
                # signal at all (unlike HLS's weak-but-present #EXT-X-DISCONTINUITY) -- this
                # self-inferred gap signal is the ONLY one available here.
                discontinuity = gap_detected and index == 0
                yield MediaSegment(
                    payload=payload,
                    sequence=entry.number,
                    duration_ms=round(entry.duration * 1000 / timescale),
                    discontinuity=discontinuity,
                    program_date_time_ms=_program_date_time_ms(availability_start, entry, timescale),
                    representation_id=representation_id,
                    received_at_ns=time.perf_counter_ns(),
                )
                highest_number_processed[representation_id] = entry.number
            any_new = True

        stale_polls = 0 if any_new else stale_polls + 1

        if mpd.type == "static":
            return

        if stale_polls >= max_stale_polls:
            raise TimeoutError(
                f"dash_pull {url}: no new segment seen across {max_stale_polls} consecutive "
                f"polls (watermarks: {highest_number_processed}) -- treating the origin as "
                "dead (see module docstring finding 7: a hard origin crash gives no other "
                "signal at all)"
            )

        await asyncio.sleep(poll_interval)
        mpd = await _fetch_manifest(client, url)


@asynccontextmanager
async def pull_dash(
    url: str,
    *,
    handler_factory: Callable[[], SessionHandler[MediaSegment, Any]],
    context_enricher: ContextEnricher | None = None,
    http_client: httpx.AsyncClient | None = None,
    poll_interval: float | None = None,
    max_stale_polls: int = 10,
) -> AsyncIterator[None]:
    """
    Async context manager driving one DASH pull session end to end --
    identical shape to `hls_pull.py`'s own `pull_hls`; see its docstring
    for the full lifecycle reasoning (commit-point-before-`open_session`,
    background `run_opened_session` task, clean cancellation/teardown on
    exit, `http_client` ownership). Only the DASH-specific pieces are
    called out below.

    Args:
        url:              The remote DASH MPD URL to poll.
        handler_factory:  Builds a fresh `SessionHandler[MediaSegment, Any]`
                          for this pull session. Called once.
        context_enricher: Passed straight through to `build_request_context`.
        http_client:      An existing `httpx.AsyncClient` to reuse (its
                          lifecycle is then the caller's — never closed
                          here), or `None` to have `pull_dash` construct
                          and own a default one. Same injection point and
                          justification as `pull_hls`'s own `http_client`
                          — see `hls_pull.py`'s own module docstring.
        poll_interval:    Seconds between polls. `None` (the default)
                          derives a value directly from the first fetched
                          MPD's own `minimumUpdatePeriod` (see module
                          docstring finding 5), falling back to a
                          conservative hardcoded `2.0`s if the MPD
                          declares none or it fails to parse.
        max_stale_polls:  Consecutive polls with no sequence advance (on
                          ANY representation) before the origin is treated
                          as dead and the session closes with
                          `CloseReason.ERROR` (see module docstring
                          finding 7). Defaults to `10`.

    Usage::

        async with pull_dash(
            "https://origin.example/live/manifest.mpd",
            handler_factory=get_pull_handler,
        ):
            await asyncio.sleep(float("inf"))  # run until the context exits or the stream ends
    """
    owns_client = http_client is None
    client = http_client if http_client is not None else httpx.AsyncClient()

    try:
        # Commit point (mirrors pull_hls's own, see hls_pull.py's docstring): the first real
        # fetch happens before open_session, so a 404/network error/invalid MPD surfaces as a
        # plain exception out of this context manager's __aenter__, nothing else started yet.
        first_mpd = await _fetch_manifest(client, url)
        resolved_poll_interval = _resolve_poll_interval(first_mpd, poll_interval)

        context = build_request_context(_build_headers(url), context_enricher=context_enricher)
        handler = handler_factory()
        session = DashPullSession(context=context, url=url)

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
                first_mpd=first_mpd,
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
