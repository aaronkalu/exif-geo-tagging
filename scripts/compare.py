"""Compare the original geotag.py with the current version on real photos that already carry GPS.

The photos' own GPS positions are the ground truth. Two copies are made with GPS removed, one is tagged by the
old script and one by the current version, and both are scored against the truth. Every file is also checked
for side effects: changes to non-GPS metadata, pixel data or file system state, leftovers after --dry-run and
--undo. The source folder is only read.

    uv run python scripts/compare.py --photos ~/Pictures/sample --json Timeline.json --out /tmp/geotag-compare
    uv run python scripts/compare.py ... -- --timezone Europe/Vienna   # extra arguments for the current version
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
OLD_REF = "f2e090b"

MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".heic", ".heif", ".png", ".tif", ".tiff", ".webp", ".dng",
    ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".raf", ".orf", ".rw2", ".pef", ".srw",
    ".x3f", ".3fr", ".iiq", ".rwl", ".erf", ".mef", ".mos",
    ".mp4", ".mov", ".m4v", ".3gp",
}
SAME_METRES = 50
REGRESSION_METRES = 200
WRONG_PLACE_METRES = 1000

# Tags ExifTool rewrites whenever it saves a file, or that are computed rather than stored.
VOLATILE_GROUPS = {"System", "ExifTool", "Composite"}
# Byte positions of embedded previews move when the GPS block grows.
LAYOUT_SUFFIXES = ("Offset", "Offsets", "Start")
WRITER_STAMPS = {"XMP-x:XMPToolkit"}


@dataclass
class Snapshot:
    files: dict[str, tuple[str, int, int]]
    """relative path -> (sha256, size, mtime_ns), hidden files included."""

    tags: dict[str, dict[str, Any]]


@dataclass
class ToolRun:
    name: str
    exit_code: int
    seconds: float
    output_tail: str
    positions: dict[str, tuple[float, float]] = field(default_factory=dict)


def main() -> int:
    args, tool_args = parse_args()
    out: Path = args.out
    work_old, work_new = out / "old", out / "new"

    print("Reading ground truth from the source photos...")
    media = sorted(
        relative for relative in walk(args.photos)
        if relative.suffix.lower() in MEDIA_EXTENSIONS and not is_hidden(relative)
    )
    if not media:
        sys.exit(f"No photos or videos found in {args.photos}")
    truth = read_positions(args.photos)
    source_tags = read_all_tags(args.photos)

    for copy in (work_old, work_new):
        print(f"Copying to {copy} and removing GPS...")
        shutil.copytree(args.photos, copy, copy_function=shutil.copy2, symlinks=True)
        strip_gps(copy)
    leftover = read_positions(work_new)
    if leftover:
        print(f"Warning: {len(leftover)} files still have GPS after stripping; they are left out of scoring.")

    before = snapshot(work_new)
    before_old = snapshot(work_old)

    print("Dry run of the current version...")
    dry_log = out / "dry-run.csv"
    dry = run_new(work_new, args, tool_args, ["--dry-run", "--log", str(dry_log)])
    after_dry = snapshot(work_new)

    print("Running the original script...")
    old = run_old(work_old, args)
    old.positions = read_positions(work_old)
    after_old = snapshot(work_old)

    print("Running the current version...")
    log = out / "new.csv"
    new = run_new(work_new, args, tool_args, ["--log", str(log)])
    new.positions = read_positions(work_new)
    after_new = snapshot(work_new)
    new_log = {Path(row["image"]).resolve(): row for row in read_csv(log)}

    print("Undoing the current version's run...")
    undo = run_new(work_new, args, [], ["--undo", str(log)])
    after_undo = snapshot(work_new)

    rows = score(media, truth, leftover, old, new, new_log, work_new)
    write_rows(out / "files.csv", rows)
    report = render_report(
        args, media, rows, old, new, dry, undo, source_tags,
        side_effects("Original script", before_old, after_old, set(old.positions)),
        side_effects("Current version", before, after_new, set(new.positions)),
        dry_run_check(before, after_dry),
        undo_check(before, after_undo),
    )
    (out / "report.md").write_text(report, encoding="utf-8")
    print(report)
    print(f"\nReport: {out / 'report.md'}\nPer file: {out / 'files.csv'}")
    return 0


def parse_args() -> tuple[argparse.Namespace, list[str]]:
    argv = sys.argv[1:]
    tool_args: list[str] = []
    if "--" in argv:
        split = argv.index("--")
        argv, tool_args = argv[:split], argv[split + 1:]
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--photos", type=Path, required=True, help="Photos and videos that already have GPS.")
    parser.add_argument("--json", type=Path, required=True, help="Google Timeline export covering those photos.")
    parser.add_argument("--out", type=Path, required=True, help="New, empty working directory (needs 2x the space).")
    parser.add_argument("-t", "--tolerance", type=float, default=1.0, help="Tolerance in hours for both tools.")
    parser.add_argument("--old-ref", default=OLD_REF, help=f"Git revision of the original script (default {OLD_REF}).")
    args = parser.parse_args(argv)

    args.photos, args.json, args.out = args.photos.resolve(), args.json.resolve(), args.out.resolve()
    if not args.photos.is_dir():
        parser.error(f"not a directory: {args.photos}")
    if not args.json.is_file():
        parser.error(f"not a file: {args.json}")
    if args.out.is_relative_to(args.photos) or args.photos.is_relative_to(args.out):
        parser.error("--out must not overlap --photos")
    if args.out.exists() and any(args.out.iterdir()):
        parser.error(f"--out must be new or empty: {args.out}")
    if shutil.which("exiftool") is None:
        parser.error("exiftool is not installed")
    args.out.mkdir(parents=True, exist_ok=True)
    return args, tool_args


def walk(directory: Path) -> list[Path]:
    return [path.relative_to(directory) for path in directory.rglob("*") if path.is_file()]


def is_hidden(relative: Path) -> bool:
    return any(part.startswith(".") for part in relative.parts)


def exiftool_json(directory: Path, *arguments: str) -> list[dict[str, Any]]:
    result = subprocess.run(
        ["exiftool", "-j", "-n", "-r", "-q", "-q", "-charset", "filename=utf8", *arguments, str(directory)],
        capture_output=True, text=True,
    )
    return json.loads(result.stdout) if result.stdout.strip() else []


def read_positions(directory: Path) -> dict[str, tuple[float, float]]:
    """Positions by media file; a RAW file's position may live in its XMP sidecar."""
    records = exiftool_json(
        directory, "-G1",
        "-Composite:GPSLatitude", "-Composite:GPSLongitude", "-XMP-exif:GPSLatitude", "-XMP-exif:GPSLongitude",
    )
    positions: dict[str, tuple[float, float]] = {}
    for record in records:
        relative = str(Path(record["SourceFile"]).relative_to(directory))
        for group in ("Composite", "XMP-exif"):
            latitude, longitude = record.get(f"{group}:GPSLatitude"), record.get(f"{group}:GPSLongitude")
            if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
                if (latitude, longitude) != (0, 0):
                    positions[relative] = (float(latitude), float(longitude))
                break
    for relative in list(positions):
        if relative.lower().endswith(".xmp"):
            position = positions.pop(relative)
            for owner in sidecar_owners(directory, Path(relative)):
                positions.setdefault(owner, position)
    return positions


