# openframe-protocol-media

RTMP ingest (write side), HLS pull ingest (poll side), and HLS/DASH
delivery (read side) — the first *binary-payload* transport in the
`openframe-protocol-*` family, after HTTP, gRPC, and SSE/WebSocket's
text/JSON events. Wires a
`SessionHandler` (`openframe.core.inbound`,
ADR-008) to a real RTMP publish connection via `pyrtmp`, translating raw
FLV-tagged audio/video chunks into `openframe.core.media.MediaFrame`
before a handler ever sees them, and provides `MediaFlowController` — a
transport-neutral, back-pressure-aware frame buffer any future binary
ingest transport (RTSP, SRT, WebRTC) can reuse without depending on RTMP
or `pyrtmp` at all.

Built directly on top of a real spike against `pyrtmp` 0.3.1 and real
`ffmpeg` — not a clean-room design from the RTMP spec. Several of
`pyrtmp`'s real behaviours are surprising enough that they shape this
package's structure directly: a plain `raise` from `on_ns_publish` sends
nothing to the client at all (you must hand-build an AMF0 `onStatus`
rejection yourself); `self.transport` does not exist anywhere on the
controller (the real `asyncio.Transport` is `session.writer.transport`);
and `cleanup()`, not `on_stream_closed`, is the only teardown hook
`pyrtmp` calls on *every* disconnect path, clean or crashed. See
`openframe.protocol.media.ingest`'s own module docstring for the full
write-up with the spike's concrete findings.

Install the `[ingest]` extra for the RTMP server (`pyrtmp`). `flow.py`
(`MediaFlowController`/`DropPolicy`/`FlowConfig`/`FlowStats`/
`BackPressureExceeded`/`MediaGap`) has zero third-party dependencies and
is always importable — a consumer can unit-test flow control without
installing `pyrtmp` at all.

## Server-side wiring example

```python
from openframe.protocol.media import FlowConfig, bind_ingest, create_server
from openframe.protocol.media import RtmpRegistry

registry = RtmpRegistry()
bind_ingest(
    registry, "live",
    handler_factory=get_ingest_handler,
    flow=FlowConfig(high_watermark_bytes=8 * 1024 * 1024),
)

async with create_server(
    port=1935,
    configure=lambda reg: bind_ingest(reg, "live", handler_factory=get_ingest_handler),
    on_startup=dependencies.initialise,
    on_shutdown=dependencies.shutdown,
) as server:
    await server.serve_forever()
```

A publisher then points ffmpeg at `rtmp://host:1935/live/<stream-key>`;
`<stream-key>` arrives at the handler's `on_open` as part of the session's
`RequestContext` (`x-stream-key` header), the same way an HTTP path
parameter or a gRPC metadata key would.

## Flow control — `MediaFlowController`

Transport-neutral hysteresis-based back-pressure: `offer()` is a
synchronous, non-blocking call (safe from inside a `pyrtmp` callback) that
buffers a `MediaFrame`, pausing the upstream reader at a high watermark
and resuming it at a low watermark. Under sustained overflow, the default
`DropPolicy.DROP_VIDEO_UNTIL_KEYFRAME` sheds interframes (never audio,
never a codec config frame — that invariant holds under every policy,
with zero exceptions) until a fresh keyframe arrives and the buffer has
drained back below the low watermark, then resumes with a `MediaGap`
marker so a consumer can detect and react to the gap.

## Pull ingest — HLS

Polls a remote HLS origin we do not control — a live/sliding-window media
playlist — and feeds newly-appeared segments
(`openframe.core.media.MediaSegment`) into a `SessionHandler`. The third,
architecturally distinct capability in this package: outbound HTTP
polling, rather than an inbound connection of any kind (RTMP ingest) or
serving already-produced files (HLS/DASH delivery).

Built on two real spikes this session — a real `ffmpeg`-produced live HLS
stream polled with real `httpx` + the real `m3u8` library, including a
real encoder restart and a real `SIGKILL`. Confirmed findings that shape
this module directly: `m3u8` assigns every segment a reliable
`media_sequence` itself — dedup on that, **never** on the segment URI
(confirmed: URIs can repeat across an unrelated encoder restart);
`segment.current_program_date_time` (not the sparse `program_date_time`)
is the field to read for wall-clock timestamps, since PDT tagging is
opt-in at the origin and ffmpeg's own default emits none;
`#EXT-X-DISCONTINUITY` is never automatic (confirmed by a real encoder
restart with zero tags appearing without the origin explicitly
configuring `-hls_flags +discont_start`), so this module independently
infers a discontinuity whenever its own sequence-gap detection fires;
polling too slowly silently drops segments with zero errors raised
anywhere (confirmed: 22% loss at a too-slow poll rate) — `poll_interval`
therefore defaults to half the playlist's own `#EXT-X-TARGETDURATION`;
and a hard origin crash gives no signal at all (the playlist is
byte-for-byte unchanged before and after a real kill), detected instead
via a stale-poll timeout (`max_stale_polls`, default `10`). See
`hls_pull.py`'s own module docstring for the full write-up.

