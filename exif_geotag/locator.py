from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta
from typing import Sequence

from exif_geotag.timeline import Location, Span


class LocationIndex:
    def __init__(self, locations: Sequence[Location], spans: Sequence[Span] = ()) -> None:
        self._locations = sorted(locations, key=lambda location: location.timestamp)
        self._timestamps = [location.timestamp for location in self._locations]

        spans = sorted(spans, key=lambda span: span.start.timestamp)
        self._span_starts = [span.start.timestamp for span in spans]
        # Spans may overlap, so for each prefix keep the span reaching furthest: if it does not
        # cover a timestamp, no span starting earlier does either.
        self._furthest_reaching: list[Span] = []
        for span in spans:
            if self._furthest_reaching and self._furthest_reaching[-1].end.timestamp >= span.end.timestamp:
                span = self._furthest_reaching[-1]
            self._furthest_reaching.append(span)

    def __len__(self) -> int:
        return len(self._locations)

    def closest(self, timestamp: datetime, tolerance: timedelta) -> Location | None:
        """The nearest point within `tolerance`, else the nearer end of a visit or activity spanning `timestamp`."""
        nearest = self._nearest_point(timestamp)
        if nearest is not None and abs(nearest.timestamp - timestamp) <= tolerance:
            return nearest

        span = self._covering_span(timestamp)
        return span.nearer_end(timestamp) if span is not None else None

    def _nearest_point(self, timestamp: datetime) -> Location | None:
        position = bisect_left(self._timestamps, timestamp)
        neighbours = self._locations[max(position - 1, 0) : position + 1]
        if not neighbours:
            return None
        # min() keeps the first of equal keys, so on a tie the earlier location wins.
        return min(neighbours, key=lambda location: abs(location.timestamp - timestamp))

    def _covering_span(self, timestamp: datetime) -> Span | None:
        started = bisect_right(self._span_starts, timestamp)
        if started == 0:
            return None
        span = self._furthest_reaching[started - 1]
        return span if span.contains(timestamp) else None