def sidecar_owners(directory: Path, sidecar: Path) -> list[str]:
    darktable = sidecar.with_suffix("")
    if (directory / darktable).is_file():
        return [str(darktable)]
    return [
        str(path.relative_to(directory))
        for path in (directory / sidecar).parent.glob(f"{sidecar.stem}.*")
        if path.suffix.lower() in MEDIA_EXTENSIONS
    ]


def read_all_tags(directory: Path) -> dict[str, dict[str, Any]]:
    records = exiftool_json(directory, "-a", "-G1", "-s", "-all", "-api", "ImageHashType=SHA256", "-ImageDataHash")
    return {str(Path(record.pop("SourceFile")).relative_to(directory)): record for record in records}


def strip_gps(directory: Path) -> None:
    subprocess.run(
        ["exiftool", "-r", "-q", "-q", "-P", "-overwrite_original", "-charset", "filename=utf8",
         "-gps:all=", "-GPS*=", "-LocationInformation=", str(directory)],
        capture_output=True,
    )


def snapshot(directory: Path) -> Snapshot:
    files = {}
    for relative in walk(directory):
        path = directory / relative
        stat = path.stat()
        files[str(relative)] = (sha256(path), stat.st_size, stat.st_mtime_ns)
    return Snapshot(files, read_all_tags(directory))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_old(directory: Path, args: argparse.Namespace) -> ToolRun:
    script = args.out / "old-tool" / "geotag.py"
    script.parent.mkdir()
    source = subprocess.run(
        ["git", "-C", str(REPO), "show", f"{args.old_ref}:geotag.py"], capture_output=True, text=True, check=True
    ).stdout
    script.write_text(source, encoding="utf-8")
    command = [
        sys.executable, str(script), "-j", str(args.json), "-d", str(directory), "-r",
        "-t", str(max(1, math.ceil(args.tolerance))), "-w", str(os.cpu_count() or 1),
    ]
    return run("old", command, script.parent)


