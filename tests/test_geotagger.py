import shutil
import subprocess
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from media_files import TINY_MP4, gps, make_file, make_jpeg

from exif_geotag import exiftool
from exif_geotag.geotagger import MAX_BATCH_SIZE, Geotagger, Issue, Outcome
from exif_geotag.locator import LocationIndex
from exif_geotag.media import find_media
from exif_geotag.timeline import Location

needs_exiftool = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not installed")

INDEX = LocationIndex([
    Location(datetime(2024, 1, 1, 10, tzinfo=UTC), -33.85, 151.2, "visit_start", utc_offset=timedelta(hours=11))
])
HOUR = timedelta(hours=1)


@pytest.fixture
def tool() -> Iterator[exiftool.ExifTool]:
    with exiftool.ExifTool() as tool:
        yield tool


def process(geotagger: Geotagger, image: Path, tool: exiftool.ExifTool):  # type: ignore[no-untyped-def]
    return geotagger.process_batch([image], tool)[0]


@needs_exiftool
def test_tags_image_with_southern_and_eastern_coordinates(tool: exiftool.ExifTool, tmp_path: Path) -> None:
    image = make_jpeg(tmp_path / "a.jpg", "-DateTimeOriginal=2024:01:01 12:30:00", "-OffsetTimeOriginal=+02:00")

    result = process(Geotagger(INDEX, HOUR), image, tool)

    assert result.outcome is Outcome.TAGGED
    assert result.issues == ()
    assert result.taken_at == datetime(2024, 1, 1, 10, 30, tzinfo=UTC)
    assert result.match is not None and result.match.time_difference == timedelta(minutes=30)
    assert gps(image) == ("33.8500 S", "151.2000 E")


@needs_exiftool
def test_dry_run_changes_nothing(tool: exiftool.ExifTool, tmp_path: Path) -> None:
    image = make_jpeg(tmp_path / "a.jpg", "-DateTimeOriginal=2024:01:01 10:00:00", "-OffsetTimeOriginal=+00:00")
    before = image.read_bytes()

    result = process(Geotagger(INDEX, HOUR, dry_run=True), image, tool)

    assert result.outcome is Outcome.TAGGED and result.dry_run
    assert result.message.startswith("Would set GPS")
    assert image.read_bytes() == before


@needs_exiftool
def test_skips_existing_gps_unless_overwrite(tool: exiftool.ExifTool, tmp_path: Path) -> None:
    image = make_jpeg(
        tmp_path / "a.jpg",
        "-DateTimeOriginal=2024:01:01 10:00:00", "-OffsetTimeOriginal=+00:00",
        "-GPSLatitude=1", "-GPSLatitudeRef=N", "-GPSLongitude=1", "-GPSLongitudeRef=W",
    )

    result = process(Geotagger(INDEX, HOUR), image, tool)
    assert result.outcome is Outcome.SKIPPED
    assert result.issues == ()
    assert gps(image) == ("1.0000 N", "1.0000 W")

    result = process(Geotagger(INDEX, HOUR, overwrite=True), image, tool)
    assert result.outcome is Outcome.TAGGED
    assert result.previous_position == (1.0, -1.0)
    assert gps(image) == ("33.8500 S", "151.2000 E")


@needs_exiftool
def test_overwrite_keeps_hemisphere_in_existing_xmp_gps(tool: exiftool.ExifTool, tmp_path: Path) -> None:
    image = make_jpeg(
        tmp_path / "a.jpg", "-DateTimeOriginal=2024:01:01 10:00:00", "-OffsetTimeOriginal=+00:00",
        "-XMP:GPSLatitude=10", "-XMP:GPSLongitude=20",
    )
    index = LocationIndex([Location(datetime(2024, 1, 1, 10, tzinfo=UTC), -33.85, -70.6, "visit_start")])

    assert process(Geotagger(index, HOUR, overwrite=True), image, tool).outcome is Outcome.TAGGED

    output = subprocess.run(
        ["exiftool", "-s3", "-n", "-XMP:GPSLatitude", "-XMP:GPSLongitude", str(image)],
        capture_output=True, text=True, check=True,
    ).stdout
    assert output.split() == ["-33.85", "-70.6"]
    assert gps(image) == ("33.8500 S", "70.6000 W")


