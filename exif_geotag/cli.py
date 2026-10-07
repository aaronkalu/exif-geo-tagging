from __future__ import annotations

import argparse
import math
import os
import re
import sys
from collections import Counter
from collections.abc import Sequence
from contextlib import ExitStack, closing
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tqdm import tqdm

from exif_geotag import exiftool
from exif_geotag.geotagger import Geotagger
from exif_geotag.loader import load_timeline
from exif_geotag.locator import LocationIndex
from exif_geotag.matchlog import MatchLog, read_log
from exif_geotag.media import find_media
from exif_geotag.report import RunReport
from exif_geotag.timeline import Timeline, TimelineError
from exif_geotag.undo import UndoOutcome, tagged_rows, undo

MAX_TOLERANCE_HOURS = 24 * 365
INTERRUPTED_EXIT_CODE = 130


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Geotag photos and videos using Google Timeline, GPX or KML location data."
    )
    parser.add_argument(
        "-j", "--json", action="extend", nargs="+", type=Path, default=[], metavar="FILE",
        help="Location data: Google Timeline JSON (any export format), Records.json, GPX or KML. Repeatable.",
    )
    parser.add_argument("-d", "--dir", type=Path, help="Directory containing images and videos.")
    parser.add_argument(
        "-t", "--tolerance", type=float, default=1.0,
        help="Maximum time difference in hours between an image and its matched location (default: 1).",
    )
    parser.add_argument("-o", "--overwrite", action="store_true", help="Overwrite existing GPS data.")
    parser.add_argument("-r", "--recursive", action="store_true", help="Process images in subdirectories recursively.")
    parser.add_argument(
        "-w", "--workers", type=int, default=os.cpu_count() or 1,
        help="Number of parallel ExifTool processes (default: number of CPUs).",
    )
    parser.add_argument("-n", "--dry-run", action="store_true", help="Show what would be done without changing files.")
    parser.add_argument(
        "--backup", action="store_true", help="Keep ExifTool's FILE_original backup of each changed file."
    )
    parser.add_argument(
        "--log", type=Path, metavar="FILE", help="Write a CSV (or .json) log of every match, for review or --undo."
    )
    parser.add_argument(
        "--undo", type=Path, metavar="LOG",
        help="Revert the images tagged in a log written by --log, restoring any GPS data they had before.",
    )
    parser.add_argument(
        "--timezone", type=_timezone, metavar="ZONE",
        help="Time zone for files without a timezone offset, e.g. Europe/Berlin or +02:00. "
        "Default: inferred from the timeline, else UTC.",
    )
    parser.add_argument(
        "--time-shift", type=_time_shift, default=timedelta(0), metavar="[+-]H:MM[:SS]",
        help="Correction added to the camera clock, e.g. +0:03:12 if the camera was 3 min 12 s slow. "
        "Write negative shifts as --time-shift=-0:03:12.",
    )
    parser.add_argument(
        "--min-probability", type=float, default=0.0, metavar="P",
        help="Ignore visits and activities Google rated less likely than P (0 to 1, default: 0).",
    )
    parser.add_argument(
        "--no-interpolation", dest="interpolate", action="store_false",
        help="Use the nearest timeline point instead of interpolating between points.",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="Only print the progress bar and the summary.")
    args = parser.parse_args(argv)

    if args.undo is not None:
        if not args.undo.is_file():
            parser.error(f"Log file not found: {args.undo}")
        return args
    if not args.json or args.dir is None:
        parser.error("--json and --dir are required (unless using --undo).")
    if not (math.isfinite(args.tolerance) and 0 <= args.tolerance <= MAX_TOLERANCE_HOURS):
        parser.error(f"--tolerance must be between 0 and {MAX_TOLERANCE_HOURS} hours.")
    if not 0 <= args.min_probability <= 1:
        parser.error("--min-probability must be between 0 and 1.")
    if args.workers < 1:
        parser.error("--workers must be at least 1.")
    for path in args.json:
        if not path.is_file():
            parser.error(f"Location file not found: {path}")
    if not args.dir.is_dir():
        parser.error(f"Image directory not found: {args.dir}")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        print(f"ExifTool version: {exiftool.version()}")
    except exiftool.ExifToolError as error:
        print(error, file=sys.stderr)
        return 1

    if args.undo is not None:
        return _undo(args)

    print("Loading location data...")
    try:
        timeline = sum((load_timeline(path) for path in args.json), Timeline([]))
    except TimelineError as error:
        print(error, file=sys.stderr)
        return 1
    timeline = timeline.without_unlikely_spans(args.min_probability)
    locations = LocationIndex(timeline.locations, timeline.spans, interpolate=args.interpolate)
    if not locations:
        print("No locations found in the location data.", file=sys.stderr)
        return 1

    images = find_media(args.dir, args.recursive)
    geotagger = Geotagger(
        locations, timedelta(hours=args.tolerance), args.overwrite, dry_run=args.dry_run, keep_backup=args.backup,
        time_shift=args.time_shift, assumed_timezone=args.timezone,
    )

    report = RunReport(timeline.malformed_entries, dry_run=args.dry_run)
    try:
        with ExitStack() as stack:
            log = stack.enter_context(MatchLog(args.log)) if args.log else None
            # closing() shuts the worker pool down on Ctrl+C, so queued images are not processed after the interrupt.
            results = stack.enter_context(closing(geotagger.process_all(images, args.workers)))
            for result in tqdm(results, total=len(images)):
                report.add(result)
                if log is not None:
                    log.add(result)
                if not args.quiet:
                    tqdm.write(str(result))
    except KeyboardInterrupt:
        report.interrupted = True

    print(report.render())
    if report.interrupted:
        return INTERRUPTED_EXIT_CODE
    return 1 if report.has_failures else 0


