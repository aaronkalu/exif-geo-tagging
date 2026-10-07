from datetime import datetime, timedelta, timezone

from exif_geotag.locator import LocationIndex
from exif_geotag.timeline import Location

HOUR = timedelta(hours=1)


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2024, 1, 1, hour, minute, tzinfo=timezone.utc)


def location(hour: int, name: str) -> Location:
    return Location(at(hour), 0.0, 0.0, source=name)


INDEX = LocationIndex([location(12, "noon"), location(10, "ten"), location(14, "two")])


def test_picks_nearest_neighbour() -> None:
    assert INDEX.closest(at(11, 10), HOUR).source == "noon"
    assert INDEX.closest(at(10, 50), HOUR).source == "ten"


def test_exact_match() -> None:
    assert INDEX.closest(at(12), timedelta(0)).source == "noon"


def test_tie_prefers_earlier_location() -> None:
    assert INDEX.closest(at(11), HOUR).source == "ten"


def test_before_first_and_after_last() -> None:
    assert INDEX.closest(at(9, 30), HOUR).source == "ten"
    assert INDEX.closest(at(14, 30), HOUR).source == "two"


def test_outside_tolerance_returns_none() -> None:
    assert INDEX.closest(at(8), HOUR) is None
    assert INDEX.closest(at(16, 1), 2 * HOUR) is None


def test_empty_index_returns_none() -> None:
    assert LocationIndex([]).closest(at(12), HOUR) is None
