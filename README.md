# Geotag Photos and Videos with Google Timeline Data

This tool geotags photos and videos by matching their capture time against your location history: Google Timeline exports, Google Takeout `Records.json`, GPX tracks or KML files. It writes the matching GPS position into each file's metadata with ExifTool.

## Requirements

- **Python:** Python 3.11 or newer.
- **ExifTool:** This tool requires `exiftool` to be installed. You can download it from [ExifTool's official website](https://exiftool.org/) or install it using a package manager.

## Installation Instructions

1. **Install Python**:
   - Download and install Python from [python.org](https://www.python.org/downloads/).
   - Make sure to check the box to add Python to your system PATH during installation.

2. **Install ExifTool**:
   - **Windows**: Download the Windows executable from [here](https://exiftool.org/) and follow the installation instructions.
   - **macOS**: You can install it via Homebrew:
     ```bash
     brew install exiftool
     ```
   - **Linux**: Install via your distribution's package manager (e.g., `apt`, `dnf`):
     ```bash
     sudo apt install libimage-exiftool-perl
     ```

3. **Install Python dependencies** (from the repository root):
   ```bash
   pip install .
   ```
   This also installs a `geotag` command, so `geotag --json ... --dir ...` works from anywhere.

## Usage

From the repository root run:

```bash
python geotag.py --json /path/to/location_data.json --dir /path/to/images/ [options]
```

After `pip install .` you can use `geotag` instead of `python geotag.py` from any directory.

**Parameters:**

- `--json` or `-j`: Location data file(s): any Google Timeline JSON export, `Records.json`, `.gpx` or `.kml`. Give several files (`-j a.json b.gpx`) to combine them.
- `--dir` or `-d`: Directory containing images and videos.
- `--tolerance` or `-t`: Maximum time difference in hours (decimals allowed, up to 8760) between an image and the location data used for it. Images with no location inside this window, and not taken during a visit or activity, are skipped. Default is 1 hour.
- `--overwrite` or `-o`: Overwrite existing GPS data.
- `--recursive` or `-r`: Process images in subdirectories recursively. Hidden files and folders, such as macOS `._*` files and `.Trashes`, are ignored.
- `--workers` or `-w`: Number of parallel ExifTool processes. Default is the number of CPUs.
- `--dry-run` or `-n`: Show what would be done without changing any file.
- `--backup`: Keep ExifTool's `FILE_original` backup of every changed file.
- `--log FILE`: Write one row per image (capture time, position written, its source, time difference, previous position and GPS tags) to a CSV file, or JSON if `FILE` ends in `.json`. Use it to review a run or to undo it.
- `--undo LOG`: Revert the images tagged in a log written by `--log`. Every GPS tag the run changed gets its previous value back (position, datum and GPS time), and tags the run added are removed. Works with `--dry-run` and `--backup`.
- `--timezone ZONE`: Time zone for files without a timezone offset, e.g. `Europe/Berlin` or `+02:00`. See [Image timestamps](#image-timestamps).
- `--time-shift [+-]H:MM[:SS]`: Correction added to the camera clock, e.g. `+0:03:12` if the camera was 3 minutes 12 seconds slow. Write negative values with `=`: `--time-shift=-0:03:12`.
- `--min-probability P`: Ignore visits and activities that Google rated less likely than `P` (0 to 1).
- `--no-interpolation`: Use the nearest location point instead of interpolating between points.
- `--quiet` or `-q`: Only print the progress bar and the summary.

**Examples:**

Preview a run with a 2-hour tolerance and keep a log:
```bash
python geotag.py -j location_data.json -d /path/to/images/ -t 2 --dry-run --log preview.csv
```

Tag for real, then undo it:
```bash
python geotag.py -j location_data.json -d /path/to/images/ --log run.csv
python geotag.py --undo run.csv
```

**Run summary:**

At the end of a run the tool prints how many images were tagged, skipped and failed, followed by a **Warnings** section listing anything unexpected, grouped by cause with the affected files:

- no supported images or videos found in the directory
- malformed location entries that were ignored
- images without a capture time (skipped)
- images with no location within `--tolerance` (skipped)
- images without a timezone offset, whose offset was inferred from the timeline
- images without a timezone offset where none could be inferred (capture time assumed to be UTC)
- ExifTool errors (failed)
- unexpected errors (failed); one failing image does not stop the others

Images skipped because they already have GPS data are expected and not reported as warnings. A GPS position of exactly 0, 0 counts as missing, because some cameras write it when they have no fix. The exit code is 1 if any image failed, and 130 if the run was interrupted with Ctrl+C (the summary then covers the images processed so far).

**Supported file formats:**

- **Images** (written directly): JPEG, HEIC/HEIF, PNG, TIFF, WebP, DNG. The tool writes `GPSLatitude`/`GPSLongitude` with their Ref tags, `GPSDateStamp`/`GPSTimeStamp` (the capture time in UTC) and `GPSMapDatum`.
- **RAW** (CR2, CR3, NEF, ARW, RAF, ORF, RW2, PEF and others): the RAW file is never modified. GPS data goes into an XMP sidecar: an existing `IMG_1234.CR2.xmp` (darktable style) is updated, otherwise `IMG_1234.xmp` (Adobe style) is updated or created.
- **Videos** (MP4, MOV, M4V, 3GP): `Keys:GPSCoordinates` (read by Apple Photos) and `UserData:GPSCoordinates`.

Files keep their modification time.

**Image timestamps:**

For photos, the capture time is read from `DateTimeOriginal`. Its timezone comes from an offset in the tag itself (XMP), or else from the first of `OffsetTimeOriginal`, `OffsetTimeDigitized` or `OffsetTime`. Videos use Apple's `CreationDate`, which includes the offset, or else the QuickTime `CreateDate`. That is UTC by specification, and phones write it so, but many cameras (GoPro, DJI, Sony and others) write local time there: such videos are assumed to be UTC, with a warning, unless `--timezone` is given.

When a file has no timezone offset:

1. `--timezone` is used if given (daylight saving time is handled for named zones).
2. Otherwise the offset is inferred from the timeline: Google Timeline exports record the local offset of every entry, so the tool picks the offset that matches the timeline data around the photo's local time.
3. Otherwise (e.g. GPX data in UTC) the time is assumed to be UTC.

`--time-shift` is applied to the camera clock before any of this.

## How locations are matched

For each image the tool tries, in order:

1. **A location point within a minute** of the capture time (interpolated with its neighbour, if interpolation is on and both are within `--tolerance`). A GPS track fix beats a visit, which only gives the place's centre.
2. **A visit** covering the capture time, however long it lasted. If visits overlap, the innermost (higher `hierarchyLevel`, then shorter) wins.
3. **Interpolation between the two location points** around the capture time, if both are within `--tolerance`.
4. **An activity** (a journey) covering the capture time: the position is interpolated along the straight line from its start to its end.
5. **The nearest location point** within `--tolerance`.

With `--no-interpolation`, steps 3 and 4 are replaced by the nearest point within `--tolerance` and then the nearer end of a covering activity. Longitudes are interpolated the short way round across the antimeridian.

## Supported location formats

- **Google Timeline, iOS on-device export**: a JSON array of entries. Activities contribute their start and end, visits their place location at both ends, and `timelinePath` entries each point at `startTime` plus `durationMinutesOffsetFromStartTime`.
- **Google Timeline, Android on-device export** (`Timeline.json`): an object with `semanticSegments` (visits, activities and timeline paths) and `rawSignals` (positions).
- **Google Takeout `Records.json`**: raw location fixes with `latitudeE7`/`longitudeE7` and `timestamp` or `timestampMs`.
- **GPX**: track points, route points and waypoints that have a `<time>`.
- **KML**: `gx:Track` elements (as in Google Takeout KML), placemarks with a `TimeStamp`, and placemarks with a `TimeSpan` (a single point is treated as a visit, a line as an activity).

Unknown entry types are ignored. Malformed entries are skipped and counted in the warnings.

Example entries of the iOS export:

1. Activity Data:
```json
{
  "endTime": "2024-01-01T12:00:00.000+02:00",
  "startTime": "2024-01-01T11:00:00.00+02:00",
  "activity": {
    "probability": "0.99",
    "end": "geo:XX.000000,XX.000000",
    "topCandidate": {
      "type": "in passenger vehicle",
      "probability": "0.94"
    },
    "distanceMeters": "200.00",
    "start": "geo:XX.100000,XX.000000"
  }
}
```
2. Visit Data:
```json
{
  "endTime": "2024-01-01T18:00:00.000+02:00",
  "startTime": "2024-01-01T11:00:00.000+02:00",
  "visit": {
    "hierarchyLevel": "0",
    "topCandidate": {
      "probability": "0.30",
      "semanticType": "Unknown",
      "placeID": "XXXXXXX",
      "placeLocation": "geo:XX.000000,XX.000000"
    },
    "probability": "0.90"
  }
}
```
3. Timeline Path Data:
```json
{
  "endTime": "2024-01-01T13:00:00.000Z",
  "startTime": "2024-01-01T11:00:00.000Z",
  "timelinePath": [
    {
      "point": "geo:XX.000000,XX.000000",
      "durationMinutesOffsetFromStartTime": "10"
    }
  ]
}
```

`--min-probability` compares against the visit's or activity's own `probability`, not the `topCandidate` probability (which rates the place identification).

## Performance

Starting ExifTool takes far longer than tagging a photo, so each worker keeps one ExifTool process running (`-stay_open`) for the whole run: metadata is read 50 files per command and each write is a separate command, so an error only affects its own file. On a 500-photo test this is about 16 times faster than starting ExifTool for every write.

## Important Notes

- **Backup:** Files are modified in place. Use `--dry-run` first, `--log` to be able to `--undo`, and `--backup` (or your own backup) to keep the originals.
- **EXIF Quality:** Modifying the metadata does not affect the image quality.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check .
mypy
```

The code lives in the `exif_geotag` package:

- `timeline.py`: the `Location`, `Span` and `Timeline` model and shared parsing helpers
- `google.py`: Google Timeline (iOS and Android exports) and `Records.json` parsers
- `tracks.py`: GPX and KML parsers
- `loader.py`: detects the format of a location file and loads it
- `locator.py`: finds the position for a capture time (visits, interpolation, activities, nearest point) and infers time zones
- `media.py`: supported file types, sidecars and file discovery
- `exiftool.py`: a persistent ExifTool process that reads metadata in batches and writes or removes GPS data
- `geotagger.py`: resolves capture times, matches images to locations and writes their GPS data in parallel
- `matchlog.py`: the CSV/JSON log written by `--log`
- `undo.py`: reverts a run from its log
- `report.py`: end-of-run summary and warnings
- `cli.py`: command-line interface
