from __future__ import annotations

import enum
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from exif_geotag import exiftool


class UndoOutcome(enum.Enum):
    RESTORED = "restored"
    """The image had GPS data before and it was overwritten: the old position was written back."""

    REMOVED = "removed"
    FAILED = "failed"


@dataclass(frozen=True)
class UndoResult:
    image: Path
    outcome: UndoOutcome
    message: str

    def __str__(self) -> str:
        return f"Image {self.image}: {self.message}"


def tagged_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows from dry runs, or for images that were skipped or failed, have nothing to undo."""
    return [row for row in rows if row.get("outcome") == "tagged" and not _is_true(row.get("dry_run"))]


def undo(rows: list[dict[str, Any]], dry_run: bool = False, keep_backup: bool = False) -> Iterator[UndoResult]:
    if not rows:
        return
    with exiftool.ExifTool() as tool:
        for row in rows:
            yield _undo_row(tool, row, dry_run, keep_backup)


def _undo_row(tool: exiftool.ExifTool, row: dict[str, Any], dry_run: bool, keep_backup: bool) -> UndoResult:
    image = Path(str(row.get("image", "")))
    try:
        previous = _position(row.get("previous_latitude"), row.get("previous_longitude"))
        if previous is not None:
            if not dry_run:
                tool.write_gps(image, *previous, keep_backup=keep_backup)
            message = f"{'Would restore' if dry_run else 'Restored'} previous GPS {previous}."
            return UndoResult(image, UndoOutcome.RESTORED, message)
        if not dry_run:
            tool.remove_gps(image, keep_backup=keep_backup)
        return UndoResult(image, UndoOutcome.REMOVED, f"{'Would remove' if dry_run else 'Removed'} GPS data.")
    except Exception as error:
        return UndoResult(image, UndoOutcome.FAILED, f"Failed: {error}")


def _position(latitude: Any, longitude: Any) -> tuple[float, float] | None:
    if latitude in (None, "") or longitude in (None, ""):
        return None
    return float(latitude), float(longitude)


def _is_true(value: Any) -> bool:
    return value is True or str(value).lower() == "true"
