from __future__ import annotations

import enum
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Iterator

from exif_geotag import exiftool
from exif_geotag.locator import LocationIndex

IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg"})


class Outcome(enum.Enum):
    TAGGED = "tagged"
    SKIPPED = "skipped"
    FAILED = "failed"


class Issue(enum.Enum):
    """Unexpected conditions; expected skips such as existing GPS data are not issues."""

    MISSING_CAPTURE_TIME = "no DateTimeOriginal in EXIF data (skipped)"
    NO_NEARBY_LOCATION = "no timeline location within tolerance (skipped)"
    MISSING_TIMEZONE = "no OffsetTimeOriginal in EXIF data (capture time assumed to be UTC)"
    EXIFTOOL_ERROR = "ExifTool error (failed)"


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
    return sorted(path for path in candidates if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS)


@dataclass(frozen=True)
class Geotagger:
    locations: LocationIndex
    tolerance: timedelta
    overwrite: bool = False

    def process(self, image: Path) -> Result:
        try:
            return self._process(image)
        except exiftool.ExifToolError as error:
            return Result(image, Outcome.FAILED, f"ExifTool error: {error}", (Issue.EXIFTOOL_ERROR,))

    def _process(self, image: Path) -> Result:
        metadata = exiftool.read_metadata(image)

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
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = [executor.submit(self.process, image) for image in images]
            for future in as_completed(futures):
                yield future.result()
