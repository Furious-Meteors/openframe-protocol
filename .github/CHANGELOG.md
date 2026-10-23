# Changelog

All notable changes to this project will be documented in this file.

---

## [openframe-protocol-http 0.11.1] - 2026-10-19

### Fixed — `install_error_handlers`'s `HTTPException` handler dropped `exc.headers`

Flagged twice during the `openframe-protocol-media` build: `_handle_http_exception` rebuilt a raised
`HTTPException` into the shared `{"error": {...}}` envelope using only `status_code`/`content` on the
`JSONResponse`, silently dropping `exc.headers` — unlike FastAPI's own default `HTTPException` handler,
which does propagate them. No current binder in this repo happened to raise an `HTTPException` with
headers attached, so this went unnoticed in practice until `openframe-protocol-media`'s delivery binder
specifically had to route around it (returning a raw `Response` for a 416 instead of raising
`HTTPException`, to avoid losing the required `Content-Range` header) rather than depend on it being
fixed. Now passes `headers=exc.headers` through — `None` is a safe no-op (confirmed: both
`HTTPException.headers` and `JSONResponse`'s own `headers` parameter already treat `None` as "nothing
extra"), so this is purely additive for every existing caller. 2 new regression tests.

---

## [openframe-protocol-media 0.4.0] - 2026-10-19

### Added — DASH pull ingest, the fourth and final planned capability, completing the set alongside RTMP ingest (push), HLS/DASH delivery (serve), and HLS pull ingest (poll)

`pull_dash`/`DashPullSession` (`dash_pull.py`, same `[pull]` extra as HLS pull, now also pulling
in `mpegdash>=0.4`) — the DASH counterpart to `hls_pull.py`: polls a remote DASH *origin* we do
not control (a live MPD), parses it, and feeds newly-appeared segments
(`openframe.core.media.MediaSegment`) into a `SessionHandler` (ADR-008). Architecturally
identical to `pull_hls` (same outbound-HTTP-poll shape, same `Session`/`SessionHandler` wiring,
same `http_client` injection point, same `CloseReason.CLIENT_DISCONNECT`-never-used narrowing) —
only the manifest format and URL-resolution logic differ.

Built on a real spike this session against real `ffmpeg` DASH output (`-f dash -seg_duration 2
-window_size 5 -use_template 1 -use_timeline 1`) and the real `mpegdash` 0.4.1 library — not a
clean-room design from the DASH-IF spec or `mpegdash`'s own README:

- **`mpegdash` does zero URL resolution of any kind** — confirmed by reading its actual
  installed source (`mpegdash/nodes.py`): `SegmentTemplate.media`/`.initialization` and
  `S.t`/`.d`/`.r` are plain strings/ints, no substitution logic anywhere in the library.
  Confirmed live: `media="chunk-stream$RepresentationID$-$Number%05d$.m4s"` comes back as that
  raw placeholder string. `pull_dash` hand-writes resolution for both `$Number[%0Nd]$` and
  `$Time[%0Nd]$` forms, plus `$RepresentationID$` — a resolver handling only one of
  `$Number$`/`$Time$` would silently do nothing useful against the other.
- **A `SegmentTimeline`'s `<S t= d= r=>` elements must be walked per-element, independently —
  never assumed to be "one `<S>` with `r=N-1`".** Confirmed by the real spike: the AUDIO track's
  timeline split into multiple separate `<S>` elements with DIFFERENT `d` values each (AAC
  frame-boundary rounding), while the VIDEO track happened to produce one clean `<S t= r=N-1>`.
  The implemented algorithm iterates every `<S>` in document order, carrying a running
  `current_time` forward across elements that omit `t=` (confirmed: every `<S>` after the first
  omits it in real output) and a running `number` counter seeded from `SegmentTemplate
  .startNumber` (default `1`), expanding each `<S>`'s own `r` into `r + 1` segments spaced by
  that `<S>`'s own `d`. Verified byte-for-byte against the real spike's own ffmpeg output with
  zero mismatches.
- **Everything `mpegdash` hands back untyped needs hand-written parsing** — confirmed: every
  duration/dateTime MPD attribute (`availabilityStartTime`, `minimumUpdatePeriod`,
  `suggestedPresentationDelay`, …) is parsed by `mpegdash` with plain `str`, zero interpretation.
  A small `datetime.fromisoformat`-based parser (after normalising a trailing `Z`) handles the
  datetime fields; a narrow hand-rolled regex handles the simple `PT<seconds>S` duration form
  real ffmpeg output exclusively used, raising clearly on anything richer rather than silently
  mis-parsing. No `isodate`-style third-party dependency pulled in for this.
- **Live-edge formula, confirmed against real numbers**: `live_edge_seconds = (now_utc -
  availability_start_time).total_seconds() - suggested_presentation_delay_seconds` — the
  subtraction is load-bearing (without it, the computed edge points past the last actually
  available segment). In practice, "starting near the live edge" falls out naturally from
  processing everything in the first fetched MPD, since `ffmpeg -window_size N` already prunes
  the `SegmentTimeline` itself to a live sliding window before `pull_dash` ever sees it — the
  same reasoning `hls_pull.py` already applies to HLS's own sliding-window media playlist.
- **DASH DOES have a clean, reliable end-of-stream signal — a correction to the assumption DASH
  was weaker here than HLS.** A real clean `ffmpeg` stop flips the MPD root's `type` attribute
  from `"dynamic"` to `"static"` and drops every live-only attribute from the root. `pull_dash`
  maps `mpd.type == "static"` (re-checked on every poll) to `CloseReason.COMPLETED` — the direct
  DASH-native analogue of HLS's `#EXT-X-ENDLIST`. A real hard kill, confirmed separately, leaves
  the manifest completely untouched (`type="dynamic"`, every live attribute intact) — caught
  instead via the same `max_stale_polls`-style no-advance timeout heuristic as `hls_pull.py`,
  for `CloseReason.ERROR`.
- **DASH has no per-segment discontinuity signal of any kind — not even an opt-in one**, unlike
  HLS's `#EXT-X-DISCONTINUITY`. `pull_dash`'s own sequence-gap-inferred discontinuity (the same
  mechanism `hls_pull.py` uses as a robustness addition on top of the origin's own flag) is
  therefore the ONLY discontinuity signal available here at all.
- **Manifest re-fetch cadence derives directly from `minimumUpdatePeriod`** (confirmed equal to
  `-seg_duration` in real output), used as-is rather than halved the way `hls_pull.py` halves
  `#EXT-X-TARGETDURATION` — the origin already states its own "poll me this often" value here.
  Diffing between fetches fully re-runs the `<S>`-expansion algorithm into concrete
  `(number, time, duration)` tuples every poll and diffs those against a per-representation
  `highest_number_processed` watermark — never a raw XML/`<S>`-element diff, confirmed necessary
  since an `r=N` timeline can re-shape across polls (one segment rolling in/out can re-split or
  re-merge `<S>` elements) even though only one segment actually changed.

**Scope decisions** (explicit, not bugs — mirroring `hls_pull.py`'s own master-playlist
exclusion): single `Period` only; no multi-bitrate rendition selection (first `Representation`
per `AdaptationSet` used, logged) while every `AdaptationSet` IS handled naturally (video + audio
both, confirmed the common real-ffmpeg case, each independently sequenced via
`representation_id`); no init-segment (`initialization=`) fetch, since demuxing a CMAF track is
explicitly a downstream adapter's job, not this package's; `SegmentList`/`SegmentBase`
representations are skipped with a warning (`SegmentTemplate` + `SegmentTimeline` only).

Verified against: 14 pure unit tests of the resolution algorithm (including the audio-track
independent-`<S>`-elements case against hand-constructed fixtures), 5 controlled gap-detection/
stale-timeout/clean-close tests, 6 conformance tests against a real local server built on this
package's own `bind_dash_delivery`, and 1 real end-to-end spike test against real `ffmpeg` DASH
output over a ~40s live window with a real clean stop — all passing, zero regressions against
the existing 259 passed / 2 xfailed monorepo baseline.

---

## [openframe-protocol-media 0.3.0] - 2026-10-19

### Added — HLS pull ingest, a third architecturally distinct capability alongside RTMP ingest (push) and HLS/DASH delivery (serve)

`pull_hls`/`HlsPullSession` (`hls_pull.py`, needs the new `[pull]` extra: `httpx>=0.27` +
`m3u8>=6.0`) — polls a remote HLS *origin* we do not control (a live/sliding-window media
playlist), parses it, and feeds newly-appeared segments
(`openframe.core.media.MediaSegment`, new in `openframe-core` 3.10.0, the dependency floor this
release bumps to) into a `SessionHandler` (ADR-008). Outbound HTTP polling, not an inbound
connection of any kind — the first capability in this package built on a self-initiated
background loop rather than something connecting to us.

Built on two real spikes this session against real `ffmpeg` HLS output and the real `m3u8`
6.0.0 + `httpx` 0.28.1 libraries, including a real encoder restart and a real `SIGKILL` of the
origin — not a clean-room design from the HLS spec:

- **Dedup/ordering key is `media_sequence`, never the segment URI.** `m3u8` assigns
  `segment.media_sequence` to every segment itself (confirmed by reading its source), reliable
  across 50 real rotations with zero misses or duplicates. URIs are explicitly unsafe to dedup
  on — confirmed real: an unrelated encoder/stream restart can reset filename counters, so two
  different segments can share a URI.
- **`m3u8` has no `reload()`/diff method** — this module fetches playlist text itself via an
  injected/owned `httpx.AsyncClient` (never `m3u8.load(url)`, which would own its own HTTP
  stack), parses fresh each poll with `m3u8.loads()`, and diffs against a locally-tracked
  highest-sequence-processed watermark itself.
- **`segment.current_program_date_time`, not the sparse `segment.program_date_time`, is the
  field to read.** Confirmed: ffmpeg's own default `-f hls` emits no `#EXT-X-PROGRAM-DATE-TIME`
  tags at all; `current_program_date_time` is `m3u8`'s own backfilled value and was populated far
  more often in the spike.
- **Discontinuity must be inferred, not just read off the origin's tag.** Confirmed by actually
  restarting a real encoder with a resolution/GOP change into the same stream: zero
  `#EXT-X-DISCONTINUITY` tags appeared without the origin explicitly configuring `ffmpeg`'s own
  `-hls_flags +discont_start`. `pull_hls` independently infers a discontinuity whenever its own
  sequence-gap detection fires, OR'd with the origin's own flag.
- **Polling too slowly silently drops segments with zero errors raised anywhere** — confirmed:
  polling at roughly the full sliding-window duration (not the target duration) missed 22% of
  real segments, the only symptom being `media_sequence` jumping by more than 1 between polls.
  `poll_interval=None` therefore derives a default from the first fetched playlist's own
  `#EXT-X-TARGETDURATION` (half of it — confirmed safe: 1s against a real 2s target duration,
  zero misses across 50 rotations), falling back to a conservative hardcoded 2.0s only if the
  manifest declares no target duration at all.
- **A clean stream end and a hard origin crash are two completely different signals, needing two
  completely different detections.** `#EXT-X-ENDLIST` reliably means the stream ended on purpose
  -> `CloseReason.COMPLETED`. A real `SIGKILL`-equivalent against a real origin produced *no*
  signal whatsoever — the raw playlist text was confirmed byte-for-byte identical before and
  after the kill. `max_stale_polls` (default 10) counts consecutive polls with no sequence
  advance; exceeding it raises inside the poll loop, which `run_opened_session` maps to
  `CloseReason.ERROR`.
- **`SessionHandler`'s own contract is mechanically reusable for a self-initiated poll loop** —
  confirmed by direct design research this session: ADR-008's own "forward a Kafka consumer into
  an open session" example is the same shape. One deliberate narrowing: `CloseReason
  .CLIENT_DISCONNECT` is never used by this transport — there is no remote peer to disconnect
  from when this side is the one polling.
- **`HlsPullSession.send()` has no outbound wire channel at all** (unlike RTMP's `onStatus`) — a
  `MediaSegment` is recorded for same-process handoff, and an in-band error mapping is logged
  rather than delivered, matching `send_error_event`'s own documented "never make a failing
  session worse" contract rather than re-raising into it.
- `http_client` mirrors `HttpRemoteQueryClient`'s (`openframe.protocol.http.client`) own
  inject-or-build-default convention exactly, confirmed by reading its real `_owns_client`/
  `aclose()` logic: an injected client is never closed by `pull_hls`, enabling a caller to attach
  custom auth headers, rotate credentials mid-stream, or fail over across CDNs via a custom
  `httpx.AsyncBaseTransport` — the entire reason this capability belongs in `-media` rather than
  just pointing `ffmpeg -i` at the remote URL directly.
- **Scope**: a single-rendition HLS *media* playlist only. A *master* playlist (multiple variant
  streams, needing a `rendition_selector`) is explicitly out of scope for this release.
- `tests/test_hls_pull_gap_detection.py` proves the gap/discontinuity-inference, stale-poll
  timeout, and `#EXT-X-ENDLIST` behaviours deterministically, against a hand-rolled local HTTP
  server serving hand-written playlist fixtures (no real-ffmpeg timing dependency).
  `tests/test_hls_pull_spike.py` re-runs the real spike as a test — real `ffmpeg` producing a
  genuinely live/rotating playlist (`-hls_time 2 -hls_list_size 5 -hls_flags delete_segments`)
  served over a real local server via this package's own `bind_hls_delivery`, polled by the real
  `pull_hls` binding for ~40s, asserting strictly increasing sequence order, zero duplicates, and
  a clean `CloseReason.COMPLETED` after stopping ffmpeg. `tests/test_hls_pull_conformance.py`
  runs `SessionConformanceTests` (`openframe-protocol-base`) against the real binding, reusing
  `bind_hls_delivery` as the real origin server — a fifth real transport to pass this suite, and
  the first outbound-poller one; `ordered_delivery` is a genuinely *stronger* signal here than for
  RTMP ingest's own weaker onStatus-description bridge, since real segments are really fetched in
  real sequence order; `abrupt_disconnect` is `xfail`'d with a documented reason (no remote peer
  to disconnect from), the same honest-not-papered-over treatment RTMP ingest's own conformance
  suite gives its own unfalsifiable case.
- Dependency floor: `openframe-core>=3.10.0,<4` (up from `>=3.9.0,<4`), needed for
  `openframe.core.media.MediaSegment`.

---

## [openframe-protocol-media 0.2.0] - 2026-10-19

### Added — HLS/DASH delivery, the read side completing this package's RTMP ingest (0.1.0)

Serving already-produced manifest/segment files over HTTP with full Range support.
Transcoding/segmenting/packaging is explicitly out of scope (a separate ffmpeg-based adapter the
wider platform builds handles that) — this is bytes-that-already-exist delivery only.

- **`types.py` — `MediaRequest`/`MediaObject`/`MediaRange`/`MediaProfile`/`MediaObjectKind`/
  `HLS_PROFILE`/`DASH_PROFILE`/`MediaCachePolicy`/`MediaCorsPolicy`**: pure-stdlib vocabulary, no
  `fastapi` import anywhere — a service's own `QueryHandler` implementation can spell
  `MediaRequest`/`MediaObject` in its type annotations without installing the new `[delivery]`
  extra at all, the same "no extra dependency for a plain type" shape `flow.py` already established
  for the ingest half. `MediaRequest.range` is passed down to the handler and
  `MediaObject.served_range`/`total_size` come back up, deliberately — a top-rung ABR segment can be
  ~15MB and a real player seeks constantly, so a binder that fetched whole objects and sliced them
  itself would materialize the full object on every single seek, and would make a genuinely
  range-aware storage read (`S3 GetObject(Range=...)`, `os.pread()`) unreachable from this package
  entirely.
- **`ranges.py` — `parse_range_header`/`resolve_range`/`UnsatisfiableRangeError`**: RFC 9110
  §14.1.2/§14.2 byte-range parsing, pure stdlib. All three `bytes=` forms
  (`start-end`/`start-`/`-suffix_length`) resolved precisely; a non-`bytes` unit, a multi-range
  request, or malformed syntax is treated as "ignore it, serve 200" per the RFC's own permission to
  do so (multi-range support was deliberately skipped — a media player only ever requests one
  contiguous range at a time in practice); a well-formed-but-out-of-bounds range (a zero-length
  suffix, or a start at/beyond the object's real size) raises `UnsatisfiableRangeError`, the one
  case that really is a 416. Exhaustively unit-tested in `tests/test_ranges.py`, including the exact
  boundary (`start == total_size - 1` satisfiable vs. `start == total_size` not).
- **`delivery.py` — `bind_media_delivery`/`bind_hls_delivery`/`bind_dash_delivery`**: wires a
  `QueryHandler[MediaRequest, MediaObject]` (`openframe.core.inbound`) to a real FastAPI GET/HEAD
  route (plus an OPTIONS preflight route), needs the new `[delivery]` extra (`fastapi>=0.110`).
  Binds a `QueryHandler`, not a new source abstraction — a `MediaSource` port would be an outbound
  port belonging in `openframe-core`'s `ports/outbound/` per ADR-006's intake rule, not invented
  here; the handler decides whether bytes come from S3, a local mount, or a cache, keeping this
  package's "protocol never sees a repository" philosophy intact. Depends on `openframe-protocol-base`
  + `fastapi` directly, **never** on `openframe-protocol-http` — the seam is a plain `APIRouter`, a
  service wires both into `create_app(..., routers=(api_router, media_router))` itself, and the
  ingest half of this same package needs no ASGI stack at all, so a package-wide `-http` dependency
  would be dead weight for half of what this package does. An unrecognised suffix 404s before the
  handler is ever called — content type is matched against `profile.suffixes`, never guessed. A 416
  response is built as a raw `Response` with `Content-Range: bytes */<size>` set directly, **never**
  raised as `HTTPException` — read directly from the installed `openframe-protocol-http` source
  (not guessed): `_handle_http_exception` builds its `JSONResponse` without passing
  `headers=exc.headers`, silently dropping any headers attached to a raised `HTTPException`, which
  would lose exactly the one header RFC 9110 §14.4 requires a 416 to carry. This package never wires
  that handler (see above), so the bug is structurally unreachable here rather than merely avoided —
  documented as a separate, future drive-by fix for `-http` itself, not addressed in this release.
  `Cache-Control` defaults to `no-cache` for manifests (a live-edge manifest changes every time a new
  segment is produced) and a one-year `immutable` cache for segments/init files (written once under
  a name that never changes). `ETag`/`Last-Modified` are passed through only when the handler
  actually sets them — never synthesized by this binder. A handler-reported `served_range`/body width
  that doesn't match the resolved request range raises `ValueError`, surfacing as a `500` — correctly,
  since that's a storage-layer bug, not a client error.
- `tests/test_bind_media_delivery.py` exercises all of the above end to end against a real FastAPI
  app and `httpx.AsyncClient` (`ASGITransport`, matching `-http`'s own `test_conformance.py`
  pattern) bound to a fake in-memory `QueryHandler`: 200/206/404/416/500 status codes, all three
  Range forms, `HEAD` (empty body, correct headers), per-kind `Cache-Control`, CORS headers on
  success/404/OPTIONS preflight, and the 416-carries-`Content-Range` regression test proving the
  `-http` bug above is actually routed around, not just described.
- `pyproject.toml` gained the `delivery` extra (`fastapi>=0.110`) and `dev` gained `fastapi`/`httpx`
  (for `TestClient`/`AsyncClient`), without touching the existing `ingest`/`dev` entries; package
  bumped to 0.2.0 as a real feature addition, not a patch. `.github/workflows/app-test.yml` already
  listed `openframe-protocol-media` in all three matrices from the 0.1.0 (ingest) release and needed
  no changes — `[dev]` already installs everything both halves of this package need.

---

## [openframe-protocol-media 0.1.0] - 2026-10-19

### Added — RTMP ingest, the first binary-payload transport in the family, built on a real `pyrtmp` + `ffmpeg` spike

New package, `openframe-protocol-media`. Everything but SSE/WebSocket/gRPC so far has moved
JSON-shaped events; RTMP ingest moves raw audio/video bytes, and `pyrtmp` 0.3.1 (the only
maintained pure-Python asyncio RTMP server implementation found) turned out to diverge from its
own README in several places significant enough to shape this package's whole structure. Nothing
below was assumed from the RTMP spec — every claim was confirmed by spiking against a real
`pyrtmp` server and real `ffmpeg`, and in three cases, by directly reproducing a real bug.

- **`flow.py` — `MediaFlowController`/`DropPolicy`/`FlowConfig`/`FlowStats`/`BackPressureExceeded`/
  `MediaGap`**: transport-neutral, zero-third-party-dependency back-pressure between a binary media
  producer and whatever consumes `MediaFrame`s downstream (`openframe.core.media`, added in
  `openframe-core` 3.9.0). Hysteresis-based `pause()`/`resume()` callbacks at configurable byte
  watermarks; under sustained overflow, the default `DropPolicy.DROP_VIDEO_UNTIL_KEYFRAME` sheds
  non-config video interframes until a fresh keyframe arrives *and* the buffer has drained back
  below the low watermark (both conditions, confirmed required independently — a keyframe alone
  isn't enough), then resumes with a `MediaGap` marker. A config frame (`is_config=True`) is never
  dropped under **any** policy, with zero exceptions — the single invariant this package treats as
  non-negotiable, since losing one can make a whole stream undecodable with no recovery short of a
  new session. Built on a plain `collections.deque` + `asyncio.Event`, not `asyncio.Queue` —
  `Queue.put_nowait` can't do byte-budgeted eviction or keyframe-aware shedding, and `offer()` must
  stay synchronous (it's called directly from a `pyrtmp` callback, not an awaiting coroutine).
  Proven under genuine sustained back-pressure (not just unit-level): `tests/test_backpressure.py`
  pushes ~100MB of synthetic frames through an 8MiB-scale budget against a deliberately slow
  consumer and asserts the byte ceiling, zero audio/config drops, and post-gap keyframe invariants
  all hold for real.
- **`ingest.py` — `RtmpSession`/`RtmpRegistry`/`bind_ingest`/`create_server`**: wires a
  `SessionHandler` (ADR-008) to a real `pyrtmp`-backed RTMP publish connection. Three genuinely
  surprising, spike-confirmed `pyrtmp` behaviours shaped this module directly: (1) a plain `raise`
  from `on_ns_publish` sends **nothing** to the client — rejecting for real requires hand-building
  an AMF0 `onStatus(level="error")` message, since there is no built-in reject helper; (2)
  `self.transport` does not exist on the controller at all — the real `asyncio.Transport` is
  `session.writer.transport`, confirmed by reading `pyrtmp`'s own `SessionManager`; (3) `cleanup()`,
  not `on_stream_closed`, is the *only* teardown hook called on every disconnect path, clean or
  crashed — `connection_lost(exc)` was `exc=None` for both a real SIGTERM and a real SIGKILL during
  the spike, so crash vs. clean stop cannot be reliably told apart here, and this binding always
  reports `CloseReason.COMPLETED` rather than guessing (a known, accepted, documented limitation,
  not an oversight). A fourth bug was found and fixed while building the conformance suite, not by
  inspection: `RtmpSession.close()` originally closed the TCP writer eagerly, but
  `openframe.protocol.base.session.open_session`'s own reject path calls `session.close()` *before*
  re-raising — racing the subsequent hand-built rejection write against an already-closing socket,
  confirmed by reproducing a real `ConnectionResetError`. `RtmpSession.close()` now only tracks the
  idempotent closed flag; real teardown is left to `pyrtmp`'s own unconditional `writer.close()`
  after `cleanup()` returns.
- **`SessionConformanceTests` (`openframe-protocol-base`) passes against this binding** — the fourth
  real transport to prove it, after SSE/WebSocket and gRPC — via a minimal hand-rolled RTMP publish
  client (`pyrtmp` itself ships no client). One scenario is a confirmed, documented exception:
  `test_abrupt_disconnect_closes_with_client_disconnect` is marked `xfail` — not weakened or
  skipped — because this binding can never report `CLIENT_DISCONNECT` (see `cleanup()`'s own
  limitation above); `ordered_delivery`/`correlation_id_stable` are driven through the `onStatus`
  error channel rather than real media frames, a weaker signal than the other transports, documented
  as such rather than hidden. `tests/test_ingest_spike.py` publishes a few real seconds of
  H.264+AAC from real `ffmpeg` into a real `create_server`, asserting config frames, a post-config
  keyframe, and a `COMPLETED` close all land as expected.
- Root `pyproject.toml`'s `pythonpath`, `meta/openframe-protocol`'s `media` extra (and `all`), and
  `.github/workflows/app-test.yml`'s `test`/`lint-and-typecheck`/`test-lowest-versions` matrices all
  updated for the new package.
- HLS/DASH delivery (the read side) is a later phase — this release is ingest-only.

---

## [openframe-protocol-http 0.11.0] - 2026-10-18

### Added — webhook replay protection and body-size cap, the two items left open from the previous review-driven pass

- **`bind_webhook` gained `max_body_size`**: rejects (413) a request whose body exceeds this many
  bytes, checked against `Content-Length` *before* the body is read (avoiding reading an unbounded
  body into memory at all when the sender is honest about its size), and again against the actual
  bytes read afterward (a sender can omit `Content-Length`, lie about it, or use chunked transfer,
  which has none at all).
- **`bind_webhook` gained `timestamp_header`/`replay_window_seconds`**: if a timestamp header is
  configured, a request whose timestamp is missing, unparseable, or outside the window (default
  300s either direction) is rejected (401) *before* signature verification runs. Opt-in, since not
  every provider sends one (GitHub/Shopify's own scheme doesn't; Stripe's does).
- **`bind_webhook` gained `is_replay`**: an optional `(signature) -> bool` hook (awaited), checked
  after signature verification succeeds — a timestamp window alone only bounds *how long* a
  captured, validly-signed request stays replayable, not whether it's replayed within that window.
  This binder is stateless and has nowhere of its own to keep a "seen signatures" set, so the hook
  is a dedup *point*, not a dedup *implementation* — the caller supplies the actual store.
- **`_STATUS_TO_CATEGORY`'s reverse lookup (used by the `HTTPException` handler added in 0.10.0)
  extended for 400 and 413** — both are "the request itself was malformed," the same bucket 422
  already occupies, now mapped to `ErrorCategory.INVALID` instead of falling back to a generic
  `HTTP_400`/`HTTP_413` code. 401/403/409 are deliberately left on the generic fallback — none of
  the five categories draw an auth/conflict distinction (see `webhook.py`'s own docstring), so
  forcing one would misrepresent the error rather than clarify it.
- 9 new tests covering all three new `bind_webhook` parameters.

---

## [tooling] Lint, type-check, and coverage added to CI - 2026-10-18

The last unaddressed item from the external review: `app-test.yml` only ran `pytest`, with no
lint/type-check/coverage gate at all.

- **New `lint-and-typecheck` CI job** (one Python version per package, not the full 3.11-3.14
  matrix — these results don't vary meaningfully across versions): `ruff check` and `mypy
  --ignore-missing-imports --namespace-packages --explicit-package-bases` for all three packages.
- **A real `[tool.ruff]`/`[tool.ruff.lint]` config added to the repo-root `pyproject.toml`** (ruff
  finds it via its own upward config search from any package subdirectory) — generated protobuf
  files (`generic_pb2*.py`) excluded (ruff's own `--fix` rewrote one during this pass, confirmed
  and reverted — exactly the failure mode the exclude prevents), and `BLE001`/`B008` ignored
  project-wide: both are deliberate, documented idioms here (the catch-all exception boundary every
  transport binding maps through `categorize()`/`build_envelope()` at, and FastAPI's own
  `Depends(...)`-in-a-default-argument pattern), not oversights ruff happened to catch.
  `ruff format` was deliberately **not** added as a CI gate — a first real run found 43 of ~50
  files would be reformatted, a large, purely cosmetic diff with real review cost and no
  correctness value; left for a deliberate separate pass, not folded into this one.
- **11 real mypy errors fixed** (3 in `-base`, 3 in `-http`, 5 in `-grpc`) surfaced once mypy
  actually ran — all pre-existing, all latent rather than live bugs (confirmed: full test suite
  stayed green before and after each fix), including a real signature-type narrowing in
  `FastAPI(description=...)` (was passed `None`, the real signature defaults to `""`) and three
  `return` statements after a `context.abort()`/`_abort_for_exception()` call that mypy couldn't
  know never returns, now `raise AssertionError("unreachable: ...")` instead — clearer intent, and
  type-checks cleanly without needing a `NoReturn`-typed stub for either.
- **`pytest-cov` added to `[dev]`**; `Run tests` now reports `--cov=openframe --cov-report=term-missing`
  (not a hard coverage-percentage gate — no established baseline existed to set one against yet).
- `py.typed` is unrelated to this entry (added in the previous pass) but is what made adding mypy
  here meaningful at all.

---

## [openframe-protocol-grpc 0.4.1] - 2026-10-18

### Fixed — correctness/robustness findings from an external code review, all verified against the actual code before fixing

- **`bind_service`'s unary handler no longer catches `asyncio.CancelledError`.** It previously
  wrapped `handler.execute()` in `except BaseException`, which swallowed a client disconnect or
  server shutdown and reported it to the client as an `INTERNAL` abort instead of letting the
  cancellation propagate for `grpc.aio` to handle normally. Narrowed to `except Exception`, with an
  explicit `except asyncio.CancelledError: raise` ahead of it.
- **`create_server` no longer leaks telemetry on a failed `configure`/`on_startup`.** `setup_telemetry()`
  ran unconditionally before `configure(server)`/`start()`; if either raised (a non-degraded
  exception), the `finally` block owning `shutdown_telemetry()` was never reached. Now wrapped so a
  failure during setup tears down telemetry before re-raising.
- **`create_server` gained `stop_grace` (default `5.0`s).** `server.stop(None)` (immediate, no
  grace period) killed every in-flight RPC on shutdown unconditionally; now configurable, `None`
  still available for the previous behaviour.
- **`create_server` gained `manage_telemetry` (default `True`).** `create_app`/`create_server` each
  call `setup_telemetry()`/`shutdown_telemetry()` independently; a service running both
  concurrently (see the top-level README's "Multiple entrypoints" example, now updated) had
  whichever transport's lifespan exited first tear down the telemetry providers the other was still
  using. `manage_telemetry=False` opts a transport out, letting the composition root own it once.
- **`GrpcRemoteQueryClient`/`GrpcRemoteCommandClient` now serialise via `_to_jsonable`** (the same
  helper `bind_service` already used on responses) instead of a bare `json.dumps(value)` — a caller
  passing a `BaseModel` (the normal input shape for a handler built for `-http`) previously failed
  with a raw `TypeError` instead of being sent.
- **`bind_service`/`bind_service_stream` accept `context_enricher`**, threaded through to
  `build_request_context` — the same `principal`/`tenant` hook `-http`'s binders now all accept.
- **`protobuf>=5.26` added as an explicit runtime dependency** of both `[server]` and `[client]`.
  `generic_pb2.py` imports `google.protobuf` directly, but `grpcio` itself declares no protobuf
  dependency (confirmed: only `typing-extensions`) — a clean install resolving only `grpcio` was
  missing it; `grpcio-tools` (dev-only) happened to pull it in, masking the gap in tests.
- **Dependency floors corrected**: `openframe-core>=3.2.1` → `>=3.8.0` (where `SessionHandler`/
  `CloseReason` actually land, imported directly by this package) and `openframe-protocol-base>=0.1`
  → `>=0.4` (where `session.py` itself was added) — the previous floors resolved to a release
  missing symbols this package imports unconditionally.
- Added `py.typed` (the `"Typing :: Typed"` classifier had nothing backing it).
- New CI job (`app-test.yml`): installs this package with every dependency pinned to its newly
  corrected floor and runs the suite against it — the gap that let the floor drift above go
  unnoticed by CI in the first place.

---

## [openframe-protocol-http 0.10.0] - 2026-10-18

### Fixed — correctness/robustness findings from an external code review, all verified against the actual code before fixing

- **`HttpRemoteQueryClient`/`HttpRemoteCommandClient` no longer leak raw `httpx` exceptions.**
  `_call` only caught `httpx.TimeoutException` and `httpx.ConnectError`; broadened the second to
  `httpx.TransportError` (its actual base class), which also covers `ReadError`, `WriteError`,
  `RemoteProtocolError`, and `ProxyError` — all previously escaped unmapped, contradicting this
  package's own "callers only see `AdapterError`" contract.
- **The client no longer crashes on a non-JSON error body.** `response.json()` on an error response
  had no guard; a proxy's own HTML 502 page raised `JSONDecodeError` instead of a normal
  `AdapterError`. Now caught, falling back to an empty body.
- **429 (rate limited) and 502 (bad gateway) now map to `ErrorCategory.UNAVAILABLE`** (same bucket
  as 503) instead of the `INTERNAL` default — both are transient failures of the remote side, not
  evidence this request was malformed, and the category decides which `AdapterError` subclass the
  client reconstructs, not just a `retryable` flag.
- **Path params are now URL-encoded** (`urllib.parse.quote`) before being substituted into a
  client's path template — an unescaped value (e.g. containing `/` or `..`) previously changed the
  request path itself, not just the value inside it.
- **`not_found_when` (`bind_query`/`bind_command`) and `bind_webhook`'s 401s now return the same
  `{"error": {...}}` envelope every other error does.** Both raised a plain `fastapi.HTTPException`,
  which FastAPI's own default handler serialises as `{"detail": ...}` — a different, undocumented
  shape a caller using `raise_for_envelope` couldn't parse. A new `HTTPException` handler in
  `install_error_handlers` maps it into the envelope via the already-existing status→category table,
  falling back to `HTTP_<status>` for a status (401) that table doesn't cover.
- **`create_app`'s lifespan no longer leaks telemetry on a failed `on_startup`.** Same fix as
  `create_server`'s (above) for the symmetric bug.
- **`create_app` gained `manage_telemetry` (default `True`)** — see `openframe-protocol-grpc 0.4.1`'s
  entry above for the shared reasoning; the top-level README's multi-entrypoint example now passes
  `manage_telemetry=False` to both and owns `setup_telemetry`/`shutdown_telemetry` itself.
- **`SseSession`'s queue is now bounded** (`queue_maxsize`, default 1000) — unbounded before, a slow
  or stalled client let a fast-producing handler grow server memory without limit; `send()` now
  applies natural backpressure once full.
- **`bind_session` (SSE) sends `Cache-Control: no-cache` and `X-Accel-Buffering: no`** — without
  them, an intermediary proxy can buffer or cache a stream that was never meant to be either.
- **`bind_websocket` gained `allowed_origins` and `max_message_size`** (both optional, default
  `None`/no check — preserves previous behaviour). A connection whose `Origin` header isn't allowed
  is rejected with a policy-violation close (1008) before `on_open` runs; an inbound text frame over
  the size limit closes the connection (1009) instead of reaching `on_event` unbounded.
- **`bind_query`/`bind_command`/`bind_session`/`bind_websocket`/`bind_webhook` all accept
  `context_enricher`** now, threaded through to `build_request_context` — the seam already existed
  there but no binder exposed it, making `principal`/`tenant` unreachable without editing this
  package directly.
- **A client-supplied `X-Correlation-ID` is now validated before being trusted** — length-capped
  (128 chars) and required to be printable with no `\r`/`\n`, falling back to a fresh `uuid4`
  otherwise. Previously reflected back into the response header and every log line/span verbatim,
  an unbounded log-flooding and (if some downstream consumer builds a raw header line from it)
  header-injection surface.
- **Dependency floors corrected**: `openframe-core>=3.2.1` → `>=3.8.0`, `openframe-protocol-base>=0.1`
  → `>=0.4` — same drift and fix as `openframe-protocol-grpc 0.4.1`'s entry above.
- Added `py.typed`.
- New CI job (`app-test.yml`): lowest-declared-floors matrix entry for this package too.

---

## [openframe-protocol-base 0.4.1] - 2026-10-18

### Fixed

- **`ContextEnricher` is now exported** (`openframe.protocol.base.context.__all__`) — every binder
  in `-http`/`-grpc` that now accepts a `context_enricher` parameter imports it from here.
- **A client-supplied correlation-id header is now sanity-checked** before `build_request_context`
  trusts it (see `openframe-protocol-http 0.10.0`'s entry above for the full reasoning) —
  length-capped and required to be printable with no CR/LF, otherwise a fresh `uuid4` is used.
- **Dependency floor corrected**: `openframe-core>=3.2.1` → `>=3.8.0` — `session.py` imports
  `SessionHandler`/`Session`/`CloseReason` directly (ADR-008, landed in core 3.8.0); the previous
  floor resolved to a core release without them.

---

## [openframe-protocol 0.3.0 (meta)] - 2026-10-18

### Fixed

Stale version table (`base 0.1.0`, `http 0.1.0`, no gRPC row — actual state was 0.4.0/0.8.0/0.4.0
at the time) and stale extra floors (`openframe-protocol-http>=0.1`, `-grpc>=0.1`, both already past
1.0-worth of real releases) — both flagged by an external review and confirmed against the real repo
state. Floors now match each package's own `pyproject.toml`.

---

## [openframe-protocol-http 0.9.0] - 2026-10-18

### Added — `create_quic_config`, HTTP/3 over QUIC via Hypercorn — zero changes to any binder

A new `quic.py` module (alongside `sse.py`/`websocket.py`/`webhook.py`) with its own `[quic]`
extra (`hypercorn[h3]`), confirmed by a real end-to-end spike, not assumed: a `bind_query`-bound
route, served via `create_app()`'s unmodified output under a real Hypercorn server configured for
QUIC, reached by a real HTTP/3 client over a real QUIC connection — status 200, correct body, and
`build_request_context`'s own correlation-id generation intact, with **zero changes** to
`bind_query`/`bind_command`/`install_error_handlers`/`create_app` themselves.

- **`create_quic_config(*, bind, certfile, keyfile, alpn_protocols=("h3",))`** — the one genuinely
  repeatable piece of boilerplate this needed: `quic_bind` must mirror `bind`, and `alpn_protocols`
  must include `"h3"`, both easy to get wrong by hand and uninteresting enough not to leave
  copy-pasted in every service. Returns a `hypercorn.Config`; the caller still calls
  `hypercorn.asyncio.serve(app, config)` and owns the process, the exact same boundary `create_app`
  already draws for `uvicorn`.
- **Deliberately no `bind_quic` and no separate package.** QUIC is a transport-layer concern, not a
  new way an inbound event reaches a handler (unlike SSE/WebSocket/webhooks, each a genuinely new
  binding shape) — confirmed by the spike itself working with zero binder changes. `uvicorn` has no
  QUIC support; `hypercorn` does, so this is purely a server-choice decision made in each service's
  own `main.py`, not a new openframe-protocol concern.
- Test cert generated with the `cryptography` library directly (no `openssl` subprocess dependency)
  — a self-signed cert with a proper `SubjectAltName` for `127.0.0.1`/`localhost`, confirmed needed
  after an earlier spike attempt without SANs hung indefinitely on an unrelated IPv6/`localhost`
  resolution mismatch between client and server.
- 1 new test (a full real client/server HTTP/3 round trip). Full monorepo suite: 175 passing (up
  from 174).

---

## [openframe-protocol-grpc 0.4.0] - 2026-10-18

### Added — `bind_service_stream`, the third `SessionHandler` transport, via `grpc.aio`'s native streaming

`GenericService` gains a second RPC, `CallStream` (`stream GenericRequest` → `stream GenericResponse`),
alongside the existing unary `Call` — same route-multiplexing trick (`x-openframe-route` metadata),
regenerated `generic_pb2.py`/`generic_pb2_grpc.py` via `grpc_tools.protoc`, confirmed the existing
`Call` RPC's generated shape stayed byte-for-byte compatible (31 pre-existing tests still pass
unchanged). Unlike SSE/WebSocket, this needed **no new library or dependency** — `grpcio` already
supports all four RPC shapes; this codebase had simply never used the streaming ones until now.

- **`GrpcSession`** — `Session[TOut]` backed by an `asyncio.Queue`, structurally identical to
  `SseSession`'s own (the servicer method's `yield` loop and the handler are different coroutines
  that need to hand items between them, same reason SSE needed one).
- **`bind_service_stream(server, route, *, handler_factory, input_model=None)`** — `on_open` runs
  (and can reject) before the method's first `yield`, mirroring SSE's pre-`StreamingResponse` and
  WebSocket's pre-`accept()` commit-point rule, reusing `open_session` unchanged.
- **A real race SSE/WebSocket never had, found by spiking, not assumed**: `request_iterator`
  (inbound) and the servicer method's own `yield` loop (outbound) are two independently-scheduled
  `grpc.aio` signals for the same client disconnect — confirmed across four separate live
  server/channel spikes that `request_iterator` reliably resolves via plain `StopAsyncIteration`
  *before* `context.cancelled()` reflects the same disconnect, not a one-off timing fluke.
  `bind_service_stream` therefore does **not** reuse
  `openframe.protocol.base.session.run_opened_session` wholesale — inbound dispatch polls
  `context.cancelled()` for a short window (confirmed needed: a single `asyncio.sleep(0)` tick was
  NOT enough; the underlying HTTP/2 GOAWAY frame takes a few real milliseconds to process) right
  after its own loop ends normally, before trusting that as `COMPLETED` rather than
  `CLIENT_DISCONNECT`.
- Mid-session `on_event` failures sent in-band via the now-shared `send_error_event`, same as
  SSE/WebSocket; a reject propagates as a clean `AioRpcError` with zero messages, confirmed by a
  real spike, mirroring `Call`'s own existing reject path (`_abort_for_exception`, reused unchanged).
- `SessionConformanceTests` now proven against a **third** real transport (`tests/test_grpc_session_conformance.py`
  — named to avoid the cross-package `tests/` module-name collision this package's own `conftest.py`
  already documents, since `openframe-protocol-http` has its own `test_session_conformance.py`).
- 10 new tests. Full monorepo suite: 174 passing (up from 164).

---

## [openframe-protocol-http 0.8.0] - 2026-10-18

### Added — `bind_webhook`, a one-shot receiver with HMAC signature verification

A new `webhook.py` module inside `-http` (not its own package — per the design spec's own
placement decision, alongside `sse.py`/`websocket.py`). Deliberately **not** a `SessionHandler`
transport — a webhook is a one-shot exchange (confirmed by the design spec's own text, §5.5:
`CommandHandler`/`QueryHandler` already fit webhooks), so `bind_webhook` dispatches to a plain
`CommandHandler`/`UseCase`, the same contract `bind_command` uses.

- **`bind_webhook(router, path, *, handler_dep, body_model, signature_header, secret, verify, status_code=200)`**
  — reads the raw request body once, verifies *signature_header*'s value against it with *verify*
  **before** parsing the body or calling the handler, then parses `body_model` from those same raw
  bytes (never a re-serialized copy, which wouldn't byte-match what was actually signed).
- **`verify_hmac_sha256(secret, raw_body, signature, prefix="sha256=")`** — the default verifier,
  covering the common GitHub/Shopify-style scheme (HMAC-SHA256 over the raw body, hex-encoded,
  optionally prefixed), using `hmac.compare_digest` for constant-time comparison. Deliberately not
  universal — a provider with a different scheme (Stripe's timestamp+payload concatenation, for
  instance) supplies its own `verify` callable.
- **A missing or invalid signature returns 401 before the handler ever runs** — a sender that can't
  prove who it is doesn't get to cause a validation error or a handler side effect either. This
  maps straight to 401, not through `categorize()`/`build_envelope()`: an auth failure specific to
  this one binder doesn't warrant a new `ErrorCategory` member (none of the existing five draw an
  authenticated/unauthenticated distinction, and `-base`'s own rule-of-three discipline argues
  against adding one for a single binder's pre-handler check).
- **Any error the handler itself raises after signature verification passes still flows through
  `install_error_handlers`'s normal `categorize`/`build_envelope` path unchanged** — proven with a
  real `AdapterConnectionError` → 503, confirming the handler actually ran (not short-circuited by
  the signature check).
- **A real `TestClient` quirk found and fixed while spiking this, not new but newly hit**: a bare
  `pydantic.ValidationError` (raised directly by this binder's own `body_model.model_validate_json`
  call, not by FastAPI's internal body-extraction) is only caught by `install_error_handlers`'s
  bare `@app.exception_handler(Exception)` when `TestClient(app, raise_server_exceptions=False)` —
  otherwise it re-raises into the test itself. Same root cause as the conformance driver's own
  documented `httpx.ASGITransport(raise_app_exceptions=False)` requirement, hit again here in a new
  binder's own test.
- 7 new tests. Full monorepo suite: 164 passing (up from 157).

---

## [openframe-protocol-http 0.7.0] - 2026-10-17

### Added — `bind_websocket`, the second `SessionHandler` transport and the first to exercise `on_event`

A new `websocket.py` module inside `-http` (not its own package — Starlette's native WebSocket
support needs no dependency beyond `[server]`, same as SSE). Unlike SSE, WebSocket is genuinely
bidirectional: this is the first transport in this repo to actually dispatch inbound events
through `on_event`.

- **`WebSocketSession`** — `Session[TOut]` backed directly by the Starlette `WebSocket` object (no
  intermediate queue needed, unlike `SseSession` — `send()` calls the real socket's `send_text()`
  directly, accepting the connection on first call if `accept()` hasn't happened yet).
- **`bind_websocket(router, path, *, handler_factory)`** — `on_open` always runs, and can reject,
  *before* `accept()`, mirroring SSE's own commit-point timing rule but for a handshake instead of
  a response's headers. Confirmed with a real spike (`TestClient.websocket_connect`): calling
  `websocket.close()` before `accept()` produces a genuine failed handshake the client sees as
  `WebSocketDisconnect` on connect, not a message after connecting.
- **`WebSocketDisconnect` → `asyncio.CancelledError` translation**: `run_opened_session`
  (`openframe-protocol-base`) only special-cases `CancelledError` for disconnect handling — it has
  no Starlette dependency of its own. `bind_websocket`'s `inbound_events()` generator re-raises a
  real client disconnect as `CancelledError` instead of letting `WebSocketDisconnect` propagate
  unmapped, so the existing generic handling applies unchanged.
- **A real race found and fixed while spiking this**: a handler that closes the session itself
  from inside `on_event` (not a client disconnect) left the inbound loop's next `receive_text()`
  call hitting an already-closed socket, raising `WebSocketDisconnected` (a *different* exception
  than `WebSocketDisconnect`, confirmed by the actual traceback) — unmapped, this surfaced as
  `CloseReason.ERROR` instead of `COMPLETED`. Fixed by checking the session's closed state before
  each receive, ending the generator normally instead.
- **A test-harness-only limitation found and worked around, not a `bind_websocket` bug**:
  `TestClient.websocket_connect` does not service an orphan `asyncio.create_task` the way a real
  server does — a background-task-pushing handler hung forever under `TestClient` but worked
  correctly when verified against a real `uvicorn.Server` with a real `websockets`-library client
  (confirmed directly, not assumed). `SessionConformanceTests`'s own WebSocket driver uses a
  client-triggered close pattern instead, which `TestClient` can actually test.
- `SessionConformanceTests` (from `-base`) now proven against a *second* real transport — all 6
  pass, alongside SSE's own 6.
- 16 new tests. Full monorepo suite: 157 passing (up from 146).
- **Not done as part of this**: nothing else in the M3 milestone (webhooks, MCP) — WebSocket was
  the last transport this plan called for.

---

## [openframe-protocol-http 0.6.0] - 2026-10-17

### Added — `bind_session` (SSE), the first real `SessionHandler` transport

Built on `openframe-protocol-base` 0.4.0's `open_session`/`run_opened_session`/
`send_error_event`. A new `sse.py` module inside `-http` (not a new
package), per the design spec's own placement decision — SSE has no
handshake beyond a normal HTTP GET.

- **`SseSession`** — `Session[TOut]` backed by an `asyncio.Queue`; `send()`
  enqueues, `close()` enqueues a sentinel and records the *first* reason
  it was called with.
- **`bind_session(router, path, *, handler_factory)`** — calls
  `open_session` (accept/reject) **before** constructing the
  `StreamingResponse`, confirmed necessary by actually tracing Starlette's
  `StreamingResponse.stream_response`: it sends the ASGI
  `http.response.start` message (status 200) before ever pulling the body
  generator's first item, so a reject decided inside the generator would
  already be too late. A reject now propagates straight out of the route
  function into `install_error_handlers`'s normal `categorize`/
  `build_envelope` → status-code path — proven with a real
  `AdapterTimeoutError` → 504 response, not assumed. Sets
  `X-Correlation-ID` on the response, matching `bind_query`/
  `bind_command`'s own convention.
- **Deliberately does not use `run_opened_session`** — SSE has no inbound
  events at all (`on_event` is never called), so there's nothing for it
  to dispatch. An SSE handler drives its own background production after
  `on_open` returns and reports its own failures via the now-public
  `send_error_event` directly.
- **Spiked against real infrastructure, not just `httpx.ASGITransport`**:
  confirmed the hard way that `ASGITransport` does not simulate a real
  client disconnect at all (no parallel `http.disconnect`-watcher task
  the way a real server has) — an early version of the disconnect test
  hung forever. Rewrote it against a real `uvicorn.Server` and a real
  socket close instead; `uvicorn>=0.30` added as a `[dev]`-only
  dependency for this. Proven: ordered multi-event delivery over one
  connection, reject → mapped HTTP error (not a broken stream), mid-session
  failure → in-band error event with no message leak, and real client
  disconnect → `on_close(CLIENT_DISCONNECT)`.
- `SessionConformanceTests` (from `-base`) now also proven against this
  real transport, not just the in-process fake — all 6 pass.
- `openframe-protocol-base` floor bumped to `>=0.1,<1` (unchanged —
  already covers 0.4.0).
- 11 new tests. Full monorepo suite: 146 passing (up from 135).
- **Not done as part of this**: WebSocket — its own package-boundary
  question (still `[verify]` in the design spec) and genuinely
  bidirectional, unlike SSE. Next step once this is reviewed.

---

## [openframe-protocol-base 0.4.0] - 2026-10-17

### Added — `run_session` + `SessionConformanceTests`, the session-lifecycle counterpart to `TransportConformanceTests`

Built on `openframe-core` 3.8.0's new `SessionHandler`/`Session`/`CloseReason`
(ADR-008) — the driving-side contract for long-lived connections
(WebSocket, SSE), added directly to core rather than incubated here first
(see that ADR's "Why skip incubation" section).

- **`open_session(handler, session)` + `run_opened_session(handler, session,
  inbound_events)`** (`session.py`) — the open/dispatch/error/close state
  machine every session transport's `server.py` reuses, the
  session-lifecycle sibling of `create_app`/`create_server` (M1c).
  `run_session(...)` is a convenience wrapper combining both, correct
  only when there's no wire "commit point" to worry about. Two failure
  points, two different wire treatments — found while designing this,
  not assumed: an `on_open` rejection propagates out of `open_session`
  (nothing committed to the wire yet — the transport maps it through the
  usual `categorize`/`build_envelope` status-code path), while an
  `on_event` failure mid-session (`run_opened_session`) is sent
  **in-band** via `session.send()` (the stream already committed by
  then, no status code left to change) and does not propagate.
  `asyncio.CancelledError` is reported as `CloseReason.CLIENT_DISCONNECT`
  and always re-raised — standard cancellation hygiene, never swallowed.
  `on_close`/`session.close()` always fire exactly once for the path
  taken, including when `on_open` itself failed.
  **`open_session`/`run_opened_session` were split from one single
  `run_session` function mid-build**, confirmed by tracing Starlette's
  own `StreamingResponse.stream_response`: it sends the ASGI
  `http.response.start` message (status + headers) before ever pulling
  the body generator's first item — so a reject decided *inside* a
  generator would already be too late to turn into a real 4xx/5xx. A
  transport with a commit-point distinction (SSE, WebSocket) must call
  `open_session` before constructing its `StreamingResponse`/completing
  its handshake, then `run_opened_session` after.
- **`SessionResult`/`SessionDriver`/`SessionConformanceTests`**
  (`session_testing.py`, its own module, not added to the existing
  `testing.py` — the two suites share no scenarios) — covers exactly what
  the design spec's session section specifies: open/reject, ordered
  delivery, clean close, abrupt disconnect, and correlation id stability
  across the session's whole lifetime. Proven against a real in-process
  driver running real handlers through the real `run_session`/
  `InMemorySession` (`tests/test_session_testing.py`), not just hand-built
  result objects — doubles as an integration test of `run_session` itself.
  Promotion to `openframe.core.testing` gated on two session transports
  (SSE, WebSocket) passing this suite for real, same rule-of-three
  discipline as `TransportConformanceTests`.
- `openframe-core` floor bumped to `>=3.2.1,<4` (unchanged — already
  covers 3.8.0).
- 21 new tests. Full monorepo suite: 135 passing (up from 114).
- **Not done as part of this**: any real session transport implementing
  `SessionHandler`/calling `run_session` — this built and proved the
  generic plumbing, not yet a transport. SSE (inside
  `openframe-protocol-http`, per the design spec's own placement
  decision) is next.

---

## [openframe-protocol-grpc 0.3.0] - 2026-10-13

### Added — `bind_service(input_model=...)`, found while wiring a real HTTP-built handler over gRPC

Adding a parallel gRPC entrypoint to a real `openframe-local-validation-framework` service
(sharing the exact same `application/handlers.py` classes the HTTP entrypoint already uses, not
a gRPC-specific duplicate) surfaced two real gaps `bind_service` never needed before, since
nothing had actually called it with a handler built for `-http`'s own validated-input
assumption:

- **Input casting.** A handler like `CreateItem.execute(self, item: Item, ...)` expects a
  validated domain object. FastAPI's `body_model` already validates before the handler ever
  sees it; `bind_service`'s decoded `json_payload` was just a plain dict/list with no casting
  step of its own. `input_model` (any type `pydantic.TypeAdapter` accepts — a `BaseModel`
  subclass, `list[Item]`, etc.) fixes this; `None` (the default) keeps passing the raw decoded
  value through unchanged, so every existing call site is unaffected.
- **Output serialisation.** `json.dumps` has no idea what to do with a `BaseModel` a handler
  returns on its own (`GetItem` returning an `Item`, `ListItems` returning `{"items": [Item,
  ...], "total": N}`). Added `_to_jsonable()` — recursively converts `BaseModel`/dict/list
  results into something `json.dumps` can actually serialise, the gRPC counterpart of
  `response_model`'s implicit job on `-http`.
- A validation failure from an invalid `input_model` cast now categorises as
  `INVALID_ARGUMENT`/`ErrorCategory.INVALID` through the same `categorize()`/`build_envelope()`
  path a handler's own exception goes through — matching how FastAPI's own
  `RequestValidationError` categorises, not a separate uncaught crash.
- `pydantic` moved from `[dev]`-only to a real `[server]` extra runtime dependency —
  `TypeAdapter`/`BaseModel.model_dump` are no longer just a test-time concern.
- 5 new tests (`test_bind_service_input_model.py`). Full monorepo suite: 114 passing (up from
  109).

---

## [infra] - 2026-10-13

### Fixed — real CI failure: `app-test.yml` couldn't install `-http`/`-grpc`

A real GitHub Actions run failed `pip install -e ".[dev]"` for
`openframe-protocol-grpc` with `ERROR: No matching distribution found for
openframe-protocol-base` — confirmed from the actual CI log, not assumed.
Cause: `openframe-protocol-base` isn't published to PyPI (unlike
`openframe-core`/`openframe-adapters-*`), so a clean runner has no way to
resolve it as a dependency of `-http`/`-grpc`'s own `pyproject.toml`. The
same problem the Docker-based validation-framework build hit earlier
(worked around there with local wheels) — CI needed its own fix since
there's no Docker layer to inject a wheel into.

Fixed in `app-test.yml`: added a step installing `openframe-protocol-base`
from the same checkout (`pip install -e packages/openframe-protocol-base`)
before each matrix job's own `pip install -e ".[dev]"` — pip then finds
the requirement already locally satisfied and never reaches the index for
it. Harmless no-op duplication for the `openframe-protocol-base` matrix
entry itself. Verified locally end to end, not just reasoned about: a
from-scratch venv running the exact same two steps installed
`openframe-protocol-base` 0.3.0 + `openframe-protocol-grpc` 0.2.0 cleanly,
26/26 tests passing.

### Added — `.github/requirements/{test,docs}.txt`

Added the local-dev convenience files every sibling repo
(`openframe-core`/`openframe-adapters`/`openframe-tooling`) already has —
`test.txt` (aggregates every package's `[dev]` extras so `pytest
packages/` works from the repo root in one shared environment, not used
by CI itself) and `docs.txt` (mkdocs toolchain, same versions every other
repo pins). Verified `pip install -r .github/requirements/test.txt` into
a from-scratch venv, then `pip install -e` all three packages and
`pytest packages/` — 109/109 passing.

---

## [openframe-protocol-http 0.5.0 / openframe-protocol-grpc 0.2.0] - 2026-10-13

### Added — server/app creation, a boundary M1/M1b deliberately left out

M1/M1b's own design drew the boundary at "protocol configures routes and
error handling on a server object the caller already created" — `FastAPI()`
instantiation, lifespan, middleware registration, and the uvicorn process
stayed hand-written per service. That produced real, demonstrated
duplication: every one of `openframe-local-validation-framework`'s seven
migrated services hand-wrote the identical ~15-line lifespan block
(`setup_telemetry()`, `record_lifecycle_event("cold_start")`, a try/except
around the service's own `initialise()` catching
`(AdapterConnectionError, ValidationError)` for degraded-mode startup,
`yield`, `shutdown()`, `shutdown_telemetry()`). This release moves that
shape into the protocol packages themselves, parameterised by the one
thing that actually differs per service: the startup/shutdown hooks.

- **`openframe-protocol-http`: `create_app(title=, description=,
  on_startup=, on_shutdown=, routers=, degraded_mode_exceptions=)`** —
  returns a configured `FastAPI` instance with the lifespan above,
  `TelemetryMiddleware` added, `install_error_handlers` wired, and every
  router in `routers` included. Does NOT start uvicorn and does NOT decide
  what `on_startup`/`on_shutdown` do internally (still each service's own
  `dependencies.initialise`/`shutdown`) — a service with extra lifespan
  needs (e.g. a background Kafka consumer task) does that extra work
  inside its own hook; `create_app` doesn't need to know it exists.
- **`openframe-protocol-grpc`: `create_server(address=, configure=,
  on_startup=, on_shutdown=, degraded_mode_exceptions=)`** — an async
  context manager, not a plain factory like `create_app`, and deliberately
  so: `grpc.aio.Server` has no external process to hand off to the way
  FastAPI hands off to uvicorn, and `bind_service` calls must register
  before `start()`, not after — `configure` is where those calls belong.
  Yields a started server; stashes the real bound port on
  `server._openframe_bound_port` for the ephemeral-port (`"...:0"`) test
  pattern every conformance suite in this repo already uses.
- Both proven with dedicated tests (not folded into existing conformance
  suites, since this is server*creation*, a different concern from
  request binding): `on_startup`/`on_shutdown` actually fire, a
  degraded-mode exception doesn't crash startup, a non-degraded exception
  still propagates, routes/error-handlers are reachable on the result.
  9 new tests (5 HTTP, 4 gRPC). Full monorepo suite: 109 passing (up from
  99).

### Found and fixed while writing the HTTP test

- `httpx.ASGITransport` alone never runs the ASGI lifespan protocol — it
  only forwards HTTP-scope requests. An early version of the `create_app`
  startup/shutdown test used a raw `httpx.AsyncClient` over it and got an
  empty "nothing called" result that looked like a `create_app` bug but
  wasn't one. Fixed by switching those specific tests to
  `fastapi.testclient.TestClient`'s own context manager, which does drive
  lifespan on `__enter__`/`__exit__`.

---

## [openframe-protocol-http 0.4.0] - 2026-10-13

### Added

- **`bind_query`/`bind_command(not_found_detail=...)`** — a second real
  service (`items-cached`) needed its 404 response's `detail` string to
  include the handler's own input (`"Item 'abc' not found"`), matching
  its pre-migration hand-written route exactly; caught by that service's
  own pre-existing test, not invented for this package. `not_found_when`
  alone only controls whether a 404 is raised, not what it says — the
  generic `"not found"` default (still the fallback when omitted) lost
  that per-route detail during migration. Purely additive.

---

## [openframe-protocol-http 0.3.0] - 2026-10-13

### Added — migrating `openframe-local-validation-framework`'s remaining services

- **`bind_command(query_model=...)`** — a real second service
  (`artifacts-mongo`, later `research-pipeline`) needed a command whose
  input is a path param plus a query param and no body at all (`PATCH
  /{id}/status?status=...`). `_bind` already handled this generically;
  only the public `bind_command` wrapper hadn't exposed `query_model`
  yet, since `items-postgres` (M1) never needed that exact combination.
  Purely additive — every existing call site is unaffected.

### Found and fixed during the build

- **Cross-package `tests/` namespace collision, again**: adding a new
  test file to `openframe-protocol-http` changed test collection order
  enough that `test_client.py`'s own pre-existing `from
  tests.test_conformance import _build_app` (same pattern already fixed
  in `openframe-protocol-grpc`, see that package's `2026-10-13` entry
  below) started resolving to `openframe-protocol-grpc`'s
  `test_conformance.py` instead of its own. Fixed the same way: moved
  `_build_app` into `conftest.py` as a `fastapi_app` fixture, used by
  `test_conformance.py` and `test_client.py` alike instead of either
  importing it from the other.

---

## [openframe-protocol-base 0.3.0 / openframe-protocol-grpc 0.1.0] - 2026-10-13

### Added — M4 (gRPC server + client), per PLAN.md's "M4 — gRPC" section

- **New package, `openframe-protocol-grpc`**: `bind_service` (`server.py`)
  and `GrpcRemoteQueryClient`/`GrpcRemoteCommandClient` (`client.py`) —
  same two-file schema `openframe-protocol-http` established for M1/M1b,
  confirming the schema generalizes past HTTP rather than being an
  HTTP-specific convention.
- One generic RPC (`GenericService.Call`, `{json_payload: string}` both
  ways) instead of a per-route message type — the gRPC equivalent of
  `-http`'s dynamic `inspect.Signature` trick. The route is selected by
  an `x-openframe-route` metadata key, the gRPC equivalent of an HTTP
  path. Reuses `categorize()`/`build_envelope()`/`raise_for_envelope()`
  from `openframe-protocol-base` almost verbatim — only the status-code
  table changes (`grpc.StatusCode` instead of HTTP integers), confirmed
  during the M4 research spike to match `ErrorCategory`'s five members
  1:1, cleaner than HTTP's own mapping.
- `[server]`/`[client]` extras both depend on `grpcio` — unlike HTTP,
  gRPC has no separate client-only wire library; the split is kept
  anyway for schema consistency across every `openframe-protocol-*`
  package, not because the dependency differs here.
- **`openframe-protocol-base`**: `RemoteClientDriver` (Protocol) +
  `RemoteClientConformanceTests` — the reusable client-side conformance
  suite that was missing when M1b shipped (HTTP's client tests were
  ad hoc, not a reusable suite). Built and proven against HTTP first
  (`openframe-protocol-http`'s `test_client.py`, 76 tests total, up from
  71), *then* reused as-is for gRPC — two transports passing the same
  suite is what actually proves `raise_for_envelope()`'s reconstruction
  logic isn't accidentally HTTP-shaped.
- **Both `TransportConformanceTests` and `RemoteClientConformanceTests`
  now pass for a second real transport** (`grpc.aio`, a real loopback
  socket — confirmed `grpc.aio` has no true in-memory transport, same
  rigor as HTTP's `httpx.ASGITransport`). Per draft 8's rule-of-three
  discipline, this is what would justify promoting both suites into
  `openframe.core.testing` — not done automatically by this build; a
  separate, deliberate step.
- 22 new tests in `openframe-protocol-grpc`. Full monorepo suite: 98
  passing (up from 76).

### Found and fixed during the build

- **Cross-package `tests/` module-name collision**: `openframe-protocol-grpc`'s
  `test_client.py` originally did `from tests.test_conformance import
  _build_server`, mirroring `-http`'s own existing pattern — but once
  both packages' `tests/` directories (neither has `__init__.py`) are on
  `sys.path` in the same pytest session, Python merges them into one
  namespace package, and the plain import silently resolved to the
  *other* package's `test_conformance.py`. Fixed by moving the shared
  server-builder into `conftest.py` as a `grpc_server` fixture instead of
  a cross-module import — pytest loads each `conftest.py` by its own
  file path, which isn't subject to this ambiguity. Confirmed by running
  the full `pytest packages/` sweep (98 passed), not just each package
  in isolation.
- **`generic_pb2_grpc.py`'s generated import was a bare `import
  generic_pb2`**, the flat-layout default `grpc_tools.protoc` emits —
  breaks immediately once the stub lives inside a real package
  (`openframe.protocol.grpc`) rather than a standalone script directory.
  Fixed to `from openframe.protocol.grpc import generic_pb2`.

### Status

M0, M1, M1b, M4 complete. MCP, sessions, and every other media protocol
remain out of scope — gated on their own open design decisions per the
project's spec. The true cross-service Docker-based check remains
deferred until Docker is available in this environment.

---

## [openframe-protocol-base 0.2.0 / openframe-protocol-http 0.2.0] - 2026-10-11

### Added — M1b (HTTP client, service-to-service calls)

- **`openframe-core` v3.7.0 dependency**: `openframe.core.remote` —
  `RemoteCommandClient[TIn]`/`RemoteQueryClient[TIn, TOut]`, the outbound
  counterparts of `core.inbound`'s `CommandHandler`/`QueryHandler`.
  Structurally identical signature, deliberately separate classes (see
  that module's own docstring); no `BasePort`/`Capability` involvement
  per draft 8's D10 — no dynamic "which remote client fills this role"
  lookup use case exists, a caller always knows exactly which service
  it's calling.
- **`openframe-protocol-base`**: `raise_for_envelope()` — the client-side
  reverse of `build_envelope()`, reconstructing the right `AdapterError`
  subclass from a received error envelope. `INVALID`/`INTERNAL` both
  reconstruct as `AdapterQueryError` (core's `AdapterError` family has no
  dedicated validation-error subclass to mirror symmetrically) —
  documented as a deliberate choice. Proven as genuine inverses of
  `build_envelope`, not just independently-plausible mappings, via a
  round-trip test.
- **`openframe-protocol-http`**: `HttpRemoteQueryClient`/
  `HttpRemoteCommandClient` + `RequestSpec`. Request construction
  mirrors `server.py`'s own `build_input` philosophy — a `build_request`
  hook returns exactly which path params, query params, and JSON body a
  call needs, rather than guessing how to serialise an arbitrary input.
  No `PluginRegistry` lifecycle — each client owns (or borrows) a plain
  `httpx.AsyncClient`, closed via its own `aclose()`.
- Validated end-to-end against a real, in-process FastAPI app built with
  this same package's own server binder (`httpx.ASGITransport`, real
  routing/DI/exception-handlers, nothing mocked except the TCP socket) —
  including a genuine network-level connection refusal (not a simulated
  503) and a real OTel traceparent round trip (client injects, server's
  derived correlation id matches the exact trace id, not just "some
  non-empty value"). A true cross-service Docker-based check (this
  client calling a separately-running container) remains a further step
  once Docker is available in this environment.
- 14 new tests. Full suite: 71 passing (up from 57).

### Found and fixed during the build

- **`__init__.py` eagerly imported both `server.py` (needs FastAPI) and
  `client.py` (needs httpx) unconditionally** — silently defeating the
  `[server]`/`[client]` extras split this package's own `pyproject.toml`
  already promised; installing just `[client]` would still raise
  `ImportError` on the unrelated FastAPI dependency the moment anything
  imported `openframe.protocol.http` at all. Fixed with lazy
  `__getattr__`-based submodule imports (PEP 562) before this shipped,
  not after. Added the missing `[client]` extra (`httpx>=0.27`) itself,
  which had never been declared at all despite being planned from M1.

### Status

M1 and M1b complete. gRPC, MCP, sessions, and every media protocol
remain out of scope — gated on their own open design decisions per the
project's spec.

---

## [openframe-protocol-base 0.1.0 / openframe-protocol-http 0.1.0 / meta 0.1.0] - 2026-10-11

### Added — M1 (base package + HTTP server binding), per PLAN.md

- **`openframe-protocol-base`** — `errors.py` (`categorize()`/`build_envelope()`,
  table-tested against every real `AdapterError`/`PluginError` subclass
  in the installed `openframe-core`, not a hand-maintained list), `context.py`
  (`build_request_context()`), and `testing.py` (`TransportConformanceTests`
  + `TransportDriver`, the suite every binding proves itself against).
  Incubating here rather than in `openframe-core`, per draft 8's own
  rule-of-three discipline — promoted to core only once a second
  transport (gRPC) passes the same suite for real.
- **`openframe-protocol-http`** — `bind_query`/`bind_command` wiring a
  `QueryHandler`/`UseCase`/`CommandHandler` to a FastAPI route through
  the shared error mapper and `RequestContext` builder, plus
  `install_error_handlers`. Spiked and confirmed against a real
  `TestClient` before building: FastAPI 0.143 supports a Pydantic
  `BaseModel` as a query dependency via `Annotated[Model, Query()]`, and
  a dynamically-constructed `inspect.Signature` assigned to a plain
  `async def (**kwargs)` endpoint — both load-bearing for a binder that
  doesn't need one hand-written function signature per route.
- **Migrated `items-postgres`** (`openframe-local-validation-framework`,
  patch only, `ItemService` unchanged): new thin handler classes in
  `application/handlers.py` (all `UseCase`-shaped, not `CommandHandler`
  — every route here returns a value the caller needs, and
  `CommandHandler.execute()` always returns `None` by contract);
  `routes.py` rewritten onto the binder with identical paths and status
  codes; `main.py` wires `install_error_handlers`; new
  `test_architecture.py` rules enforcing the transport-import boundary
  (mirroring the existing `openframe.adapters` boundary rules). Core
  floor bumped to `>=3.6,<4` for guaranteed traceparent extraction.
  **57/57 relevant tests pass** (42 architecture/service/domain/bootstrap
  + 15 routes) — zero regressions. (7 `test_adapter.py` + 4
  `test_observability.py` failures are pre-existing, unrelated — real
  Postgres/OTel-collector connections this sandbox has neither for.)

### Found and fixed during the build (not left as follow-ups)

- **A real bug in `build_request_context`'s own design, not just an
  implementation slip**: checking `trace.get_current_span()` (ambient
  OTel state) cannot distinguish "a trace genuinely propagated in from
  an upstream caller" from "`TelemetryMiddleware` started its own fresh
  local span because there was no incoming trace at all" — the latter
  always has a valid span context too, so an explicit
  `X-Correlation-ID` header could never win once `TelemetryMiddleware`
  was wired into a real app, even though the original design's resolution
  order said it should. Caught only because the HTTP conformance driver
  was built with `TelemetryMiddleware` actually wired in, not a bare
  toy app — an earlier, unwired version of the same test suite passed
  on this exact bug by accident. Fixed by extracting directly from the
  request's own headers (`propagate.extract(headers)`) instead of
  ambient state.
- **`httpx.ASGITransport`'s `raise_app_exceptions=True` default** (its
  own, not this project's) re-raises an unhandled exception into the
  test itself instead of letting `install_error_handlers`'s own
  `@app.exception_handler(Exception)` catch it and return a real 500 —
  the opposite of what a real `uvicorn` deployment does. Set to `False`
  in the conformance driver to test the real production error path.
- **`BulkImport` handler dropped the original route's response shape**
  during migration — `ItemService.bulk_import()` returns a plain `int`;
  the original hand-written route wrapped it as `{"imported": N}`
  before returning it. Caught by `test_routes.py`'s own pre-existing
  assertion, not missed silently.
- **Handler-dependency functions initially called `get_item_service()`
  as a plain function** instead of through `Depends(get_item_service)`
  — bypassing FastAPI's dependency graph entirely, which silently broke
  `test_routes.py`'s own `app.dependency_overrides[get_item_service]`
  mock-override mechanism. Fixed before it shipped, not after.

### Status

M1 complete. M1b (HTTP client, service-to-service), gRPC, MCP, sessions,
and every media protocol remain out of scope for this release — gated on
their own open design decisions (D10 especially) per the project's spec.