def _undo(args: argparse.Namespace) -> int:
    try:
        rows = tagged_rows(read_log(args.undo))
    except (OSError, ValueError, UnicodeDecodeError) as error:
        print(f"Could not read log file {args.undo}: {error}", file=sys.stderr)
        return 1

    outcomes: Counter[UndoOutcome] = Counter()
    try:
        for result in tqdm(undo(rows, dry_run=args.dry_run, keep_backup=args.backup), total=len(rows)):
            outcomes[result.outcome] += 1
            if not args.quiet or result.outcome is UndoOutcome.FAILED:
                tqdm.write(str(result))
    except KeyboardInterrupt:
        print("Interrupted.")
        return INTERRUPTED_EXIT_CODE

    heading = "Dry run, no files were changed. Would have:" if args.dry_run else "Done."
    print(f"{heading} {', '.join(f'{outcomes[outcome]} {outcome.value}' for outcome in UndoOutcome)}.")
    return 1 if outcomes[UndoOutcome.FAILED] else 0


def _timezone(value: str) -> tzinfo:
    if value.upper() in ("UTC", "Z"):
        return UTC
    if re.fullmatch(r"[+-]\d{2}:?\d{2}", value):
        zone = datetime.strptime(value, "%z").tzinfo
        assert zone is not None
        return zone
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise argparse.ArgumentTypeError(f"unknown time zone {value!r}; use e.g. Europe/Berlin or +02:00") from error


def _time_shift(value: str) -> timedelta:
    match = re.fullmatch(r"([+-]?)(\d+):(\d{2})(?::(\d{2}))?", value)
    if not match:
        raise argparse.ArgumentTypeError(f"invalid time shift {value!r}; use [+-]H:MM or [+-]H:MM:SS")
    sign, hours, minutes, seconds = match.groups()
    shift = timedelta(hours=int(hours), minutes=int(minutes), seconds=int(seconds or 0))
    return -shift if sign == "-" else shift
