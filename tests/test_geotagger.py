import base64
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from exif_geotag import exiftool
from exif_geotag.geotagger import Geotagger, Issue, Outcome, find_images
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


def test_process_all_handles_every_image(tmp_path: Path) -> None:
    images = [make_jpeg(tmp_path / f"{i}.jpg", "-DateTimeOriginal=2024:01:01 10:00:00") for i in range(4)]
    results = list(Geotagger(INDEX, timedelta(hours=1)).process_all(images, workers=3))
    assert sorted(result.image for result in results) == images
    assert all(result.outcome is Outcome.TAGGED for result in results)


def test_find_images_matches_extensions_case_insensitively(tmp_path: Path) -> None:
    for name in ["a.jpg", "b.JPG", "c.Jpeg", "notjpg", "d.png", "sub/e.jpeg"]:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).touch()

    assert [p.name for p in find_images(tmp_path, recursive=False)] == ["a.jpg", "b.JPG", "c.Jpeg"]
    assert [p.name for p in find_images(tmp_path, recursive=True)] == ["a.jpg", "b.JPG", "c.Jpeg", "e.jpeg"]
