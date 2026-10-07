from __future__ import annotations

from collections.abc import Iterator
from xml.etree.ElementTree import Element

from exif_geotag.timeline import (
    Location,
    Span,
    SpanKind,
    Timeline,
    collect,
    parse_local_timestamp,
    validated_coordinates,
)


def parse_gpx(root: Element) -> Timeline:
    """Track, route and waypoints that have a <time>; points without one cannot be matched to a photo."""
    points = [point for tag in ("trkpt", "rtept", "wpt") for point in root.iterfind(f".//{{*}}{tag}")]
    return collect((point for point in points if point.find("{*}time") is not None), _parse_gpx_point)


def _parse_gpx_point(point: Element) -> Iterator[Location]:
    timestamp, offset = parse_local_timestamp(_text(point.find("{*}time")))
    latitude, longitude = validated_coordinates(float(point.attrib["lat"]), float(point.attrib["lon"]))
    yield Location(timestamp, latitude, longitude, source="gpx", utc_offset=offset)


def parse_kml(root: Element) -> Timeline:
    """gx:Track elements (Google Takeout KML) and placemarks with a TimeStamp or TimeSpan."""
    tracks = collect(root.iterfind(".//{*}Track"), _parse_kml_track)
    placemarks = collect(
        (placemark for placemark in root.iterfind(".//{*}Placemark") if placemark.find(".//{*}Track") is None),
        _parse_kml_placemark,
    )
    return tracks + placemarks


def _parse_kml_track(track: Element) -> Iterator[Location]:
    times = [_text(when) for when in track.iterfind("{*}when")]
    coords = [_text(coord) for coord in track.iterfind("{*}coord")]
    if len(times) != len(coords):
        raise ValueError("gx:Track has a different number of <when> and <gx:coord> elements")
    for time, coord in zip(times, coords, strict=True):
        timestamp, offset = parse_local_timestamp(time)
        longitude, latitude = (float(value) for value in coord.split()[:2])
        yield Location(timestamp, *validated_coordinates(latitude, longitude), source="kml", utc_offset=offset)


def _parse_kml_placemark(placemark: Element) -> Iterator[Location | Span]:
    coordinates = [_kml_coordinates(element) for element in placemark.iterfind(".//{*}coordinates")]
    points = [point for group in coordinates for point in group]
    when = placemark.find(".//{*}TimeStamp/{*}when")
    begin, end = placemark.find(".//{*}TimeSpan/{*}begin"), placemark.find(".//{*}TimeSpan/{*}end")
    if not points or (when is None and (begin is None or end is None)):
        return
    if when is not None:
        timestamp, offset = parse_local_timestamp(_text(when))
        yield Location(timestamp, *points[0], source="kml", utc_offset=offset)
        return

    assert begin is not None and end is not None
    start_time, start_offset = parse_local_timestamp(_text(begin))
    end_time, end_offset = parse_local_timestamp(_text(end))
    if end_time < start_time:
        raise ValueError("TimeSpan ends before it begins")
    # A single point is a stay somewhere; a line is a journey from its first to its last point.
    kind = SpanKind.VISIT if len(points) == 1 else SpanKind.ACTIVITY
    yield Span(
        Location(start_time, *points[0], source=f"kml_{kind.value}_start", utc_offset=start_offset),
        Location(end_time, *points[-1], source=f"kml_{kind.value}_end", utc_offset=end_offset),
        kind,
    )


def _kml_coordinates(element: Element) -> list[tuple[float, float]]:
    points = []
    for tuple_text in _text(element).split():
        longitude, latitude = (float(value) for value in tuple_text.split(",")[:2])
        points.append(validated_coordinates(latitude, longitude))
    return points


def _text(element: Element | None) -> str:
    if element is None or element.text is None:
        raise ValueError("missing element text")
    return element.text.strip()
