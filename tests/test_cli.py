import csv
import json
import shutil
from datetime import UTC, timedelta
from pathlib import Path

import pytest
from media_files import gps, make_jpeg, read_tags

from exif_geotag import geotagger
from exif_geotag.cli import main, parse_args

needs_exiftool = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not installed")

VISIT = {
    "startTime": "2024-01-01T09:00:00.000+02:00",
    "endTime": "2024-01-01T18:00:00.000+02:00",
    "visit": {"topCandidate": {"placeLocation": "geo:-33.85,151.2"}},
}


@pytest.fixture
def timeline(tmp_path: Path) -> Path:
    path = tmp_path / "timeline.json"
    path.write_text(json.dumps([VISIT]), encoding="utf-8")
    return path


@pytest.fixture
def photos(tmp_path: Path) -> Path:
    directory = tmp_path / "photos"
    make_jpeg(directory / "a.jpg", "-DateTimeOriginal=2024:01:01 12:00:00")
    make_jpeg(
        directory / "b.jpg", "-DateTimeOriginal=2024:01:01 12:00:00",
        "-GPSLatitude=1", "-GPSLatitudeRef=N", "-GPSLongitude=2", "-GPSLongitudeRef=E",
    )
    return directory


@pytest.mark.parametrize("tolerance", ["-1", "nan", "inf", "1e20"])
def test_rejects_invalid_tolerance(tmp_path: Path, timeline: Path, tolerance: str) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parse_args(["-j", str(timeline), "-d", str(tmp_path), "-t", tolerance])
    assert exit_info.value.code == 2


def test_accepts_fractional_tolerance(tmp_path: Path, timeline: Path) -> None:
    assert parse_args(["-j", str(timeline), "-d", str(tmp_path), "-t", "0.5"]).tolerance == 0.5


def test_requires_json_and_dir_unless_undo(tmp_path: Path, timeline: Path) -> None:
    with pytest.raises(SystemExit):
        parse_args(["-d", str(tmp_path)])
    log = tmp_path / "log.csv"
    log.touch()
    assert parse_args(["--undo", str(log)]).undo == log


def test_accepts_several_location_files(tmp_path: Path, timeline: Path) -> None:
    gpx = tmp_path / "track.gpx"
    gpx.touch()
    args = parse_args(["-j", str(timeline), str(gpx), "-j", str(timeline), "-d", str(tmp_path)])
    assert args.json == [timeline, gpx, timeline]


@pytest.mark.parametrize(
    ("value", "offset"),
    [("UTC", timedelta(0)), ("+02:00", timedelta(hours=2)), ("-0530", timedelta(hours=-5, minutes=-30))],
)
def test_parses_fixed_timezones(tmp_path: Path, timeline: Path, value: str, offset: timedelta) -> None:
    zone = parse_args(["-j", str(timeline), "-d", str(tmp_path), "--timezone", value]).timezone
    assert zone.utcoffset(None) == offset


def test_parses_named_timezone_and_rejects_unknown(tmp_path: Path, timeline: Path) -> None:
    zone = parse_args(["-j", str(timeline), "-d", str(tmp_path), "--timezone", "Europe/Berlin"]).timezone
    assert str(zone) == "Europe/Berlin"
    with pytest.raises(SystemExit):
        parse_args(["-j", str(timeline), "-d", str(tmp_path), "--timezone", "Mars/Olympus"])


@pytest.mark.parametrize(
    ("value", "shift"),
    [("0:03", timedelta(minutes=3)), ("+1:00:30", timedelta(hours=1, seconds=30)), ("-0:00:10", timedelta(seconds=-10))],
)
def test_parses_time_shift(tmp_path: Path, timeline: Path, value: str, shift: timedelta) -> None:
    assert parse_args(["-j", str(timeline), "-d", str(tmp_path), f"--time-shift={value}"]).time_shift == shift


