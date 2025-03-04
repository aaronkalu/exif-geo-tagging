import os
import argparse
import datetime
from bisect import bisect_left
import json
import subprocess
from fractions import Fraction
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor


class Location:
    def __init__(self, timestamp=None, latitude=None, longitude=None, maps_type=None):
        self.timestampUtc = timestamp  # Assume this is already in UTC
        self.latitude = latitude
        self.longitude = longitude
        self.maps_type = maps_type

    def __repr__(self):
        return f"Location({self.timestampUtc}, {self.latitude}, {self.longitude}, {self.maps_type})"

    def __eq__(self, other):
        return self.timestampUtc == other.timestampUtc

    def __lt__(self, other):
        return self.timestampUtc < other.timestampUtc


# Utility methods

def parse_geo_point(geo_point):
    """Parses a 'geo:lat,lng' string and returns latitude and longitude as floats."""
    lat, lng = geo_point.split(':')[1].split(',')
    return float(lat), float(lng)


def format_offset(offset_str):
    """Converts offset from +0200 to +02:00."""
    return f"{offset_str[:-2]}:{offset_str[-2:]}"


def convert_to_utc(local_time, offset_str):
    """Converts local time to UTC by applying the offset (in hours and minutes) to the local time."""
    if offset_str:
        sign = 1 if offset_str[0] == '+' else -1
        offset_hours, offset_minutes = map(int, offset_str[1:].split(':'))
        total_offset = datetime.timedelta(hours=sign * offset_hours, minutes=sign * offset_minutes)
        utc_time = local_time - total_offset
    else:
        utc_time = local_time

    return utc_time.replace(tzinfo=None, microsecond=0)


def to_deg(value, location):
    """Convert decimal coordinates into degrees, minutes, and seconds tuple."""
    loc_value = location[0] if value < 0 else location[1]
    abs_value = abs(value)
    deg = int(abs_value)
    min = int((abs_value - deg) * 60)
    sec = round(((abs_value - deg) * 60 - min) * 60, 5)
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


# Core functionality methods

def generate_locations_from_timeline(data):
    """Generate location data from Google Timeline JSON."""
    locations = []

    for entry in data:
        start_time = entry['startTime']
        start_time_dt = datetime.datetime.strptime(start_time, '%Y-%m-%dT%H:%M:%S.%f%z')

        if 'timelinePath' in entry:  # Already in UTC
            locations.extend(generate_timeline_locations(entry, start_time_dt))
        elif 'activity' in entry:  # Needs UTC conversion
            locations.extend(generate_activity_locations(entry, start_time_dt))
        elif 'visit' in entry:  # Needs UTC conversion
            locations.extend(generate_visit_locations(entry, start_time_dt))

    locations.sort(key=lambda l: l.timestampUtc)
    return locations


def generate_timeline_locations(entry, start_time_dt):
    """Generate location points from timelinePath."""
    locations = []
    timeline_path = entry.get('timelinePath', [])

    for point_data in timeline_path:
        point_geo = point_data['point']
        duration_offset = int(point_data['durationMinutesOffsetFromStartTime'])

        timestamp_utc = start_time_dt + datetime.timedelta(minutes=duration_offset)
        timestamp_utc = timestamp_utc.replace(tzinfo=None)
        latitude, longitude = parse_geo_point(point_geo)

        location = Location(timestamp=timestamp_utc, latitude=latitude, longitude=longitude, maps_type='timeline')
        locations.append(location)

    return locations


def generate_activity_locations(entry, start_time_dt):
    """Generate start and end location points from activity data."""
    locations = []
    activity = entry['activity']
    offset_str = format_offset(start_time_dt.strftime('%z'))
    start_time_utc = convert_to_utc(start_time_dt, offset_str)

    start_lat, start_lng = parse_geo_point(activity['start'])
    end_lat, end_lng = parse_geo_point(activity['end'])

    locations.append(
        Location(timestamp=start_time_utc, latitude=start_lat, longitude=start_lng, maps_type='activity_start'))

    end_time_dt = datetime.datetime.strptime(entry['endTime'], '%Y-%m-%dT%H:%M:%S.%f%z')
    end_time_utc = convert_to_utc(end_time_dt, format_offset(end_time_dt.strftime('%z')))

    locations.append(Location(timestamp=end_time_utc, latitude=end_lat, longitude=end_lng, maps_type='activity_end'))

    return locations


def generate_visit_locations(entry, start_time_dt):
    """Generate start and end location points from visit data."""
    locations = []
    top_candidate = entry['visit']['topCandidate']
    location_lat, location_lng = parse_geo_point(top_candidate['placeLocation'])

    offset_str = format_offset(start_time_dt.strftime('%z'))
    start_time_utc = convert_to_utc(start_time_dt, offset_str)
    locations.append(
        Location(timestamp=start_time_utc, latitude=location_lat, longitude=location_lng, maps_type='visit_start'))

    end_time_dt = datetime.datetime.strptime(entry['endTime'], '%Y-%m-%dT%H:%M:%S.%f%z')
    end_time_utc = convert_to_utc(end_time_dt, format_offset(end_time_dt.strftime('%z')))
    locations.append(
        Location(timestamp=end_time_utc, latitude=location_lat, longitude=location_lng, maps_type='visit_end'))

    return locations


