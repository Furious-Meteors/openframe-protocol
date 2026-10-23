"""
openframe.protocol.media
============================
RTMP ingest (write side), HLS/DASH pull ingest (poll side), and HLS/DASH
delivery (read side) — the first binary-payload transport in the
`openframe-protocol-*` family, after HTTP/gRPC/SSE/WebSocket's text/JSON
events. See `ingest.py`'s own module docstring for the full write-up of
the real `pyrtmp` + `ffmpeg` spike the RTMP ingest half is built directly
on top of, `hls_pull.py`'s own module docstring for the two real spikes
(real `ffmpeg` HLS output + real `m3u8`/`httpx` polling) the HLS pull-ingest
half is built on, `dash_pull.py`'s own module docstring for the real spike
(real `ffmpeg` DASH output + the real `mpegdash` library) the DASH
pull-ingest half is built on, and `delivery.py`'s own module docstring for
the design behind the delivery half.

- `ingest.py` (needs the `[ingest]` extra, `pyrtmp`) — `RtmpSession`,
  `RtmpRegistry`, `bind_ingest`, `create_server`: wires a `SessionHandler`
  (ADR-008, `openframe-core`) to a real RTMP publish connection.
- `hls_pull.py` (needs the `[pull]` extra, `httpx` + `m3u8`) —
  `HlsPullSession`, `pull_hls`: polls a remote HLS origin's live playlist
  and feeds newly-appeared segments (`openframe.core.media.MediaSegment`)
  into a `SessionHandler`, outbound-HTTP-poll ingest rather than an
  inbound connection of any kind — the third, architecturally distinct
  capability in this package after RTMP ingest (push) and HLS/DASH
  delivery (serve).
- `dash_pull.py` (needs the `[pull]` extra, `httpx` + `mpegdash`) —
  `DashPullSession`, `pull_dash`: the DASH counterpart to `hls_pull.py`,
  polling a remote DASH origin's live MPD and feeding newly-appeared
  segments into a `SessionHandler`. The fourth and final planned
  capability in this package — architecturally identical to `hls_pull.py`
  (same outbound-HTTP-poll shape), differing only in manifest format and
  URL-resolution logic (DASH `SegmentTemplate`/`SegmentTimeline`
  resolution, which `mpegdash` itself does none of — see its own module
  docstring).
- `flow.py` (zero third-party dependencies, always importable) —
  `MediaFlowController`/`DropPolicy`/`FlowConfig`/`FlowStats`/
  `BackPressureExceeded`/`MediaGap`: transport-neutral back-pressure
  between a binary media producer and whatever consumes `MediaFrame`s
  downstream. Deliberately NOT gated behind `[ingest]` — a consumer
  should be able to unit-test flow control without installing `pyrtmp`
  at all, and a hypothetical future RTSP/SRT/WebRTC ingest binding can
  reuse it unchanged, since it imports nothing RTMP-specific.
- `types.py` (zero third-party dependencies, always importable) —
  `MediaRequest`/`MediaObject`/`MediaRange`/`MediaProfile`/
  `MediaObjectKind`/`HLS_PROFILE`/`DASH_PROFILE`/`MediaCachePolicy`/
  `DEFAULT_CACHE`/`MediaCorsPolicy`/`DEFAULT_CORS`: the vocabulary a
  service's own `QueryHandler` implementation binds against. Pure stdlib
  on purpose — a handler module needs to spell `MediaRequest`/
  `MediaObject` in its own type annotations without installing the
  `[delivery]` extra (fastapi) just to do that.
- `ranges.py` (zero third-party dependencies, always importable) —
  `parse_range_header`/`resolve_range`/`UnsatisfiableRangeError`: RFC
  9110 §14.1.2/§14.2 byte-range parsing and resolution, independent of
  any ASGI framework.
- `delivery.py` (needs the `[delivery]` extra, fastapi) —
  `bind_media_delivery`/`bind_hls_delivery`/`bind_dash_delivery`: wires a
  `QueryHandler[MediaRequest, MediaObject]` (`openframe.core.inbound`) to
  a real HTTP GET/HEAD route serving HLS/DASH manifests and segments with
  full Range support.

Lazily imported for `ingest.py`'s, `hls_pull.py`'s, `dash_pull.py`'s, and
`delivery.py`'s own names (PEP 562, `__getattr__` below), matching
`-grpc`'s/`-http`'s own `__init__.py` shape exactly — `flow.py`'s and
`types.py`'s/`ranges.py`'s names are imported eagerly instead, since none
of them carry an extra dependency to defer.

Dependency order:
    protocol/media → protocol/media/flow, protocol/media/types,
    protocol/media/ranges, protocol/media/ingest, protocol/media/hls_pull,
    protocol/media/dash_pull, protocol/media/delivery

.. stability: experimental
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from openframe.protocol.media.flow import (
    BackPressureExceeded,
    DropPolicy,
    FlowConfig,
    FlowStats,
    MediaFlowController,
    MediaGap,
)
from openframe.protocol.media.ranges import (
    UnsatisfiableRangeError,
    parse_range_header,
    resolve_range,
)
from openframe.protocol.media.types import (
    DASH_PROFILE,
    DEFAULT_CACHE,
    DEFAULT_CORS,
    HLS_PROFILE,
    MediaCachePolicy,
    MediaCorsPolicy,
    MediaObject,
    MediaObjectKind,
    MediaProfile,
    MediaRange,
    MediaRequest,
)

if TYPE_CHECKING:
    from openframe.protocol.media.dash_pull import DashPullSession, pull_dash
    from openframe.protocol.media.delivery import (
        bind_dash_delivery,
        bind_hls_delivery,
        bind_media_delivery,
    )
    from openframe.protocol.media.hls_pull import HlsPullSession, pull_hls
    from openframe.protocol.media.ingest import (
        RtmpRegistry,
        RtmpSession,
        bind_ingest,
        create_server,
    )

__all__ = [
    "DASH_PROFILE",
    "DEFAULT_CACHE",
    "DEFAULT_CORS",
    "HLS_PROFILE",
    "BackPressureExceeded",
    "DashPullSession",
    "DropPolicy",
    "FlowConfig",
    "FlowStats",
    "HlsPullSession",
    "MediaCachePolicy",
    "MediaCorsPolicy",
    "MediaFlowController",
    "MediaGap",
    "MediaObject",
    "MediaObjectKind",
    "MediaProfile",
    "MediaRange",
    "MediaRequest",
    "RtmpRegistry",
    "RtmpSession",
    "UnsatisfiableRangeError",
    "bind_dash_delivery",
    "bind_hls_delivery",
    "bind_ingest",
    "bind_media_delivery",
    "create_server",
    "parse_range_header",
    "pull_dash",
    "pull_hls",
    "resolve_range",
]

_FROM_INGEST = {"RtmpRegistry", "RtmpSession", "bind_ingest", "create_server"}
_FROM_HLS_PULL = {"HlsPullSession", "pull_hls"}
_FROM_DASH_PULL = {"DashPullSession", "pull_dash"}
_FROM_DELIVERY = {"bind_media_delivery", "bind_hls_delivery", "bind_dash_delivery"}


def __getattr__(name: str):
    if name in _FROM_INGEST:
        from openframe.protocol.media import ingest

        return getattr(ingest, name)
    if name in _FROM_HLS_PULL:
        from openframe.protocol.media import hls_pull

        return getattr(hls_pull, name)
    if name in _FROM_DASH_PULL:
        from openframe.protocol.media import dash_pull

        return getattr(dash_pull, name)
    if name in _FROM_DELIVERY:
        from openframe.protocol.media import delivery

        return getattr(delivery, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