def test_rejects_bad_time_shift_and_probability(tmp_path: Path, timeline: Path) -> None:
    for extra in (["--time-shift", "3 minutes"], ["--min-probability", "2"], ["--workers", "0"]):
        with pytest.raises(SystemExit):
            parse_args(["-j", str(timeline), "-d", str(tmp_path), *extra])


@needs_exiftool
def test_invalid_timeline_exits_cleanly(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "timeline.json"
    path.write_text("[{", encoding="utf-8")

    assert main(["-j", str(path), "-d", str(tmp_path)]) == 1
    assert "Could not read timeline file" in capsys.readouterr().err


@needs_exiftool
def test_dry_run_writes_log_but_no_gps(timeline: Path, photos: Path, tmp_path: Path) -> None:
    log = tmp_path / "log.csv"

    assert main(["-j", str(timeline), "-d", str(photos), "--dry-run", "--log", str(log), "-q"]) == 0

    assert read_tags(photos / "a.jpg", "GPSLatitude") == []
    rows = {Path(row["image"]).name: row for row in csv.DictReader(log.open(encoding="utf-8"))}
    assert rows["a.jpg"]["outcome"] == "tagged" and rows["a.jpg"]["dry_run"] == "True"
    assert rows["a.jpg"]["source"] == "visit_start"
    assert rows["b.jpg"]["outcome"] == "skipped"


@needs_exiftool
@pytest.mark.parametrize("log_name", ["log.csv", "log.json"])
def test_undo_removes_new_gps_and_restores_overwritten_gps(
    timeline: Path, photos: Path, tmp_path: Path, log_name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / log_name
    assert main(["-j", str(timeline), "-d", str(photos), "--overwrite", "--log", str(log), "-q"]) == 0
    assert gps(photos / "a.jpg") == gps(photos / "b.jpg") == ("33.8500 S", "151.2000 E")

    assert main(["--undo", str(log), "--dry-run"]) == 0
    assert gps(photos / "a.jpg") == ("33.8500 S", "151.2000 E")

    assert main(["--undo", str(log)]) == 0

    assert read_tags(photos / "a.jpg", "GPSLatitude") == []
    assert gps(photos / "b.jpg") == ("1.0000 N", "2.0000 E")
    assert "Done. 1 restored, 1 removed, 0 failed." in capsys.readouterr().out


@needs_exiftool
def test_timezone_is_inferred_from_timeline(timeline: Path, photos: Path, tmp_path: Path) -> None:
    log = tmp_path / "log.json"
    assert main(["-j", str(timeline), "-d", str(photos), "--log", str(log), "-q"]) == 0
    row = next(row for row in json.loads(log.read_text(encoding="utf-8")) if row["image"].endswith("a.jpg"))
    assert row["taken_at_utc"] == "2024-01-01T10:00:00+00:00"


@needs_exiftool
def test_ctrl_c_prints_partial_report(
    timeline: Path, photos: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted(*args: object, **kwargs: object) -> object:
        raise KeyboardInterrupt
        yield

    monkeypatch.setattr(geotagger.Geotagger, "process_all", interrupted)

    assert main(["-j", str(timeline), "-d", str(photos)]) == 130
    assert "Interrupted, processed so far: 0 tagged" in capsys.readouterr().out


@needs_exiftool
def test_no_locations_after_probability_filter(tmp_path: Path, photos: Path, capsys: pytest.CaptureFixture[str]) -> None:
    unlikely = {**VISIT, "visit": {**VISIT["visit"], "probability": "0.1"}}  # type: ignore[dict-item]
    path = tmp_path / "timeline.json"
    path.write_text(json.dumps([unlikely]), encoding="utf-8")
    assert main(["-j", str(path), "-d", str(photos), "--min-probability", "0.5"]) == 1
    assert "No locations found" in capsys.readouterr().err


def test_utc_is_accepted_as_timezone(tmp_path: Path, timeline: Path) -> None:
    assert parse_args(["-j", str(timeline), "-d", str(tmp_path), "--timezone", "utc"]).timezone is UTC
