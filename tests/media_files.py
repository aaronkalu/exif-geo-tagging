import base64
import struct
import subprocess
import zlib
from pathlib import Path

# Smallest valid baseline JPEG (1x1 pixel).
TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////"
    "////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA="
)


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


TINY_PNG = (
    b"\x89PNG\r\n\x1a\n"
    + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
    + _png_chunk(b"IDAT", zlib.compress(b"\x00\x00"))
    + _png_chunk(b"IEND", b"")
)

# ftyp + moov/mvhd + empty mdat: enough for ExifTool to read and write QuickTime tags.
_MVHD = _box(
    b"mvhd",
    bytes(4) + struct.pack(">IIII", 0, 0, 600, 0) + struct.pack(">IH", 0x10000, 0x100) + bytes(10)
    + struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000) + bytes(24) + struct.pack(">I", 2),
)
TINY_MP4 = _box(b"ftyp", b"isom" + bytes(4) + b"isommp42") + _box(b"moov", _MVHD) + _box(b"mdat", b"")


def make_file(path: Path, content: bytes, *tags: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if tags:
        subprocess.run(["exiftool", "-q", "-q", "-overwrite_original", *tags, str(path)], check=True)
    return path


def make_jpeg(path: Path, *tags: str) -> Path:
    return make_file(path, TINY_JPEG, *tags)


def read_tags(path: Path, *tags: str) -> list[str]:
    output = subprocess.run(
        ["exiftool", "-s3", "-n", "-api", "QuickTimeUTC", *(f"-{tag}" for tag in tags), str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return output.splitlines()


def gps(path: Path) -> tuple[str, str]:
    output = subprocess.run(
        ["exiftool", "-s3", "-c", "%.4f", "-GPSLatitude", "-GPSLongitude", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    latitude, longitude = output.splitlines()
    return latitude, longitude
