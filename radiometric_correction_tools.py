# %%
"""
RedEdge Image Class for Radiometric Correction

Simplified from the MicaSense RedEdge Image Processing Library to avoid issues with missing 
Exif metadata in RedEdge-P images (firmware v1.3.1 and v1.4.5)

For the original/complete library, see:
https://github.com/micasense/imageprocessing

The MicaSense imageprocessing Library is licensed under the MIT License
"""

# Standard Library Imports
import hashlib
import json
import os
import platform
import re
import warnings
from datetime import datetime, timezone
from pathlib import Path

# Third-Party Imports
import cv2
import matplotlib.pyplot as plt
# import matplotlib.patches as patches
import numpy as np
from pyzbar.pyzbar import decode, ZBarSymbol
from shapely.geometry import Point as PointS, LinearRing as LinearR, LineString as LineS
from shapely.geometry.polygon import Polygon as PolyS
from skimage import measure


# Polygons drawn on the images (reference panel regions)
def _segments_intersect(a, b, c, d):
    """ True if the segments ab and cd cross (touching at a shared end point is not crossing) """
    def orientation(p, q, r):
        value = (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
        return int(value > 0) - int(value < 0)
    return orientation(a, b, c) * orientation(a, b, d) < 0 and orientation(c, d, a) * orientation(c, d, b) < 0


def validate_polygon(points, image_size, name='polygon'):
    """
    Check a polygon [[x, y], ...] drawn on an image of size (width, height): at least 3 vertices, inside the image,
    with area and without crossing edges. Raises ValueError with the reason
    """
    points = np.asarray(points, dtype=float)
    if points.ndim != 2 or points.shape[1] != 2 or len(points) < 3:
        raise ValueError(f"{name}: a region needs at least 3 vertices [x, y]")
    if not np.all(np.isfinite(points)):
        raise ValueError(f"{name}: the region has non-finite coordinates")
    width, height = image_size
    if (points < 0).any() or (points[:, 0] > width - 1).any() or (points[:, 1] > height - 1).any():
        raise ValueError(f"{name}: the region is not inside the {width} x {height} image")
    count = len(points)
    for i in range(count):
        for j in range(i + 1, count):
            if j == i + 1 or (i == 0 and j == count - 1):
                continue
            if _segments_intersect(points[i], points[(i + 1) % count], points[j], points[(j + 1) % count]):
                raise ValueError(f"{name}: the edges of the region cross each other")
    if cv2.contourArea(points.astype(np.float32)) <= 0:
        raise ValueError(f"{name}: the region has no area")


# Define the camera matrix
def cv2_camera_matrix(principal_point, focal_length, focal_plane_resolution_px_per_mm): 
    center_x = principal_point[0] * focal_plane_resolution_px_per_mm[0]
    center_y = principal_point[1] * focal_plane_resolution_px_per_mm[1]

    # set up camera matrix for cv2
    cam_mat = np.zeros((3, 3))
    cam_mat[0, 0] = focal_length * focal_plane_resolution_px_per_mm[0]
    cam_mat[1, 1] = focal_length * focal_plane_resolution_px_per_mm[1]
    cam_mat[2, 2] = 1.0
    cam_mat[0, 2] = center_x
    cam_mat[1, 2] = center_y

    # set up distortion coefficients for cv2
    return cam_mat


# Metadata validation helpers - report the file and the field when metadata are missing or invalid
class MetadataError(ValueError):
    """ Raised when image metadata are missing or invalid, naming the file and the field """


def _metadata_value(metadata, key, image_path):
    if key not in metadata or metadata[key] is None:
        raise MetadataError(f"{image_path}: missing metadata field '{key}'")
    return metadata[key]


def _metadata_floats(metadata, key, image_path, count=None):
    """ Return a field as a list of finite floats, accepting a list or a string separated by commas/spaces """
    value = _metadata_value(metadata, key, image_path)
    if isinstance(value, str):
        value = value.replace(',', ' ').split()
    elif np.isscalar(value):
        value = [value]
    try:
        numbers = [float(val) for val in value]
    except (TypeError, ValueError):
        raise MetadataError(f"{image_path}: metadata field '{key}' is not numeric: {metadata[key]!r}")
    if len(numbers) == 0 or not np.all(np.isfinite(numbers)):
        raise MetadataError(f"{image_path}: metadata field '{key}' has no finite values: {metadata[key]!r}")
    if count is not None and len(numbers) != count:
        raise MetadataError(f"{image_path}: metadata field '{key}' has {len(numbers)} values, expected {count}")
    return numbers


def _metadata_positive(metadata, key, image_path):
    """ Return a field (unchanged) after checking that it is a single positive finite number """
    value = _metadata_value(metadata, key, image_path)
    number = _metadata_floats(metadata, key, image_path, count=1)[0]
    if number <= 0:
        raise MetadataError(f"{image_path}: metadata field '{key}' must be positive, got {value!r}")
    return value


# Map the files of one capture to their bands using the band name in the metadata (not the file order)
def map_band_files(image_paths, metadata_list, expected_bands=None, ignore_bands=()):
    """
    Inputs: image paths of one capture, their metadata (same order), optionally the expected band names and the names of
            the bands that are in the files but must be left out (for example a thermal band)
    Outputs: dictionary {band name: (image path, metadata)}, with exactly one file per band
    """
    ignored = set(ignore_bands or ())
    if len(image_paths) != len(metadata_list):
        raise ValueError("image_paths and metadata_list must have the same length")
    band_files = {}
    for image_path, metadata in zip(image_paths, metadata_list):
        band_name = _metadata_value(metadata, 'XMP:BandName', image_path)
        if band_name in ignored:
            continue
        if band_name in band_files:
            raise MetadataError(f"Band '{band_name}' appears twice: {band_files[band_name][0]} and {image_path}")
        band_files[band_name] = (image_path, metadata)
    capture_ids = { metadata.get('XMP:CaptureId') for _, metadata in band_files.values() }
    if len(capture_ids) > 1:
        raise MetadataError(f"Files belong to different captures (XMP:CaptureId): {sorted(map(str, capture_ids))}")
    if expected_bands is not None:
        missing = [band for band in expected_bands if band not in band_files]
        unexpected = [band for band in band_files if band not in expected_bands]
        if missing or unexpected:
            raise MetadataError(f"Band mismatch - missing: {missing}, unexpected: {unexpected}")
    return band_files


# DLS correction - ratio between the irradiance at the panel capture and at the image capture
def dls_irradiance_ratio(panel_image, image):
    """
    Inputs: panel and target rct.Image objects of the SAME band
    Outputs: E_panel / E_image, to be multiplied by the panel-calibrated reflectance. It is 1 for the panel capture
    The Panchro band is treated like the others, using the irradiance written in its own file.
    A ratio outside DLS_CHECKS['ratio_range'] gives a warning
    """
    if panel_image.band_name != image.band_name:
        raise MetadataError(f"DLS correction needs the same band: panel is '{panel_image.band_name}' "
                            f"({panel_image.image_path}), image is '{image.band_name}' ({image.image_path})")
    if (panel_image.irradiance_scale_to_si is None) != (image.irradiance_scale_to_si is None):
        raise MetadataError(f"'XMP:IrradianceScaleToSIUnits' exists in only one of {panel_image.image_path} "
                            f"and {image.image_path}")
    ratio = panel_image.dls_irradiance() / image.dls_irradiance()
    low, high = DLS_CHECKS['ratio_range']
    if not low <= ratio <= high:
        warnings.warn(f"{image.image_path}: DLS ratio {ratio:.3f} is outside the plausible range {low}-{high}",
                      RuntimeWarning)
    return ratio


def capture_dls_ratios(panel_images, images):
    """
    DLS ratios (E_panel / E_image) of all the bands of a capture, Panchro included
    Inputs: dictionaries {band name: rct.Image} for the panel capture and for the image capture
    Outputs: dictionary {band name: (ratio, source)}, with source 'metadata'. A missing or invalid irradiance in
             any band raises MetadataError: no ratio is estimated from other bands
    """
    return { band: (dls_irradiance_ratio(panel_images[band], image), 'metadata') for band, image in images.items() }


# Firmware from which these cameras write a reliable XMP:HorizontalIrradiance (MicaSense's horizontal_irradiance_valid())
HORIZONTAL_IRRADIANCE_FIRMWARE = {'RedEdge': '5.1.7', 'RedEdge-M': '5.1.7', 'Altum': '1.2.3'}


def _version(text):
    """ A firmware version such as 'v7.5.0-beta6' as a tuple of numbers, (7, 5, 0); () if there is none """
    numbers = re.findall(r'\d+', str(text or '').split('-')[0])
    return tuple(int(number) for number in numbers)


# Checks of the DLS data - thresholds (adjust them if needed). They are guesses, not validated limits
DLS_CHECKS = { 'ratio_range': (0.5, 2.0),            # plausible E_panel / E_image
                'band_ratio_tolerance': 0.03,         # largest relative departure of a band from the median ratio
                'max_sun_angle_discrepancy_deg': 10.0,  # see Image.dls_sun_angle_discrepancy_deg
                'jump_threshold': 0.10,               # relative change between consecutive captures
                'panel_drift_tolerance': 0.05,        # relative change of the panel factor (and of E_DLS / E_panel_estimate)
                                                      # between two panel captures
                'panel_illumination_tolerance': 0.15 }  # how different the light of a capture may be from that of the nearest panel
                                                        # (see illumination_distance; 0.15 is about 15 %)


def check_dls_capture(panel_images, images, ratios=None):
    """
    Check the DLS data of one capture against the panel (see DLS_CHECKS for the limits)
    Inputs: dictionaries {band name: rct.Image} for the panel capture (after detect_reference_panel; {} without a panel, then
            only the image is checked) and the image
            capture, and optionally the output of capture_dls_ratios
    Outputs: dictionary with the values checked and 'flags', a list of messages (also issued as RuntimeWarning), and
             'flag_details', one dictionary per flag (same order) saying what was checked, so that the same criterion can be
             counted over many captures: {'criterion', 'scope' ('panel' or 'image', or None), 'band', 'value', 'limit'}.
             Data that are not available are reported in 'unavailable' instead of raising errors
    """
    flags, details, unavailable = [], [], []
    if ratios is None:
        ratios = {}
        for band, image in images.items():
            if band not in panel_images:           # no panel capture (reflectance from the DLS alone): no ratio
                continue
            try:
                ratios[band] = (dls_irradiance_ratio(panel_images[band], image), 'metadata')
            except MetadataError as error:
                unavailable.append(str(error))
    values = { band: ratio for band, (ratio, _) in ratios.items() }
    result = { 'band_ratios': values, 'ratio_sources': { band: source for band, (_, source) in ratios.items() } }

    # Same illumination change in all bands
    if values:
        median = float(np.median(list(values.values())))
        departures = { band: value / median - 1 for band, value in values.items() }
        result['median_ratio'] = median
        result['band_ratio_departures'] = departures
        for band, departure in departures.items():
            if abs(departure) > DLS_CHECKS['band_ratio_tolerance']:
                flags.append(f"Band '{band}' DLS ratio departs {100 * departure:.1f} % from the median of the bands")
                details.append({'criterion': 'band_ratio_departure', 'scope': 'image', 'band': band,
                                'value': float(departure), 'limit': DLS_CHECKS['band_ratio_tolerance']})

    # Irradiance of the DLS compared with the irradiance estimated from the panel. It differs between bands
    #    (18 % in the example, a calibration difference between camera and DLS), so it is only reported here; compare it
    #    between two panel captures with compare_panel_captures()
    estimates = {}
    for band, panel in panel_images.items():
        audit = getattr(panel, 'panel_audit', None)
        try:
            if audit is not None:
                estimates[band] = panel.dls_irradiance() / audit['panel_irradiance_estimate']
        except MetadataError as error:
            unavailable.append(str(error))
    if len(estimates) > 1:
        cv = float(np.std(list(estimates.values())) / np.mean(list(estimates.values())))
        result['dls_over_panel_estimate'] = estimates
        result['dls_over_panel_estimate_variation_across_bands'] = cv

    # DLS pointing: sun angle seen by the DLS against the solar zenith
    result['sun_angle_discrepancy_deg'] = {}
    for name, group in (('panel', panel_images), ('image', images)):
        if not group:
            continue
        angles = [ image.dls_sun_angle_discrepancy_deg() for image in group.values() ]
        angles = [ angle for angle in angles if angle is not None ]
        if not angles:
            unavailable.append(f"sun angle discrepancy of the {name} capture (solar elevation or light vector missing)")
            continue
        result['sun_angle_discrepancy_deg'][name] = float(np.median(angles))
        if np.median(angles) > DLS_CHECKS['max_sun_angle_discrepancy_deg']:
            flags.append(f"DLS of the {name} capture: sun angle differs {np.median(angles):.1f} deg from the solar zenith "
                         f"(DLS tilted at least that much, or the angles are inconsistent)")
            details.append({'criterion': 'dls_tilt', 'scope': name, 'band': None, 'value': float(np.median(angles)),
                            'limit': DLS_CHECKS['max_sun_angle_discrepancy_deg']})

    # Plausible range (also warned by dls_irradiance_ratio)
    low, high = DLS_CHECKS['ratio_range']
    for band, value in values.items():
        if not low <= value <= high:
            flags.append(f"Band '{band}' DLS ratio {value:.3f} is outside the plausible range {low}-{high}")
            details.append({'criterion': 'ratio_range', 'scope': 'image', 'band': band, 'value': float(value),
                            'limit': [low, high]})

    result['flags'], result['flag_details'], result['unavailable'] = flags, details, unavailable
    for flag in flags:
        warnings.warn(flag, RuntimeWarning)
    return result


# Changes of the irradiance along a flight
def classify_irradiance_changes(irradiances, threshold=None):
    """
    Find the changes between consecutive captures of the irradiance of one band, in acquisition order
    Inputs: positive irradiances; threshold is the relative change to report (default DLS_CHECKS['jump_threshold'])
    Outputs: list of dictionaries {'index', 'kind', 'change'}: 'spike' is a value that jumps and comes back to the level
             of its neighbors (a single capture, e.g. a shaded or tilted DLS); 'step' is a change that persists
             (e.g. the sun going behind a cloud). 'index' is the first capture after the change; 'change' is the
             relative change from the previous capture
    """
    if threshold is None:
        threshold = DLS_CHECKS['jump_threshold']
    values = [float(value) for value in irradiances]
    if any(not np.isfinite(value) or value <= 0 for value in values):
        raise ValueError("irradiances must be positive and finite")
    events, skip = [], set()
    for i in range(1, len(values)):
        if i in skip:
            continue
        change = values[i] / values[i - 1] - 1
        if abs(change) <= threshold:
            continue
        if i + 1 < len(values):
            back = values[i + 1] / values[i] - 1
            if abs(back) > threshold and back * change < 0 and abs(values[i + 1] / values[i - 1] - 1) <= threshold:
                events.append({'index': i, 'kind': 'spike', 'change': change})
                skip.add(i + 1)
                continue
        events.append({'index': i, 'kind': 'step', 'change': change})
    return events


# Panel captured before and after the flight
def compare_panel_captures(before, after, tolerance=None):
    """
    Compare two panel captures (e.g. before and after the flight)
    Inputs: dictionaries {band name: rct.Image} after detect_reference_panel for each capture
    Outputs: dictionary {'bands': {band: {'factor_ratio', 'irradiance_ratio' and 'dls_over_panel_estimate_ratio' (if
             the DLS is available)}}, 'flags'}. A factor ratio far from 1 shows a change of the light or a problem with
             the panel (shadow, dirt, handling); the DLS ratio of ratios far from 1 shows that the DLS did not follow
             the light seen by the camera
    """
    if tolerance is None:
        tolerance = DLS_CHECKS['panel_drift_tolerance']
    result, flags = {}, []
    for band in before:
        if band not in after:
            flags.append(f"Band '{band}' is missing in the second panel capture")
            continue
        entry = { 'factor_ratio': after[band].panel_audit['radiance_to_reflectance_factor'] /
                                  before[band].panel_audit['radiance_to_reflectance_factor'] }
        try:
            entry['irradiance_ratio'] = after[band].dls_irradiance() / before[band].dls_irradiance()
            # DLS and camera should change together: this ratio of ratios is 1 if the DLS followed the light
            estimate_ratio = (after[band].panel_audit['panel_irradiance_estimate'] /
                              before[band].panel_audit['panel_irradiance_estimate'])
            entry['dls_over_panel_estimate_ratio'] = entry['irradiance_ratio'] / estimate_ratio
        except MetadataError:
            pass
        result[band] = entry
        if abs(entry['factor_ratio'] - 1) > tolerance:
            flags.append(f"Band '{band}': panel factor changed {100 * (entry['factor_ratio'] - 1):.1f} % between captures")
        if abs(entry.get('dls_over_panel_estimate_ratio', 1) - 1) > tolerance:
            flags.append(f"Band '{band}': DLS irradiance / panel irradiance estimate changed "
                         f"{100 * (entry['dls_over_panel_estimate_ratio'] - 1):.1f} % between captures")
    for flag in flags:
        warnings.warn(flag, RuntimeWarning)
    return { 'bands': result, 'flags': flags }


# Geometry of the acquisition: tilt of the camera and direction in which it points with respect to the sun
# The camera writes its attitude (XMP:Roll, XMP:Pitch, XMP:Yaw, in degrees) and the sun (XMP:SolarAzimuth, from north
# clockwise, and XMP:SolarElevation, in radians). The heading of the camera is the direction of the top of its image.
# The angle between the heading and the sun goes from 0 (facing the sun) to 90 (the sun at the side) and 180 (the back to
# the sun). These are the limits (first guesses, to be adjusted to the data), also kept in the configuration
GEOMETRY_CHECKS = { 'max_camera_tilt_deg': 5.0,             # angle between the optical axis of the camera and the vertical
                    'panel_heading_tolerance_deg': 30.0,    # panel: largest angle between the heading and the line of the
                                                            # sun (0, facing it, or 180, back to it)
                    'image_heading_tolerance_deg': 30.0,    # images: largest departure from the sun at the side (90)
                    'heading_offset_deg': 0.0 }             # added to XMP:Yaw to refer it to true north (the magnetic
                                                            # declination, east positive, if the yaw is magnetic)


def _angle_of(metadata, key):
    """ A number written in the metadata (possibly as text), or None if it is missing or is not a number """
    try:
        return _metadata_floats(metadata, key, key, count=1)[0]
    except MetadataError:
        return None


def camera_tilt_deg(metadata):
    """ Angle between the optical axis of the camera and the vertical, in degrees, from XMP:Roll and XMP:Pitch (or None) """
    roll, pitch = _angle_of(metadata, 'XMP:Roll'), _angle_of(metadata, 'XMP:Pitch')
    if roll is None or pitch is None:
        return None
    return float(np.degrees(np.arccos(np.clip(np.cos(np.radians(roll)) * np.cos(np.radians(pitch)), -1, 1))))


def sun_elevation_deg(metadata):
    """ Elevation of the sun above the horizon, in degrees, from XMP:SolarElevation (written in radians), or None """
    elevation = _angle_of(metadata, 'XMP:SolarElevation')
    return None if elevation is None else float(np.degrees(elevation))


def heading_to_sun_deg(metadata, offset=0.0):
    """
    Angle between the heading of the camera (XMP:Yaw plus the offset) and the direction of the sun (XMP:SolarAzimuth, in
    radians), from 0 to 180 degrees without a side: 0 is facing the sun, 90 the sun at the side, 180 the back to the sun.
    None if a field is missing
    """
    yaw, azimuth = _angle_of(metadata, 'XMP:Yaw'), _angle_of(metadata, 'XMP:SolarAzimuth')
    if yaw is None or azimuth is None:
        return None
    return float(abs((yaw + offset - np.degrees(azimuth) + 180) % 360 - 180))


def measure_geometry(images):
    """
    The geometry of a capture: the median over its bands of the tilt of the camera, the angle between its heading and the
    sun, the elevation of the sun and the tilt of the DLS (see Image.dls_sun_angle_discrepancy_deg)
    Inputs: {band name: rct.Image}. Outputs: {'camera_tilt_deg', 'heading_to_sun_deg', 'sun_elevation_deg', 'dls_tilt_deg'},
    each a number or None
    """
    offset = GEOMETRY_CHECKS['heading_offset_deg']
    functions = {'camera_tilt_deg': lambda image: camera_tilt_deg(image.metadata),
                 'heading_to_sun_deg': lambda image: heading_to_sun_deg(image.metadata, offset),
                 'sun_elevation_deg': lambda image: sun_elevation_deg(image.metadata),
                 'dls_tilt_deg': lambda image: image.dls_sun_angle_discrepancy_deg()}
    values = {}
    for name, function in functions.items():
        numbers = [number for number in (function(image) for image in images.values()) if number is not None]
        values[name] = float(np.median(numbers)) if numbers else None
    return values


def _check_geometry(images, kind):
    values = measure_geometry(images)
    flags, details, unavailable = [], [], []
    tilt, heading = values['camera_tilt_deg'], values['heading_to_sun_deg']
    if tilt is None:
        unavailable.append(f"camera tilt of the {kind} capture (XMP:Roll or XMP:Pitch missing)")
    elif tilt > GEOMETRY_CHECKS['max_camera_tilt_deg']:
        flags.append(f"Camera tilt of the {kind} capture: {tilt:.1f} deg from the vertical (limit "
                     f"{GEOMETRY_CHECKS['max_camera_tilt_deg']:g} deg)")
        details.append({'criterion': 'camera_tilt', 'scope': kind, 'band': None, 'value': float(tilt),
                        'limit': GEOMETRY_CHECKS['max_camera_tilt_deg']})
    if heading is None:
        unavailable.append(f"heading of the camera of the {kind} capture (XMP:Yaw or XMP:SolarAzimuth missing)")
    elif kind == 'panel':
        off_line = min(heading, 180 - heading)
        values['heading_departure_deg'] = off_line               # from facing the sun or the back to it
        if off_line > GEOMETRY_CHECKS['panel_heading_tolerance_deg']:
            flags.append(f"Heading of the camera in the panel capture: {heading:.0f} deg from the sun; for the panel "
                         f"expect within {GEOMETRY_CHECKS['panel_heading_tolerance_deg']:g} deg of 0 (facing the sun) or "
                         f"180 (the back to the sun)")
            # the value shown is the heading; what is compared with the limit is its departure from 0 or 180
            details.append({'criterion': 'heading', 'scope': 'panel', 'band': None, 'value': float(heading),
                            'departure': float(off_line), 'limit': GEOMETRY_CHECKS['panel_heading_tolerance_deg']})
    else:
        departure = abs(heading - 90)                            # from the sun at the side
        values['heading_departure_deg'] = departure
        if departure > GEOMETRY_CHECKS['image_heading_tolerance_deg']:
            flags.append(f"Heading of the camera in the image capture: {heading:.0f} deg from the sun; for mapping "
                         f"expect within {GEOMETRY_CHECKS['image_heading_tolerance_deg']:g} deg of 90 (the sun at the side)")
            # the value shown is the heading; what is compared with the limit is its departure from 90
            details.append({'criterion': 'heading', 'scope': 'image', 'band': None, 'value': float(heading),
                            'departure': float(departure), 'limit': GEOMETRY_CHECKS['image_heading_tolerance_deg']})
    for flag in flags:
        warnings.warn(flag, RuntimeWarning)
    return {'values': values, 'flags': flags, 'flag_details': details, 'unavailable': unavailable}


def check_panel_geometry(panel_images):
    """
    Check the tilt of the camera and its heading with respect to the sun in a panel capture (see GEOMETRY_CHECKS): the
    panel is expected to be photographed with the camera facing the sun or with its back to it
    Inputs: {band name: rct.Image}. Outputs: {'values' (see measure_geometry, plus 'heading_departure_deg': the angle from
    0 or 180), 'flags', 'flag_details' (see check_dls_capture), 'unavailable'}; the flags are also issued as RuntimeWarning
    """
    return _check_geometry(panel_images, 'panel')


def check_image_geometry(images):
    """
    The same for a capture to correct, where the sun is expected at the side of the camera (90 degrees); here
    'heading_departure_deg' is the departure from 90
    """
    return _check_geometry(images, 'image')


# Provenance record - what was used to produce the outputs, saved next to them
def _jsonable(value):
    """ Convert numpy values (and containers of them) to plain Python types for json """
    if isinstance(value, dict):
        return { str(key): _jsonable(val) for key, val in value.items() }
    if isinstance(value, (list, tuple)):
        return [ _jsonable(val) for val in value ]
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    return value


# Two panel captures (before and after the flight)
PANELS_USED = ('first', 'last', 'mean', 'interpolate_time', 'nearest_time', 'nearest_illumination',
               'interpolate_illumination', 'adjacent_time')
PANELS_BY_ILLUMINATION = ('nearest_illumination', 'interpolate_illumination')      # they compare the light read by the DLS
ILLUMINATION_POWER = 2         # interpolate_illumination weighs each panel by 1 / distance ** ILLUMINATION_POWER


def capture_time(metadata, name='image'):
    """ Time of a capture, in seconds, from EXIF:DateTimeOriginal (written to the second); MetadataError if missing """
    from datetime import datetime
    text = metadata.get('EXIF:DateTimeOriginal')
    try:
        return datetime.strptime(str(text).strip(), '%Y:%m:%d %H:%M:%S').timestamp()
    except ValueError:
        raise MetadataError(f"{name}: EXIF:DateTimeOriginal is missing or not a date: {text!r}")


def illumination_distance(image_irradiances, panel_irradiances):
    """
    How different the light of a capture is from the light of a panel capture: the root mean square, over the bands, of
    the natural logarithm of the ratio of the horizontal irradiances of the DLS (so 0.1 is about a 10 % difference, in the
    level of the light and in how it is spread over the bands; 0 is the same light)
    Inputs: {band: irradiance} of the capture and of the panel capture (the bands that are in both are compared)
    Outputs: the distance; MetadataError if there is no band in common or an irradiance is not positive
    """
    bands = [band for band in image_irradiances if band in panel_irradiances]
    if not bands:
        raise MetadataError("the capture and the panel capture have no band in common to compare their light")
    ratios = []
    for band in bands:
        image, panel = image_irradiances[band], panel_irradiances[band]
        if not (np.isfinite(image) and np.isfinite(panel) and image > 0 and panel > 0):
            raise MetadataError(f"band '{band}': the irradiances to compare the light must be positive numbers, "
                                f"got {image!r} and {panel!r}")
        ratios.append(np.log(image / panel))
    return float(np.sqrt(np.mean(np.square(ratios))))


def panel_weight(mode, image_time=None, first_time=None, second_time=None, distances=None):
    """
    Weight of the second panel in the calibration of an image
    mode: 'first' (0: only the first panel), 'last' (1: only the second one, the one after the flight), 'mean' (0.5), 'interpolate_time' (the position in time of the image between
    the two panel captures, from 0 at the first to 1 at the second; outside them the nearest panel is used),
    'nearest_time' (0 or 1: the panel captured closest in time to the image; the first one if they are equally close or
    were captured at the same time), 'nearest_illumination' (0 or 1: the panel whose light is the most like that of the
    image, the first one if they are equally like it) or 'interpolate_illumination' (both panels, each weighted by
    1 / distance ** 2, so a panel with the light of the image counts alone, and the weight changes smoothly with the
    light, without steps; 0.5 if both distances are the same, also if both are 0)
    distances: (distance to the first panel, distance to the second panel) from illumination_distance, for the modes by
    illumination
    """
    if mode in PANELS_BY_ILLUMINATION:
        if distances is None:
            raise ValueError(f"panels_used {mode!r} needs the distances of the light of the image to the light of the panels")
        first, second = distances
        if mode == 'nearest_illumination':
            return 1.0 if second < first else 0.0
        first, second = first ** ILLUMINATION_POWER, second ** ILLUMINATION_POWER
        return 0.5 if first + second == 0 else float(first / (first + second))
    if mode == 'first':
        return 0.0
    if mode == 'last':
        return 1.0
    if mode == 'mean':
        return 0.5
    if mode == 'interpolate_time':
        if second_time == first_time:
            return 0.5
        return float(min(1.0, max(0.0, (image_time - first_time) / (second_time - first_time))))
    if mode == 'nearest_time':
        return 1.0 if abs(image_time - second_time) < abs(image_time - first_time) else 0.0
    raise ValueError(f"panels_used must be one of {list(PANELS_USED)}, got {mode!r}")


def combine_panels(panels, image=None):
    """
    The radiance to reflectance factor of one band of an image, from the panel captures that calibrate it
    Inputs: [(panel rct.Image of the band, after detect_reference_panel, weight)], with weights that add up to 1 (a panel
            of weight 0 is left out) and, for the DLS correction, the image (rct.Image) of the same band. With the image,
            each panel factor is first brought to the light of the image, adjusted factor = factor x E_panel / E_image
            (see dls_irradiance_ratio); without it the factors are used as they are
    Outputs: (factor applied to the radiance of the image = the sum of weight x adjusted factor,
              one dictionary per panel used: {'panel' (the rct.Image), 'weight', 'factor', 'dls_ratio' (None without the
              DLS), 'adjusted_factor'})
    """
    used = []
    for panel, weight in panels:
        if weight <= 0:
            continue
        factor = panel.panel_audit['radiance_to_reflectance_factor']
        ratio = dls_irradiance_ratio(panel, image) if image is not None else None
        used.append({'panel': panel, 'weight': float(weight), 'factor': factor, 'dls_ratio': ratio,
                     'adjusted_factor': factor * ratio if ratio is not None else factor})
    if not used:
        raise ValueError("combine_panels needs at least one panel with a weight above 0")
    total = sum(entry['weight'] for entry in used)
    if abs(total - 1) > 1e-9:
        raise ValueError(f"the weights of the panels must add up to 1, got {total}")
    return sum(entry['weight'] * entry['adjusted_factor'] for entry in used), used


def irradiance_si(image):
    """
    The DLS horizontal irradiance of an image in W/m2/nm, for the reflectance from the DLS alone, as MicaSense's
    irradiance_scale_factor(): XMP:HorizontalIrradiance x XMP:IrradianceScaleToSIUnits, or x 0.01 when the image has no
    scale (a DLS 2 that writes the irradiance in uW/cm2/nm). The ratios of the panel correction do not need the scale
    Outputs: (irradiance, scale, where the scale came from: 'metadata' or 'assumed')
    """
    if image.irradiance_scale_to_si is not None:
        return image.dls_irradiance(), float(image.irradiance_scale_to_si), 'metadata'
    return image.dls_irradiance() * 0.01, 0.01, 'assumed'


def dls_reflectance_factor(image):
    """
    The factor that turns the radiance of an image into reflectance with the DLS alone, without a panel:
    reflectance = pi x radiance / E (Lambertian surface), with E the DLS irradiance in W/m2/nm (see irradiance_si)
    Outputs: (factor, {'irradiance', 'scale', 'scale_source'})
    """
    irradiance, scale, source = irradiance_si(image)
    return np.pi / irradiance, {'irradiance': irradiance, 'scale': scale, 'scale_source': source}


def band_record(output_path, image, reflectance, panels, factor_applied, dls_only=None, output='reflectance'):
    """
    Describe how one output band was produced
    Inputs: path of the saved file, the image (rct.Image) of the band, the array that was saved, and the panels and the
            factor from combine_panels (the reflectance is the radiance of the image x factor_applied); for the reflectance
            from the DLS alone, no panels and the details of dls_reflectance_factor; for radiance, no panels, no factor
            and output 'radiance'
    Outputs: dictionary that can be saved as json (see write_provenance)
    """
    return _jsonable({ 'band': image.band_name,
                       'output': output,
                       'output_file': str(output_path),
                       'output_dtype': str(reflectance.dtype),
                       'output_shape': reflectance.shape,
                       'image_file': str(image.image_path),
                       'image_capture_id': image.metadata.get('XMP:CaptureId'),
                       'factor_applied': factor_applied,
                       'panels': [ { 'file': str(entry['panel'].image_path),
                                     'capture_id': entry['panel'].metadata.get('XMP:CaptureId'),
                                     'weight': entry['weight'],
                                     'factor': entry['factor'],
                                     'dls_ratio': entry['dls_ratio'],
                                     'adjusted_factor': entry['adjusted_factor'],
                                     'horizontal_irradiance': entry['panel'].horizontal_irradiance,
                                     'irradiance_scale_to_si': entry['panel'].irradiance_scale_to_si,
                                     'audit': getattr(entry['panel'], 'panel_audit', None) } for entry in panels ],
                       'dls_only': dls_only,
                       'dls': { 'used': dls_only is not None or any(entry['dls_ratio'] is not None for entry in panels),
                                'attitude_as_written': { key: image.metadata.get('XMP:' + key)
                                                                    for key in ('Roll', 'Pitch', 'Yaw') },
                                'sun_angle_discrepancy_deg': image.dls_sun_angle_discrepancy_deg(),
                                'image_horizontal_irradiance': image.horizontal_irradiance,
                                'image_irradiance_scale_to_si': image.irradiance_scale_to_si } })


# What the panel audit says that is written somewhere else already: the file (in the record of the band), the band, the
# geometry, and the parameters of the run (in the configuration)
_AUDIT_OMITTED = ('image_path', 'band_name', 'geometry', 'sat_threshold', 'percent_inner_area')


def compact_panel_audit(audit):
    """ The audit of a panel (Image.panel_audit) without what is already in the record of the band or in the configuration """
    if not audit:
        return audit
    compact = {key: value for key, value in audit.items() if key not in _AUDIT_OMITTED}
    if compact.get('barcode_data') is None:
        compact.pop('barcode_data', None)
    return compact


def _number(value):
    """ A value written as text in the metadata (for example '0.798') as a number, when it is one """
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def compact_band_records(band_records):
    """
    The records of the bands of one capture (see band_record) without repetitions
    Outputs: (what is the same in all the bands: the ids of the captures and the attitude of the camera, the records without
             it and without what is empty or not used: the DLS ratio and the adjusted factor when the DLS was not used, the
             audits without what the configuration or the record already says)
    """
    records = json.loads(json.dumps(band_records))
    capture = {}
    values = {record.get('image_capture_id') for record in records}
    if len(values) == 1:
        capture['image_capture_id'] = values.pop()
        for record in records:
            record.pop('image_capture_id', None)
    panel_ids = {json.dumps([panel.get('capture_id') for panel in record['panels']]) for record in records}
    if len(panel_ids) == 1 and json.loads(next(iter(panel_ids))):
        capture['panel_capture_ids'] = json.loads(panel_ids.pop())
        for record in records:
            for panel in record['panels']:
                panel.pop('capture_id', None)
    for record in records:                       # what a reflectance from the DLS alone or a radiance does not have
        for key, empty in (('output', 'reflectance'), ('dls_only', None), ('panels', []), ('factor_applied', None)):
            if key in record and record[key] == empty:
                record.pop(key)
        record.setdefault('panels', [])
    attitudes = [json.dumps(record['dls'].get('attitude_as_written'), sort_keys=True) for record in records]
    if records and len(set(attitudes)) == 1 and records[0]['dls'].get('attitude_as_written'):
        capture['camera_attitude_as_written'] = {key: _number(value) for key, value
                                                 in records[0]['dls']['attitude_as_written'].items()}
    for record in records:
        record['dls'].pop('attitude_as_written', None)
        record['dls'].pop('sun_angle_discrepancy_deg', None)              # it is in the checks, once for the capture
        if record['dls'].get('image_irradiance_scale_to_si') is None:
            record['dls'].pop('image_irradiance_scale_to_si', None)
        for panel in record['panels']:
            if not record['dls'].get('used'):
                panel.pop('dls_ratio', None)
                panel.pop('adjusted_factor', None)
            if panel.get('irradiance_scale_to_si') is None:
                panel.pop('irradiance_scale_to_si', None)
            panel['audit'] = compact_panel_audit(panel.get('audit'))
            if panel['audit']:
                panel['audit'].pop('radiance_to_reflectance_factor', None)     # it is the 'factor' of the panel
    return capture, records


def compact_checks(checks):
    """
    The checks of a capture without what says nothing: the source of the ratios (always the metadata), the details of the
    flags (they are for counting the same criterion over the captures) and the empty list
    """
    compact = {key: value for key, value in (checks or {}).items() if key not in ('ratio_sources', 'flag_details')}
    if not compact.get('unavailable'):
        compact.pop('unavailable', None)
    return compact


def write_provenance(path, band_records, parameters=None, checks=None):
    """
    Save a json file with the records of the bands (see band_record), the parameters of the run and the software used
    The module is identified by the hash of its text (line endings normalized), so changes in the code are noticed.
    What is the same in all the bands is written once (the ids of the captures and the attitude of the camera), what is
    empty or not used is left out, and the limits of the checks are those of the configuration saved in the parameters
    """
    import cv2
    code = Path(__file__).read_bytes().replace(b'\r\n', b'\n')
    dls_used = any(record['dls']['used'] for record in band_records)
    capture, bands = compact_band_records(band_records)
    if any(record.get('output') == 'radiance' for record in band_records):
        method = 'radiance'
    elif any(record.get('dls_only') for record in band_records):
        method = 'dls_only'
    else:
        method = 'panel_and_dls' if dls_used else 'panel_only'
    for band in bands:
        if not band['panels']:
            band.pop('panels')
    record = { 'created_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
               'method': method,
               'software': { 'radiometric_correction_tools_sha256': hashlib.sha256(code).hexdigest(),
                             'python': platform.python_version(),
                             'numpy': np.__version__,
                             'opencv': cv2.__version__ },
               'capture': capture,
               'parameters': _jsonable(parameters or {}),
               'checks': _jsonable(compact_checks(checks)),
               'bands': bands }
    path = Path(path)
    path.write_text(json.dumps(record, indent=2), encoding='utf-8')
    return path


# Names of the files: a name with {capture} where the name of the capture is (the part shared by the files of its bands),
# {index} where the number of the file in the capture is (optional) and * for any other text, for example
# {capture}_{index}.tif. Not case sensitive. A regular expression with a group named capture (and, optionally, index) is
# accepted too, for names that the template cannot describe. The band of each file is not taken from the name but from
# the metadata
CAPTURE_FILE_PATTERN = '{capture}_{index}.tif'

_TEMPLATE_PARTS = re.compile(r'(\{capture\}|\{index\}|\*)')


def template_to_regex(template):
    """ The regular expression that a file name template stands for ({capture}, {index}, * = any text) """
    pieces = []
    for part in _TEMPLATE_PARTS.split(template):
        if part == '{capture}':
            pieces.append('(?P<capture>.+)')
        elif part == '{index}':
            pieces.append(r'(?P<index>\d+)')
        elif part == '*':
            pieces.append('.*')
        else:
            pieces.append(re.escape(part))
    return ''.join(pieces) + '$'


def compile_file_pattern(pattern=None):
    """ The compiled pattern of the file names (a template or a regular expression); ValueError with the reason if it cannot be used """
    text = pattern or CAPTURE_FILE_PATTERN
    if '{capture}' in text:
        if text.count('{capture}') > 1 or text.count('{index}') > 1:
            raise ValueError("the file name template can have {capture} and {index} only once each")
        text = template_to_regex(text)
    try:
        compiled = re.compile(text, re.IGNORECASE)
    except re.error as error:
        raise ValueError(f"the file name pattern is not a valid regular expression: {error}")
    if 'capture' not in compiled.groupindex:
        raise ValueError("the file name pattern needs {capture} where the name of the capture is, e.g. {capture}_{index}.tif "
                         "(or a regular expression with a group named capture)")
    return compiled


FOLDER_SEPARATOR = ' | '                  # between the folders of a text that names several


def folder_list(value):
    """
    The folders that a value names, as written, in its order and each once: a path, a list of paths, or a text with
    several paths separated by ' | ' (or on several lines). An empty value gives []
    """
    if isinstance(value, (list, tuple)):
        items = list(value)
    elif isinstance(value, Path):
        items = [value]
    else:
        items = str(value or '').replace(FOLDER_SEPARATOR.strip(), '\n').splitlines()
    folders, seen = [], set()
    for item in items:
        text = str(item).strip().strip('"')
        if text and _folder_identity(text) not in seen:
            folders.append(text)
            seen.add(_folder_identity(text))
    return folders


def _folder_identity(folder):
    return os.path.normcase(os.path.abspath(str(folder)))


class ImageFolders:
    """
    The raw images of one or several folders, read as if they were one. A capture whose name is in more than one of the
    folders (for example a panel capture copied to the folders of two flights) is a repeated capture: the files of the
    copy in chosen[name] are used and those of the other copies are left out; without a choice it cannot be read
    (find_capture_paths says so). No file is changed
        folders   a path, a list of paths or a text with the paths separated by ' | '
        chosen    {capture name: folder of the copy to use}; a choice for a capture that is not repeated is not used
    """

    def __init__(self, folders, chosen=None):
        self.folders = [Path(folder) for folder in folder_list(folders)]
        self.chosen = {name: _folder_identity(folder) for name, folder in (chosen or {}).items()}

    def __str__(self):
        return FOLDER_SEPARATOR.join(str(folder) for folder in self.folders)

    def files(self):
        """ [(folder, path)] of the files of the folders, in the order of the folders (the subfolders are not read) """
        return [(folder, path) for folder in self.folders for path in sorted(folder.iterdir()) if path.is_file()]


def _image_folders(images_folder):
    return images_folder if isinstance(images_folder, ImageFolders) else ImageFolders(images_folder)


def _matching_files(images_folder, pattern=None):
    """ [(capture name, index or None, folder, path)] of the files whose names match the pattern, all copies included """
    compiled = compile_file_pattern(pattern)
    found = []
    for folder, path in _image_folders(images_folder).files():
        match = compiled.match(path.name)
        if match:
            index = match.groupdict().get('index')
            found.append((match.group('capture'), int(index) if index and index.isdigit() else None, folder, path))
    return found


def repeated_captures(images_folder, pattern=None):
    """
    The captures that are in more than one of the folders: {capture name: {folder: [paths of its files]}}, in the order
    of the names and of the folders. The choices are not applied, so a capture stays here after its copy was chosen
    """
    copies = {}
    for name, _, folder, path in _matching_files(images_folder, pattern):
        copies.setdefault(name, {}).setdefault(folder, []).append(path)
    return {name: copies[name] for name in sorted(copies) if len(copies[name]) > 1}


def unresolved_captures(images_folder, pattern=None, names=None):
    """ The repeated captures (of names, if given) without a valid choice of the copy to use """
    source = _image_folders(images_folder)
    return [name for name, copies in repeated_captures(source, pattern).items()
            if (names is None or name in names) and
            source.chosen.get(name) not in {_folder_identity(folder) for folder in copies}]


def _capture_files(images_folder, pattern=None):
    """
    [(capture name, index or None, path)] of the files whose names match the pattern; of a repeated capture, only the copy
    that was chosen (all of them while there is no choice)
    """
    source = _image_folders(images_folder)
    found = _matching_files(source, pattern)
    folders = {}
    for name, _, folder, _ in found:
        folders.setdefault(name, set()).add(_folder_identity(folder))
    return [(name, index, path) for name, index, folder, path in found
            if len(folders[name]) == 1 or source.chosen.get(name) not in folders[name]
            or source.chosen[name] == _folder_identity(folder)]


def list_capture_names(images_folder, pattern=None):
    """
    Return the sorted names of the captures in a folder, e.g. IMG_0005_1.tif ... IMG_0005_6.tif -> 'IMG_0005'.
    images_folder: a folder, several (see folder_list) or ImageFolders; a repeated capture is listed once
    """
    return sorted({name for name, _, _ in _capture_files(images_folder, pattern)})


def find_capture_paths(images_folder, capture_name, pattern=None):
    """
    Return the image paths of a capture, in the order of their numbers: IMG_0005_1.tif, IMG_0005_2.tif, ...
    ValueError if the capture is in more than one of the folders and the copy to use was not chosen
    """
    files = [(index, path) for name, index, path in _capture_files(images_folder, pattern) if name == capture_name]
    if not files:
        raise FileNotFoundError(f"No images of the capture '{capture_name}' matching the file name pattern "
                                f"{pattern or CAPTURE_FILE_PATTERN} in {images_folder}")
    folders = list(dict.fromkeys(str(path.parent) for _, path in files))
    if len(folders) > 1:
        raise ValueError(f"The capture '{capture_name}' is in more than one images folder ({', '.join(folders)}): choose "
                         "the copy to use (in the window: Browse... of the images folder, Review repeated captures)")
    files.sort(key=lambda item: (item[0] is None, item[0] or 0, item[1].name))
    return [str(path) for _, path in files]


def pattern_template(pattern):
    """
    The file name pattern written as a name: a template is returned as it is; for a regular expression the groups become
    <capture> and <index> and the anchors and the escapes are removed: r'(?P<capture>.+)_(?P<index>\\d+)\\.tif$' -> '<capture>_<index>.tif'
    """
    if '{capture}' in (pattern or CAPTURE_FILE_PATTERN):
        return pattern or CAPTURE_FILE_PATTERN
    text = re.sub(r'\(\?P<(\w+)>(?:[^()]|\([^()]*\))*\)', lambda match: f"<{match.group(1)}>", pattern or CAPTURE_FILE_PATTERN)
    text = text.lstrip('^').rstrip('$')
    return re.sub(r'\\(.)', r'\1', text)


def preview_file_pattern(images_folder, pattern=None, examples=3):
    """
    How a file name pattern reads the files of a folder (or of several: each copy of a repeated capture counts once in
    files_per_capture)
    Outputs: {'files' (all the files), 'matched', 'captures', 'files_per_capture' ({number of files: number of captures}),
             'examples' ([(file name, capture, index or None)], the first of each of the first captures), 'unmatched'
             (names of a few files that do not match)}. ValueError if the pattern is not valid
    """
    compiled = compile_file_pattern(pattern)
    files = sorted(_image_folders(images_folder).files(), key=lambda item: item[1].name)
    found = {}
    unmatched = []
    for folder, path in files:
        match = compiled.match(path.name)
        if match:
            index = match.groupdict().get('index')
            found.setdefault((match.group('capture'), str(folder)), []).append(
                (path.name, int(index) if index and index.isdigit() else None))
        else:
            unmatched.append(path.name)
    per_capture = {}
    for copy in found.values():
        per_capture[len(copy)] = per_capture.get(len(copy), 0) + 1
    shown = [(copy[0][0], capture, copy[0][1]) for (capture, _), copy in sorted(found.items())[:examples]]
    return {'files': len(files), 'matched': sum(len(copy) for copy in found.values()),
            'captures': len({capture for capture, _ in found}),
            'files_per_capture': dict(sorted(per_capture.items())), 'examples': shown, 'unmatched': unmatched[:3]}


def suggest_file_pattern(file_names):
    """
    A file name template for the names of a folder, or None if there is no obvious one. The names are expected to be
    <capture name><separator><number of the file>.<extension> (IMG_0005_1.tif); the most common extension and separator
    are used and the pattern is kept only if most of the files follow it
    """
    file_names = list(file_names)
    stems = {}
    for name in file_names:
        stem, dot, extension = str(name).rpartition('.')
        if not dot or not stem:
            continue
        match = re.match(r'^(.+?)([_\-. ])(\d+)$', stem)
        if match:
            stems.setdefault(extension.lower(), []).append(match.group(2))
    if not stems:
        return None
    extension = max(stems, key=lambda key: len(stems[key]))
    separators = stems[extension]
    separator = max(set(separators), key=separators.count)
    if len(separators) < 0.5 * len(file_names):
        return None
    return f"{{capture}}{separator}{{index}}.{extension}"


def _read_metadata(image_paths, exiftool_path=None):
    import exiftool
    with exiftool.ExifToolHelper(executable=exiftool_path) as et:
        return [ et.get_metadata(image_path, params=['-b', '-F'])[0] for image_path in image_paths ]


def read_band_files(image_paths, expected_bands=None, exiftool_path=None, ignore_bands=()):
    """ Read the metadata with ExifTool and map the files of one capture to their bands (see map_band_files) """
    return map_band_files(image_paths, _read_metadata(image_paths, exiftool_path), expected_bands, ignore_bands)


def read_band_definitions(image_paths, exiftool_path=None):
    """
    Read the bands of a capture from the metadata of its files: name (XMP:BandName), central wavelength and bandwidth
    (XMP:CentralWavelength, XMP:WavelengthFWHM, in nm) and the position of the camera in the rig (XMP:RigCameraIndex)
    Outputs: {'camera', 'serial', 'bands': [{'name', 'center_nm', 'bandwidth_nm', 'index', 'file', 'thermal'}]} with the
             bands in the order of the rig. 'thermal' is a guess (center above 2500 nm or a name like LWIR) to help to
             leave out bands that are not reflective. MetadataError if a field is missing
    """
    metadata_list = _read_metadata(image_paths, exiftool_path)
    bands = []
    for image_path, metadata in zip(image_paths, metadata_list):
        name = _metadata_value(metadata, 'XMP:BandName', image_path)
        center = _metadata_floats(metadata, 'XMP:CentralWavelength', image_path, count=1)[0]
        width = _metadata_floats(metadata, 'XMP:WavelengthFWHM', image_path, count=1)[0]
        rig = metadata.get('XMP:RigCameraIndex')
        bands.append({'name': name, 'center_nm': center, 'bandwidth_nm': width,
                      'index': int(rig) if isinstance(rig, (int, float)) else None, 'file': str(image_path),
                      'thermal': center > 2500 or name.strip().lower() in ('lwir', 'thermal')})
    names = [band['name'] for band in bands]
    duplicated = sorted({name for name in names if names.count(name) > 1})
    if duplicated:
        raise MetadataError(f"Band name(s) used by more than one file: {duplicated}")
    bands.sort(key=lambda band: (band['index'] is None, band['index'] if band['index'] is not None else 0, band['name']))
    first = metadata_list[0] if metadata_list else {}
    return {'camera': ' '.join(str(first.get(key, '')).strip() for key in ('EXIF:Make', 'EXIF:Model')).strip(),
            'serial': str(first.get('EXIF:SerialNumber', '')), 'bands': bands}


#
# The vignetting corrections already computed, by model, size and coefficients (see Image.vignetting_correction)
_VIGNETTING_CACHE = {}


class Image(object):

    """
    Create an image object from the path for a MicaSense RedEdge image and corresponding metadata
    """

    def __init__(self, image_path, image_metadata):

        # Import image and store general information
        self.image_path = image_path
        self.metadata = image_metadata
        self.image = plt.imread( image_path )
        self.size = _metadata_positive(self.metadata, 'EXIF:ImageWidth', image_path), \
                    _metadata_positive(self.metadata, 'EXIF:ImageHeight', image_path)
        self.bits_per_pixel = _metadata_positive(self.metadata, 'EXIF:BitsPerSample', image_path)
        self.band_name = _metadata_value(self.metadata, 'XMP:BandName', image_path)

        # Parameters for camera matrix calculation
        self.principal_point = _metadata_floats(self.metadata, 'XMP:PrincipalPoint', image_path, count=2)
        self.focal_plane_resolution_px_per_mm = [ _metadata_positive(self.metadata, 'EXIF:FocalPlaneXResolution', image_path) ]*2
        self.focal_length = _metadata_positive(self.metadata, 'EXIF:FocalLength', image_path)
        self.distortion_parameters = np.array(
            _metadata_floats(self.metadata, 'XMP:PerspectiveDistortion', image_path, count=5) )[[0, 1, 3, 4, 2]]
        self.camera_matrix = cv2_camera_matrix(self.principal_point, self.focal_length , self.focal_plane_resolution_px_per_mm)

        # Vignetting model, as in MicaSense's Image.vignette(): the 2D polynomial of the RedEdge-P and Altum-PT, or the
        # radial polynomial of older cameras (RedEdge-M/MX, Altum); the fields of the image say which one
        if self.metadata.get('XMP:VignettingPolynomial2D') is not None:
            self.vignetting_model = '2d'
            self.k = _metadata_floats(self.metadata, 'XMP:VignettingPolynomial2D', image_path) # pol2DCoeff
            self.e = _metadata_floats(self.metadata, 'XMP:VignettingPolynomial2DName', image_path) # pol2DExponents
            if len(self.e) != 2 * len(self.k):
                raise MetadataError(f"{image_path}: 'XMP:VignettingPolynomial2DName' needs {2 * len(self.k)} exponents, "
                                    f"found {len(self.e)}")
        elif self.metadata.get('XMP:VignettingCenter') is not None:
            self.vignetting_model = 'radial'
            self.vignetting_center = _metadata_floats(self.metadata, 'XMP:VignettingCenter', image_path, count=2)
            self.vignetting_polynomial = _metadata_floats(self.metadata, 'XMP:VignettingPolynomial', image_path)
        else:
            raise MetadataError(f"{image_path}: no vignetting model: neither 'XMP:VignettingPolynomial2D' (RedEdge-P, "
                                f"Altum-PT) nor 'XMP:VignettingCenter' and 'XMP:VignettingPolynomial' (RedEdge-M/MX, Altum)")

        # Parameters for radiometric calibration - radiometric sensitivity
        self.a1, self.a2, self.a3 = _metadata_floats(self.metadata, 'XMP:RadiometricCalibration', image_path, count=3)
        if self.a1 <= 0:
            raise MetadataError(f"{image_path}: first 'XMP:RadiometricCalibration' coefficient must be positive")

        # Dark current pixel values
        self.black_levels = np.array( _metadata_floats(self.metadata, 'EXIF:BlackLevel', image_path) )

        # Exposure time & gain (gain = ISO/100)
        self.exposure_time = float( _metadata_positive(self.metadata, 'EXIF:ExposureTime', image_path) )
        self.gain          = float( _metadata_positive(self.metadata, 'EXIF:ISOSpeed', image_path) ) / 100.0
        # Some legacy RedEdge firmware wrote 1/6329 s for an exposure of 0.274 ms (MicaSense's Metadata.exposure())
        self.camera_model = str(self.metadata.get('EXIF:Model') or '')
        if self.camera_model != 'Altum' and abs(self.exposure_time - 1.0 / 6329.0) < 1e-6:
            self.exposure_time = 0.000274

        # Downwelling light sensor (DLS) - optional, only needed when the DLS correction is requested
        self.horizontal_irradiance = self.metadata.get('XMP:HorizontalIrradiance')
        self.irradiance_scale_to_si = self.metadata.get('XMP:IrradianceScaleToSIUnits')


    # Undistort the image
    def undistorted(self, image=None):
        """ return the undistorted version of the image (default: the input image, or any array of the same size) """
        if image is None:
            image = self.image
        # get the optimal new camera matrix
        new_cam_mat, _ = cv2.getOptimalNewCameraMatrix( self.camera_matrix,
                                                        self.distortion_parameters,
                                                        self.size,
                                                        1 )
        map1, map2 = cv2.initUndistortRectifyMap( self.camera_matrix,
                                                  self.distortion_parameters,
                                                  np.eye(3),
                                                  new_cam_mat,
                                                  self.size,
                                                  cv2.CV_32F)  # cv2.CV_32F for 32 bit floats
        # Compute the undistorted image (16-bit for the raw image)
        image_undistorted = cv2.remap(image, map1, map2, cv2.INTER_LINEAR)
        return image_undistorted


    # Calculate region statistics
    def region_stats(self, region, sat_threshold=None, image=None):
        """
        Provide regional statistics for an image over a region
        Inputs: region is a skimage shape, image is any image ndarray (default: the input image)
        Outputs: mean, std, count, and saturated count tuple for the region
        """
        if image is None:
            image = self.image
        rev_panel_pts = np.fliplr( np.asarray(region, dtype=float) )  # the region is (x, y); the mask works in (row, column)
        rows, columns = image.shape[0:2]
        # only the pixels of the box around the region are tested (the same pixels as on the whole image, much faster)
        top, left = (max(int(np.floor(value)), 0) for value in rev_panel_pts.min(axis=0))
        bottom, right = int(np.ceil(rev_panel_pts[:, 0].max())) + 1, int(np.ceil(rev_panel_pts[:, 1].max())) + 1
        bottom, right = min(bottom, rows), min(right, columns)
        if bottom <= top or right <= left:
            panel_pixels = image[:0, :0].ravel()
        else:
            mask = measure.grid_points_in_poly((bottom - top, right - left), rev_panel_pts - [top, left])
            panel_pixels = image[top:bottom, left:right][mask]
        num_pixels = panel_pixels.size
        stdev = panel_pixels.std()
        mean_value = panel_pixels.mean()
        saturated_count = 0
        if sat_threshold is not None:
            saturated_count = (panel_pixels > sat_threshold).sum()
        return mean_value, stdev, num_pixels, saturated_count


    # Vignetting correction: the multiplier of each pixel
    def vignetting_correction(self):
        """
        1 / polynomial of the position of the pixel, as in MicaSense's Image.vignette():
        - 2D model: the coefficients (XMP:VignettingPolynomial2D) and pairs of exponents (XMP:VignettingPolynomial2DName)
          of x, the position across the width (column / width, 0 at the left), and y, down the height (row / height);
        - radial model: 1 + c1 r + c2 r^2 + ..., with c from XMP:VignettingPolynomial and r the distance in pixels from
          XMP:VignettingCenter (column, row)
        Outputs: array of the size of the image, 1 where the lens does not darken the image and above 1 towards the edges
        """
        height, width = self.image.shape[:2]
        if self.vignetting_model == 'radial':
            key = ('radial', height, width, tuple(self.vignetting_center), tuple(self.vignetting_polynomial))
        else:
            key = ('2d', height, width, tuple(self.k), tuple(self.e))
        cached = _VIGNETTING_CACHE.get(key)                 # the same for every image of one band of one camera
        if cached is not None:
            return cached
        if self.vignetting_model == 'radial':
            rows, columns = np.mgrid[0:height, 0:width]
            distance = np.hypot(columns - self.vignetting_center[0], rows - self.vignetting_center[1])
            correction = 1.0 / np.polyval(list(reversed(self.vignetting_polynomial)) + [1.0], distance)
        else:
            # each term is c x^ex y^ey, with x of the column and y of the row: the sum over the terms is the product of
            # a (rows x terms) matrix of y^ey and a (terms x columns) matrix of c x^ex
            x, y = np.arange(width) / width, np.arange(height) / height
            ys = np.stack([y ** self.e[2 * number + 1] for number in range(len(self.k))], axis=1)
            xs = np.stack([coefficient * x ** self.e[2 * number] for number, coefficient in enumerate(self.k)])
            correction = 1.0 / (ys @ xs)
        correction.setflags(write=False)
        if len(_VIGNETTING_CACHE) >= 16:                    # a few bands of a few cameras at most
            _VIGNETTING_CACHE.pop(next(iter(_VIGNETTING_CACHE)))
        _VIGNETTING_CACHE[key] = correction
        return correction


    # Row gradient correction: the multiplier of each row
    def row_gradient_correction(self):
        """
        1 / (1 + a2 * row / exposure time - a3 * row), with a2 and a3 from XMP:RadiometricCalibration, as in MicaSense's
        Image.radiance(): the sensor reads the image row by row, so the rows further down need a small correction
        Outputs: array of one column (height x 1), to be multiplied by the image
        """
        rows = np.arange(self.image.shape[0], dtype=float)[:, np.newaxis]
        return 1.0 / (1.0 + self.a2 * rows / self.exposure_time - self.a3 * rows)


    # Convert DNs to radiance
    def digital_numbers_to_radiance(self):
        """
        Radiance (W/m2/sr/nm) of every pixel, in the geometry of the original image, as in MicaSense's Image.radiance():
        1. subtract the dark level (mean of EXIF:BlackLevel);
        2. multiply by the vignetting and row gradient corrections; negative values (noise around the dark level) become 0;
        3. divide by gain x exposure time (gain = ISO / 100), multiply by a1 (XMP:RadiometricCalibration) and divide by
           2 ** bits per pixel, because the coefficients are given for values scaled to 0-1
        """
        dark_level = self.black_levels.mean()
        L = self.vignetting_correction() * self.row_gradient_correction() * (self.image - dark_level)
        L[L < 0] = 0
        dn_maximum = float(2 ** self.bits_per_pixel)
        return L.astype(float) / (self.gain * self.exposure_time) * self.a1 / dn_maximum


    # Horizontal irradiance measured by the DLS for this band
    def dls_irradiance(self):
        """
        Return the DLS horizontal irradiance of this band (XMP:HorizontalIrradiance), multiplied by
        XMP:IrradianceScaleToSIUnits when it exists. The scale is only needed to express the value in SI units:
        it cancels when two images of the same camera are compared, so it is not guessed when it is absent
        """
        irradiance = _metadata_positive(self.metadata, 'XMP:HorizontalIrradiance', self.image_path)
        if not self.horizontal_irradiance_trusted():
            raise MetadataError(f"{self.image_path}: the 'XMP:HorizontalIrradiance' written by the firmware "
                                f"{self.metadata.get('EXIF:Software')} of the {self.camera_model} is not reliable "
                                f"(MicaSense needs at least v{HORIZONTAL_IRRADIANCE_FIRMWARE[self.camera_model]}); "
                                f"update the firmware of the camera or do not use the DLS")
        scale = 1.0
        if self.irradiance_scale_to_si is not None:
            scale = float( _metadata_positive(self.metadata, 'XMP:IrradianceScaleToSIUnits', self.image_path) )
        return float(irradiance) * scale


    # Whether the horizontal irradiance written by the camera can be used
    def horizontal_irradiance_trusted(self):
        """
        As MicaSense's Metadata.horizontal_irradiance_valid(): the RedEdge, RedEdge-M (and MX, which writes RedEdge-M) and
        Altum write a reliable XMP:HorizontalIrradiance only from the firmware versions of HORIZONTAL_IRRADIANCE_FIRMWARE;
        the other models (RedEdge-P, Altum-PT) always do
        """
        minimum = HORIZONTAL_IRRADIANCE_FIRMWARE.get(self.camera_model)
        if minimum is None:
            return True
        return _version(self.metadata.get('EXIF:Software')) >= _version(minimum)


    # Angle between the sun direction seen by the DLS and the solar zenith
    def dls_sun_angle_discrepancy_deg(self):
        """
        The DLS estimates the direction of the direct light in its own frame (XMP:EstimatedDirectLightVector) and the
        solar elevation is written in radians (XMP:SolarElevation). With a level DLS the angle between the light vector
        and the DLS axis equals the solar zenith, so their difference is a lower bound of the DLS tilt towards or away
        from the sun. Returns degrees, or None if the fields are missing. The conventions were checked only against the
        example panel capture (level DLS: 0.3 deg); keep the limit in DLS_CHECKS generous
        """
        try:
            vector = np.array( _metadata_floats(self.metadata, 'XMP:EstimatedDirectLightVector', self.image_path, count=3) )
            elevation = _metadata_floats(self.metadata, 'XMP:SolarElevation', self.image_path, count=1)[0]
        except MetadataError:
            return None
        norm = np.linalg.norm(vector)
        if norm == 0:
            return None
        incidence = np.degrees(np.arccos(np.clip(-vector[2] / norm, -1, 1)))
        return float(abs(incidence - (90 - np.degrees(elevation))))


    # Convert radiance to reflectance
    def digital_numbers_to_reflectance(self, radiance_to_reflectance_factor):
        """
        Inputs: the factor applied to the image, from combine_panels() (the panel factor and, with the DLS, the DLS ratio)
        Outputs: reflectance image (float) = radiance x factor
        """
        return self.digital_numbers_to_radiance() * radiance_to_reflectance_factor


    # Undistorted raw image, kept because the panel region is measured on it
    def undistorted_dn(self):
        if getattr(self, '_undistorted_dn', None) is None:
            self._undistorted_dn = self.undistorted()
        return self._undistorted_dn


    # Image to show when a panel region is drawn by hand: undistorted, stretched to 8 bits
    def display_image(self, low_percentile=1, high_percentile=99.5):
        image = self.undistorted_dn().astype(float)
        valid = image[image > 0]
        low, high = np.percentile(valid if valid.size else image, [low_percentile, high_percentile])
        return np.clip((image - low) / max(high - low, 1) * 255, 0, 255).astype(np.uint8)


    # Locate the reference panel from its QR code
    def locate_reference_panel(self, percent_inner_area, sat_threshold=65000):
        """
        Outputs: (panel bounds, QR code data, geometry where the QR code was found) or None if there is no QR code
                 The bounds are in the undistorted image when the geometry is 'undistorted', otherwise in the original
        """
        # Undistorted image
        image_undistorted = self.undistorted_dn()
        # plt.imshow( image_undistorted ) 

        # Convert from uint16 to uint8
        gray_undistorted = (image_undistorted // 256).astype(np.uint8)
        # plt.imshow( gray )

        # Detect and decode barcodes
        barcode = decode(gray_undistorted, symbols=[ZBarSymbol.QRCODE])
        if( len(barcode) > 0 ):
            barcode = barcode[0]
            barcode_source = 'undistorted'
        else: # if barcode is not detected in the undistorted image, use the QR code from the original image
            gray = (self.image // 256).astype(np.uint8)
            barcode = decode(gray, symbols=[ZBarSymbol.QRCODE])
            if( len(barcode) == 0 ):
                return None
            barcode = barcode[0]
            barcode_source = 'original'
        barcode_data = barcode.data.decode("utf-8")

        # Get bounds of the barcode
        barcode_bounds = []
        for point in barcode.polygon:
            barcode_bounds.append([point.x, point.y])
        barcode_bounds = np.asarray(barcode_bounds, np.int32)

        # Layout of the panel by its version (digits 2-3 of the QR code), in mm, as in MicaSense's Panel.panel_corners():
        # s is half the side of the panel, p half the side of the QR code and T the centre of the panel from the centre of
        # the QR code. Only the inner part of the panel is measured: percent_inner_area of its side (MicaSense uses 0.5)
        if int(barcode_data[2:4]) < 3:
            s = 76.2                     # panel 152.4 mm wide, QR code 84 mm, panel 143.2 mm to the left of the QR code
            p = 42
            T = np.array([-143.2, 0])
        elif (int(barcode_data[2:4]) >= 3) and (int(barcode_data[2:4]) < 6):
            s = 50
            p = 45
            T = np.array([-145.8, 0])
        elif int(barcode_data[2:4]) >= 6:
            p = 41                       # panel 100 mm wide, QR code 82 mm, panel below the QR code
            s = 50
            T = np.array([0, -130.84])

        reference_panel_pts = np.asarray([[-s, s], [s, s], [s, -s], [-s, -s]], dtype=np.float32) * percent_inner_area + T
        reference_qr_pts    = np.asarray([[-p, p], [p, p], [p, -p], [-p, -p]], dtype=np.float32)
        bounds = []
        costs  = []

        # The panel region must be applied to the same geometry where the QR code was found: the undistorted
        # image, or the original image when the QR code was only found there
        if barcode_source == 'undistorted':
            panel_dn = image_undistorted
        else:
            panel_dn = self.image
        for rotation in range(0, 4):
            qr_points = np.roll(reference_qr_pts, rotation, axis=0)

            src = np.asarray([tuple(row) for row in qr_points[:]], np.float32)
            dst = np.asarray([tuple(row) for row in barcode_bounds[:]], np.float32)  

            # we determine the homography from the 4 corner points
            warp_matrix = cv2.getPerspectiveTransform(src, dst)

            pts = np.asarray([reference_panel_pts], 'float32')
            panel_bounds = cv2.convexHull(cv2.perspectiveTransform(pts, warp_matrix), clockwise=False)
            panel_bounds = np.squeeze(panel_bounds)  # remove nested lists

            # Keep the rotation with the most homogeneous region (lowest coefficient of variation)
            mean, std, _, _ = self.region_stats( region=panel_bounds,
                                                 sat_threshold=sat_threshold,
                                                 image=panel_dn )
            bounds.append( panel_bounds.astype(np.int32) )
            costs.append(std / mean)

        idx = costs.index(min(costs))
        panel_bounds = bounds[idx]

        # # plot image to check
        # fig, ax = plt.subplots()
        # ax.imshow( image_undistorted )
        # polygon = patches.Polygon(panel_bounds, closed=True, edgecolor='r', facecolor='none')
        # ax.add_patch(polygon)

        return panel_bounds, barcode_data, barcode_source


    # Statistics of a region drawn by hand, to give feedback while it is drawn
    def panel_region_stats(self, panel_bounds, sat_threshold=65000):
        """
        Inputs: polygon [[x, y], ...] in the undistorted image
        Outputs: dictionary with the number of pixels, the saturated pixels and the variation (std / mean, in %) of the
                 raw values inside; None if the polygon has no pixels
        """
        panel_bounds = np.asarray(panel_bounds, dtype=np.int32)
        mean, std, count, saturated = self.region_stats(panel_bounds, sat_threshold, image=self.undistorted_dn())
        if count == 0 or mean == 0:
            return None
        return {'n_pixels': int(count), 'n_saturated': int(saturated), 'variation_percent': float(100 * std / mean)}


    # Calibrate the panel from a region: radiance to reflectance factor
    def calibrate_panel_region(self, panel_bounds, geometry, reflectance_level_reference_panel, sat_threshold=65000,
                               audit_extra=None, validate=True):
        """
        Inputs: polygon [[x, y], ...], the geometry of the image where it is drawn ('undistorted' or 'original'), the
                reflectance of the panel by band and the saturation threshold
                validate: check the polygon (see validate_polygon); the regions found from the QR code are not checked
                because a panel close to the border of the image may be partly outside it
        Outputs: the radiance to reflectance factor (also in self.panel_audit, with audit_extra)
        """
        panel_bounds = np.asarray(panel_bounds, dtype=np.int32)
        if geometry == 'undistorted':
            panel_dn = self.undistorted_dn()
        elif geometry == 'original':
            panel_dn = self.image
        else:
            raise ValueError(f"unknown geometry {geometry!r}")
        if validate:
            validate_polygon(panel_bounds, panel_dn.shape[1::-1], self.image_path)

        ##
        # Calculate radiance for reference panel image
        image_panel_radiance = self.digital_numbers_to_radiance()
        if geometry == 'undistorted':
            image_panel_radiance = self.undistorted(image_panel_radiance)
        # plt.imshow( image_panel_radiance  )

        # Create an empty mask for panel region
        panel_mask = np.zeros_like(panel_dn, dtype=np.uint8)

        # Fill the polygon in the mask
        cv2.fillPoly(panel_mask, [panel_bounds], 255)
        # plt.imshow( panel_mask  )

        # Extract pixel values inside the polygon, leaving out saturated pixels
        inside_panel = panel_mask == 255
        saturated = inside_panel & (panel_dn > sat_threshold)
        panel_radiance = image_panel_radiance[ inside_panel & ~saturated ]
        if panel_radiance.size == 0:
            raise ValueError(f"{self.image_path}: every pixel in the reference panel region is saturated")
        masked_values_average = panel_radiance.mean()
        # print('Mean Radiance in panel region: {:1.4f} W/m^2/nm/sr'.format( masked_values_average ))

        # Specific calibrated reflectance level for the band - provided by MicaSense
        if self.band_name not in reflectance_level_reference_panel:
            raise KeyError(f"{self.image_path}: band '{self.band_name}' not in the reference panel reflectance levels "
                           f"{list(reflectance_level_reference_panel)}")
        panel_reflectance_level = reflectance_level_reference_panel[ self.band_name ]

        # Calculate radiance to reflectance conversion factor
        radiance_to_reflectance_factor = panel_reflectance_level / masked_values_average

        # Keep what was used, so the calibration can be audited (self.panel_audit)
        self.panel_audit = { 'image_path': str(self.image_path),
                             'band_name': self.band_name,
                             'geometry': geometry,
                             'panel_bounds': panel_bounds,
                             'sat_threshold': sat_threshold,
                             'n_pixels': int(inside_panel.sum()),
                             'n_saturated': int(saturated.sum()),
                             'radiance_mean': float(masked_values_average),
                             'radiance_std': float(panel_radiance.std()),
                             'reflectance_level': float(panel_reflectance_level),
                             # irradiance that the panel receives, from its radiance: E = pi * L / reflectance
                             'panel_irradiance_estimate': float(np.pi * masked_values_average / panel_reflectance_level),
                             'radiance_to_reflectance_factor': float(radiance_to_reflectance_factor) }
        self.panel_audit.update(audit_extra or {})
        return radiance_to_reflectance_factor


    # Detect the reference panel (or use a region drawn by hand) and return radiance to reflectance conversion factor
    def detect_reference_panel(self, reflectance_level_reference_panel, percent_inner_area, sat_threshold=65000,
                               manual_bounds=None, region_origin=None):
        """
        Inputs: reflectance of the panel by band, the inner fraction of the panel to measure, the saturation threshold
                and, optionally, a polygon [[x, y], ...] accepted or drawn by hand in the undistorted image (then no QR
                code is needed and percent_inner_area is not used) with where it came from (region_origin: 'edited' or
                'accepted_automatic', recorded in the audit)
        Outputs: panel bounds, radiance to reflectance factor, QR code data (None if manual) and where the region was
                 found ('undistorted', 'original' or 'manual')
        """
        if manual_bounds is not None:
            panel_bounds = np.asarray(manual_bounds, dtype=np.int32)
            barcode_data, barcode_source, geometry = None, 'manual', 'undistorted'
            region_source = 'manual'
        else:
            located = self.locate_reference_panel(percent_inner_area, sat_threshold)
            if located is None:
                raise ValueError(f"{self.image_path}: no QR code found for the reference panel "
                                 f"(band {self.band_name}), in either the undistorted or the original image")
            panel_bounds, barcode_data, barcode_source = located
            geometry, region_source = barcode_source, 'automatic'
        factor = self.calibrate_panel_region(panel_bounds, geometry, reflectance_level_reference_panel, sat_threshold,
                                             audit_extra={'region_source': region_source,
                                                          'barcode_data': barcode_data,
                                                          'barcode_source': barcode_source,
                                                          'percent_inner_area': percent_inner_area,
                                                          'region_origin': region_origin},
                                             validate=manual_bounds is not None)
        return panel_bounds, factor, barcode_data, barcode_source