def run_new(directory: Path, args: argparse.Namespace, tool_args: list[str], extra: list[str]) -> ToolRun:
    if "--undo" in extra:
        command = [sys.executable, str(REPO / "geotag.py"), *extra]
    else:
        command = [
            sys.executable, str(REPO / "geotag.py"), "-j", str(args.json), "-d", str(directory), "-r",
            "-t", str(args.tolerance), "-q", *tool_args, *extra,
        ]
    return run("new", command, args.out)


def run(name: str, command: list[str], cwd: Path) -> ToolRun:
    start = time.monotonic()
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    output = (result.stdout + result.stderr).strip().splitlines()
    return ToolRun(name, result.returncode, time.monotonic() - start, "\n".join(output[-15:]))


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def score(
    media: list[Path], truth: dict[str, tuple[float, float]], leftover: dict[str, tuple[float, float]],
    old: ToolRun, new: ToolRun, new_log: dict[Path, dict[str, str]], work_new: Path,
) -> list[dict[str, Any]]:
    rows = []
    for relative in map(str, media):
        reference = truth.get(relative) if relative not in leftover else None
        log = new_log.get((work_new / relative).resolve(), {})
        row: dict[str, Any] = {
            "file": relative,
            "extension": Path(relative).suffix.lower(),
            "truth_latitude": reference[0] if reference else None,
            "truth_longitude": reference[1] if reference else None,
            "new_outcome": log.get("outcome"),
            "new_source": log.get("source") or None,
            "new_time_difference_seconds": log.get("time_difference_seconds") or None,
            "new_message": log.get("message") or None,
        }
        for run_ in (old, new):
            position = run_.positions.get(relative)
            row[f"{run_.name}_latitude"] = position[0] if position else None
            row[f"{run_.name}_longitude"] = position[1] if position else None
            row[f"{run_.name}_error_m"] = round(haversine(reference, position), 1) if reference and position else None
        old_position, new_position = old.positions.get(relative), new.positions.get(relative)
        row["old_new_distance_m"] = (
            round(haversine(old_position, new_position), 1) if old_position and new_position else None
        )
        rows.append(row)
    return rows


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def tag_category(key: str) -> str | None:
    group, _, name = key.rpartition(":")
    if group in VOLATILE_GROUPS:
        return None
    if "GPS" in name or name == "LocationInformation":
        return "gps"
    if key in WRITER_STAMPS:
        return "writer stamp"
    if name.endswith(LAYOUT_SUFFIXES):
        return "layout"
    return "unexpected"


def tag_changes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[str]]:
    changes: dict[str, list[str]] = defaultdict(list)
    for key in before.keys() | after.keys():
        if before.get(key) != after.get(key) and (category := tag_category(key)) is not None:
            changes[category].append(key)
    return changes


