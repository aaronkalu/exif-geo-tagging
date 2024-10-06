import os
import argparse
import datetime
from bisect import bisect_left
from PIL import Image
import json
import subprocess
from fractions import Fraction
import pytz


class Location:
    def __init__(self, timestamp=None, latitude=None, longitude=None):
        self.timestampUtc = timestamp  # Assume this is already in UTC
        self.latitude = latitude
        self.longitude = longitude

    def __repr__(self):
        return f"Location({self.timestampUtc}, {self.latitude}, {self.longitude})"

    def __eq__(self, other):
        return self.timestampUtc == other.timestampUtc

    def __lt__(self, other):
        return self.timestampUtc < other.timestampUtc


def parse_geo_point(geo_point):
    """Parses a 'geo:lat,lng' string and returns latitude and longitude as floats."""
    lat, lng = geo_point.split(':')[1].split(',')
    return float(lat), float(lng)


def filter_timeline_entries(data):
    """Filters out entries that don't have a 'timelinePath' key. (e.g. entries that save activity data)"""
    return [entry for entry in data if 'timelinePath' in entry]


def generate_locations_from_timeline(data):
    locations = []
    filtered_data = filter_timeline_entries(data)

    for entry in filtered_data:
        start_time = entry['startTime']
        timeline_path = entry.get('timelinePath', [])

        start_time_dt = datetime.datetime.strptime(start_time, '%Y-%m-%dT%H:%M:%S.%fZ')

        for point_data in timeline_path:
            point_geo = point_data['point']
            duration_offset = int(point_data['durationMinutesOffsetFromStartTime'])

            # Assume Google Timeline times are in UTC
            timestamp_utc = start_time_dt + datetime.timedelta(minutes=duration_offset)

            latitude, longitude = parse_geo_point(point_geo)
            location = Location(timestamp=timestamp_utc, latitude=latitude, longitude=longitude)
            locations.append(location)

    return locations


def find_closest_in_time(locations, a_location):
    pos = bisect_left(locations, a_location)
    if pos == 0:
        return locations[0]
    if pos == len(locations):
        return locations[-1]

    before = locations[pos - 1]
    after = locations[pos]

    # Select the closer timestamp
    return after if after.timestampUtc - a_location.timestampUtc < a_location.timestampUtc - before.timestampUtc else before


def to_deg(value, location):
    """Convert decimal coordinates into degrees, minutes, and seconds tuple."""
    if value < 0:
        loc_value = location[0]
    elif value > 0:
        loc_value = location[1]
    else:
        loc_value = ""
    abs_value = abs(value)
    deg = int(abs_value)
    t1 = (abs_value - deg) * 60
    min = int(t1)
    sec = round((t1 - min) * 60, 5)
    return deg, min, sec, loc_value


def change_to_rational(number):
    """Convert a number to rational."""
    fraction = Fraction(str(number))
    return fraction.numerator, fraction.denominator


def check_exiftool_installed():
    """Check if ExifTool is installed."""
    try:
        result = subprocess.run(['exiftool', '-ver'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        print(f"ExifTool version: {result.stdout.decode().strip()}")
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("ExifTool is not installed. Please install ExifTool to use this script.")
        exit(1)


def convert_to_utc(local_time, offset_str):
    """Converts local time to UTC by applying the offset (in hours and minutes) to the local time."""
    if offset_str:
        # Determine the sign of the offset
        sign = 1 if offset_str[0] == '+' else -1
        # Split the offset string into hours and minutes
        offset_hours, offset_minutes = map(int, offset_str[1:].split(':'))
        # Create a timedelta for the offset
        total_offset = datetime.timedelta(hours=sign * offset_hours, minutes=sign * offset_minutes)
        # Calculate the UTC time by subtracting the offset from local time
        utc_time = local_time - total_offset
    else:
        # If no offset is provided, assume local_time is already in UTC
        utc_time = local_time

    return utc_time



check_exiftool_installed()

parser = argparse.ArgumentParser()
parser.add_argument('-j', '--json', help='The JSON file containing your location history.', required=True)
parser.add_argument('-d', '--dir', help='Images folder.', required=True)
parser.add_argument('-t', '--time', help='Hours of tolerance.', default=1, required=False)
args = vars(parser.parse_args())
locations_file = args['json']
image_dir = args['dir']
hours_threshold = int(args['time'])

print('Loading data (takes a while)...')
with open(locations_file) as f:
    location_data = json.load(f)

my_locations = generate_locations_from_timeline(location_data)

print(f'Found {len(my_locations)} valid locations')
for loc in my_locations:
    print(loc)

included_extensions = ['jpg', 'JPG', 'jpeg', 'JPEG']
file_names = [fn for fn in os.listdir(image_dir) if any(fn.endswith(ext) for ext in included_extensions)]

for image_file in file_names:
    image_file_path = os.path.join(image_dir, image_file)

    # Use exiftool to get the EXIF data
    cmd = ["exiftool", "-DateTimeOriginal", "-SubSecTimeOriginal", "-OffsetTimeOriginal", "-T", image_file_path]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    if result.returncode != 0:
        print(f"Error reading EXIF data for {image_file}: {result.stderr}")
        continue

    exif_data = result.stdout.strip().split()

    if len(exif_data) < 3:
        print(f"Unexpected EXIF data format for {image_file}: {result.stdout}")
        continue

    date_original = exif_data[0]  # "YYYY:MM:DD"
    time_original = exif_data[1]  # "HH:MM:SS"
    subsec_time_original = exif_data[2]  # Subsecond part
    current_offset_str = exif_data[3] if len(exif_data) > 3 else None  # Current offset (e.g., "+10:00")

    datetime_original_str = f"{date_original} {time_original}"
    dt_format = "%Y:%m:%d %H:%M:%S"
    dt_obj = datetime.datetime.strptime(datetime_original_str, dt_format)

    # Convert image time to UTC based on the offset
    image_time_utc = convert_to_utc(dt_obj, current_offset_str)

    # Convert to UNIX timestamp (UTC)
    time_jpeg_unix = image_time_utc.timestamp()

    curr_loc = Location(timestamp=image_time_utc)  # No conversion needed here
    approx_location = find_closest_in_time(my_locations, curr_loc)
    approx_location_unix = int(approx_location.timestampUtc.timestamp())
    hours_away = abs(approx_location_unix - time_jpeg_unix) / 3600

    print(f"Image: {image_file} - Image time UTC: {image_time_utc} - "
          f"Approx. location: {approx_location} - "
          f"Hours away: {hours_away}")

    if hours_away < hours_threshold:
        if 2 in exif_data or 3 in exif_data:  # 2 = GPSLatitude, 3 = GPSLongitude
            print(f"Skipping {image_file}: GPS data already exists.")
            continue

        lat_deg = to_deg(approx_location.latitude, ["S", "N"])
        lng_deg = to_deg(approx_location.longitude, ["W", "E"])

        command = [
            'exiftool',
            "-overwrite_original",
            f'-GPSLatitude={lat_deg[0]} {lat_deg[1]} {lat_deg[2]}',
            f'-GPSLatitudeRef={lat_deg[3]}',
            f'-GPSLongitude={lng_deg[0]} {lng_deg[1]} {lng_deg[2]}',
            f'-GPSLongitudeRef={lng_deg[3]}',
            image_file_path
        ]

        try:
            subprocess.run(command, check=True)
            print(f"Updated GPS data for {image_file}.")
        except subprocess.CalledProcessError as e:
            print(f"Failed to update GPS data for {image_file}: {e}")
    else:
        print(f"Skipping {image_file}: No location data within {hours_threshold} hours.")
