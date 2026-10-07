from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from exif_geotag.timeline import (
    Location,
    Span,
    SpanKind,
    Timeline,
    collect,
    parse_geo_point,
    parse_local_timestamp,
    validated_coordinates,
)


def parse_timeline(entries: Iterable[Any]) -> Timeline:
    """The on-device export from the iOS app: a JSON array of visit, activity and timelinePath entries."""
    return collect(entries, _parse_array_entry)


def _parse_array_entry(entry: Any) -> Iterator[Location | Span]:
    if not isinstance(entry, dict):
        raise TypeError(f"Timeline entry is not an object: {entry!r}")
    if "timelinePath" in entry:
        yield from _parse_array_path(entry)
    elif "activity" in entry:
        activity = entry["activity"]
        yield _span(
            entry, SpanKind.ACTIVITY, parse_geo_point(activity["start"]), parse_geo_point(activity["end"]),
            probability=activity.get("probability"),
        )
    elif "visit" in entry:
        visit = entry["visit"]
        point = parse_geo_point(visit["topCandidate"]["placeLocation"])
        yield _span(
            entry, SpanKind.VISIT, point, point,
            hierarchy_level=visit.get("hierarchyLevel"), probability=visit.get("probability"),
        )


def _parse_array_path(entry: dict[str, Any]) -> Iterator[Location]:
    start_time, offset = parse_local_timestamp(entry["startTime"])
    for point in entry["timelinePath"]:
        timestamp = start_time + timedelta(minutes=float(point["durationMinutesOffsetFromStartTime"]))
        yield Location(timestamp, *parse_geo_point(point["point"]), source="timeline", utc_offset=offset)


def parse_semantic_segments(document: dict[str, Any]) -> Timeline:
    """The on-device export from the Android app: an object with `semanticSegments` and `rawSignals`."""
    segments = collect(_list(document.get("semanticSegments")), _parse_segment)
    signals = collect(_list(document.get("rawSignals")), _parse_raw_signal)
    return segments + signals


def _parse_segment(segment: Any) -> Iterator[Location | Span]:
    if "timelinePath" in segment:
        for point in segment["timelinePath"]:
            timestamp, offset = parse_local_timestamp(point["time"])
            yield Location(timestamp, *parse_geo_point(point["point"]), source="timeline", utc_offset=offset)
    elif "activity" in segment:
        activity = segment["activity"]
        yield _span(
            segment, SpanKind.ACTIVITY,
            parse_geo_point(activity["start"]["latLng"]), parse_geo_point(activity["end"]["latLng"]),
            probability=activity.get("probability"),
        )
    elif "visit" in segment:
        visit = segment["visit"]
        point = parse_geo_point(visit["topCandidate"]["placeLocation"]["latLng"])
        yield _span(
            segment, SpanKind.VISIT, point, point,
            hierarchy_level=visit.get("hierarchyLevel"), probability=visit.get("probability"),
        )


def _parse_raw_signal(signal: Any) -> Iterator[Location]:
    if "position" not in signal:
        return  # wifiScan and activityRecord signals carry no location
    position = signal["position"]
    lat_lng = position.get("LatLng") or position["latLng"]
    timestamp, offset = parse_local_timestamp(position["timestamp"])
    yield Location(timestamp, *parse_geo_point(lat_lng), source="raw_signal", utc_offset=offset)


def parse_records(document: dict[str, Any]) -> Timeline:
    """`Records.json` from Google Takeout: raw location fixes in a `locations` array."""
    return collect(_list(document.get("locations")), _parse_record)


def _parse_record(record: Any) -> Iterator[Location]:
    if "timestamp" in record:
        timestamp, offset = parse_local_timestamp(record["timestamp"])
    else:
        timestamp, offset = datetime.fromtimestamp(int(record["timestampMs"]) / 1000, UTC), None
    yield Location(
        timestamp, *validated_coordinates(_e7(record["latitudeE7"]), _e7(record["longitudeE7"])),
        source="records", utc_offset=offset,
    )


def _e7(value: Any) -> float:
    number = int(value)
    # Some older Takeout exports stored negative coordinates as unsigned 32-bit integers.
    if number > 1_800_000_000:
        number -= 2**32
    return number / 1e7


def _span(
    entry: dict[str, Any],
    kind: SpanKind,
    start_point: tuple[float, float],
    end_point: tuple[float, float],
    hierarchy_level: Any = None,
    probability: Any = None,
) -> Span:
    start_time, start_offset = parse_local_timestamp(entry["startTime"])
    end_time, end_offset = parse_local_timestamp(entry["endTime"])
    if end_time < start_time:
        raise ValueError(f"{kind.value} ends before it starts")
    return Span(
        Location(start_time, *start_point, source=f"{kind.value}_start", utc_offset=start_offset),
        Location(end_time, *end_point, source=f"{kind.value}_end", utc_offset=end_offset),
        kind,
        hierarchy_level=int(hierarchy_level or 0),
        probability=None if probability is None else float(probability),
    )


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []
