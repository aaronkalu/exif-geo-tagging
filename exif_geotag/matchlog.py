from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import IO, Any

from exif_geotag.geotagger import Result

FIELDS = (
    "image", "outcome", "dry_run", "taken_at_utc", "latitude", "longitude", "source", "time_difference_seconds",
    "previous_latitude", "previous_longitude", "previous_gps_tags", "message",
)


class MatchLog:
    """One row per image, as CSV, or as JSON if the file name ends in .json; `--undo` reads it back."""

    def __init__(self, path: Path) -> None:
        self._json = path.suffix.lower() == ".json"
        self._file: IO[str] = path.open("w", encoding="utf-8", newline="")
        self._rows: list[dict[str, Any]] = []
        self._csv = None
        if not self._json:
            self._csv = csv.DictWriter(self._file, FIELDS)
            self._csv.writeheader()

    def __enter__(self) -> MatchLog:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def add(self, result: Result) -> None:
        row = _row(result)
        if self._csv is not None:
            if row["previous_gps_tags"] is not None:
                row["previous_gps_tags"] = json.dumps(row["previous_gps_tags"])
            self._csv.writerow({key: "" if value is None else value for key, value in row.items()})
            self._file.flush()  # keep what is done if the run is interrupted
        else:
            self._rows.append(row)

    def close(self) -> None:
        if self._file.closed:
            return
        if self._json:
            json.dump(self._rows, self._file, indent=2)
        self._file.close()


def read_log(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8", newline="") as file:
        if path.suffix.lower() == ".json":
            rows = json.load(file)
            if not isinstance(rows, list):
                raise ValueError("expected a JSON array of rows")
            return rows
        return list(csv.DictReader(file))


def _row(result: Result) -> dict[str, Any]:
    location = result.match.location if result.match else None
    previous = result.previous_position
    return {
        "image": str(result.image.resolve()),
        "outcome": result.outcome.value,
        "dry_run": result.dry_run,
        "taken_at_utc": result.taken_at.isoformat() if result.taken_at else None,
        "latitude": location.latitude if location else None,
        "longitude": location.longitude if location else None,
        "source": location.source if location else None,
        "time_difference_seconds": int(result.match.time_difference.total_seconds()) if result.match else None,
        "previous_latitude": previous[0] if previous else None,
        "previous_longitude": previous[1] if previous else None,
        "previous_gps_tags": dict(result.previous_gps_tags) if result.previous_gps_tags is not None else None,
        "message": result.message,
    }