def side_effects(label: str, before: Snapshot, after: Snapshot, tagged: set[str]) -> str:
    added = sorted(after.files.keys() - before.files.keys())
    removed = sorted(before.files.keys() - after.files.keys())
    backups = [name for name in added if name.endswith("_original")]
    sidecars = [name for name in added if name.lower().endswith(".xmp")]
    other_added = [name for name in added if name not in backups and name not in sidecars]

    pixel_changed, untagged_changed, hidden_changed, mtime_changed = [], [], [], []
    unexpected: Counter[str] = Counter()
    examples: dict[str, str] = {}
    layout: Counter[str] = Counter()
    stamps = 0
    for name in sorted(before.files.keys() & after.files.keys()):
        old_hash, _, old_mtime = before.files[name]
        new_hash, _, new_mtime = after.files[name]
        if old_hash == new_hash:
            continue
        if is_hidden(Path(name)):
            hidden_changed.append(name)
        # ExifTool -P restores whole seconds only.
        if old_mtime // 10**9 != new_mtime // 10**9:
            mtime_changed.append(name)
        owner_tagged = name in tagged or any(owner in tagged for owner in sidecar_owners_from_set(name, tagged))
        if not owner_tagged:
            untagged_changed.append(name)
        tags_before, tags_after = before.tags.get(name, {}), after.tags.get(name, {})
        if "File:ImageDataHash" in tags_before and tags_before.get("File:ImageDataHash") != tags_after.get(
            "File:ImageDataHash"
        ):
            pixel_changed.append(name)
        changes = tag_changes(tags_before, tags_after)
        for key in changes.get("unexpected", []):
            unexpected[key] += 1
            examples.setdefault(key, f"{name}: {tags_before.get(key)!r} -> {tags_after.get(key)!r}")
        layout.update(changes.get("layout", []))
        stamps += bool(changes.get("writer stamp"))

    lines = [f"### {label}", ""]
    lines += check("Pixel/video data unchanged (ImageDataHash)", pixel_changed)
    lines += check("Files without a written position left byte-identical", untagged_changed)
    lines += check("Hidden files untouched", hidden_changed)
    lines += check("No files removed", removed)
    lines += check("No unexpected new files", other_added)
    lines += check("No leftover _original backups", backups)
    lines += check("File modification times kept", mtime_changed, severity="WARN")
    if sidecars:
        lines.append(f"- INFO: {len(sidecars)} XMP sidecars created (expected for RAW files)")
    if not unexpected:
        lines.append("- PASS: No non-GPS metadata changed")
    else:
        lines.append(f"- FAIL: Non-GPS metadata changed ({sum(unexpected.values())} tag changes)")
        lines.append("")
        lines.append("  | Tag | Files | Example |")
        lines.append("  |---|---|---|")
        for key, count in unexpected.most_common(30):
            lines.append(f"  | `{key}` | {count} | {examples[key][:120]} |")
    if layout:
        lines.append(f"- INFO: Preview/thumbnail offsets moved in {max(layout.values())} files (harmless)")
    if stamps:
        lines.append(f"- INFO: XMPToolkit writer stamp updated in {stamps} files (harmless)")
    return "\n".join(lines)


def sidecar_owners_from_set(name: str, tagged: set[str]) -> list[str]:
    if not name.lower().endswith(".xmp"):
        return []
    stem = Path(name).with_suffix("")
    return [owner for owner in tagged if Path(owner) == stem or Path(owner).with_suffix("") == stem]


def check(label: str, offenders: list[str], severity: str = "FAIL") -> list[str]:
    if not offenders:
        return [f"- PASS: {label}"]
    shown = ", ".join(f"`{name}`" for name in offenders[:5])
    more = f" and {len(offenders) - 5} more" if len(offenders) > 5 else ""
    return [f"- {severity}: {label}: {len(offenders)} files, e.g. {shown}{more}"]


def dry_run_check(before: Snapshot, after: Snapshot) -> str:
    changed = sorted(name for name in before.files if after.files.get(name, ("",))[0] != before.files[name][0])
    added = sorted(after.files.keys() - before.files.keys())
    return "\n".join(["### --dry-run", "", *check("No file changed", changed), *check("No file created", added)])


