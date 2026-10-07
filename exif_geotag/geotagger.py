from __future__ import annotations

import enum
import math
import threading
from collections.abc import Generator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path

from exif_geotag import exiftool
from exif_geotag.locator import LocationIndex, Match

MAX_BATCH_SIZE = 50


class Outcome(enum.Enum):
    TAGGED = "tagged"
    SKIPPED = "skipped"
    FAILED = "failed"


class Issue(enum.Enum):
    """Unexpected conditions; expected skips such as existing GPS data are not issues."""

    MISSING_CAPTURE_TIME = "no capture time (DateTimeOriginal, or CreateDate for videos) in metadata (skipped)"
    NO_NEARBY_LOCATION = "no timeline location within tolerance (skipped)"
    TIMEZONE_INFERRED = "no timezone offset in metadata (inferred from the timeline)"
    MISSING_TIMEZONE = "no timezone offset in metadata (capture time assumed to be UTC)"
    EXIFTOOL_ERROR = "ExifTool error (failed)"
    UNEXPECTED_ERROR = "unexpected error (failed)"


@dataclass(frozen=True)
class Result:
    image: Path
    outcome: Outcome
    message: str
    issues: tuple[Issue, ...] = ()
    taken_at: datetime | None = None
    """In UTC, after time shift and timezone resolution."""

    match: Match | None = None
    previous_position: tuple[float, float] | None = None
    dry_run: bool = False

    def __str__(self) -> str:
        return f"Image {self.image}: {self.message}"


@dataclass(frozen=True)
class Geotagger:
    locations: LocationIndex
    tolerance: timedelta
    overwrite: bool = False
    dry_run: bool = False
    keep_backup: bool = False
    time_shift: timedelta = timedelta(0)
    """Added to the camera clock, to correct a camera set to the wrong time."""

    assumed_timezone: tzinfo | None = None
    """For files without a timezone offset; if None, the offset is inferred from the timeline."""

    def process_batch(self, images: Sequence[Path], tool: exiftool.ExifTool) -> list[Result]:
        try:
            metadata = tool.read_metadata(images)
        except Exception as error:
            return [_failure(image, error) for image in images]
        return [self._process_safely(image, metadata[image], tool) for image in images]

    def _process_safely(
        self, image: Path, metadata: exiftool.ImageMetadata | exiftool.ExifToolError, tool: exiftool.ExifTool
    ) -> Result:
        if isinstance(metadata, exiftool.ExifToolError):
            return _failure(image, metadata)
        try:
            return self._process(image, metadata, tool)
        except Exception as error:
            return _failure(image, error)

    def _process(self, image: Path, metadata: exiftool.ImageMetadata, tool: exiftool.ExifTool) -> Result:
        if metadata.has_gps and not self.overwrite:
            return Result(image, Outcome.SKIPPED, "Skipping, GPS data already present.")
        if metadata.local_time is None:
            message = "Skipping, no capture time in metadata."
            return Result(image, Outcome.SKIPPED, message, (Issue.MISSING_CAPTURE_TIME,))

        taken_at, issues = self._capture_time_in_utc(metadata.local_time + self.time_shift, metadata.utc_offset)
        match = self.locations.locate(taken_at, self.tolerance)
        if match is None:
            message = f"Skipping, no location within {self.tolerance} of {taken_at:%Y-%m-%d %H:%M:%S} UTC."
            return Result(image, Outcome.SKIPPED, message, (*issues, Issue.NO_NEARBY_LOCATION), taken_at)

        location = match.location
        if not self.dry_run:
            tool.write_gps(image, location.latitude, location.longitude, taken_at, self.keep_backup)
        verb = "Would set" if self.dry_run else "Set"
        message = f"{verb} GPS from {location}, {_describe(match.time_difference)} from the photo."
        return Result(
            image, Outcome.TAGGED, message, issues, taken_at, match, metadata.position if metadata.has_gps else None,
            self.dry_run,
        )

    def _capture_time_in_utc(
        self, local_time: datetime, offset: timedelta | None
    ) -> tuple[datetime, tuple[Issue, ...]]:
        if offset is not None:
            return (local_time - offset).replace(tzinfo=UTC), ()
        if self.assumed_timezone is not None:
            return local_time.replace(tzinfo=self.assumed_timezone).astimezone(UTC), ()
        inferred = self.locations.utc_offset_at(local_time)
        if inferred is not None:
            return (local_time - inferred).replace(tzinfo=UTC), (Issue.TIMEZONE_INFERRED,)
        return local_time.replace(tzinfo=UTC), (Issue.MISSING_TIMEZONE,)

    def process_all(self, images: list[Path], workers: int = 1) -> Generator[Result, None, None]:
        workers = max(1, workers)
        batch_size = max(1, min(MAX_BATCH_SIZE, math.ceil(len(images) / workers)))
        batches = [images[start : start + batch_size] for start in range(0, len(images), batch_size)]
        with _ToolPerThread() as tools:

            def run(batch: list[Path]) -> list[Result]:
                try:
                    tool = tools.get()
                except Exception as error:
                    return [_failure(image, error) for image in batch]
                return self.process_batch(batch, tool)

            executor = ThreadPoolExecutor(max_workers=workers)
            try:
                futures = [executor.submit(run, batch) for batch in batches]
                for future in as_completed(futures):
                    yield from future.result()
            finally:
                # Runs when the caller closes the generator early (e.g. Ctrl+C):
                # drop queued batches, finish running ones.
                executor.shutdown(wait=True, cancel_futures=True)


class _ToolPerThread:
    """Gives each worker thread its own ExifTool process for the whole run, and closes them all at the end."""

    def __init__(self) -> None:
        self._local = threading.local()
        self._tools: list[exiftool.ExifTool] = []
        self._lock = threading.Lock()

    def get(self) -> exiftool.ExifTool:
        tool: exiftool.ExifTool | None = getattr(self._local, "tool", None)
        if tool is None or not tool.alive:
            tool = exiftool.ExifTool()
            self._local.tool = tool
            with self._lock:
                self._tools.append(tool)
        return tool

    def __enter__(self) -> _ToolPerThread:
        return self

    def __exit__(self, *exc_info: object) -> None:
        for tool in self._tools:
            tool.close()


def _describe(difference: timedelta) -> str:
    minutes = round(difference.total_seconds() / 60)
    if minutes < 1:
        return "under a minute"
    if minutes < 120:
        return f"{minutes} min"
    return f"{minutes / 60:.1f} h"


def _failure(image: Path, error: Exception) -> Result:
    if isinstance(error, exiftool.ExifToolError):
        return Result(image, Outcome.FAILED, f"ExifTool error: {error}", (Issue.EXIFTOOL_ERROR,))
    return Result(image, Outcome.FAILED, f"Unexpected error: {error!r}", (Issue.UNEXPECTED_ERROR,))
