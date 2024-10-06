# Geotag Images with Google Timeline Data

This Python script uses Google Timeline location data to geotag images by matching their timestamps with the closest location from your Google location history. It adjusts the EXIF GPS coordinates of the images accordingly.

## Requirements

- **Python:** Ensure Python 3.x is installed on your system.
- **ExifTool:** This script requires exiftool to be installed. You can download it from ExifTool's official website or install it using a package manager.
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

## Usage

To use the script, navigate to the directory containing geotag.py and run the following command in your terminal:

```bash
python geotag.py --json /path/to/location_data.json --dir /path/to/images/ [--time hours] [--overwrite]
```

**Parameters:**

- `--json` or `-j`: The path to the Google Timeline JSON file containing your location history.
- `--dir` or `-d`: The path to the directory containing the images you want to geotag.
- `--tolerance` or `-t`: The number of hours of tolerance to match the image timestamp with a location (default is 1 hour).
- `--overwrite` or `-o`: Specify this flag to overwrite existing GPS data in the images.

**Example:**

To geotag images in the directory /path/to/images/ using location data from location_data.json with a 2-hour tolerance, you would run:
```bash
python geotag.py --json /path/to/location_data.json --dir /path/to/images/ --time 2
```

**Supported File Formats:**

This script currently supports the following image file formats:

- JPEG (.jpg, .jpeg)

## Supported Google Timeline JSON Format

The script supports the following formats of Google Timeline location data:

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
    "distanceMeters": "20912.751953",
    "start": "geo:XX.100000,XX.000000"
  }
}
```
2. Visit Data:
```json
{
  "endTime": "2024-01-01T11:00:00.000+02:00",
  "startTime": "2024-01-01T18:00:00.000+02:00",
  "visit": {
    "hierarchyLevel": "0",
    "topCandidate": {
      "probability": "0.30",
      "semanticType": "Unknown",
      "placeID": "ChIJXXXXXXX",
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

- **Backup:** It is recommended to keep a backup of your images before running the script, especially if you are unsure about the changes.
- **EXIF Quality:** Modifying the EXIF data does not affect the image quality, as it only updates the metadata.