def undo_check(before: Snapshot, after: Snapshot) -> str:
    differing: Counter[str] = Counter()
    example: dict[str, str] = {}
    for name in before.files.keys() & after.files.keys():
        tags_before, tags_after = before.tags.get(name, {}), after.tags.get(name, {})
        for category, keys in tag_changes(tags_before, tags_after).items():
            if category in ("gps", "unexpected"):
                for key in keys:
                    differing[key] += 1
                    example.setdefault(key, f"{name}: {tags_before.get(key)!r} -> {tags_after.get(key)!r}")
    lines = ["### --undo", ""]
    if not differing:
        lines.append("- PASS: Metadata after undo equals metadata before the run")
    else:
        lines.append("- FAIL: Metadata after undo differs from before the run")
        lines.append("")
        lines.append("  | Tag | Files | Example |")
        lines.append("  |---|---|---|")
        for key, count in differing.most_common(30):
            lines.append(f"  | `{key}` | {count} | {example[key][:120]} |")
    leftover = sorted(after.files.keys() - before.files.keys())
    lines += check("No files left behind", [name for name in leftover if not name.lower().endswith(".xmp")])
    return "\n".join(lines)


def error_stats(errors: list[float]) -> str:
    if not errors:
        return "| – | – | – | – |"
    ordered = sorted(errors)
    p90 = ordered[min(len(ordered) - 1, math.ceil(0.9 * len(ordered)) - 1)]
    wrong = sum(error > WRONG_PLACE_METRES for error in errors)
    return f"| {fmt(statistics.median(errors))} | {fmt(p90)} | {fmt(max(errors))} | {wrong} |"


def fmt(metres: float) -> str:
    return f"{metres / 1000:.1f} km" if metres >= 10_000 else f"{metres:,.0f} m"


