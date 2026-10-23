"""
openframe/protocol/media/ranges.py
======================================
HTTP byte-range parsing/resolution per RFC 9110 §14.1.2/§14.2 — pure
stdlib, no `fastapi`, so it can be unit-tested (and reused by a future
non-HTTP transport, hypothetically) without the `[delivery]` extra.

Two distinct outcomes are modelled deliberately as two different things,
not one:

- "There's effectively no usable Range request" (no header, wrong unit,
  a malformed value, or a multi-range request) -> `parse_range_header`
  returns `None`, meaning "serve the normal full 200 response". RFC 9110
  §14.2 explicitly permits a server to ignore a Range header it doesn't
  want to honor rather than reject it; multi-range (`multipart/byteranges`)
  support is skipped on purpose here — a media player only ever requests
  one contiguous range at a time in real seeking behaviour, so the extra
  complexity of assembling a multipart response buys nothing for this
  package's actual audience (hls.js/dash.js/native `<video>` range
  requests), and treating it as "ignore, serve 200" rather than
  implementing it is exactly the escape hatch the RFC built in for this.
- "There IS a well-formed, single, `bytes`-unit range, but it doesn't fit
  the object" -> `UnsatisfiableRangeError`, which the binder turns into a
  real 416. This is the one case that really is an error: the client
  asked for something specific and nameable that doesn't exist (e.g.
  `bytes=9999-` against a 100-byte object), as opposed to a request this
  server simply doesn't know how to interpret.

Dependency order:
    protocol/media/ranges → protocol/media/types (stdlib only)
"""
from __future__ import annotations

import re

from openframe.protocol.media.types import MediaRange

__all__ = ["UnsatisfiableRangeError", "parse_range_header", "resolve_range"]

_SINGLE_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


class UnsatisfiableRangeError(Exception):
    """
    Raised when a Range header IS well-formed (single, `bytes`-unit) but
    doesn't fit within the object's real size — the one case that maps to
    a real 416, as opposed to every other malformed/unsupported shape,
    which `parse_range_header` treats as "ignore it, serve 200 instead"
    per RFC 9110 §14.2.
    """


def parse_range_header(header_value: str | None, total_size: int) -> MediaRange | None:
    """
    Parse a raw `Range` request header into a `MediaRange`.

    Returns `None` — meaning "treat this as if there were no Range header
    at all, serve the full 200" — for every one of these cases, per RFC
    9110 §14.2's permission to ignore a Range request outright:

    - No header at all (`header_value is None`).
    - A non-`bytes` unit (e.g. `items=0-5` — RFC 9110 reserves the
      registry for other units; this package only ever serves bytes).
    - A multi-range request (`bytes=0-10,20-30`) — not supported, see
      this module's own docstring for why.
    - Anything else that doesn't match the single-range grammar at all
      (garbage text, missing `=`, etc).

    Raises `UnsatisfiableRangeError` only for a single well-formed
    `bytes=` range that is syntactically fine but cannot be satisfied
    against `total_size` — a zero-length suffix (`bytes=-0`) or a `start`
    at or beyond `total_size`. Resolution of the concrete `(start, end)`
    offsets (including the suffix-length and open-ended forms) is done by
    `resolve_range`, called here only far enough to validate
    satisfiability eagerly, so a caller gets the 416 at parse time rather
    than later.
    """
    if header_value is None:
        return None

    match = _SINGLE_RANGE_RE.match(header_value.strip())
    if match is None:
        # Either a non-"bytes" unit, a multi-range list, or outright
        # garbage — all are "ignore it" per RFC 9110 §14.2.
        return None

    start_str, end_str = match.group(1), match.group(2)
    if start_str == "" and end_str == "":
        # "bytes=-" — neither a start nor a suffix length. Malformed;
        # ignore rather than error.
        return None

    if start_str == "":
        # "bytes=-500" form — suffix length.
        suffix_length = int(end_str)
        media_range = MediaRange(suffix_length=suffix_length)
    else:
        start = int(start_str)
        end = int(end_str) if end_str != "" else None
        if end is not None and end < start:
            # "bytes=500-100" — syntactically parses but backwards;
            # not a shape the RFC's grammar actually allows. Ignore it.
            return None
        media_range = MediaRange(start=start, end=end)

    # Validate satisfiability eagerly so a malformed-but-plausible range
    # fails fast as a 416 rather than surfacing confusingly later.
    resolve_range(media_range, total_size)
    return media_range


def resolve_range(range_: MediaRange, total_size: int) -> tuple[int, int]:
    """
    Resolve a `MediaRange` (which may carry only `start`, only
    `suffix_length`, or `start`+`end`) against a known `total_size`,
    returning the concrete inclusive `(start, end)` byte offsets.

    Raises `UnsatisfiableRangeError` if the range doesn't fit:
    - `suffix_length == 0` is never satisfiable (there is no such thing
      as "the last zero bytes" as a servable range).
    - `start >= total_size` is never satisfiable — note `start ==
      total_size - 1` (the single last byte) IS satisfiable; the
      boundary is `total_size` itself, one past the last valid offset.
    """
    if range_.suffix_length is not None:
        if range_.suffix_length <= 0:
            raise UnsatisfiableRangeError(f"suffix length {range_.suffix_length} is not satisfiable")
        start = max(0, total_size - range_.suffix_length)
        end = total_size - 1
        if end < start:
            raise UnsatisfiableRangeError("suffix range is not satisfiable against an empty object")
        return start, end

    start = range_.start if range_.start is not None else 0
    if start < 0 or start >= total_size:
        raise UnsatisfiableRangeError(f"start {start} is beyond total_size {total_size}")
    end = range_.end if range_.end is not None else total_size - 1
    end = min(end, total_size - 1)
    return start, end