Install the `[pull]` extra (`httpx` + `m3u8`). Scope: a single-rendition
*media* playlist only — a *master* playlist (multiple variant streams) is
explicitly out of scope for this release.

```python
from openframe.protocol.media import pull_hls

async with pull_hls(
    "https://origin.example/live/playlist.m3u8",
    handler_factory=get_pull_handler,
) as _:
    await asyncio.sleep(float("inf"))  # run until the stream ends or the context exits
```

`pull_hls`'s `http_client` parameter mirrors `HttpRemoteQueryClient`'s own
inject-or-build-default convention exactly (`openframe.protocol.http
.client`): pass an existing `httpx.AsyncClient` to attach custom auth
headers, rotate credentials mid-stream, or fail over across CDNs via a
custom `httpx.AsyncBaseTransport`, and its lifecycle stays the caller's;
leave it `None` and `pull_hls` builds and owns a default one for the
pull's lifetime. This injection point is the entire reason this
capability belongs in `-media` at all, rather than just pointing
`ffmpeg -i` at the remote URL directly — `ffmpeg`'s own internal HTTP
client exposes no such hook.

`HlsPullSession.send()` has no outbound wire channel to actually push
anything back over (there is no remote peer listening on a
self-initiated poller) — a `MediaSegment` is simply recorded for
same-process handoff, and an in-band error mapping is logged rather than
delivered, matching `send_error_event`'s own "never make a failing
session worse" contract. `CloseReason.CLIENT_DISCONNECT` is never used by
this transport, by design — only `COMPLETED` (a clean `#EXT-X-ENDLIST`,
or the caller exiting the context manager) and `ERROR` (the stale-poll
timeout, or an unrecoverable fetch/parse failure) apply.

## Pull ingest — DASH

The DASH counterpart to HLS pull above — polls a remote DASH origin's
live MPD and feeds newly-appeared segments
(`openframe.core.media.MediaSegment`) into a `SessionHandler`.
Architecturally identical to `pull_hls` (same outbound-HTTP-poll shape,
same `CloseReason` narrowing, same `http_client` injection point); only
the manifest format and URL-resolution logic differ. The fourth and final
planned capability in this package.