def render_report(
    args: argparse.Namespace, media: list[Path], rows: list[dict[str, Any]], old: ToolRun, new: ToolRun,
    dry: ToolRun, undo: ToolRun, source_tags: dict[str, dict[str, Any]], *sections: str,
) -> str:
    scored = [row for row in rows if row["truth_latitude"] is not None]
    lines = [
        "# Geotag comparison", "",
        f"- Photos: `{args.photos}` ({len(media)} media files, {len(scored)} with ground-truth GPS)",
        f"- Timeline: `{args.json}`",
        f"- Tolerance: {args.tolerance} h (original script: {max(1, math.ceil(args.tolerance))} h, which it ignores)",
        f"- Original: `{args.old_ref}:geotag.py`, current: working tree",
        "",
        "## Runs", "",
        "| Run | Exit code | Time |", "|---|---|---|",
    ]
    for label, run_ in (("Original", old), ("Current", new), ("Current --dry-run", dry), ("Current --undo", undo)):
        lines.append(f"| {label} | {run_.exit_code} | {run_.seconds:.1f} s |")
    for label, run_ in (("Original", old), ("Current", new), ("Current --undo", undo)):
        if run_.exit_code != 0:
            lines += ["", f"{label} output (last lines):", "```", run_.output_tail, "```"]

    lines += [
        "", "## Accuracy against the photos' own GPS", "",
        f"Phone GPS is itself off by 5–50 m, so differences under {SAME_METRES} m are noise.", "",
        "| Tool | Files | Tagged | Coverage | Median error | P90 error | Max error | Wrong place (>1 km) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name in ("old", "new"):
        errors = [row[f"{name}_error_m"] for row in scored if row[f"{name}_error_m"] is not None]
        coverage = f"{len(errors) / len(scored):.0%}" if scored else "–"
        lines.append(
            f"| {'Original' if name == 'old' else 'Current'} | {len(scored)} | {len(errors)} | {coverage} "
            + error_stats(errors)
        )

    both = [row for row in scored if row["old_error_m"] is not None and row["new_error_m"] is not None]
    better = sum(row["new_error_m"] < row["old_error_m"] - SAME_METRES for row in both)
    worse = sum(row["new_error_m"] > row["old_error_m"] + SAME_METRES for row in both)
    only_old = [row for row in scored if row["old_error_m"] is not None and row["new_error_m"] is None]
    only_new = [row for row in scored if row["old_error_m"] is None and row["new_error_m"] is not None]
    lines += [
        "", "### Head to head", "",
        f"- Tagged by both: {len(both)}. Current closer: {better}, original closer: {worse}, "
        f"same within {SAME_METRES} m: {len(both) - better - worse}",
        f"- Tagged only by the current version: {len(only_new)}"
        + (f" (of these {sum(r['new_error_m'] > WRONG_PLACE_METRES for r in only_new)} wrong by >1 km)"
           if only_new else ""),
        f"- Tagged only by the original: {len(only_old)}"
        + (f" (of these {sum(r['old_error_m'] <= WRONG_PLACE_METRES for r in only_old)} within 1 km, "
           "i.e. coverage the current version lost)" if only_old else ""),
    ]

    lines += ["", "### Current version by file type", "", "| Type | Files | Tagged | Median | P90 | Max | >1 km |",
              "|---|---|---|---|---|---|---|"]
    by_extension: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in scored:
        by_extension[row["extension"]].append(row)
    for extension, group in sorted(by_extension.items()):
        errors = [row["new_error_m"] for row in group if row["new_error_m"] is not None]
        lines.append(f"| {extension} | {len(group)} | {len(errors)} " + error_stats(errors))

    lines += ["", "### Current version by location source", "", "| Source | Tagged | Median | P90 | Max | >1 km |",
              "|---|---|---|---|---|---|"]
    by_source: dict[str, list[float]] = defaultdict(list)
    for row in scored:
        if row["new_error_m"] is not None:
            by_source[row["new_source"] or "?"].append(row["new_error_m"])
    for source, errors in sorted(by_source.items(), key=lambda item: -len(item[1])):
        lines.append(f"| {source} | {len(errors)} " + error_stats(errors))

    skipped = Counter(
        (row["new_message"] or row["new_outcome"] or "not processed")
        for row in scored if row["new_error_m"] is None
    )
    if skipped:
        lines += ["", "### Why the current version left files untagged", "", "| Reason | Files |", "|---|---|"]
        lines += [f"| {reason[:100]} | {count} |" for reason, count in skipped.most_common(15)]

    regressions = sorted(
        (row for row in both if row["new_error_m"] > row["old_error_m"] + REGRESSION_METRES),
        key=lambda row: row["old_error_m"] - row["new_error_m"],
    )
    regressions += [row for row in only_old if row["old_error_m"] <= WRONG_PLACE_METRES]
    worst = sorted((row for row in scored if (row["new_error_m"] or 0) > WRONG_PLACE_METRES),
                   key=lambda row: -row["new_error_m"])
    for title, selection in (
        (f"Regressions (current worse by >{REGRESSION_METRES} m, or untagged where the original was right)",
         regressions),
        ("Current version wrong by more than 1 km", worst),
    ):
        lines += ["", f"### {title}", ""]
        if not selection:
            lines.append("None.")
            continue
        lines += ["| File | Taken | Original error | Current error | Source | Time difference |",
                  "|---|---|---|---|---|---|"]
        for row in selection[:25]:
            taken = source_tags.get(row["file"], {}).get("ExifIFD:DateTimeOriginal", "")
            seconds = row["new_time_difference_seconds"]
            lines.append(
                f"| `{row['file']}` | {taken} | {opt(row['old_error_m'])} | {opt(row['new_error_m'])} "
                f"| {row['new_source'] or '–'} | {f'{int(seconds) // 60} min' if seconds else '–'} |"
            )
        if len(selection) > 25:
            lines.append(f"\n…and {len(selection) - 25} more in files.csv")

    lines += ["", "## Side effects", "", *(section + "\n" for section in sections)]
    return "\n".join(lines)


def opt(metres: float | None) -> str:
    return fmt(metres) if metres is not None else "untagged"


if __name__ == "__main__":
    raise SystemExit(main())
