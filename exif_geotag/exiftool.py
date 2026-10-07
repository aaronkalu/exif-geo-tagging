from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any

from exif_geotag.media import MediaKind, media_kind, sidecar_for

# OffsetTime belongs to ModifyDate, which editing software may change later, so it is the last resort.
_OFFSET_TAGS = ("OffsetTimeOriginal", "OffsetTimeDigitized", "OffsetTime")
_READ_TAGS = (
    "DateTimeOriginal", *_OFFSET_TAGS, "Keys:CreationDate", "QuickTime:CreateDate",
    "Composite:GPSLatitude", "Composite:GPSLongitude", "XMP-exif:GPSLatitude", "XMP-exif:GPSLongitude",
    "GPSLatitude", "GPSLongitude", "GPSCoordinates",
)
_DATETIME = re.compile(r"^(\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2})(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?")
_NOT_INSTALLED = "ExifTool is not installed. Please install ExifTool to use this script."


class ExifToolError(RuntimeError):
    pass


@dataclass(frozen=True)
class ImageMetadata:
    local_time: datetime | None
    """Naive, as shown by the camera clock."""

    utc_offset: timedelta | None
    """None if the file does not say which time zone `local_time` is in."""

    has_gps: bool
    position: tuple[float, float] | None = None
    """Existing signed latitude and longitude, if readable."""

    @property
    def taken_at(self) -> datetime | None:
        """In UTC, assuming UTC if the offset is unknown."""
        if self.local_time is None:
            return None
        return (self.local_time - (self.utc_offset or timedelta(0))).replace(tzinfo=UTC)


def version() -> str:
    try:
        result = subprocess.run(["exiftool", "-ver"], capture_output=True, text=True)
    except FileNotFoundError as error:
        raise ExifToolError(_NOT_INSTALLED) from error
    if result.returncode != 0:
        raise ExifToolError(result.stderr.strip() or f"exiftool exited with status {result.returncode}")
    return result.stdout.strip()


