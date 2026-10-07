from datetime import UTC, datetime, timedelta

import pytest

from exif_geotag.locator import LocationIndex, interpolate
from exif_geotag.timeline import Location, Span, SpanKind

HOUR = timedelta(hours=1)


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2024, 1, 1, hour, minute, tzinfo=UTC)


def location(hour: int, name: str, latitude: float = 0.0, minute: int = 0, offset: timedelta | None = None) -> Location:
    return Location(at(hour, minute), latitude, 0.0, source=name, utc_offset=offset)


def source(index: LocationIndex, timestamp: datetime, tolerance: timedelta = HOUR) -> str | None:
    match = index.locate(timestamp, tolerance)
    return match.location.source if match else None


POINTS = [location(12, "noon"), location(10, "ten"), location(14, "two")]
NEAREST_ONLY = LocationIndex(POINTS, interpolate=False)


def test_picks_nearest_neighbour_without_interpolation() -> None:
    assert source(NEAREST_ONLY, at(11, 10)) == "noon"
    assert source(NEAREST_ONLY, at(10, 50)) == "ten"


def test_exact_match() -> None:
    match = LocationIndex(POINTS).locate(at(12), timedelta(0))
    assert match is not None and match.location.source == "noon" and match.time_difference == timedelta(0)


def test_tie_prefers_earlier_location() -> None:
    assert source(NEAREST_ONLY, at(11)) == "ten"


def test_before_first_and_after_last() -> None:
    index = LocationIndex(POINTS)
    assert source(index, at(9, 30)) == "ten"
    assert source(index, at(14, 30)) == "two"


def test_outside_tolerance_returns_none() -> None:
    index = LocationIndex(POINTS)
    assert index.locate(at(8), HOUR) is None
    assert index.locate(at(16, 1), 2 * HOUR) is None


def test_empty_index_returns_none() -> None:
    assert LocationIndex([]).locate(at(12), HOUR) is None


def test_interpolates_between_points_both_within_tolerance() -> None:
    index = LocationIndex([location(10, "path", latitude=10.0), location(11, "path", latitude=20.0)])

    match = index.locate(at(10, 15), HOUR)

    assert match is not None
    assert (match.location.latitude, match.location.timestamp) == (12.5, at(10, 15))
    assert match.location.source == "interpolated path"
    assert match.time_difference == timedelta(minutes=15)


def test_falls_back_to_nearest_when_other_neighbour_is_too_far() -> None:
    index = LocationIndex([location(10, "near", latitude=10.0), location(13, "far", latitude=20.0)])
    match = index.locate(at(10, 15), HOUR)
    assert match is not None and match.location.source == "near" and match.location.latitude == 10.0


def test_interpolation_crosses_antimeridian_the_short_way() -> None:
    west = Location(at(10), 0.0, 179.0, "a")
    east = Location(at(12), 0.0, -179.0, "b")
    assert interpolate(west, east, at(11)).longitude == -180.0
    assert interpolate(west, east, at(10, 30)).longitude == 179.5


def span(start: int, end: int, name: str, kind: SpanKind = SpanKind.VISIT, level: int = 0, latitude: float = 0.0,
         end_latitude: float | None = None) -> Span:
    return Span(
        location(start, f"{name}_start", latitude),
        location(end, f"{name}_end", latitude if end_latitude is None else end_latitude),
        kind,
        hierarchy_level=level,
    )


def test_timestamp_inside_long_visit_uses_it_however_far_the_ends_are() -> None:
    visit = span(11, 18, "visit")
    index = LocationIndex([visit.start, visit.end], [visit])
    assert source(index, at(14)) == "visit_start"
    assert source(index, at(15)) == "visit_end"
    assert index.locate(at(20), HOUR) is None


def test_visit_beats_nearby_point() -> None:
    visit = span(11, 18, "visit")
    index = LocationIndex([visit.start, location(14, "path"), visit.end], [visit])
    assert source(index, at(14, 30)) == "visit_start"


def test_precise_fix_beats_visit() -> None:
    visit = span(11, 18, "visit", latitude=5.0)
    fix = Location(at(14, 0) + timedelta(seconds=20), 7.0, 0.0, source="gpx")
    index = LocationIndex([visit.start, fix, visit.end], [visit], interpolate=False)
    assert source(index, at(14)) == "gpx"


def test_long_early_span_does_not_hide_later_ones() -> None:
    trip = span(0, 23, "trip")
    visits = [span(hour, hour, f"v{hour}", level=1) for hour in range(1, 23)]
    index = LocationIndex([], [trip, *visits])
    assert source(index, at(12)) == "v12_start"
    assert source(index, at(5, 30)) == "trip_start"


def test_dense_points_beat_straight_line_through_activity() -> None:
    drive = span(10, 12, "drive", SpanKind.ACTIVITY, latitude=0.0, end_latitude=10.0)
    road = location(11, "path", latitude=50.0)
    index = LocationIndex([drive.start, road, drive.end], [drive])
    match = index.locate(at(11, 30), HOUR)
    assert match is not None and match.location.source == "interpolated path/drive_end"
    assert match.location.latitude == 30.0


def test_activity_is_interpolated_when_points_are_sparse() -> None:
    drive = span(10, 14, "drive", SpanKind.ACTIVITY, latitude=0.0, end_latitude=40.0)
    index = LocationIndex([drive.start, drive.end], [drive])

    match = index.locate(at(11), HOUR / 2)

    assert match is not None
    assert match.location.latitude == 10.0
    assert match.time_difference == HOUR


def test_activity_without_interpolation_uses_nearer_end_after_nearest_point() -> None:
    drive = span(10, 14, "drive", SpanKind.ACTIVITY, latitude=0.0, end_latitude=40.0)
    index = LocationIndex([drive.start, location(12, "path"), drive.end], [drive], interpolate=False)
    assert source(index, at(12, 30)) == "path"
    assert source(index, at(13, 30), HOUR / 4) == "drive_end"


def test_innermost_overlapping_visit_wins() -> None:
    mall, shop = span(8, 20, "mall", level=0), span(9, 10, "shop", level=1)
    long_visit, short_visit = span(6, 22, "long"), span(11, 13, "short")
    index = LocationIndex([], [shop, mall, long_visit, short_visit])
    assert source(index, at(9, 30)) == "shop_start"
    assert source(index, at(12)) == "short_start"
    assert source(index, at(7)) == "long_start"
    assert index.locate(at(23), HOUR) is None


@pytest.mark.parametrize(("local_hour", "expected"), [(11, timedelta(hours=1)), (14, timedelta(hours=2))])
def test_utc_offset_inferred_from_nearby_timeline_points(local_hour: int, expected: timedelta) -> None:
    berlin, athens = timedelta(hours=1), timedelta(hours=2)
    index = LocationIndex([
        location(10, "berlin", offset=berlin),
        location(11, "berlin", offset=berlin),
        location(12, "athens", offset=athens),
        location(13, "athens", offset=athens),
        location(20, "unknown"),
    ])
    assert index.utc_offset_at(datetime(2024, 1, 1, local_hour)) == expected


def test_no_utc_offset_inferred_without_offsets_in_timeline() -> None:
    assert LocationIndex([location(10, "gpx")]).utc_offset_at(datetime(2024, 1, 1, 10)) is None
