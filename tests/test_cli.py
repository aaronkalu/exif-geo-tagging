import json
import shutil
from pathlib import Path

import pytest

from exif_geotag.cli import main, parse_args


@pytest.fixture
def timeline(tmp_path: Path) -> Path:
    path = tmp_path / "timeline.json"
    path.write_text(json.dumps([]), encoding="utf-8")
    return path


@pytest.mark.parametrize("tolerance", ["-1", "nan", "inf", "1e20"])
def test_rejects_invalid_tolerance(tmp_path: Path, timeline: Path, tolerance: str) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parse_args(["-j", str(timeline), "-d", str(tmp_path), "-t", tolerance])
    assert exit_info.value.code == 2


def test_accepts_fractional_tolerance(tmp_path: Path, timeline: Path) -> None:
    assert parse_args(["-j", str(timeline), "-d", str(tmp_path), "-t", "0.5"]).tolerance == 0.5


@pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not installed")
def test_invalid_timeline_exits_cleanly(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "timeline.json"
    path.write_text("[{", encoding="utf-8")

    assert main(["-j", str(path), "-d", str(tmp_path)]) == 1
    assert "Could not read timeline file" in capsys.readouterr().err