class ExifTool:
    """One long-running `exiftool -stay_open` process.

    Starting ExifTool (Perl) takes far longer than reading or writing a typical photo, so every command reuses
    this process. Not thread-safe: use one instance per thread.
    """

    def __init__(self) -> None:
        try:
            self._process = subprocess.Popen(
                [
                    "exiftool", "-stay_open", "True", "-@", "-",
                    "-common_args", "-charset", "filename=utf8", "-api", "QuickTimeUTC",
                ],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except FileNotFoundError as error:
            raise ExifToolError(_NOT_INSTALLED) from error
        self._sequence = 0
        # Drained on a thread so a burst of messages cannot fill the pipe and block ExifTool while we read stdout.
        self._stderr_lines: queue.SimpleQueue[str | None] = queue.SimpleQueue()
        threading.Thread(target=_pump_lines, args=(self._process.stderr, self._stderr_lines), daemon=True).start()

    def __enter__(self) -> ExifTool:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def alive(self) -> bool:
        return self._process.poll() is None

    def close(self) -> None:
        if self.alive:
            try:
                self._send(["-stay_open", "False"])
                self._process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                self._process.kill()
                self._process.wait()
        for pipe in (self._process.stdin, self._process.stdout, self._process.stderr):
            if pipe is not None:
                pipe.close()

    def execute(self, *arguments: str) -> tuple[str, str]:
        """Runs one ExifTool command and returns its stdout and stderr."""
        if any("\n" in argument or "\r" in argument for argument in arguments):
            raise ExifToolError("arguments containing line breaks cannot be passed to ExifTool")
        if not self.alive:
            raise ExifToolError("ExifTool exited unexpectedly")
        self._sequence += 1
        marker = f"{{ready{self._sequence}}}"
        try:
            self._send([*arguments, "-echo4", marker, f"-execute{self._sequence}"])
        except OSError as error:
            raise ExifToolError(f"ExifTool exited unexpectedly: {error}") from error

        assert self._process.stdout is not None
        stdout = []
        while (line := self._process.stdout.readline()) != b"":
            text = line.decode("utf-8", errors="replace").rstrip("\r\n")
            if text == marker:
                break
            stdout.append(text)
        else:
            raise ExifToolError("ExifTool exited unexpectedly")

        stderr = []
        while (message := self._stderr_lines.get()) != marker:
            if message is None:
                raise ExifToolError("ExifTool exited unexpectedly")
            stderr.append(message)
        return "\n".join(stdout), "\n".join(stderr)

    def read_metadata(self, images: Sequence[Path]) -> dict[Path, ImageMetadata | ExifToolError]:
        """Reads all images with one command; a file that cannot be read gets an error instead of metadata."""
        sidecars = {
            image: sidecar for image in images
            if media_kind(image) is MediaKind.RAW and (sidecar := sidecar_for(image)).is_file()
        }
        readable = [path for path in (*images, *sidecars.values()) if not _has_line_break(path)]
        tags_by_file, stderr_lines = self._read_tags(readable) if readable else ({}, [])

        metadata: dict[Path, ImageMetadata | ExifToolError] = {}
        for image in images:
            result = _metadata_or_error(image, tags_by_file, stderr_lines)
            image_sidecar = sidecars.get(image)
            if image_sidecar is not None and isinstance(result, ImageMetadata):
                sidecar_result = _metadata_or_error(image_sidecar, tags_by_file, stderr_lines)
                if isinstance(sidecar_result, ExifToolError):
                    result = ExifToolError(f"sidecar {image_sidecar.name}: {sidecar_result}")
                else:
                    result = _merge_sidecar(result, sidecar_result)
            metadata[image] = result
        return metadata

    def write_gps(
        self,
        image: Path,
        latitude: float,
        longitude: float,
        gps_time: datetime | None = None,
        keep_backup: bool = False,
    ) -> None:
        """Writes into the image itself, or into its XMP sidecar for RAW files (creating it if needed)."""
        kind = media_kind(image)
        if kind is MediaKind.VIDEO:
            coordinates = f"{latitude}, {longitude}"
            tags = [f"-Keys:GPSCoordinates={coordinates}", f"-UserData:GPSCoordinates={coordinates}"]
        elif kind is MediaKind.RAW:
            tags = [
                f"-XMP-exif:GPSLatitude={latitude}",
                f"-XMP-exif:GPSLongitude={longitude}",
                "-XMP-exif:GPSMapDatum=WGS-84",
            ]
            if gps_time is not None:
                tags.append(f"-XMP-exif:GPSDateTime={gps_time.astimezone(UTC):%Y:%m:%d %H:%M:%S}Z")
        else:
            # Signed values, so groups without a Ref tag (XMP) keep the hemisphere;
            # ExifTool derives N/S/E/W from the sign.
            tags = [
                f"-GPSLatitude={latitude}", f"-GPSLatitudeRef={latitude}",
                f"-GPSLongitude={longitude}", f"-GPSLongitudeRef={longitude}",
                "-GPSMapDatum=WGS-84",
            ]
            if gps_time is not None:
                utc = gps_time.astimezone(UTC)
                tags += [f"-GPSDateStamp={utc:%Y:%m:%d}", f"-GPSTimeStamp={utc:%H:%M:%S}"]
        self._write(image, tags, keep_backup)

    def remove_gps(self, image: Path, keep_backup: bool = False) -> None:
        kind = media_kind(image)
        xmp_tags = [
            "-XMP-exif:GPSLatitude=", "-XMP-exif:GPSLongitude=", "-XMP-exif:GPSDateTime=", "-XMP-exif:GPSMapDatum="
        ]
        if kind is MediaKind.VIDEO:
            tags = ["-Keys:GPSCoordinates=", "-UserData:GPSCoordinates="]
        elif kind is MediaKind.RAW:
            tags = xmp_tags
        else:
            tags = ["-GPS:all=", *xmp_tags]
        self._write(image, tags, keep_backup)

    def _write(self, image: Path, tags: list[str], keep_backup: bool) -> None:
        target = sidecar_for(image) if media_kind(image) is MediaKind.RAW else image
        stdout, stderr = self.execute(*tags, "-P", *([] if keep_backup else ["-overwrite_original"]), str(target))
        errors = [line for line in stderr.splitlines() if line.startswith("Error")]
        if errors or "weren't updated" in stdout or "weren't created" in stdout:
            raise ExifToolError("; ".join(errors) or stdout.strip())

    def _read_tags(self, files: Sequence[Path]) -> tuple[dict[Path, dict[str, Any]], list[str]]:
        stdout, stderr = self.execute(
            "-json", "-n", "-G1", "-Error", *(f"-{tag}" for tag in _READ_TAGS), *(str(file) for file in files)
        )
        try:
            records = json.loads(stdout) if stdout.strip() else []
        except json.JSONDecodeError as error:
            raise ExifToolError(f"unreadable ExifTool output: {error}") from error
        return {Path(tags["SourceFile"]): tags for tags in records}, stderr.splitlines()

    def _send(self, lines: list[str]) -> None:
        assert self._process.stdin is not None
        self._process.stdin.write("".join(f"{line}\n" for line in lines).encode("utf-8"))
        self._process.stdin.flush()


def _pump_lines(pipe: IO[bytes], lines: queue.SimpleQueue[str | None]) -> None:
    for line in pipe:
        lines.put(line.decode("utf-8", errors="replace").rstrip("\r\n"))
    lines.put(None)


def _has_line_break(path: Path) -> bool:
    return "\n" in str(path) or "\r" in str(path)


def _metadata_or_error(
    file: Path, tags_by_file: dict[Path, dict[str, Any]], stderr_lines: list[str]
) -> ImageMetadata | ExifToolError:
    if _has_line_break(file):
        return ExifToolError("file names containing line breaks are not supported")
    tags = tags_by_file.get(file)
    if tags is None:
        reasons = [line for line in stderr_lines if line.endswith(f" - {file}")]
        return ExifToolError("; ".join(reasons) or "no metadata returned by ExifTool")
    error = _tag(tags, "Error")
    if error is not None:
        return ExifToolError(str(error))
    return _to_metadata(tags)


def _to_metadata(tags: dict[str, Any]) -> ImageMetadata:
    local_time, offset = _capture_time(tags)
    # EXIF stores unsigned values plus a Ref tag; the Composite tag combines them. XMP files have no Composite tag
    # but store signed values themselves.
    position = _position(tags, "Composite") or _position(tags, "XMP-exif")
    has_gps = any(_tag(tags, name) is not None for name in ("GPSLatitude", "GPSLongitude", "GPSCoordinates"))
    if position == (0.0, 0.0):  # placeholder some cameras write when they have no fix
        has_gps, position = False, None
    return ImageMetadata(local_time, offset, has_gps, position)


def _capture_time(tags: dict[str, Any]) -> tuple[datetime | None, timedelta | None]:
    original = _parse_datetime(_tag(tags, "DateTimeOriginal"))
    if original is not None:
        local_time, offset = original
        if offset is None:
            offsets = (_parse_offset(_tag(tags, tag)) for tag in _OFFSET_TAGS)
            offset = next((offset for offset in offsets if offset is not None), None)
        return local_time, offset
    # Videos: Apple's CreationDate carries the local offset; QuickTime CreateDate is UTC by specification, and
    # QuickTimeUTC makes ExifTool return it with an offset.
    for name, group in (("CreationDate", "Keys"), ("CreateDate", "QuickTime")):
        parsed = _parse_datetime(_tag(tags, name, group))
        if parsed is not None:
            return parsed
    return None, None


def _position(tags: dict[str, Any], group: str) -> tuple[float, float] | None:
    latitude, longitude = _tag(tags, "GPSLatitude", group), _tag(tags, "GPSLongitude", group)
    return (float(latitude), float(longitude)) if _is_number(latitude) and _is_number(longitude) else None


def _merge_sidecar(raw: ImageMetadata, sidecar: ImageMetadata) -> ImageMetadata:
    merged = replace(raw, has_gps=raw.has_gps or sidecar.has_gps, position=sidecar.position or raw.position)
    if merged.local_time is None:
        merged = replace(merged, local_time=sidecar.local_time, utc_offset=sidecar.utc_offset)
    return merged


def _tag(tags: dict[str, Any], name: str, group: str | None = None) -> Any:
    for key, value in tags.items():
        key_group, _, key_name = key.rpartition(":")
        if key_name == name and (group is None or key_group == group):
            return value
    return None


def _parse_datetime(value: object) -> tuple[datetime, timedelta | None] | None:
    if not isinstance(value, str):
        return None
    match = _DATETIME.match(value.strip())
    if not match:
        return None
    try:
        local_time = datetime.strptime(match[1], "%Y:%m:%d %H:%M:%S")
    except ValueError:  # e.g. the placeholder "0000:00:00 00:00:00"
        return None
    return local_time, _parse_offset(match[2])


def _parse_offset(offset: object) -> timedelta | None:
    if not isinstance(offset, str):
        return None
    if offset.strip() in ("Z", "z"):
        return timedelta(0)
    try:
        return datetime.strptime(offset.strip(), "%z").utcoffset()
    except ValueError:
        return None


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