@needs_exiftool
def test_skips_image_outside_tolerance(tool: exiftool.ExifTool, tmp_path: Path) -> None:
    image = make_jpeg(tmp_path / "a.jpg", "-DateTimeOriginal=2024:01:01 13:00:00", "-OffsetTimeOriginal=+00:00")
    result = process(Geotagger(INDEX, HOUR), image, tool)
    assert result.outcome is Outcome.SKIPPED
    assert result.issues == (Issue.NO_NEARBY_LOCATION,)


@needs_exiftool
def test_timezone_inferred_from_timeline(tool: exiftool.ExifTool, tmp_path: Path) -> None:
    # 21:00 local in Sydney (+11:00) is 10:00 UTC, when the timeline puts us there.
    image = make_jpeg(tmp_path / "a.jpg", "-DateTimeOriginal=2024:01:01 21:00:00")
    result = process(Geotagger(INDEX, timedelta(0)), image, tool)
    assert result.outcome is Outcome.TAGGED
    assert result.issues == (Issue.TIMEZONE_INFERRED,)
    assert result.taken_at == datetime(2024, 1, 1, 10, tzinfo=UTC)


@needs_exiftool
def test_assumed_timezone_and_time_shift(tool: exiftool.ExifTool, tmp_path: Path) -> None:
    # The camera showed 20:57 but was 3 minutes slow; Sydney is +11:00 in January.
    image = make_jpeg(tmp_path / "a.jpg", "-DateTimeOriginal=2024:01:01 20:57:00")
    geotagger = Geotagger(
        INDEX, timedelta(0), assumed_timezone=ZoneInfo("Australia/Sydney"), time_shift=timedelta(minutes=3)
    )
    result = process(geotagger, image, tool)
    assert result.outcome is Outcome.TAGGED
    assert result.issues == ()


@needs_exiftool
def test_falls_back_to_utc_without_any_offset(tool: exiftool.ExifTool, tmp_path: Path) -> None:
    index = LocationIndex([Location(datetime(2024, 1, 1, 10, tzinfo=UTC), 1.0, 2.0, "gpx")])
    image = make_jpeg(tmp_path / "a.jpg", "-DateTimeOriginal=2024:01:01 10:00:00")
    result = process(Geotagger(index, HOUR), image, tool)
    assert result.issues == (Issue.MISSING_TIMEZONE,)
    assert result.taken_at == datetime(2024, 1, 1, 10, tzinfo=UTC)


@needs_exiftool
def test_tags_video(tool: exiftool.ExifTool, tmp_path: Path) -> None:
    video = make_file(tmp_path / "a.mp4", TINY_MP4, "-api", "QuickTimeUTC", "-QuickTime:CreateDate=2024:01:01 10:10:00Z")
    result = process(Geotagger(INDEX, HOUR), video, tool)
    assert result.outcome is Outcome.TAGGED, result.message
    assert result.issues == (Issue.VIDEO_TIME_ASSUMED_UTC,)
    assert result.taken_at == datetime(2024, 1, 1, 10, 10, tzinfo=UTC)


@needs_exiftool
def test_timezone_applies_to_video_create_date(tool: exiftool.ExifTool, tmp_path: Path) -> None:
    # A camera that wrote 21:10 Sydney time into CreateDate.
    video = make_file(tmp_path / "a.mp4", TINY_MP4, "-QuickTime:CreateDate=2024:01:01 21:10:00")
    result = process(Geotagger(INDEX, HOUR, assumed_timezone=ZoneInfo("Australia/Sydney")), video, tool)
    assert result.issues == ()
    assert result.taken_at == datetime(2024, 1, 1, 10, 10, tzinfo=UTC)


@needs_exiftool
def test_skips_image_without_capture_time(tool: exiftool.ExifTool, tmp_path: Path) -> None:
    result = process(Geotagger(INDEX, HOUR), make_jpeg(tmp_path / "a.jpg"), tool)
    assert result.outcome is Outcome.SKIPPED
    assert result.issues == (Issue.MISSING_CAPTURE_TIME,)


@needs_exiftool
def test_reports_failure_for_unwritable_file(tool: exiftool.ExifTool, tmp_path: Path) -> None:
    image = make_jpeg(tmp_path / "a.jpg", "-DateTimeOriginal=2024:01:01 10:00:00", "-OffsetTimeOriginal=+00:00")
    tmp_path.chmod(0o555)  # exiftool writes a temp file next to the image
    try:
        result = process(Geotagger(INDEX, HOUR), image, tool)
    finally:
        tmp_path.chmod(0o755)
    assert result.outcome is Outcome.FAILED
    assert result.issues == (Issue.EXIFTOOL_ERROR,)


