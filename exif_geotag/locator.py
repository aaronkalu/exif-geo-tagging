from __future__ import annotations

from bisect import bisect_left
from datetime import datetime, timedelta
from typing import Sequence

from exif_geotag.timeline import Location


class LocationIndex:
    def __init__(self, locations: Sequence[Location]) -> None:
        self._locations = sorted(locations, key=lambda location: location.timestamp)
        self._timestamps = [location.timestamp for location in self._locations]

    def __len__(self) -> int:
        return len(self._locations)

    def closest(self, timestamp: datetime, tolerance: timedelta) -> Location | None:
        position = bisect_left(self._timestamps, timestamp)
        neighbours = self._locations[max(position - 1, 0) : position + 1]
        if not neighbours:
            return None

        # min() keeps the first of equal keys, so on a tie the earlier location wins.
        nearest = min(neighbours, key=lambda location: abs(location.timestamp - timestamp))
        if abs(nearest.timestamp - timestamp) > tolerance:
            return None
        return nearest