def find_closest_in_time(locations, a_location):
    """Find the closest location in time to the given image's timestamp."""
    pos = bisect_left(locations, a_location)
    if pos == 0:
        return locations[0]
    if pos == len(locations):
        return locations[-1]

    before, after = locations[pos - 1], locations[pos]
    return after if after.timestampUtc - a_location.timestampUtc < a_location.timestampUtc - before.timestampUtc else before


def update_image_gps(image_file, approx_location):
    """Update the GPS data in the image using ExifTool."""
    lat_deg = to_deg(approx_location.latitude, ["S", "N"])
    lng_deg = to_deg(approx_location.longitude, ["W", "E"])

    command = [
        'exiftool', '-overwrite_original',
        f'-GPSLatitude={lat_deg[0]} {lat_deg[1]} {lat_deg[2]}',
        f'-GPSLatitudeRef={lat_deg[3]}',
        f'-GPSLongitude={lng_deg[0]} {lng_deg[1]} {lng_deg[2]}',
        f'-GPSLongitudeRef={lng_deg[3]}',
        image_file
    ]

    try:
        subprocess.run(command, check=True)
        print(f"Image {image_file}: GPS data updated.")
    except subprocess.CalledProcessError as e:
        print(f"Image {image_file}: Error updating GPS data: {e}")


# Main script execution

check_exiftool_installed()

parser = argparse.ArgumentParser()

parser.add_argument('-j', '--json', required=True, help="Path to Google Timeline JSON file.")
parser.add_argument('-d', '--dir', required=True, help="Directory containing images.")
parser.add_argument('-t', '--tolerance', type=int, default=1, help="Tolerance in hours for matching images to locations.")
parser.add_argument('-o', '--overwrite', action='store_true', help="Overwrite existing GPS data.")
parser.add_argument('-r', '--recursive', action='store_true', help="Process images in subdirectories recursively.")
parser.add_argument('-w', '--workers', type=int, default=1, help="Number of parallel threads to use.")
args = parser.parse_args()

locations_file = args.json
image_dir = args.dir
hours_threshold = args.tolerance

print('Loading data (takes a while)...')
with open(locations_file) as f:
    location_data = json.load(f)

my_locations = generate_locations_from_timeline(location_data)

included_extensions = ['jpg', 'jpeg', 'JPG', 'JPEG']

def get_image_files(directory, recursive):
    if recursive:
        image_files = []
        for root, _, files in os.walk(directory):
            image_files.extend(os.path.join(root, fn) for fn in files if any(fn.endswith(ext) for ext in included_extensions))
        return image_files
    else:
        return [os.path.join(directory, fn) for fn in os.listdir(directory) if any(fn.endswith(ext) for ext in included_extensions)]

file_names = get_image_files(image_dir, args.recursive)

# Parallel processing using ThreadPoolExecutor
def process_image(image_file_path):
    cmd = ["exiftool", "-DateTimeOriginal", "-SubSecTimeOriginal", "-OffsetTimeOriginal", "-T", "-GPSLatitude", "-GPSLongitude", image_file_path]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    if result.returncode != 0:
        print(f"Image {image_file_path} - ExifTool error: {result.stderr}")
        return

    exif_data = result.stdout.strip().split()

    if len(exif_data) < 6:
        print(f"Image {image_file_path} - Unexpected ExifTool output or missing fields: {exif_data}")
        return

    if exif_data[4] != "-" and not args.overwrite:
        print(f"Image {image_file_path}: Skipping, GPS data already present.")
        return

    date_original = exif_data[0]  # "YYYY:MM:DD"
    time_original = exif_data[1]  # "HH:MM:SS"
    offset_time_original = exif_data[3] if len(exif_data) > 2 else None

    timestamp_original = f'{date_original} {time_original}'
    timestamp_dt = datetime.datetime.strptime(timestamp_original, '%Y:%m:%d %H:%M:%S')

    timestamp_utc = convert_to_utc(timestamp_dt, offset_time_original)
    image_location = Location(timestamp=timestamp_utc)

    approx_location = find_closest_in_time(my_locations, image_location)
    print(f"Image {image_file_path}: Closest location: {approx_location}")

    update_image_gps(image_file_path, approx_location)

# Use tqdm for progress bar and parallel processing
max_workers = max(1, args.workers)

with ThreadPoolExecutor(max_workers=max_workers) as executor:
    list(tqdm(executor.map(process_image, file_names), total=len(file_names)))

print('Done.')