@needs_exiftool
def test_unexpected_error_fails_only_that_image(
    tool: exiftool.ExifTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    images = [make_jpeg(tmp_path / f"{i}.jpg", "-DateTimeOriginal=2024:01:01 10:00:00", "-OffsetTimeOriginal=+00:00") for i in range(2)]
    real_write_gps = tool.write_gps

    def write_gps(image: Path, *args: object, **kwargs: object) -> None:
        if image == images[0]:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        real_write_gps(image, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(tool, "write_gps", write_gps)
    results = Geotagger(INDEX, HOUR).process_batch(images, tool)

    assert [result.outcome for result in results] == [Outcome.FAILED, Outcome.TAGGED]
    assert results[0].issues == (Issue.UNEXPECTED_ERROR,)


@needs_exiftool
def test_failed_batch_read_fails_every_image_in_batch(
    tool: exiftool.ExifTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def read_metadata(images: list[Path]) -> None:
        raise ValueError("garbled output")

    monkeypatch.setattr(tool, "read_metadata", read_metadata)
    results = Geotagger(INDEX, HOUR).process_batch([tmp_path / "a.jpg", tmp_path / "b.jpg"], tool)

    assert [result.issues for result in results] == [(Issue.UNEXPECTED_ERROR,)] * 2


@needs_exiftool
def test_process_all_handles_every_image(tmp_path: Path) -> None:
    images = [make_jpeg(tmp_path / f"{i}.jpg", "-DateTimeOriginal=2024:01:01 10:00:00", "-OffsetTimeOriginal=+00:00") for i in range(4)]
    missing = tmp_path / "missing.jpg"
    results = list(Geotagger(INDEX, HOUR).process_all([*images, missing], workers=3))
    outcomes = {result.image: result.outcome for result in results}
    assert len(results) == 5
    assert outcomes == {**{image: Outcome.TAGGED for image in images}, missing: Outcome.FAILED}


def test_process_all_fails_images_cleanly_if_exiftool_cannot_start(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> None:
        raise exiftool.ExifToolError("ExifTool is not installed.")

    monkeypatch.setattr(exiftool, "ExifTool", broken)
    results = list(Geotagger(INDEX, HOUR).process_all([Path("a.jpg"), Path("b.jpg")], workers=2))
    assert [result.issues for result in results] == [(Issue.EXIFTOOL_ERROR,)] * 2


def test_find_media_matches_extensions_case_insensitively(tmp_path: Path) -> None:
    for name in ["a.jpg", "b.JPG", "c.Jpeg", "d.heic", "e.CR2", "e.xmp", "f.MOV", "notjpg", "g.txt", "sub/h.jpeg",
                 "a.jpg_original"]:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).touch()

    expected = ["a.jpg", "b.JPG", "c.Jpeg", "d.heic", "e.CR2", "f.MOV"]
    assert [p.name for p in find_media(tmp_path, recursive=False)] == expected
    assert [p.name for p in find_media(tmp_path, recursive=True)] == [*expected, "h.jpeg"]


def test_find_media_skips_hidden_files_and_folders(tmp_path: Path) -> None:
    for name in ["a.jpg", "._a.jpg", ".hidden.jpg", ".Trashes/b.jpg", "sub/c.jpg", "sub/._c.jpg"]:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).touch()

    assert [p.name for p in find_media(tmp_path, recursive=True)] == ["a.jpg", "c.jpg"]


def test_find_media_works_inside_hidden_directory(tmp_path: Path) -> None:
    directory = tmp_path / ".photos"
    directory.mkdir()
    (directory / "a.jpg").touch()
    assert [p.name for p in find_media(directory, recursive=False)] == ["a.jpg"]


@needs_exiftool
def test_closing_process_all_cancels_queued_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    geotagger = Geotagger(INDEX, HOUR)
    processed = []
    release = threading.Event()

    def process_batch(self: Geotagger, batch: list[Path], tool: exiftool.ExifTool) -> list[Path]:
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


@needs_exiftool
def test_process_all_reuses_one_exiftool_per_worker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    started = []
    real_exiftool = exiftool.ExifTool

    def counting_exiftool() -> exiftool.ExifTool:
        started.append(1)
        return real_exiftool()

    monkeypatch.setattr(exiftool, "ExifTool", counting_exiftool)
    images = [make_jpeg(tmp_path / f"{i}.jpg") for i in range(3 * MAX_BATCH_SIZE)]

    results = list(Geotagger(INDEX, HOUR).process_all(images, workers=1))

    assert len(results) == len(images)
    assert len(started) == 1
