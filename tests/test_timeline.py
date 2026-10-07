import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from exif_geotag.google import parse_timeline
from exif_geotag.loader import load_timeline
from exif_geotag.timeline import SpanKind, TimelineError, parse_geo_point, parse_local_timestamp, parse_timestamp


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def write(tmp_path: Path, name: str, content: object) -> Path:
    path = tmp_path / name
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return path


def test_parse_timestamp_converts_offset_to_utc() -> None:
    assert parse_timestamp("2024-01-01T11:00:00.000+02:00") == utc(2024, 1, 1, 9)
    assert parse_timestamp("2024-01-01T11:00:00.00-05:30") == utc(2024, 1, 1, 16, 30)
    assert parse_timestamp("2024-01-01T11:00:00Z") == utc(2024, 1, 1, 11)


def test_parse_local_timestamp_keeps_offset_but_not_for_z() -> None:
    assert parse_local_timestamp("2024-01-01T11:00:00+02:00")[1] == timedelta(hours=2)
    assert parse_local_timestamp("2024-01-01T11:00:00+00:00")[1] == timedelta(0)
    assert parse_local_timestamp("2024-01-01T11:00:00.000Z")[1] is None


@pytest.mark.parametrize("value", ["yesterday", "2024-01-01T11:00:00", 42])
def test_parse_timestamp_rejects_garbage_and_naive_times(value: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        parse_timestamp(value)  # type: ignore[arg-type]


def test_parse_geo_point_formats() -> None:
    assert parse_geo_point("geo:-33.85,151.2") == (-33.85, 151.2)
    assert parse_geo_point("52.5163°, -13.3777°") == (52.5163, -13.3777)
    for invalid in ("geo:1", "somewhere", "geo:91,0", "geo:0,181"):
        with pytest.raises(ValueError):
            parse_geo_point(invalid)


def test_parse_activity_visit_and_path_entries() -> None:
    entries = [
        {
            "startTime": "2024-01-01T12:00:00.000+02:00",
            "endTime": "2024-01-01T18:00:00.000+02:00",
            "visit": {"hierarchyLevel": "1", "probability": "0.9", "topCandidate": {"placeLocation": "geo:3,3"}},
        },
        {
            "startTime": "2024-01-01T11:00:00.000+02:00",
            "endTime": "2024-01-01T12:00:00.000+02:00",
            "activity": {"start": "geo:1,1", "end": "geo:2,2", "probability": "0.5"},
        },
        {
            "startTime": "2024-01-01T10:00:00.000Z",
            "endTime": "2024-01-01T12:00:00.000Z",
            "timelinePath": [{"point": "geo:4,4", "durationMinutesOffsetFromStartTime": "10"}],
        },
    ]

    timeline = parse_timeline(entries)

    assert [(location.timestamp, location.latitude, location.source) for location in timeline.locations] == [
        (utc(2024, 1, 1, 10), 3.0, "visit_start"),
        (utc(2024, 1, 1, 16), 3.0, "visit_end"),
        (utc(2024, 1, 1, 9), 1.0, "activity_start"),
        (utc(2024, 1, 1, 10), 2.0, "activity_end"),
        (utc(2024, 1, 1, 10, 10), 4.0, "timeline"),
    ]
    assert [location.utc_offset for location in timeline.locations] == [timedelta(hours=2)] * 4 + [None]
    visit, activity = timeline.spans
    assert (visit.kind, visit.hierarchy_level, visit.probability) == (SpanKind.VISIT, 1, 0.9)
    assert (activity.kind, activity.probability) == (SpanKind.ACTIVITY, 0.5)


def test_malformed_and_unknown_entries_are_skipped() -> None:
    entries = [
        {"startTime": "2024-01-01T11:00:00.000Z", "endTime": "2024-01-01T12:00:00.000Z", "visit": {}},
        {"startTime": "2024-01-01T11:00:00.000Z", "endTime": "2024-01-01T12:00:00.000Z", "timelineMemory": {}},
        {"startTime": "2024-01-01T12:00:00.000Z", "endTime": "2024-01-01T11:00:00.000Z", "activity": {"start": "geo:1,1", "end": "geo:2,2"}},
        {
            "startTime": "2024-01-01T11:00:00.000Z",
            "endTime": "2024-01-01T12:00:00.000Z",
            "activity": {"start": "geo:1,1", "end": "geo:2,2"},
        },
        "timelinePath",
        7,
    ]

    timeline = parse_timeline(entries)

    assert [location.source for location in timeline.locations] == ["activity_start", "activity_end"]
    assert timeline.malformed_entries == 4  # unknown entry types are ignored, not malformed


def test_without_unlikely_spans_drops_span_and_its_endpoints() -> None:
    entries = [
        {"startTime": "2024-01-01T11:00:00Z", "endTime": "2024-01-01T12:00:00Z", "activity": {"start": "geo:1,1", "end": "geo:2,2", "probability": 0.2}},
        {"startTime": "2024-01-01T12:00:00Z", "endTime": "2024-01-01T13:00:00Z", "activity": {"start": "geo:3,3", "end": "geo:4,4", "probability": 0.8}},
        {"startTime": "2024-01-01T12:00:00Z", "endTime": "2024-01-01T13:00:00Z", "visit": {"topCandidate": {"placeLocation": "geo:5,5"}}},
    ]

    timeline = parse_timeline(entries).without_unlikely_spans(0.5)

    assert [span.start.latitude for span in timeline.spans] == [3.0, 5.0]
    assert [location.latitude for location in timeline.locations] == [3.0, 4.0, 5.0, 5.0]


@pytest.mark.parametrize(
    "content",
    [b'[{"startTime": ', b"\xff\xfe not utf-8", b"42", b"null", b'{"something": []}'],
)
def test_load_timeline_rejects_unreadable_files(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "timeline.json"
    path.write_bytes(content)
    with pytest.raises(TimelineError):
        load_timeline(path)


def test_load_timeline_reads_array(tmp_path: Path) -> None:
    entry = {"startTime": "2024-01-01T11:00:00Z", "endTime": "2024-01-01T12:00:00Z", "activity": {"start": "geo:1,1", "end": "geo:2,2"}}
    timeline = load_timeline(write(tmp_path, "timeline.json", [entry, 7]))
    assert len(timeline.locations) == 2
    assert timeline.malformed_entries == 1


def test_load_timeline_reads_semantic_segments(tmp_path: Path) -> None:
    document = {
        "semanticSegments": [
            {
                "startTime": "2024-01-01T10:00:00.000+01:00",
                "endTime": "2024-01-01T11:00:00.000+01:00",
                "timelinePath": [{"point": "52.5°, 13.4°", "time": "2024-01-01T10:05:00.000+01:00"}],
            },
            {
                "startTime": "2024-01-01T11:00:00.000+01:00",
                "endTime": "2024-01-01T12:00:00.000+01:00",
                "visit": {
                    "hierarchyLevel": 0,
                    "probability": 0.8,
                    "topCandidate": {"placeLocation": {"latLng": "52.52°, 13.41°"}},
                },
            },
            {
                "startTime": "2024-01-01T12:00:00.000+01:00",
                "endTime": "2024-01-01T12:30:00.000+01:00",
                "activity": {"start": {"latLng": "52.52°, 13.41°"}, "end": {"latLng": "52.6°, 13.5°"}, "probability": 0.9},
            },
            {"startTime": "2024-01-01T00:00:00.000+01:00", "endTime": "2024-01-02T00:00:00.000+01:00", "timelineMemory": {}},
            {"startTime": "2024-01-01T13:00:00.000+01:00", "endTime": "2024-01-01T14:00:00.000+01:00", "visit": {}},
        ],
        "rawSignals": [
            {"position": {"LatLng": "48.1°, 11.5°", "accuracyMeters": 10, "timestamp": "2024-01-01T15:00:00.000+01:00"}},
            {"wifiScan": {}},
        ],
        "userLocationProfile": {},
    }

    timeline = load_timeline(write(tmp_path, "Timeline.json", document))

    assert [(location.source, location.timestamp, location.latitude) for location in timeline.locations] == [
        ("timeline", utc(2024, 1, 1, 9, 5), 52.5),
        ("visit_start", utc(2024, 1, 1, 10), 52.52),
        ("visit_end", utc(2024, 1, 1, 11), 52.52),
        ("activity_start", utc(2024, 1, 1, 11), 52.52),
        ("activity_end", utc(2024, 1, 1, 11, 30), 52.6),
        ("raw_signal", utc(2024, 1, 1, 14), 48.1),
    ]
    assert {location.utc_offset for location in timeline.locations} == {timedelta(hours=1)}
    assert [span.kind for span in timeline.spans] == [SpanKind.VISIT, SpanKind.ACTIVITY]
    assert timeline.malformed_entries == 1


def test_load_timeline_reads_records_json(tmp_path: Path) -> None:
    document = {
        "locations": [
            {"latitudeE7": 525163000, "longitudeE7": 133777000, "timestamp": "2024-01-01T10:00:00.000Z"},
            {"latitudeE7": 4294967296 - 338500000, "longitudeE7": 1512000000, "timestampMs": "1704106800000"},
            {"latitudeE7": "x", "longitudeE7": 0, "timestamp": "2024-01-01T10:00:00Z"},
        ]
    }

    timeline = load_timeline(write(tmp_path, "Records.json", document))

    assert [(location.timestamp, location.latitude, location.longitude) for location in timeline.locations] == [
        (utc(2024, 1, 1, 10), 52.5163, 13.3777),
        (utc(2024, 1, 1, 11), -33.85, 151.2),
    ]
    assert timeline.malformed_entries == 1


def test_load_timeline_reads_gpx(tmp_path: Path) -> None:
    gpx = """<?xml version="1.0"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <wpt lat="1" lon="2"><time>2024-01-01T09:00:00Z</time></wpt>
  <wpt lat="9" lon="9"><name>no time</name></wpt>
  <trk><trkseg>
    <trkpt lat="-33.85" lon="151.2"><ele>3</ele><time>2024-01-01T10:00:00+10:00</time></trkpt>
    <trkpt lat="bad" lon="151.3"><time>2024-01-01T10:01:00Z</time></trkpt>
  </trkseg></trk>
</gpx>"""

    timeline = load_timeline(write(tmp_path, "track.GPX", gpx))

    assert [(location.timestamp, location.latitude, location.utc_offset) for location in timeline.locations] == [
        (utc(2024, 1, 1, 0), -33.85, timedelta(hours=10)),
        (utc(2024, 1, 1, 9), 1.0, None),
    ]
    assert timeline.malformed_entries == 1


def test_load_timeline_reads_kml(tmp_path: Path) -> None:
    kml = """<?xml version="1.0"?>
<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2">
<Document>
  <Placemark><gx:Track>
    <when>2024-01-01T10:00:00Z</when><when>2024-01-01T10:01:00Z</when>
    <gx:coord>13.4 52.5 0</gx:coord><gx:coord>13.5 52.6 0</gx:coord>
  </gx:Track></Placemark>
  <Placemark><TimeStamp><when>2024-01-01T08:00:00Z</when></TimeStamp><Point><coordinates>2,1,0</coordinates></Point></Placemark>
  <Placemark>
    <TimeSpan><begin>2024-01-01T11:00:00Z</begin><end>2024-01-01T12:00:00Z</end></TimeSpan>
    <Point><coordinates>4,3</coordinates></Point>
  </Placemark>
  <Placemark>
    <TimeSpan><begin>2024-01-01T12:00:00Z</begin><end>2024-01-01T13:00:00Z</end></TimeSpan>
    <LineString><coordinates>4,3,0 5,4,0 6,5,0</coordinates></LineString>
  </Placemark>
  <Placemark><name>no time</name><Point><coordinates>7,7</coordinates></Point></Placemark>
</Document>
</kml>"""

    timeline = load_timeline(write(tmp_path, "history.kml", kml))

    assert [(location.latitude, location.longitude) for location in timeline.locations] == [
        (52.5, 13.4), (52.6, 13.5), (1.0, 2.0), (3.0, 4.0), (3.0, 4.0), (3.0, 4.0), (5.0, 6.0),
    ]
    assert [span.kind for span in timeline.spans] == [SpanKind.VISIT, SpanKind.ACTIVITY]


def test_load_timeline_rejects_broken_xml(tmp_path: Path) -> None:
    with pytest.raises(TimelineError):
        load_timeline(write(tmp_path, "track.gpx", "<gpx"))
