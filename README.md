# Geotagging Script for Images

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
python geotag.py --json /path/to/location_data.json --dir /path/to/images/ [--time hours]
```

**Parameters:**

- `--json`: The path to the Google Timeline JSON file containing your location history.
- `--dir`: The path to the directory containing the images you want to geotag.
- `--time`: The number of hours of tolerance to match the image timestamp with a location (default is 1 hour).

**Example:**

To geotag images in the directory /path/to/images/ using location data from location_data.json with a 2-hour tolerance, you would run:
```bash
python geotag.py --json /path/to/location_data.json --dir /path/to/images/ --time 2
```

**Supported File Formats:**

This script currently supports the following image file formats:

- JPEG (.jpg, .jpeg)
## Important Notes

- **Backup:** It is recommended to keep a backup of your images before running the script, especially if you are unsure about the changes.
- **GPS Data:** The script will skip images that already contain GPS data to avoid overwriting existing location information.
- **EXIF Quality:** Modifying the EXIF data does not affect the image quality, as it only updates the metadata.