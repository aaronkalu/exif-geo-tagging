from __future__ import annotations

import enum
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TypeVar

_Entry = TypeVar("_Entry")


@dataclass(frozen=True)
class Location:
    timestamp: datetime
    """Timezone-aware, in UTC."""

    latitude: float
    longitude: float
    source: str

    utc_offset: timedelta | None = None
    """Local UTC offset at `timestamp`, if the source recorded one."""

    def __str__(self) -> str:
        return f"({self.latitude:.6f}, {self.longitude:.6f}) at {self.timestamp:%Y-%m-%d %H:%M:%S} UTC [{self.source}]"


class SpanKind(enum.Enum):
    VISIT = "visit"
    ACTIVITY = "activity"


@dataclass(frozen=True)
class Span:
    """A visit or activity: the whereabouts are known for the whole time between its endpoints."""

    start: Location
    end: Location
    kind: SpanKind = SpanKind.VISIT
    hierarchy_level: int = 0
    """Nested visits (a shop inside a mall) have a higher level than the visits containing them."""

    probability: float | None = None

    @property
    def duration(self) -> timedelta:
        return self.end.timestamp - self.start.timestamp

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

    spans: list[Span] = field(default_factory=list)

    malformed_entries: int = 0
    """Unknown entry types are ignored rather than counted here."""

    def __add__(self, other: Timeline) -> Timeline:
        return Timeline(
            self.locations + other.locations, self.spans + other.spans, self.malformed_entries + other.malformed_entries
        )

    def without_unlikely_spans(self, min_probability: float) -> Timeline:
        """Drops visits and activities, including their endpoints, that Google rated below `min_probability`."""
        unlikely = {
            id(span): span for span in self.spans if span.probability is not None and span.probability < min_probability
        }
        if not unlikely:
            return self
        dropped = {id(location) for span in unlikely.values() for location in (span.start, span.end)}
        return Timeline(
            [location for location in self.locations if id(location) not in dropped],
            [span for span in self.spans if id(span) not in unlikely],
            self.malformed_entries,
        )


class TimelineError(Exception):
    pass


def collect(entries: Iterable[_Entry], parse_entry: Callable[[_Entry], Iterator[Location | Span]]) -> Timeline:
    locations: list[Location] = []
    spans: list[Span] = []
    malformed_entries = 0
    for entry in entries:
        try:
            # Materialise first so a bad point drops the whole entry, not half of it.
            parsed = list(parse_entry(entry))
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            malformed_entries += 1
            continue
        for item in parsed:
            if isinstance(item, Span):
                spans.append(item)
                locations += (item.start, item.end)
            else:
                locations.append(item)
    return Timeline(locations, spans, malformed_entries)


def parse_timestamp(value: str) -> datetime:
    return parse_local_timestamp(value)[0]


def parse_local_timestamp(value: str) -> tuple[datetime, timedelta | None]:
    """The time in UTC and its local UTC offset; "Z" is treated as an unknown offset, since exports use it for UTC."""
    if not isinstance(value, str):
        raise TypeError(f"Timestamp is not a string: {value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"Unrecognised timestamp: {value!r}") from error
    if parsed.tzinfo is None:
        raise ValueError(f"Timestamp without timezone: {value!r}")
    offset = None if value.endswith(("Z", "z")) else parsed.utcoffset()
    return parsed.astimezone(UTC), offset


# "geo:52.1,13.4" (Google on-device iOS export) or "52.1°, 13.4°" (Android export).
_GEO_POINT = re.compile(r"^\s*(?:geo:)?\s*(-?[\d.]+)°?\s*,\s*(-?[\d.]+)°?\s*$")


def parse_geo_point(value: str) -> tuple[float, float]:
    match = _GEO_POINT.match(value)
    if not match:
        raise ValueError(f"Unrecognised geo point: {value!r}")
    return validated_coordinates(float(match[1]), float(match[2]))


def validated_coordinates(latitude: float, longitude: float) -> tuple[float, float]:
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError(f"Coordinates out of range: {latitude}, {longitude}")
    return latitude, longitude
