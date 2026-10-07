from __future__ import annotations

import enum
from pathlib import Path


class MediaKind(enum.Enum):
    IMAGE = "image"
    RAW = "raw"
    """GPS data goes into an XMP sidecar: writing proprietary RAW files is riskier, and editors read sidecars."""

    VIDEO = "video"


_KINDS = {
    **dict.fromkeys((".jpg", ".jpeg", ".heic", ".heif", ".png", ".tif", ".tiff", ".webp", ".dng"), MediaKind.IMAGE),
    **dict.fromkeys(
        (".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".raf", ".orf", ".rw2", ".pef", ".srw",
         ".x3f", ".3fr", ".iiq", ".rwl", ".erf", ".mef", ".mos"),
        MediaKind.RAW,
    ),
    **dict.fromkeys((".mp4", ".mov", ".m4v", ".3gp"), MediaKind.VIDEO),
}


def media_kind(path: Path) -> MediaKind | None:
    return _KINDS.get(path.suffix.lower())


def sidecar_for(raw: Path) -> Path:
    """An existing darktable-style `IMG.CR2.xmp`, else the Adobe-style `IMG.xmp` (created on first write)."""
    darktable_style = raw.with_name(f"{raw.name}.xmp")
    return darktable_style if darktable_style.is_file() else raw.with_suffix(".xmp")


def write_target(path: Path) -> Path:
    return sidecar_for(path) if media_kind(path) is MediaKind.RAW else path


def find_media(directory: Path, recursive: bool) -> list[Path]:
    candidates = directory.rglob("*") if recursive else directory.iterdir()
    return sorted(
        path
        for path in candidates
        if media_kind(path) is not None and not _is_hidden(path.relative_to(directory)) and path.is_file()
    )


def _is_hidden(relative_path: Path) -> bool:
    # Also excludes macOS AppleDouble "._*" files and system folders such as .Trashes, which ExifTool cannot tag.
    return any(part.startswith(".") for part in relative_path.parts)