Built on a real spike this session against real `ffmpeg` DASH output
(`-f dash -seg_duration 2 -window_size 5 -use_template 1 -use_timeline
1`) and the real `mpegdash` 0.4.1 library. Confirmed findings that shape
this module directly: `mpegdash` does **zero** URL resolution of its own
(confirmed by reading its source) — `$RepresentationID$`/
`$Number[%0Nd]$`/`$Time[%0Nd]$` substitution is hand-written here; a
`SegmentTimeline`'s `<S t= d= r=>` elements must be walked per-element,
independently, never assumed to collapse to one `<S r=N-1>` (confirmed: a
real AAC audio track split into multiple `<S>` elements with different
`d` values each, from frame-boundary rounding, while the video track
happened to produce one clean element); `availabilityStartTime` and the
MPD's various ISO-8601 durations (`minimumUpdatePeriod`,
`suggestedPresentationDelay`, …) come back as raw, unparsed strings,
needing small hand-written parsers (no `isodate`-style dependency
pulled in, since real ffmpeg output only ever used the simple `PT<n>S`
duration form); and DASH actually has a *cleaner* end-of-stream signal
than HLS — a clean origin stop flips the MPD's `type` from `"dynamic"`
to `"static"`, confirmed via a real clean `ffmpeg` stop, mapped to
`CloseReason.COMPLETED` — while a hard crash (confirmed via a real kill)
leaves every live attribute untouched, caught instead via the same
`max_stale_polls` timeout heuristic as HLS pull. DASH also has *no*
per-segment discontinuity signal at all (not even an opt-in one like
HLS's `#EXT-X-DISCONTINUITY`), making this module's own
sequence-gap-inferred discontinuity the only signal available. See
`dash_pull.py`'s own module docstring for the full write-up, including
its explicit scope decisions (single `Period`, no multi-bitrate rendition
selection, no init-segment fetch, `SegmentTemplate`+`SegmentTimeline`
only).

Install the `[pull]` extra (`httpx` + `m3u8` + `mpegdash` — HLS and DASH
pull share one extra rather than splitting further).

```python
from openframe.protocol.media import pull_dash

async with pull_dash(
    "https://origin.example/live/manifest.mpd",
    handler_factory=get_pull_handler,
) as _:
    await asyncio.sleep(float("inf"))  # run until the stream ends or the context exits
```

`DashPullSession.send()`/`.close()` follow the identical design decision
and reasoning as `HlsPullSession` (no outbound wire channel for a pull
session; see above) — not re-derived in `dash_pull.py`'s own docstring.

## Delivery — HLS/DASH

The read side: serving already-produced manifest/segment files over HTTP
with Range support. Transcoding/segmenting/packaging that content is
explicitly out of scope — a separate ffmpeg-based adapter the wider
platform builds handles that; this package only serves bytes that
already exist somewhere.

Install the `[delivery]` extra (`fastapi`). `types.py`
(`MediaRequest`/`MediaObject`/`MediaRange`/`MediaProfile`/
`MediaObjectKind`/`HLS_PROFILE`/`DASH_PROFILE`/`MediaCachePolicy`/
`MediaCorsPolicy`) and `ranges.py` (`parse_range_header`/`resolve_range`/
`UnsatisfiableRangeError`) have zero third-party dependencies and are
always importable — a service's own `QueryHandler` implementation can
spell `MediaRequest`/`MediaObject` in its type annotations without
installing `fastapi` at all.

`bind_hls_delivery`/`bind_dash_delivery` wire a
`QueryHandler[MediaRequest, MediaObject]` (`openframe.core.inbound`) —
not a new source abstraction — to a real GET/HEAD route: the handler
decides whether bytes come from S3, a local mount, or a cache, and this
package never sees a repository, matching the rest of this family's own
philosophy. The handler also slices the range itself (`MediaRequest
.range` goes down, `MediaObject.served_range`/`total_size` come back up)
rather than this binder fetching a whole object and slicing it after the
fact — a deliberate memory decision, since a top-rung ABR segment can be
on the order of 15MB and a real player seeks constantly, making ranged
GETs the common case, not the exception.

```python
from fastapi import APIRouter
from openframe.protocol.media import bind_hls_delivery

router = APIRouter()
bind_hls_delivery(router, handler_dep=get_media_handler)

# Wire the router into a plain FastAPI app yourself (or via
# openframe.protocol.http.create_app(..., routers=(api_router, router))) —
# this package depends only on -base + fastapi, never on
# openframe-protocol-http, so the ingest half needs no ASGI stack at all.
```

A request for `GET /live/hd/seg-00042.ts` becomes a
`MediaRequest(path="live/hd/seg-00042.ts", kind=MediaObjectKind.SEGMENT,
content_type="video/mp2t", range=...)` passed to the handler's
`execute()`. An unrecognised suffix 404s before the handler is ever
called — content type is matched against `HLS_PROFILE`/`DASH_PROFILE`'s
own suffix table, never guessed. See `delivery.py`'s own module
docstring for the full design write-up, including exactly why a 416
response is built as a raw `Response` rather than raised as an
`HTTPException` (a real, documented bug in
`openframe-protocol-http`'s own error handler silently drops any headers
attached to a raised `HTTPException` — this package routes around it by
construction rather than depending on that code path at all).

## Status

Experimental, the first binary-payload transport in the family — built
on top of a real spike against `pyrtmp` 0.3.1 and real `ffmpeg`, not a
clean-room design. `SessionConformanceTests` (`openframe-protocol-base`)
passes against this package's `bind_ingest`/`create_server`, though the
`ordered_delivery`/`correlation_id_stable` scenarios drive it through
`session.send()`'s hand-built `onStatus` round-trip rather than real
media frames (RTMP's outbound channel for a publish-only connection has
no native event channel) — a weaker signal than for the other
transports, documented plainly rather than hidden. HLS/DASH delivery is
now implemented (`bind_hls_delivery`/`bind_dash_delivery`, the `[delivery]`
extra) with full Range support, exercised end to end against a real
FastAPI app and `httpx.AsyncClient` in `tests/test_bind_media_delivery.py`.
