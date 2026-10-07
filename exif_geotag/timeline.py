from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

_TIMESTAMP_FORMATS = ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z")


@dataclass(frozen=True)
class Location:
    timestamp: datetime
    """Timezone-aware, in UTC."""

    latitude: float
    longitude: float
    source: str

    def __str__(self) -> str:
        return f"({self.latitude:.6f}, {self.longitude:.6f}) at {self.timestamp:%Y-%m-%d %H:%M:%S} UTC [{self.source}]"


@dataclass(frozen=True)
class Span:
    """A visit or activity: the whereabouts are known for the whole time between its endpoints."""

    start: Location
    end: Location

    def contains(self, timestamp: datetime) -> bool:
        return self.start.timestamp <= timestamp <= self.end.timestamp

    def nearer_end(self, timestamp: datetime) -> Location:
        if timestamp - self.start.timestamp <= self.end.timestamp - timestamp:
            return self.start
        return self.end


@dataclass(frozen=True)
class Timeline:
    locations: list[Location]
    """Every timestamped point, including span endpoints, in file order."""

    spans: list[Span]

    malformed_entries: int
    """Unknown entry types are ignored rather than counted here."""


class TimelineError(Exception):
    pass


def load_timeline(path: Path) -> Timeline:
    try:
        with path.open(encoding="utf-8") as file:
            entries = json.load(file)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TimelineError(f"Could not read timeline file {path}: {error}") from error
    if not isinstance(entries, list):
        raise TimelineError(f"Unsupported timeline file {path}: expected a JSON array of entries.")
    return parse_timeline(entries)


def parse_timeline(entries: Iterable[dict[str, Any]]) -> Timeline:
    locations: list[Location] = []
    spans: list[Span] = []
    malformed_entries = 0
    for entry in entries:
        try:
            parsed = list(_parse_entry(entry))
        except (KeyError, TypeError, ValueError):
            malformed_entries += 1
            continue
        for item in parsed:
            if isinstance(item, Span):
                spans.append(item)
                locations += (item.start, item.end)
            else:
                locations.append(item)

    return Timeline(locations, spans, malformed_entries)


def _parse_entry(entry: dict[str, Any]) -> Iterator[Location | Span]:
    if "timelinePath" in entry:
        yield from _parse_timeline_path(entry)
    elif "activity" in entry:
        yield from _parse_activity(entry)
    elif "visit" in entry:
        yield from _parse_visit(entry)


def _parse_timeline_path(entry: dict[str, Any]) -> Iterator[Location]:
    start_time = parse_timestamp(entry["startTime"])
    # Materialise the list first so a bad point drops the whole entry, not half of it.
    points = [
        (
            start_time + timedelta(minutes=int(point["durationMinutesOffsetFromStartTime"])),
            parse_geo_point(point["point"]),
        )
        for point in entry["timelinePath"]
    ]
    for timestamp, (latitude, longitude) in points:
        yield Location(timestamp, latitude, longitude, source="timeline")


def _parse_activity(entry: dict[str, Any]) -> Iterator[Span]:
    activity = entry["activity"]
    start = Location(parse_timestamp(entry["startTime"]), *parse_geo_point(activity["start"]), source="activity_start")
    end = Location(parse_timestamp(entry["endTime"]), *parse_geo_point(activity["end"]), source="activity_end")
    yield Span(start, end)


def _parse_visit(entry: dict[str, Any]) -> Iterator[Span]:
    latitude, longitude = parse_geo_point(entry["visit"]["topCandidate"]["placeLocation"])
    start = Location(parse_timestamp(entry["startTime"]), latitude, longitude, source="visit_start")
    end = Location(parse_timestamp(entry["endTime"]), latitude, longitude, source="visit_end")
    yield Span(start, end)


def parse_timestamp(value: str) -> datetime:
    for timestamp_format in _TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(value, timestamp_format).astimezone(timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Unrecognised timestamp: {value!r}")


def parse_geo_point(value: str) -> tuple[float, float]:
    scheme, _, coordinates = value.partition(":")
    if scheme != "geo":
        raise ValueError(f"Unrecognised geo point: {value!r}")
    latitude, longitude = coordinates.split(",")
    return float(latitude), float(longitude)
