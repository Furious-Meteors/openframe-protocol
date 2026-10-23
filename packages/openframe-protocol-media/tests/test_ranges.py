"""
Pure unit tests for `openframe.protocol.media.ranges` — no FastAPI, no
network. Covers all three Range forms (`bytes=start-end`, `bytes=start-`,
`bytes=-suffix`), the "ignore it, serve 200" cases RFC 9110 §14.2 permits
(wrong unit, multi-range, garbage), and the exact boundary conditions
around satisfiability.
"""
from __future__ import annotations

import pytest
from openframe.protocol.media.ranges import (
    UnsatisfiableRangeError,
    parse_range_header,
    resolve_range,
)
from openframe.protocol.media.types import MediaRange

TOTAL = 1000


# ---------------------------------------------------------------------------
# parse_range_header — ignored (None) cases
# ---------------------------------------------------------------------------


def test_no_header_returns_none():
    assert parse_range_header(None, TOTAL) is None


def test_wrong_unit_is_ignored():
    assert parse_range_header("items=0-499", TOTAL) is None


def test_multi_range_is_ignored():
    assert parse_range_header("bytes=0-99,200-299", TOTAL) is None


def test_garbage_is_ignored():
    assert parse_range_header("not a range at all", TOTAL) is None


def test_empty_string_is_ignored():
    assert parse_range_header("", TOTAL) is None


def test_bytes_dash_only_is_ignored():
    assert parse_range_header("bytes=-", TOTAL) is None


def test_backwards_range_is_ignored():
    assert parse_range_header("bytes=500-100", TOTAL) is None


# ---------------------------------------------------------------------------
# parse_range_header — the three satisfiable forms
# ---------------------------------------------------------------------------


def test_start_end_form():
    result = parse_range_header("bytes=0-499", TOTAL)
    assert result == MediaRange(start=0, end=499)


def test_open_ended_form():
    result = parse_range_header("bytes=500-", TOTAL)
    assert result == MediaRange(start=500, end=None)


def test_suffix_form():
    result = parse_range_header("bytes=-500", TOTAL)
    assert result == MediaRange(suffix_length=500)


def test_whitespace_is_tolerated():
    result = parse_range_header("  bytes=0-499  ", TOTAL)
    assert result == MediaRange(start=0, end=499)


# ---------------------------------------------------------------------------
# parse_range_header — unsatisfiable (416) cases, raised eagerly
# ---------------------------------------------------------------------------


def test_zero_length_suffix_is_unsatisfiable():
    with pytest.raises(UnsatisfiableRangeError):
        parse_range_header("bytes=-0", TOTAL)


def test_start_beyond_total_size_is_unsatisfiable():
    with pytest.raises(UnsatisfiableRangeError):
        parse_range_header(f"bytes={TOTAL}-", TOTAL)


def test_start_well_beyond_total_size_is_unsatisfiable():
    with pytest.raises(UnsatisfiableRangeError):
        parse_range_header("bytes=99999-", TOTAL)


# ---------------------------------------------------------------------------
# resolve_range — concrete offset resolution + boundaries
# ---------------------------------------------------------------------------


def test_resolve_start_end():
    assert resolve_range(MediaRange(start=0, end=499), TOTAL) == (0, 499)


def test_resolve_open_ended():
    assert resolve_range(MediaRange(start=500, end=None), TOTAL) == (500, TOTAL - 1)


def test_resolve_suffix():
    assert resolve_range(MediaRange(suffix_length=500), TOTAL) == (500, TOTAL - 1)


def test_resolve_suffix_larger_than_total_clamps_to_whole_object():
    # "last 5000 bytes" of a 1000-byte object -> the whole object.
    assert resolve_range(MediaRange(suffix_length=5000), TOTAL) == (0, TOTAL - 1)


def test_resolve_end_beyond_total_is_clamped_not_unsatisfiable():
    # bytes=0-99999 against a 1000-byte object: clamp end to the last byte,
    # per RFC 9110 — a too-large end is NOT itself an error.
    assert resolve_range(MediaRange(start=0, end=99999), TOTAL) == (0, TOTAL - 1)


def test_resolve_last_byte_is_satisfiable():
    # start == total_size - 1 is the single last byte — satisfiable.
    assert resolve_range(MediaRange(start=TOTAL - 1, end=None), TOTAL) == (TOTAL - 1, TOTAL - 1)


def test_resolve_start_at_total_size_is_unsatisfiable():
    # start == total_size itself (one past the last valid offset) is not.
    with pytest.raises(UnsatisfiableRangeError):
        resolve_range(MediaRange(start=TOTAL, end=None), TOTAL)


def test_resolve_start_beyond_total_size_is_unsatisfiable():
    with pytest.raises(UnsatisfiableRangeError):
        resolve_range(MediaRange(start=TOTAL + 1, end=None), TOTAL)


def test_resolve_zero_length_suffix_is_unsatisfiable():
    with pytest.raises(UnsatisfiableRangeError):
        resolve_range(MediaRange(suffix_length=0), TOTAL)


def test_resolve_negative_suffix_is_unsatisfiable():
    with pytest.raises(UnsatisfiableRangeError):
        resolve_range(MediaRange(suffix_length=-1), TOTAL)


def test_resolve_against_zero_size_object_is_unsatisfiable():
    with pytest.raises(UnsatisfiableRangeError):
        resolve_range(MediaRange(start=0, end=None), 0)


def test_resolve_suffix_against_zero_size_object_is_unsatisfiable():
    with pytest.raises(UnsatisfiableRangeError):
        resolve_range(MediaRange(suffix_length=10), 0)


def test_resolve_full_single_byte_object():
    assert resolve_range(MediaRange(start=0, end=0), 1) == (0, 0)
