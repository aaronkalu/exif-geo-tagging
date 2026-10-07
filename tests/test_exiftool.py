import os
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from media_files import TINY_JPEG, TINY_MP4, TINY_PNG, gps, make_file, make_jpeg, read_tags

from exif_geotag.exiftool import ExifTool, ExifToolError, ImageMetadata

pytestmark = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not installed")


@pytest.fixture
def tool() -> Iterator[ExifTool]:
    with ExifTool() as tool:
        yield tool


def read_one(tool: ExifTool, path: Path) -> ImageMetadata:
    metadata = tool.read_metadata([path])[path]
    assert isinstance(metadata, ImageMetadata), metadata
    return metadata


def test_read_metadata_applies_offset(tool: ExifTool, tmp_path: Path) -> None:
    image = make_jpeg(tmp_path / "a.jpg", "-DateTimeOriginal=2024:01:01 11:30:00", "-OffsetTimeOriginal=+02:00")
    metadata = read_one(tool, image)
    assert metadata.local_time == datetime(2024, 1, 1, 11, 30)
    assert metadata.utc_offset == timedelta(hours=2)
    assert metadata.taken_at == datetime(2024, 1, 1, 9, 30, tzinfo=UTC)
    assert not metadata.has_gps


def test_read_metadata_without_offset(tool: ExifTool, tmp_path: Path) -> None:
    metadata = read_one(tool, make_jpeg(tmp_path / "a.jpg", "-DateTimeOriginal=2024:01:01 11:30:00"))
    assert metadata.utc_offset is None
    assert metadata.taken_at == datetime(2024, 1, 1, 11, 30, tzinfo=UTC)


@pytest.mark.parametrize("tag", ["OffsetTimeDigitized", "OffsetTime"])
def test_read_metadata_falls_back_to_other_offset_tags(tool: ExifTool, tmp_path: Path, tag: str) -> None:
    image = make_jpeg(tmp_path / "a.jpg", "-DateTimeOriginal=2024:01:01 11:30:00", f"-{tag}=+02:00")
    assert read_one(tool, image).utc_offset == timedelta(hours=2)


def test_read_metadata_uses_offset_inside_xmp_date(tool: ExifTool, tmp_path: Path) -> None:
    image = make_jpeg(tmp_path / "a.jpg", "-XMP-exif:DateTimeOriginal=2024:01:01 11:30:00-05:00")
    assert read_one(tool, image).taken_at == datetime(2024, 1, 1, 16, 30, tzinfo=UTC)


def test_read_metadata_reports_signed_position(tool: ExifTool, tmp_path: Path) -> None:
    image = make_jpeg(
        tmp_path / "a.jpg", "-GPSLatitude=33.85", "-GPSLatitudeRef=S", "-GPSLongitude=70.6", "-GPSLongitudeRef=W"
    )
    metadata = read_one(tool, image)
    assert metadata.has_gps
    assert metadata.position == (-33.85, -70.6)


def test_zero_zero_gps_counts_as_missing(tool: ExifTool, tmp_path: Path) -> None:
    image = make_jpeg(
        tmp_path / "a.jpg", "-GPSLatitude=0", "-GPSLatitudeRef=N", "-GPSLongitude=0", "-GPSLongitudeRef=E"
    )
    metadata = read_one(tool, image)
    assert not metadata.has_gps and metadata.position is None


def test_read_metadata_reports_unreadable_files_individually(tool: ExifTool, tmp_path: Path) -> None:
    good = make_jpeg(tmp_path / "good.jpg", "-DateTimeOriginal=2024:01:01 10:00:00")
    missing = tmp_path / "missing.jpg"
    line_break = tmp_path / "line\nbreak.jpg"

    metadata = tool.read_metadata([missing, good, line_break])

    assert str(metadata[missing]) == f"Error: File not found - {missing}"
    assert isinstance(metadata[line_break], ExifToolError)
    assert isinstance(metadata[good], ImageMetadata)


def test_writes_gps_with_time_and_datum_and_keeps_modification_time(tool: ExifTool, tmp_path: Path) -> None:
    image = make_jpeg(tmp_path / "a.jpg", "-DateTimeOriginal=2024:01:01 12:30:00")
    mtime = image.stat().st_mtime - 1000
    os.utime(image, (mtime, mtime))

    tool.write_gps(image, -33.85, 151.2, datetime(2024, 1, 1, 10, 30, 5, tzinfo=UTC))

    assert gps(image) == ("33.8500 S", "151.2000 E")
    assert read_tags(image, "GPSDateStamp", "GPSTimeStamp", "GPSMapDatum") == ["2024:01:01", "10:30:05", "WGS-84"]
    assert image.stat().st_mtime == pytest.approx(mtime)
    assert not list(tmp_path.glob("*_original"))


