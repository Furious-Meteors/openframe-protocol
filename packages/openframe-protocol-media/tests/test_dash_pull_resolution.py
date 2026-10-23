"""
Pure unit tests for `dash_pull.py`'s own `SegmentTemplate`/`SegmentTimeline`
resolver (`_expand_segment_timeline` + `_resolve_template_url`), against
hand-constructed MPD XML fixtures with KNOWN expected resolved URLs --
`mpegdash` itself does zero resolution (confirmed by reading its source,
see `dash_pull.py`'s own module docstring finding 1), so this is the one
piece of DASH pull entirely hand-written and in need of its own direct
coverage, independent of any real server or ffmpeg.

The "multiple independent `<S>` elements with different `d` values" test
below (`test_timeline_with_independent_s_elements_different_durations`)
is the single most important test in this file -- it's the exact shape a
real ffmpeg AAC audio track produced in this session's own spike (see
module docstring finding 2), and is the case a naive "one `<S>` with
`r=N-1`" implementation would silently get wrong.
"""
from __future__ import annotations

from mpegdash.parser import MPEGDASHParser
from openframe.protocol.media.dash_pull import (
    _expand_segment_timeline,
    _iter_representations,
    _live_edge_seconds,
    _parse_iso8601_datetime,
    _parse_iso8601_duration_seconds,
    _resolve_template_url,
)


def _mpd(adaptation_sets_xml: str, *, mpd_type: str = "dynamic", extra_root_attrs: str = "") -> object:
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" profiles="urn:mpeg:dash:profile:isoff-live:2011"
     type="{mpd_type}" minBufferTime="PT4.0S" {extra_root_attrs}>
  <Period id="0" start="PT0.0S">
    {adaptation_sets_xml}
  </Period>
