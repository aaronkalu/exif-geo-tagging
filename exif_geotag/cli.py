from __future__ import annotations

import argparse
import math
import sys
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from typing import Sequence

from tqdm import tqdm

from exif_geotag import exiftool
from exif_geotag.geotagger import Geotagger, find_images
from exif_geotag.locator import LocationIndex
from exif_geotag.report import RunReport
from exif_geotag.timeline import TimelineError, load_timeline

MAX_TOLERANCE_HOURS = 24 * 365


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Geotag JPEG images using Google Timeline location data.")
    parser.add_argument("-j", "--json", required=True, type=Path, help="Path to Google Timeline JSON file.")
    parser.add_argument("-d", "--dir", required=True, type=Path, help="Directory containing images.")
    parser.add_argument(
        "-t", "--tolerance", type=float, default=1.0,
        help="Maximum time difference in hours between an image and its matched location (default: 1).",
    )
    parser.add_argument("-o", "--overwrite", action="store_true", help="Overwrite existing GPS data.")
    parser.add_argument("-r", "--recursive", action="store_true", help="Process images in subdirectories recursively.")
    parser.add_argument("-w", "--workers", type=int, default=1, help="Number of parallel threads to use (default: 1).")
    args = parser.parse_args(argv)

    if not (math.isfinite(args.tolerance) and 0 <= args.tolerance <= MAX_TOLERANCE_HOURS):
        parser.error(f"--tolerance must be between 0 and {MAX_TOLERANCE_HOURS} hours.")
    if not args.json.is_file():
        parser.error(f"JSON file not found: {args.json}")
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

    print("Loading data (takes a while)...")
    try:
        timeline = load_timeline(args.json)
    except TimelineError as error:
        print(error, file=sys.stderr)
        return 1
    locations = LocationIndex(timeline.locations, timeline.spans)
    if not locations:
        print("No locations found in the timeline file.", file=sys.stderr)
        return 1

    images = find_images(args.dir, args.recursive)
    geotagger = Geotagger(locations, timedelta(hours=args.tolerance), args.overwrite)

    report = RunReport(timeline.malformed_entries)
    # closing() shuts the worker pool down on Ctrl+C, so queued images are not processed after the interrupt.
    with closing(geotagger.process_all(images, args.workers)) as results:
        for result in tqdm(results, total=len(images)):
            report.add(result)
            tqdm.write(str(result))

    print(report.render())
    return 1 if report.has_failures else 0