def test_backup_keeps_original_file(tool: ExifTool, tmp_path: Path) -> None:
    image = make_jpeg(tmp_path / "a.jpg")
    tool.write_gps(image, 1.0, 2.0, keep_backup=True)
    assert (tmp_path / "a.jpg_original").read_bytes() == TINY_JPEG


def test_writes_png(tool: ExifTool, tmp_path: Path) -> None:
    image = make_file(tmp_path / "a.png", TINY_PNG)
    tool.write_gps(image, 1.5, -2.0)
    assert read_one(tool, image).position == (1.5, -2.0)


def test_video_capture_time_and_gps(tool: ExifTool, tmp_path: Path) -> None:
    video = make_file(tmp_path / "a.mp4", TINY_MP4, "-api", "QuickTimeUTC", "-QuickTime:CreateDate=2024:01:01 10:00:00Z")
    metadata = read_one(tool, video)
    assert metadata.taken_at == datetime(2024, 1, 1, 10, tzinfo=UTC)
    assert metadata.utc_offset is not None

    tool.write_gps(video, 1.5, -2.0)

    assert read_tags(video, "Keys:GPSCoordinates", "UserData:GPSCoordinates") == ["1.5 -2", "1.5 -2"]
    assert read_one(tool, video).position == (1.5, -2.0)


def test_apple_creation_date_with_offset_wins_for_video(tool: ExifTool, tmp_path: Path) -> None:
    video = make_file(
        tmp_path / "a.mov", TINY_MP4,
        "-api", "QuickTimeUTC", "-QuickTime:CreateDate=2024:01:01 10:00:00Z", "-Keys:CreationDate=2024:01:01 13:00:00+02:00",
    )
    metadata = read_one(tool, video)
    assert (metadata.local_time, metadata.utc_offset) == (datetime(2024, 1, 1, 13), timedelta(hours=2))


def test_raw_gets_xmp_sidecar_and_raw_is_untouched(tool: ExifTool, tmp_path: Path) -> None:
    # A JPEG named .nef: ExifTool reads it by content, and the tool must only ever write the sidecar.
    raw = make_jpeg(tmp_path / "IMG_1.jpg", "-DateTimeOriginal=2024:01:01 10:00:00").rename(tmp_path / "IMG_1.nef")
    before = raw.read_bytes()

    tool.write_gps(raw, -33.85, 151.2, datetime(2024, 1, 1, 10, tzinfo=UTC))

    sidecar = tmp_path / "IMG_1.xmp"
    assert raw.read_bytes() == before
    assert read_tags(sidecar, "XMP-exif:GPSLatitude", "XMP-exif:GPSLongitude", "XMP-exif:GPSDateTime") == [
        "-33.85", "151.2", "2024:01:01 10:00:00Z",
    ]
    metadata = read_one(tool, raw)
    assert metadata.has_gps and metadata.position == (-33.85, 151.2)
    assert metadata.local_time == datetime(2024, 1, 1, 10)


def test_existing_darktable_sidecar_is_used(tool: ExifTool, tmp_path: Path) -> None:
    raw = make_jpeg(tmp_path / "IMG_1.jpg").rename(tmp_path / "IMG_1.nef")
    sidecar = tmp_path / "IMG_1.nef.xmp"
    tool.execute("-XMP-dc:Title=x", str(sidecar))

    tool.write_gps(raw, 1.0, 2.0)

    assert read_tags(sidecar, "XMP-dc:Title", "XMP-exif:GPSLatitude") == ["x", "1"]
    assert not (tmp_path / "IMG_1.xmp").exists()


def test_remove_gps(tool: ExifTool, tmp_path: Path) -> None:
    image = make_jpeg(tmp_path / "a.jpg")
    video = make_file(tmp_path / "a.mp4", TINY_MP4)
    for path in (image, video):
        tool.write_gps(path, 1.0, 2.0, datetime(2024, 1, 1, tzinfo=UTC))
        tool.remove_gps(path)
        assert not read_one(tool, path).has_gps


def test_write_error_is_reported(tool: ExifTool, tmp_path: Path) -> None:
    with pytest.raises(ExifToolError):
        tool.write_gps(tmp_path / "missing.jpg", 1.0, 2.0)


def test_dead_process_raises(tmp_path: Path) -> None:
    tool = ExifTool()
    tool._process.kill()
    tool._process.wait()
    with pytest.raises(ExifToolError, match="exited unexpectedly"):
        tool.read_metadata([make_jpeg(tmp_path / "a.jpg")])
    tool.close()
