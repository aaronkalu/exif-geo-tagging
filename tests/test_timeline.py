from datetime import datetime, timezone

import pytest

from exif_geotag.timeline import parse_geo_point, parse_timeline, parse_timestamp


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def test_parse_timestamp_converts_offset_to_utc() -> None:
    assert parse_timestamp("2024-01-01T11:00:00.000+02:00") == utc(2024, 1, 1, 9)
    assert parse_timestamp("2024-01-01T11:00:00.00-05:30") == utc(2024, 1, 1, 16, 30)
    assert parse_timestamp("2024-01-01T11:00:00Z") == utc(2024, 1, 1, 11)


def test_parse_timestamp_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_timestamp("yesterday")


def test_parse_geo_point() -> None:
    assert parse_geo_point("geo:-33.85,151.2") == (-33.85, 151.2)
    with pytest.raises(ValueError):
        parse_geo_point("52.1,13.4")


def test_parse_activity_visit_and_path_entries_sorted_by_time() -> None:
    # Ties keep file order: the visit is listed before the activity.
    entries = [
        {
            "startTime": "2024-01-01T12:00:00.000+02:00",
            "endTime": "2024-01-01T18:00:00.000+02:00",
            "visit": {"topCandidate": {"placeLocation": "geo:3,3"}},
        },
        {
            "startTime": "2024-01-01T11:00:00.000+02:00",
            "endTime": "2024-01-01T12:00:00.000+02:00",
            "activity": {"start": "geo:1,1", "end": "geo:2,2"},
        },
        {
            "startTime": "2024-01-01T10:00:00.000+02:00",
            "endTime": "2024-01-01T12:00:00.000+02:00",
            "timelinePath": [{"point": "geo:4,4", "durationMinutesOffsetFromStartTime": "10"}],
        },
    ]

    locations = parse_timeline(entries).locations

    assert [(location.timestamp, location.latitude, location.source) for location in locations] == [
        (utc(2024, 1, 1, 8, 10), 4.0, "timeline"),
        (utc(2024, 1, 1, 9), 1.0, "activity_start"),
        (utc(2024, 1, 1, 10), 3.0, "visit_start"),
        (utc(2024, 1, 1, 10), 2.0, "activity_end"),
        (utc(2024, 1, 1, 16), 3.0, "visit_end"),
    ]


def test_malformed_and_unknown_entries_are_skipped() -> None:
    entries = [
        {"startTime": "2024-01-01T11:00:00.000Z", "endTime": "2024-01-01T12:00:00.000Z", "visit": {}},
        {"startTime": "2024-01-01T11:00:00.000Z", "endTime": "2024-01-01T12:00:00.000Z", "timelineMemory": {}},
        {
            "startTime": "2024-01-01T11:00:00.000Z",
            "endTime": "2024-01-01T12:00:00.000Z",
            "activity": {"start": "geo:1,1", "end": "geo:2,2"},
        },
    ]

    timeline = parse_timeline(entries)

    assert [location.source for location in timeline.locations] == ["activity_start", "activity_end"]
    assert timeline.malformed_entries == 1  # unknown entry types are ignored, not malformed
