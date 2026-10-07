from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree

from exif_geotag.google import parse_records, parse_semantic_segments, parse_timeline
from exif_geotag.timeline import Timeline, TimelineError
from exif_geotag.tracks import parse_gpx, parse_kml


def load_timeline(path: Path) -> Timeline:
    suffix = path.suffix.lower()
    if suffix in (".gpx", ".kml"):
        try:
            root = ElementTree.parse(path).getroot()
        except (OSError, ElementTree.ParseError) as error:
            raise TimelineError(f"Could not read location file {path}: {error}") from error
        return parse_gpx(root) if suffix == ".gpx" else parse_kml(root)

    try:
        with path.open(encoding="utf-8") as file:
            document = json.load(file)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TimelineError(f"Could not read timeline file {path}: {error}") from error

    if isinstance(document, list):
        return parse_timeline(document)
    if isinstance(document, dict) and ("semanticSegments" in document or "rawSignals" in document):
        return parse_semantic_segments(document)
    if isinstance(document, dict) and "locations" in document:
        return parse_records(document)
    raise TimelineError(
        f"Unsupported timeline file {path}: expected a Google Timeline export (a JSON array, an object with "
        "semanticSegments, or Records.json), a GPX file or a KML file."
    )
