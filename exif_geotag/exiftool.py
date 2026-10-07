from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any, Sequence

from exif_geotag.timeline import Location

_EXIF_DATETIME_FORMAT = "%Y:%m:%d %H:%M:%S"
# OffsetTime belongs to ModifyDate, which editing software may change later, so it is the last resort.
_OFFSET_TAGS = ("OffsetTimeOriginal", "OffsetTimeDigitized", "OffsetTime")


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
    metadata = read_metadata_batch([image])[image]
    if isinstance(metadata, ExifToolError):
        raise metadata
    return metadata


def read_metadata_batch(images: Sequence[Path]) -> dict[Path, ImageMetadata | ExifToolError]:
    """Reads all images with one ExifTool process; a file that cannot be read gets an error instead of metadata."""
    result = _execute(
        "-json", "-n", "-charset", "filename=utf8", "-Error", "-DateTimeOriginal", *(f"-{tag}" for tag in _OFFSET_TAGS),
        "-GPSLatitude", "-GPSLongitude", "-@", "-",
        stdin="".join(f"{image}\n" for image in images),
    )
    # ExifTool exits non-zero if any file is unreadable but still reports the others.
    try:
        records = json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError as error:
        raise ExifToolError(f"unreadable ExifTool output: {error}") from error
    tags_by_image = {Path(tags["SourceFile"]): tags for tags in records}

    stderr_lines = result.stderr.splitlines()
    metadata: dict[Path, ImageMetadata | ExifToolError] = {}
    for image in images:
        tags = tags_by_image.get(image)
        if tags is None:
            reasons = [line for line in stderr_lines if line.endswith(f" - {image}")]
            metadata[image] = ExifToolError("; ".join(reasons) or "no metadata returned by ExifTool")
        elif "Error" in tags:
            metadata[image] = ExifToolError(str(tags["Error"]))
        else:
            metadata[image] = _to_metadata(tags)
    return metadata


def _to_metadata(tags: dict[str, Any]) -> ImageMetadata:
    offsets = (_parse_offset(tags.get(tag)) for tag in _OFFSET_TAGS)
    offset = next((offset for offset in offsets if offset is not None), None)
    return ImageMetadata(
        taken_at=_parse_capture_time(tags.get("DateTimeOriginal"), offset or timezone.utc),
        has_timezone=offset is not None,
        has_gps="GPSLatitude" in tags or "GPSLongitude" in tags,
    )


def write_gps(image: Path, location: Location) -> None:
    # Signed values, so groups without a Ref tag (XMP) keep the hemisphere; ExifTool derives N/S/E/W from the sign.
    _run(
        "-overwrite_original",
        f"-GPSLatitude={location.latitude}",
        f"-GPSLatitudeRef={location.latitude}",
        f"-GPSLongitude={location.longitude}",
        f"-GPSLongitudeRef={location.longitude}",
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
    result = _execute(*arguments)
    if result.returncode != 0:
        raise ExifToolError(result.stderr.strip() or f"exiftool exited with status {result.returncode}")
    return result.stdout


def _execute(*arguments: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    # ExifTool writes UTF-8 regardless of the locale, which may not be UTF-8 (e.g. cp1252 on Windows).
    return subprocess.run(
        ["exiftool", *arguments], input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
