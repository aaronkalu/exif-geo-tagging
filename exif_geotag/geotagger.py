from __future__ import annotations

import enum
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Iterator, Sequence

from exif_geotag import exiftool
from exif_geotag.locator import LocationIndex

IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg"})
MAX_BATCH_SIZE = 50


class Outcome(enum.Enum):
    TAGGED = "tagged"
    SKIPPED = "skipped"
    FAILED = "failed"


class Issue(enum.Enum):
    """Unexpected conditions; expected skips such as existing GPS data are not issues."""

    MISSING_CAPTURE_TIME = "no DateTimeOriginal in EXIF data (skipped)"
    NO_NEARBY_LOCATION = "no timeline location within tolerance (skipped)"
    MISSING_TIMEZONE = "no timezone offset in EXIF data (capture time assumed to be UTC)"
    EXIFTOOL_ERROR = "ExifTool error (failed)"
    UNEXPECTED_ERROR = "unexpected error (failed)"


@dataclass(frozen=True)
class Result:
    image: Path
    outcome: Outcome
    message: str
    issues: tuple[Issue, ...] = ()

    def __str__(self) -> str:
        return f"Image {self.image}: {self.message}"


def find_images(directory: Path, recursive: bool) -> list[Path]:
    candidates = directory.rglob("*") if recursive else directory.iterdir()
    return sorted(
        path
        for path in candidates
        if path.suffix.lower() in IMAGE_EXTENSIONS and not _is_hidden(path.relative_to(directory)) and path.is_file()
    )


def _is_hidden(relative_path: Path) -> bool:
    # Also excludes macOS AppleDouble "._*" files and system folders such as .Trashes, which ExifTool cannot tag.
    return any(part.startswith(".") for part in relative_path.parts)


@dataclass(frozen=True)
class Geotagger:
    locations: LocationIndex
    tolerance: timedelta
    overwrite: bool = False

    def process(self, image: Path) -> Result:
        return self.process_batch([image])[0]

    def process_batch(self, images: Sequence[Path]) -> list[Result]:
        try:
            metadata = exiftool.read_metadata_batch(images)
        except Exception as error:
            return [_failure(image, error) for image in images]
        return [self._process_safely(image, metadata[image]) for image in images]

    def _process_safely(self, image: Path, metadata: exiftool.ImageMetadata | exiftool.ExifToolError) -> Result:
        if isinstance(metadata, exiftool.ExifToolError):
            return _failure(image, metadata)
        try:
            return self._process(image, metadata)
        except Exception as error:
            return _failure(image, error)

    def _process(self, image: Path, metadata: exiftool.ImageMetadata) -> Result:
        if metadata.has_gps and not self.overwrite:
            return Result(image, Outcome.SKIPPED, "Skipping, GPS data already present.")
        if metadata.taken_at is None:
            return Result(image, Outcome.SKIPPED, "Skipping, no DateTimeOriginal in EXIF data.", (Issue.MISSING_CAPTURE_TIME,))

        issues = () if metadata.has_timezone else (Issue.MISSING_TIMEZONE,)
        location = self.locations.closest(metadata.taken_at, self.tolerance)
        if location is None:
            message = f"Skipping, no location within {self.tolerance} of {metadata.taken_at}."
            return Result(image, Outcome.SKIPPED, message, (*issues, Issue.NO_NEARBY_LOCATION))

        exiftool.write_gps(image, location)
        return Result(image, Outcome.TAGGED, f"GPS data updated from {location}.", issues)

    def process_all(self, images: list[Path], workers: int = 1) -> Iterator[Result]:
        workers = max(1, workers)
        batch_size = max(1, min(MAX_BATCH_SIZE, math.ceil(len(images) / workers)))
        batches = [images[start : start + batch_size] for start in range(0, len(images), batch_size)]
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(self.process_batch, batch) for batch in batches]
            for future in as_completed(futures):
                yield from future.result()


def _failure(image: Path, error: Exception) -> Result:
    if isinstance(error, exiftool.ExifToolError):
        return Result(image, Outcome.FAILED, f"ExifTool error: {error}", (Issue.EXIFTOOL_ERROR,))
    return Result(image, Outcome.FAILED, f"Unexpected error: {error!r}", (Issue.UNEXPECTED_ERROR,))
