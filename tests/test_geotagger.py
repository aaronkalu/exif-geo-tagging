import base64
import shutil
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from exif_geotag import exiftool
from exif_geotag.geotagger import MAX_BATCH_SIZE, Geotagger, Issue, Outcome, find_images
from exif_geotag.locator import LocationIndex
from exif_geotag.timeline import Location

pytestmark = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not installed")

# Smallest valid baseline JPEG (1x1 pixel).
TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////"
    "////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA="
)

INDEX = LocationIndex([Location(datetime(2024, 1, 1, 10, tzinfo=timezone.utc), -33.85, 151.2, "visit_start")])


def make_jpeg(path: Path, *tags: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(TINY_JPEG)
    if tags:
        subprocess.run(["exiftool", "-q", "-overwrite_original", *tags, str(path)], check=True)
    return path


def gps(path: Path) -> tuple[str, str]:
    output = subprocess.run(
        ["exiftool", "-s3", "-c", "%.4f", "-GPSLatitude", "-GPSLongitude", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    latitude, longitude = output.splitlines()
    return latitude, longitude


def test_read_metadata_applies_offset(tmp_path: Path) -> None:
    image = make_jpeg(tmp_path / "a.jpg", "-DateTimeOriginal=2024:01:01 11:30:00", "-OffsetTimeOriginal=+02:00")
    metadata = exiftool.read_metadata(image)
    assert metadata.taken_at == datetime(2024, 1, 1, 9, 30, tzinfo=timezone.utc)
    assert metadata.has_timezone
    assert not metadata.has_gps


def test_read_metadata_without_offset_assumes_utc(tmp_path: Path) -> None:
    image = make_jpeg(tmp_path / "a.jpg", "-DateTimeOriginal=2024:01:01 11:30:00")
    metadata = exiftool.read_metadata(image)
    assert metadata.taken_at == datetime(2024, 1, 1, 11, 30, tzinfo=timezone.utc)
    assert not metadata.has_timezone


@pytest.mark.parametrize("tag", ["OffsetTimeDigitized", "OffsetTime"])
def test_read_metadata_falls_back_to_other_offset_tags(tmp_path: Path, tag: str) -> None:
    image = make_jpeg(tmp_path / "a.jpg", "-DateTimeOriginal=2024:01:01 11:30:00", f"-{tag}=+02:00")
    metadata = exiftool.read_metadata(image)
    assert metadata.taken_at == datetime(2024, 1, 1, 9, 30, tzinfo=timezone.utc)
    assert metadata.has_timezone


def test_read_metadata_batch_reports_unreadable_files_individually(tmp_path: Path) -> None:
    good = make_jpeg(tmp_path / "good.jpg", "-DateTimeOriginal=2024:01:01 10:00:00")
    missing = tmp_path / "missing.jpg"

    metadata = exiftool.read_metadata_batch([missing, good])

    assert isinstance(metadata[missing], exiftool.ExifToolError)
    assert str(metadata[missing]) == f"Error: File not found - {missing}"
    assert metadata[good].taken_at == datetime(2024, 1, 1, 10, tzinfo=timezone.utc)


def test_tags_image_with_southern_and_eastern_coordinates(tmp_path: Path) -> None:
    image = make_jpeg(tmp_path / "a.jpg", "-DateTimeOriginal=2024:01:01 12:30:00", "-OffsetTimeOriginal=+02:00")

    result = Geotagger(INDEX, timedelta(hours=1)).process(image)

    assert result.outcome is Outcome.TAGGED
    assert result.issues == ()
    assert gps(image) == ("33.8500 S", "151.2000 E")


def test_skips_existing_gps_unless_overwrite(tmp_path: Path) -> None:
    image = make_jpeg(
        tmp_path / "a.jpg",
        "-DateTimeOriginal=2024:01:01 10:00:00",
        "-GPSLatitude=1", "-GPSLatitudeRef=N", "-GPSLongitude=1", "-GPSLongitudeRef=W",
    )

    result = Geotagger(INDEX, timedelta(hours=1)).process(image)
    assert result.outcome is Outcome.SKIPPED
    assert result.issues == ()
    assert gps(image) == ("1.0000 N", "1.0000 W")

    assert Geotagger(INDEX, timedelta(hours=1), overwrite=True).process(image).outcome is Outcome.TAGGED
    assert gps(image) == ("33.8500 S", "151.2000 E")


def test_overwrite_keeps_hemisphere_in_existing_xmp_gps(tmp_path: Path) -> None:
    image = make_jpeg(
        tmp_path / "a.jpg", "-DateTimeOriginal=2024:01:01 10:00:00", "-XMP:GPSLatitude=10", "-XMP:GPSLongitude=20"
    )
    index = LocationIndex([Location(datetime(2024, 1, 1, 10, tzinfo=timezone.utc), -33.85, -70.6, "visit_start")])

    assert Geotagger(index, timedelta(hours=1), overwrite=True).process(image).outcome is Outcome.TAGGED

    output = subprocess.run(
        ["exiftool", "-s3", "-n", "-XMP:GPSLatitude", "-XMP:GPSLongitude", str(image)],
        capture_output=True, text=True, check=True,
    ).stdout
    assert output.split() == ["-33.85", "-70.6"]
    assert gps(image) == ("33.8500 S", "70.6000 W")


def test_skips_image_outside_tolerance(tmp_path: Path) -> None:
    image = make_jpeg(tmp_path / "a.jpg", "-DateTimeOriginal=2024:01:01 13:00:00")
    result = Geotagger(INDEX, timedelta(hours=1)).process(image)
    assert result.outcome is Outcome.SKIPPED
    assert result.issues == (Issue.MISSING_TIMEZONE, Issue.NO_NEARBY_LOCATION)


def test_skips_image_without_capture_time(tmp_path: Path) -> None:
    image = make_jpeg(tmp_path / "a.jpg")
    result = Geotagger(INDEX, timedelta(hours=1)).process(image)
    assert result.outcome is Outcome.SKIPPED
    assert result.issues == (Issue.MISSING_CAPTURE_TIME,)


def test_reports_failure_for_unwritable_file(tmp_path: Path) -> None:
    image = make_jpeg(tmp_path / "a.jpg", "-DateTimeOriginal=2024:01:01 10:00:00")
    tmp_path.chmod(0o555)  # exiftool writes a temp file next to the image
    try:
        result = Geotagger(INDEX, timedelta(hours=1)).process(image)
    finally:
        tmp_path.chmod(0o755)
    assert result.outcome is Outcome.FAILED
    assert result.issues == (Issue.EXIFTOOL_ERROR,)


def test_unexpected_error_fails_only_that_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    images = [make_jpeg(tmp_path / f"{i}.jpg", "-DateTimeOriginal=2024:01:01 10:00:00") for i in range(2)]
    real_write_gps = exiftool.write_gps

    def write_gps(image: Path, location: Location) -> None:
        if image == images[0]:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        real_write_gps(image, location)

    monkeypatch.setattr(exiftool, "write_gps", write_gps)
    results = Geotagger(INDEX, timedelta(hours=1)).process_batch(images)

    assert [result.outcome for result in results] == [Outcome.FAILED, Outcome.TAGGED]
    assert results[0].issues == (Issue.UNEXPECTED_ERROR,)


def test_failed_batch_read_fails_every_image_in_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def read_metadata_batch(images: list[Path]) -> None:
        raise ValueError("garbled output")

    monkeypatch.setattr(exiftool, "read_metadata_batch", read_metadata_batch)
    results = Geotagger(INDEX, timedelta(hours=1)).process_batch([tmp_path / "a.jpg", tmp_path / "b.jpg"])

    assert [result.issues for result in results] == [(Issue.UNEXPECTED_ERROR,)] * 2


def test_process_all_handles_every_image(tmp_path: Path) -> None:
    images = [make_jpeg(tmp_path / f"{i}.jpg", "-DateTimeOriginal=2024:01:01 10:00:00") for i in range(4)]
    missing = tmp_path / "missing.jpg"
    results = list(Geotagger(INDEX, timedelta(hours=1)).process_all([*images, missing], workers=3))
    outcomes = {result.image: result.outcome for result in results}
    assert len(results) == 5
    assert outcomes == {**{image: Outcome.TAGGED for image in images}, missing: Outcome.FAILED}


def test_find_images_matches_extensions_case_insensitively(tmp_path: Path) -> None:
    for name in ["a.jpg", "b.JPG", "c.Jpeg", "notjpg", "d.png", "sub/e.jpeg"]:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).touch()

    assert [p.name for p in find_images(tmp_path, recursive=False)] == ["a.jpg", "b.JPG", "c.Jpeg"]
    assert [p.name for p in find_images(tmp_path, recursive=True)] == ["a.jpg", "b.JPG", "c.Jpeg", "e.jpeg"]


def test_find_images_skips_hidden_files_and_folders(tmp_path: Path) -> None:
    for name in ["a.jpg", "._a.jpg", ".hidden.jpg", ".Trashes/b.jpg", "sub/c.jpg", "sub/._c.jpg"]:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).touch()

    assert [p.name for p in find_images(tmp_path, recursive=True)] == ["a.jpg", "c.jpg"]


def test_find_images_works_inside_hidden_directory(tmp_path: Path) -> None:
    directory = tmp_path / ".photos"
    directory.mkdir()
    (directory / "a.jpg").touch()
    assert [p.name for p in find_images(directory, recursive=False)] == ["a.jpg"]


def test_closing_process_all_cancels_queued_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    geotagger = Geotagger(INDEX, timedelta(hours=1))
    processed = []
    release = threading.Event()

    def process_batch(self: Geotagger, batch: list[Path]) -> list[Path]:
        processed.append(batch)
        if len(processed) > 1:
            release.wait(5)  # keep the single worker busy so later batches stay queued
        return batch

    monkeypatch.setattr(Geotagger, "process_batch", process_batch)
    images = [Path(f"{i}.jpg") for i in range(4 * MAX_BATCH_SIZE)]

    results = geotagger.process_all(images, workers=1)
    next(results)
    threading.Timer(0.2, release.set).start()
    results.close()

    assert len(processed) == 2