</MPD>"""
    return MPEGDASHParser.parse(xml)


# ---------------------------------------------------------------------------
# $Number%05d$ substitution
# ---------------------------------------------------------------------------


def test_number_formatted_substitution():
    mpd = _mpd(
        """
        <AdaptationSet id="0" contentType="video">
          <Representation id="v0" mimeType="video/mp4" codecs="avc1" bandwidth="100000">
            <SegmentTemplate timescale="90000" startNumber="1"
                             media="chunk-$RepresentationID$-$Number%05d$.m4s"
                             initialization="init-$RepresentationID$.m4s">
              <SegmentTimeline>
                <S t="0" d="180000" r="2" />
              </SegmentTimeline>
            </SegmentTemplate>
          </Representation>
        </AdaptationSet>
        """
    )
    reps = _iter_representations(mpd)
    assert len(reps) == 1
    representation_id, template = reps[0]
    assert representation_id == "v0"
    entries = _expand_segment_timeline(template)
    assert [(e.number, e.time, e.duration) for e in entries] == [
        (1, 0, 180000), (2, 180000, 180000), (3, 360000, 180000),
    ]
    urls = [
        _resolve_template_url(template.media, representation_id=representation_id, number=e.number, time=e.time)
        for e in entries
    ]
    assert urls == ["chunk-v0-00001.m4s", "chunk-v0-00002.m4s", "chunk-v0-00003.m4s"]


# ---------------------------------------------------------------------------
# $Time$ substitution
# ---------------------------------------------------------------------------


def test_time_substitution():
    mpd = _mpd(
        """
        <AdaptationSet id="0" contentType="video">
          <Representation id="v0" mimeType="video/mp4" codecs="avc1" bandwidth="100000">
            <SegmentTemplate timescale="90000" startNumber="1" media="seg-$RepresentationID$-$Time$.m4s">
              <SegmentTimeline>
                <S t="0" d="180000" />
                <S d="180000" />
              </SegmentTimeline>
            </SegmentTemplate>
          </Representation>
        </AdaptationSet>
        """
    )
    representation_id, template = _iter_representations(mpd)[0]
    entries = _expand_segment_timeline(template)
    urls = [
        _resolve_template_url(template.media, representation_id=representation_id, number=e.number, time=e.time)
        for e in entries
    ]
    assert urls == ["seg-v0-0.m4s", "seg-v0-180000.m4s"]


def test_time_formatted_substitution():
    assert _resolve_template_url(
        "seg-$Time%08d$.m4s", representation_id="v0", number=1, time=42,
    ) == "seg-00000042.m4s"


# ---------------------------------------------------------------------------
# The single most important test: independent <S> elements, different d
# values each -- the real audio-track shape from the session's own spike.
# ---------------------------------------------------------------------------


def test_timeline_with_independent_s_elements_different_durations():
    mpd = _mpd(
        """
        <AdaptationSet id="1" contentType="audio">
          <Representation id="a0" mimeType="audio/mp4" codecs="mp4a.40.2" bandwidth="69000">
            <SegmentTemplate timescale="44100" startNumber="1" media="chunk-$RepresentationID$-$Number%05d$.m4s">
              <SegmentTimeline>
                <S t="0" d="88064" />
                <S d="89088" />
                <S d="88064" r="1" />
                <S d="87584" />
              </SegmentTimeline>
            </SegmentTemplate>
          </Representation>
        </AdaptationSet>
        """
    )
    _representation_id, template = _iter_representations(mpd)[0]
    entries = _expand_segment_timeline(template)

    # Five total segments: 1 (from the bare <S>) + 1 (from the next bare <S>) +
    # 2 (from <S r="1">, i.e. r+1 repetitions) + 1 (trailing bare <S>).
    assert [(e.number, e.time, e.duration) for e in entries] == [
        (1, 0, 88064),
        (2, 88064, 89088),
        (3, 177152, 88064),
        (4, 265216, 88064),
        (5, 353280, 87584),
    ]
    # current_time must have been carried forward across <S> elements that omit t=,
    # never reset to 0 or to the element's own (absent) t.
    assert entries[-1].time == entries[0].time + sum(e.duration for e in entries[:-1])


# ---------------------------------------------------------------------------
# r repeat-count expansion (single <S>, clean case -- the video-track shape)
# ---------------------------------------------------------------------------


def test_repeat_count_expansion():
    mpd = _mpd(
        """
        <AdaptationSet id="0" contentType="video">
          <Representation id="v0" mimeType="video/mp4" codecs="avc1" bandwidth="100000">
            <SegmentTemplate timescale="15360" startNumber="1" media="chunk-$RepresentationID$-$Number%05d$.m4s">
              <SegmentTimeline>
                <S t="0" d="30720" r="3" />
              </SegmentTimeline>
            </SegmentTemplate>
          </Representation>
        </AdaptationSet>
        """
    )
    _, template = _iter_representations(mpd)[0]
    entries = _expand_segment_timeline(template)
    # r="3" -> r + 1 = 4 segments.
    assert len(entries) == 4
    assert [e.number for e in entries] == [1, 2, 3, 4]
    assert [e.time for e in entries] == [0, 30720, 61440, 92160]


# ---------------------------------------------------------------------------
# startNumber offset handling
# ---------------------------------------------------------------------------


def test_start_number_offset():
    mpd = _mpd(
        """
        <AdaptationSet id="0" contentType="video">
          <Representation id="v0" mimeType="video/mp4" codecs="avc1" bandwidth="100000">
            <SegmentTemplate timescale="15360" startNumber="1061" media="chunk-$RepresentationID$-$Number%05d$.m4s">
              <SegmentTimeline>
                <S t="32563200" d="30720" r="4" />
              </SegmentTimeline>
            </SegmentTemplate>
          </Representation>
        </AdaptationSet>
        """
    )
    _, template = _iter_representations(mpd)[0]
    entries = _expand_segment_timeline(template)
    assert [e.number for e in entries] == [1061, 1062, 1063, 1064, 1065]
    assert entries[0].time == 32563200


def test_default_start_number_is_one_when_absent():
    mpd = _mpd(
        """
        <AdaptationSet id="0" contentType="video">
          <Representation id="v0" mimeType="video/mp4" codecs="avc1" bandwidth="100000">
            <SegmentTemplate timescale="15360" media="chunk-$RepresentationID$-$Number%05d$.m4s">
              <SegmentTimeline>
                <S t="0" d="30720" />
              </SegmentTimeline>
            </SegmentTemplate>
          </Representation>
        </AdaptationSet>
        """
    )
    _, template = _iter_representations(mpd)[0]
    entries = _expand_segment_timeline(template)
    assert entries[0].number == 1


# ---------------------------------------------------------------------------
# $RepresentationID$ substitution, including the initialization= string
# (resolution is implemented for it even though dash_pull.py never fetches
# it -- see module docstring's explicit init-segment scope decision)
# ---------------------------------------------------------------------------


def test_representation_id_substitution_in_media_and_initialization():
    mpd = _mpd(
        """
        <AdaptationSet id="0" contentType="video">
          <Representation id="video-7" mimeType="video/mp4" codecs="avc1" bandwidth="100000">
            <SegmentTemplate timescale="15360" startNumber="1"
                             media="chunk-$RepresentationID$-$Number%05d$.m4s"
                             initialization="init-$RepresentationID$.m4s">
              <SegmentTimeline>
                <S t="0" d="30720" />
              </SegmentTimeline>
            </SegmentTemplate>
          </Representation>
        </AdaptationSet>
        """
    )
    representation_id, template = _iter_representations(mpd)[0]
    assert representation_id == "video-7"
    assert _resolve_template_url(
        template.media, representation_id=representation_id, number=1, time=0,
    ) == "chunk-video-7-00001.m4s"
    assert _resolve_template_url(
        template.initialization, representation_id=representation_id, number=1, time=0,
    ) == "init-video-7.m4s"


# ---------------------------------------------------------------------------
# Multiple AdaptationSets (video + audio) handled independently, each with
# its own Representation/SegmentTemplate -- no rendition SELECTION involved.
# ---------------------------------------------------------------------------


def test_multiple_adaptation_sets_both_resolved_independently():
    mpd = _mpd(
        """
        <AdaptationSet id="0" contentType="video">
          <Representation id="0" mimeType="video/mp4" codecs="avc1" bandwidth="100000">
            <SegmentTemplate timescale="15360" startNumber="1" media="chunk-$RepresentationID$-$Number%05d$.m4s">
              <SegmentTimeline><S t="0" d="30720" r="1" /></SegmentTimeline>
            </SegmentTemplate>
          </Representation>
        </AdaptationSet>
        <AdaptationSet id="1" contentType="audio">
          <Representation id="1" mimeType="audio/mp4" codecs="mp4a.40.2" bandwidth="69000">
            <SegmentTemplate timescale="44100" startNumber="1" media="chunk-$RepresentationID$-$Number%05d$.m4s">
              <SegmentTimeline><S t="0" d="88064" r="1" /></SegmentTimeline>
            </SegmentTemplate>
          </Representation>
        </AdaptationSet>
        """
    )
    reps = _iter_representations(mpd)
    assert {rid for rid, _ in reps} == {"0", "1"}
    for representation_id, template in reps:
        entries = _expand_segment_timeline(template)
        assert len(entries) == 2
        assert template.timescale in (15360, 44100)


def test_multi_bitrate_adaptation_set_uses_first_representation_only(caplog):
    mpd = _mpd(
        """
        <AdaptationSet id="0" contentType="video">
          <Representation id="low" mimeType="video/mp4" codecs="avc1" bandwidth="100000">
            <SegmentTemplate timescale="15360" startNumber="1" media="low-$Number%05d$.m4s">
              <SegmentTimeline><S t="0" d="30720" /></SegmentTimeline>
            </SegmentTemplate>
          </Representation>
          <Representation id="high" mimeType="video/mp4" codecs="avc1" bandwidth="500000">
            <SegmentTemplate timescale="15360" startNumber="1" media="high-$Number%05d$.m4s">
              <SegmentTimeline><S t="0" d="30720" /></SegmentTimeline>
            </SegmentTemplate>
          </Representation>
        </AdaptationSet>
        """
    )
    reps = _iter_representations(mpd)
    assert len(reps) == 1
    assert reps[0][0] == "low"


# ---------------------------------------------------------------------------
# ISO-8601 datetime / duration parsing (hand-written, no isodate dependency)
# ---------------------------------------------------------------------------


def test_parse_iso8601_datetime_with_trailing_z():
    dt = _parse_iso8601_datetime("2026-10-19T03:04:09.315Z")
    assert dt.year == 2026 and dt.month == 10 and dt.day == 19
    assert dt.tzinfo is not None


def test_parse_simple_pt_seconds_duration():
    assert _parse_iso8601_duration_seconds("PT2S") == 2.0
    assert _parse_iso8601_duration_seconds("PT4.0S") == 4.0
    assert _parse_iso8601_duration_seconds("PT10.0S") == 10.0


def test_parse_duration_rejects_richer_forms():
    import pytest

    with pytest.raises(ValueError):
        _parse_iso8601_duration_seconds("P1DT2H")


def test_live_edge_seconds_subtracts_presentation_delay():
    from datetime import UTC, datetime, timedelta

    availability_start = datetime.now(UTC) - timedelta(seconds=20)
    edge_with_delay = _live_edge_seconds(availability_start, suggested_presentation_delay_seconds=5.0)
    edge_without_delay = _live_edge_seconds(availability_start, suggested_presentation_delay_seconds=0.0)
    assert edge_with_delay < edge_without_delay
    assert abs((edge_without_delay - edge_with_delay) - 5.0) < 0.5
