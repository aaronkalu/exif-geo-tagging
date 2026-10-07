from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from pathlib import Path

from exif_geotag.timeline import Location

_EXIF_DATETIME_FORMAT = "%Y:%m:%d %H:%M:%S"


class ExifToolError(RuntimeError):
    pass


@dataclass(frozen=True)
class ImageMetadata:
    taken_at: datetime | None
    """In UTC."""

    has_timezone: bool
    """If False, `taken_at` was assumed to be UTC."""

    has_gps: bool


def version() -> str:
    try:
        return _run("-ver").strip()
    except FileNotFoundError as error:
        raise ExifToolError("ExifTool is not installed. Please install ExifTool to use this script.") from error


def read_metadata(image: Path) -> ImageMetadata:
    output = _run("-json", "-n", "-DateTimeOriginal", "-OffsetTimeOriginal", "-GPSLatitude", "-GPSLongitude", str(image))
    tags = json.loads(output)[0]
    offset = _parse_offset(tags.get("OffsetTimeOriginal"))
    return ImageMetadata(
        taken_at=_parse_capture_time(tags.get("DateTimeOriginal"), offset or timezone.utc),
        has_timezone=offset is not None,
        has_gps="GPSLatitude" in tags or "GPSLongitude" in tags,
    )


def write_gps(image: Path, location: Location) -> None:
    _run(
        "-overwrite_original",
        f"-GPSLatitude={abs(location.latitude)}",
        f"-GPSLatitudeRef={'N' if location.latitude >= 0 else 'S'}",
        f"-GPSLongitude={abs(location.longitude)}",
        f"-GPSLongitudeRef={'E' if location.longitude >= 0 else 'W'}",
        str(image),
    )


def _parse_capture_time(date_time: object, offset: tzinfo) -> datetime | None:
    if not isinstance(date_time, str):
        return None
    try:
        local_time = datetime.strptime(date_time[:19], _EXIF_DATETIME_FORMAT)
    except ValueError:  # e.g. the placeholder "0000:00:00 00:00:00"
        return None
    return local_time.replace(tzinfo=offset).astimezone(timezone.utc)


def _parse_offset(offset: object) -> tzinfo | None:
    if not isinstance(offset, str):
        return None
    try:
        return datetime.strptime(offset, "%z").tzinfo
    except ValueError:
        return None


def _run(*arguments: str) -> str:
    result = subprocess.run(["exiftool", *arguments], capture_output=True, text=True)
    if result.returncode != 0:
        raise ExifToolError(result.stderr.strip() or f"exiftool exited with status {result.returncode}")
    return result.stdout
