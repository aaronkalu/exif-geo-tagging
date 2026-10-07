from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from exif_geotag.timeline import Location, Span, SpanKind

PRECISE_FIX = timedelta(minutes=1)
"""A point (or two to interpolate between) this close to a photo beats a visit, which only gives a place's centre."""


@dataclass(frozen=True)
class Match:
    location: Location
    time_difference: timedelta
    """Between the photo and the nearest timeline data used; zero inside a visit."""


class LocationIndex:
    def __init__(self, locations: Sequence[Location], spans: Sequence[Span] = (), interpolate: bool = True) -> None:
        self._locations = sorted(locations, key=lambda location: location.timestamp)
        self._timestamps = [location.timestamp for location in self._locations]
        self._interpolate = interpolate
        # Innermost visit first: the shop rather than the mall around it, then the shortest.
        self._visits = _SpanIndex(
            [span for span in spans if span.kind is SpanKind.VISIT],
            key=lambda span: (-span.hierarchy_level, span.duration),
        )
        self._activities = _SpanIndex(
            [span for span in spans if span.kind is SpanKind.ACTIVITY], key=lambda span: span.duration
        )

        self._offset_locations = [location for location in self._locations if location.utc_offset is not None]
        self._offset_timestamps = [location.timestamp for location in self._offset_locations]
        self._known_offsets = sorted(
            {location.utc_offset for location in self._offset_locations if location.utc_offset is not None}
        )

    def __len__(self) -> int:
        return len(self._locations)

    def locate(self, timestamp: datetime, tolerance: timedelta) -> Match | None:
        """Tries, in order: a point within PRECISE_FIX, a visit spanning `timestamp`, interpolation between points
        both within `tolerance` (only with interpolation on), an activity spanning `timestamp`, and the nearest point
        within `tolerance`. Without interpolation the nearest point is tried before the activity, whose nearer end
        is used."""
        before, after = self._neighbours(timestamp)
        nearest = _nearer(before, after, timestamp)
        if nearest is not None and nearest.timestamp == timestamp:
            return Match(nearest, timedelta(0))
        interpolated = None
        if self._interpolate and before is not None and after is not None:
            gaps = (timestamp - before.timestamp, after.timestamp - timestamp)
            if max(gaps) <= tolerance:
                interpolated = Match(interpolate(before, after, timestamp), min(gaps))
        nearest_match = None
        if nearest is not None and abs(nearest.timestamp - timestamp) <= tolerance:
            nearest_match = Match(nearest, abs(nearest.timestamp - timestamp))
        if nearest_match is not None and nearest_match.time_difference <= PRECISE_FIX:
            return interpolated or nearest_match

        visit = self._visits.best_covering(timestamp)
        if visit is not None:
            return Match(visit.nearer_end(timestamp), timedelta(0))
        if interpolated is not None:
            return interpolated
        if nearest_match is not None and not self._interpolate:
            return nearest_match

        activity = self._activities.best_covering(timestamp)
        if activity is not None:
            if self._interpolate:
                location = interpolate(activity.start, activity.end, timestamp)
                gap = min(timestamp - activity.start.timestamp, activity.end.timestamp - timestamp)
                return Match(location, gap)
            end = activity.nearer_end(timestamp)
            return Match(end, abs(end.timestamp - timestamp))
        return nearest_match

    def utc_offset_at(self, local_time: datetime) -> timedelta | None:
        """The UTC offset the timeline recorded around a naive local time, if one is consistent with it.

        An offset is consistent if the timeline point nearest to `local_time - offset` was recorded with that
        offset; when several are (e.g. around a time zone change), the one with the closest point wins.
        """
        best: tuple[timedelta, timedelta] | None = None
        for offset in self._known_offsets:
            utc_time = (local_time - offset).replace(tzinfo=UTC)
            position = bisect_left(self._offset_timestamps, utc_time)
            neighbours = self._offset_locations[max(position - 1, 0) : position + 1]
            nearest = min(neighbours, key=lambda location: abs(location.timestamp - utc_time), default=None)
            if nearest is None or nearest.utc_offset != offset:
                continue
            distance = abs(nearest.timestamp - utc_time)
            if best is None or distance < best[0]:
                best = (distance, offset)
        return best[1] if best is not None else None

    def _neighbours(self, timestamp: datetime) -> tuple[Location | None, Location | None]:
        position = bisect_left(self._timestamps, timestamp)
        before = self._locations[position - 1] if position > 0 else None
        after = self._locations[position] if position < len(self._locations) else None
        return before, after


class _SpanIndex:
    """Spans sorted by start, with a segment tree of the latest end in each range of them.

    A lookup only descends into ranges that start early enough and reach `timestamp`, so it costs O(k log n) for k
    covering spans, however long the spans before it are.
    """

    def __init__(self, spans: Sequence[Span], key: Callable[[Span], object]) -> None:
        self._spans = sorted(spans, key=lambda span: span.start.timestamp)
        self._starts = [span.start.timestamp for span in self._spans]
        self._key = key
        self._latest_end: list[datetime | None] = [None] * (4 * len(self._spans))
        if self._spans:
            self._build(1, 0, len(self._spans))

    def best_covering(self, timestamp: datetime) -> Span | None:
        covering: list[Span] = []
        if self._spans:
            self._collect(1, 0, len(self._spans), bisect_right(self._starts, timestamp), timestamp, covering)
        return min(covering, key=self._key, default=None)  # type: ignore[arg-type]

    def _build(self, node: int, low: int, high: int) -> datetime:
        if high - low == 1:
            latest = self._spans[low].end.timestamp
        else:
            middle = (low + high) // 2
            latest = max(self._build(2 * node, low, middle), self._build(2 * node + 1, middle, high))
        self._latest_end[node] = latest
        return latest

    def _collect(
        self, node: int, low: int, high: int, started: int, timestamp: datetime, covering: list[Span]
    ) -> None:
        """Adds the spans among `self._spans[low:high]` that are among the first `started` and end at or after
        `timestamp`."""
        latest = self._latest_end[node]
        if low >= started or latest is None or latest < timestamp:
            return
        if high - low == 1:
            covering.append(self._spans[low])
            return
        middle = (low + high) // 2
        self._collect(2 * node, low, middle, started, timestamp, covering)
        self._collect(2 * node + 1, middle, high, started, timestamp, covering)


def interpolate(start: Location, end: Location, timestamp: datetime) -> Location:
    duration = end.timestamp - start.timestamp
    fraction = (timestamp - start.timestamp) / duration if duration else 0.0
    latitude = start.latitude + fraction * (end.latitude - start.latitude)
    # Take the short way round across the antimeridian.
    longitude_change = (end.longitude - start.longitude + 180) % 360 - 180
    longitude = (start.longitude + fraction * longitude_change + 180) % 360 - 180
    source = start.source if start.source == end.source else f"{start.source}/{end.source}"
    return Location(
        timestamp, round(latitude, 7), round(longitude, 7), source=f"interpolated {source}",
        utc_offset=start.utc_offset if start.utc_offset == end.utc_offset else None,
    )


def _nearer(before: Location | None, after: Location | None, timestamp: datetime) -> Location | None:
    if before is None or after is None:
        return before or after
    # On a tie the earlier location wins.
    return before if timestamp - before.timestamp <= after.timestamp - timestamp else after
