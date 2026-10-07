# Geotag Images with Google Timeline Data

This Python script uses Google Timeline location data to geotag images by matching their timestamps with the closest location from your Google location history. It adjusts the EXIF GPS coordinates of the images accordingly.

## Requirements

- **Python:** Python 3.9 or newer.
- **ExifTool:** This script requires `exiftool` to be installed. You can download it from [ExifTool's official website](https://exiftool.org/) or install it using a package manager.

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
python geotag.py --json /path/to/location_data.json --dir /path/to/images/ [--tolerance hours] [--overwrite] [--recursive] [--workers num]
```

After `pip install .` you can use `geotag` instead of `python geotag.py` from any directory.

**Parameters:**

- `--json` or `-j`: Path to Google Timeline JSON file.
- `--dir` or `-d`: Directory containing images.
- `--tolerance` or `-t`: Maximum time difference in hours (decimals allowed, up to 8760) between an image and its matched location. Images with no location inside this window, and not taken during a visit or activity, are skipped. Default is 1 hour.
- `--overwrite` or `-o`: Overwrite existing GPS data.
- `--recursive` or `-r`: Process images in subdirectories recursively. Hidden files and folders, such as macOS `._*` files and `.Trashes`, are ignored.
- `--workers` or `-w`: Number of parallel workers/threads to speed up processing (default is 1).

**Example:**

To geotag images in the directory /path/to/images/ using location data from location_data.json with a 2-hour tolerance, you would run:
```bash
python geotag.py --json /path/to/location_data.json --dir /path/to/images/ --tolerance 2
```

**Run summary:**

At the end of a run the script prints how many images were tagged, skipped and failed, followed by a **Warnings** section listing anything unexpected, grouped by cause with the affected files:

- no JPEG images found in the directory
- malformed timeline entries that were ignored
- images without `DateTimeOriginal` (skipped)
- images with no timeline location within `--tolerance` (skipped)
- images without a timezone offset (capture time assumed to be UTC)
- ExifTool errors (failed)
- unexpected errors (failed); one failing image does not stop the others

Images skipped because they already have GPS data are expected and not reported as warnings. The exit code is 1 if any image failed.

**Supported File Formats:**

This script currently supports the following image file formats:

- JPEG (.jpg, .jpeg, any capitalisation)

**Image timestamps:**

The capture time is read from the EXIF `DateTimeOriginal` tag and converted to UTC using the first offset found in `OffsetTimeOriginal`, `OffsetTimeDigitized` or `OffsetTime`. If an image has none of these, its time is assumed to already be UTC. Images without `DateTimeOriginal` are skipped.

## Supported Google Timeline JSON Format

The file must be a JSON array of entries. Each entry contributes timestamped points:

- activity: its start location at `startTime` and its end location at `endTime`
- visit: the place location at both `startTime` and `endTime`
- timeline path: each point at `startTime` plus `durationMinutesOffsetFromStartTime`

Every image gets the point closest to its capture time if that point is within `--tolerance`. Otherwise, if the image was taken between the `startTime` and `endTime` of a visit or activity, it gets that entry's nearer endpoint, however long the visit or activity lasted.

Other entry types are ignored. Files in a different layout (for example an object with `semanticSegments`) are not supported and stop with an error.


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

## Important Notes

- **Backup:** Images are modified in place and ExifTool's `_original` backup files are not kept, so back up your images before running the script.
- **EXIF Quality:** Modifying the EXIF data does not affect the image quality, as it only updates the metadata.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The code lives in the `exif_geotag` package:

- `timeline.py`: parses Google Timeline JSON into `Location`s
- `locator.py`: finds the location closest in time to a photo, or the visit or activity it was taken during
- `exiftool.py`: reads image metadata in batches and writes GPS data through ExifTool
- `geotagger.py`: matches images to locations and writes their GPS data
- `report.py`: end-of-run summary and warnings
- `cli.py`: command-line interface
