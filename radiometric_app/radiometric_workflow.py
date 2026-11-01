"""
Radiometric correction workflow for MicaSense RedEdge-P captures - configuration, validation and runner

This module has no user interface. Everything the workflow needs is in a flat configuration dictionary
(FIELDS describes each key, so that a window can be built from it) and the result is a dictionary that is also saved
as json. It can be used from Python or from the command line:

    python -m radiometric_app.radiometric_workflow --write-default-config config/radiometric_inputs.json
    python -m radiometric_app.radiometric_workflow --config config/radiometric_inputs.json
    python -m radiometric_app.radiometric_workflow --request-json request.json

The configuration keys and the stage identifiers in STAGES are stable: labels and descriptions may change, the keys
and identifiers may not, because saved inputs and results refer to them.
"""

# Standard Library Imports
import argparse
import json
import os
import statistics
import sys
import traceback
import warnings
from pathlib import Path
from types import SimpleNamespace

# Third-Party Imports
import numpy as np
import tifffile

# Local Imports
from . import environment, panel_spectrum
import radiometric_correction_tools as rct

REQUEST_VERSION = 1
REGIONS_VERSION = 1
RESULT_FILE_NAME = 'radiometric_result.json'
STAGES = ('LOAD_CAPTURES', 'PANEL_CALIBRATION', 'REFLECTANCE', 'DLS_CHECKS', 'PROVENANCE')

BAND_NAMES = ('Blue', 'Green', 'Panchro', 'Red', 'Red edge', 'NIR')

# Description of every configuration key: default value, kind of value (for validation and for the editor), label
# and description. Kinds: folder, folders (one path, a list of paths or a text with paths separated by ' | '), file, text,
# bool, float, int, choice (one of 'choices'), band_values (dictionary {band name: number}, with an optional 'range' (low
# exclusive, high inclusive)), capture_folders (dictionary {capture name: folder}). Optional 'group' is a heading for the
# window and 'dialog' marks the keys that the window edits in a dialog instead of on the page
# What the groups of fields of the window are about (the window shows them under the heading of the group)
GROUP_NOTES = {
    "Reflectance of the panel":
        "The reference panel reflects a known fraction of the light that reaches it in each band (for example "
        "0.478 in the green band: it reflects 47.8 % of it).\n"
        "That known value is what turns camera readings into reflectance: the reflectance of a pixel is its "
        "radiance divided by the radiance of the panel, multiplied by the reflectance of the panel in that "
        "band.\n"
        "The manufacturer provides the values with each panel, printed on it and as a spectral curve file. "
        "They differ a little between panels and change with age, so use the ones of your own panel.\n"
        "# Two ways to give it\n"
        "- Typed: one value per band, in the 'Panel reflectance' column of the table of the bands.\n"
        "- Computed: from the spectral curve of the panel (a .csv file), as the mean of the curve over the "
        "spectral window of each band.",
    "Correction with the DLS":
        "The panel photo sets the reflectance scale for the light at the moment it was taken. The DLS "
        "(downwelling light sensor) estimates the irradiance, the light that arrives from the sky on a horizontal "
        "surface, from its own sensor readings, so it shows how the light changed between the panel photo and "
        "each image.\n"
        "# The correction\n"
        "- For each band: DLS ratio = E_panel / E_image, with E read from XMP:HorizontalIrradiance of the "
        "panel capture and of the image.\n"
        "- Reflectance = reflectance from the panel x DLS ratio.\n"
        "- With two panel captures, each panel used gets its own ratio, and the panels are combined after that "
        "(see 'Panel selection approach').\n"
        "- Example: a ratio of 0.885 means that the light was 13 % stronger at the image than at the panel, "
        "so the reflectance is multiplied by 0.885.\n"
        "Only the metadata of the image and of the panel captures is used: nothing is averaged along the "
        "flight and no value is estimated from other bands. A missing irradiance in any band, Panchro "
        "included, is an error.",
    "Checks of the DLS data (limits)":
        "These checks look for DLS data that should not be trusted. They only raise flags: the correction is "
        "applied anyway and the images are written. The flags are shown in the log, and saved in the result "
        "file and in the provenance record of each capture, so that you can look at those captures.\n"
        "With two panel captures, an image is compared with the panel that weighs most in its calibration (the "
        "first one when both weigh the same).\n"
        "# What is checked\n"
        "- Band ratio: the DLS ratio of each band is compared with the median of the ratios of all the bands "
        "of the same capture. The light changes all the bands together, so they should agree.\n"
        "- Plausible range: a ratio far from 1 means that the light at the image was very different from the "
        "light at the panel.\n"
        "- Sun angle: the sun direction measured by the DLS against the solar zenith computed from "
        "XMP:SolarElevation; they differ when the DLS is tilted. It is checked in the panel capture and in "
        "the image capture.\n"
        "- Jump: change of the irradiance of a band between consecutive captures: a spike if it comes back, a "
        "step if it stays.\n"
        "- Panel drift: the panel captures before and after the flight against each other (only when there are "
        "both).",
    "Acquisition geometry (limits)":
        "These checks look at how the camera was pointed in each capture. They only raise flags: they never "
        "change the result.\n"
        "# What is measured\n"
        "- Camera tilt: the angle between the optical axis of the camera and the vertical, from XMP:Roll and "
        "XMP:Pitch. A camera that looks straight down has 0.\n"
        "- Heading and the sun: the angle between the heading of the camera, which is the direction of the "
        "top of its image (XMP:Yaw), and the direction of the sun (XMP:SolarAzimuth).\n"
        "# How to read the angle between the heading and the sun\n"
        "- 0 degrees: the camera faces the sun.\n"
        "- 90 degrees: the sun is at the side of the camera.\n"
        "- 180 degrees: the camera has its back to the sun.\n"
        "# What is expected\n"
        "- The reference panel: facing the sun or with the back to it, close to 0 or to 180.\n"
        "- The images to correct: with the sun at the side, close to 90, which lessens the directional "
        "effects of the surfaces.\n"
        "It is the heading of the camera that counts, not the direction of the flight line. If the aircraft "
        "keeps its heading during the flight, the angle is the same in every line. If its nose follows the "
        "lines of a crossed pattern, the angle of one set of lines differs by 90 degrees from the other: the "
        "sun cannot be at the side in both. Set the limit for that, or turn the pattern 45 degrees from the "
        "sun.",
}

FIELDS = [
    {'key': 'INPUTS.images_folder', 'group': 'Folders and tools', 'kind': 'folders', 'default': 'images/raw',
     'label': 'Images folder', 'help': ("Folder with the raw .tif images of the camera, or several folders.\n"
                  "The camera writes one file per band, for example IMG_0010_1.tif to IMG_0010_6.tif; the files of one "
                  "shot are a capture.\n"
                  "# Several folders\n"
                  "For example the flight in one folder and the panel captures in another. Their captures are read "
                  "together, as if they were in one folder; the subfolders are not read. Browse... builds the list: "
                  "add folders from any location, remove the ones not wanted, then Use selected folders. The paths can "
                  "also be typed, separated by ' | '.\n"
                  "# Repeated captures\n"
                  "A capture name that is in more than one folder (for example a panel capture copied to the folders "
                  "of two flights) is a repeated capture. Review repeated captures, in Browse..., shows the copies "
                  "with a picture and chooses the one to use, with all its bands; the other copies are left out. A "
                  "repeated name does not mean that the images are the same, so every repeated capture that is used "
                  "needs a choice. No file is moved or deleted.\n"
                  "A relative path is read from the project folder.")},
    {'key': 'INPUTS.repeated_capture_folders', 'group': 'Folders and tools', 'kind': 'capture_folders', 'default': {},
     'dialog': True, 'label': 'Copy used of each repeated capture',
     'help': ("{capture name: folder}: for a capture that is in more than one of the images folders, the folder of the "
              "copy that is used. Chosen with Review repeated captures (Browse... of the images folder). A choice for a "
              "capture that is no longer repeated is not used.")},
    {'key': 'INPUTS.results_folder', 'group': 'Folders and tools', 'kind': 'folder', 'default': 'results/reflectance',
     'label': 'Results folder', 'help': ("Folder where everything that the correction produces is written.\n"
                  "# What goes there\n"
                  "- the reflectance image of each band of each corrected capture, with the name of the raw file;\n"
                  "- a provenance record (.json) of each capture: what was used to produce it, including the panel "
                  "factors, the DLS ratios and the checks;\n"
                  "- radiometric_result.json, with the status, the outputs and the flags of the run;\n"
                  "- panel_regions.json, when the panel regions are checked and no other file was chosen.")},
    {'key': 'INPUTS.exiftool_path', 'group': 'Folders and tools', 'kind': 'file', 'default': '',
     'label': 'ExifTool executable',
     'help': ("ExifTool is a separate program (exiftool.org) that reads the metadata of the images: the bands, the "
                  "QR code of the panel and the DLS irradiance. It must be installed before the correction can run.\n"
                  "This field is only needed if the program is not found by itself. Empty: the environment variable "
                  "exiftoolpath is used, else the system PATH, so it is enough that 'exiftool -ver' works in a "
                  "terminal.\n"
                  "# On Windows\n"
                  "- Download the 'Windows Executable' from exiftool.org.\n"
                  "- Rename exiftool(-k).exe to exiftool.exe and keep it next to its exiftool_files folder.\n"
                  "- Choose it here, or add its folder to the PATH.\n"
                  "The Python package pyexiftool must be installed as well (pip install pyexiftool).\n"
                  "The window looks for both when it opens and each time this field changes, and says below what it found: "
                  "the program, how it was found, its version and whether the package is installed.")},
    {'key': 'INPUTS.output_product', 'group': 'Output', 'kind': 'choice',
     'choices': ['panel_reflectance', 'dls_reflectance', 'radiance'], 'default': 'panel_reflectance',
     'labels': {'panel_reflectance': 'Reflectance, calibrated with the reference panel',
                'dls_reflectance': 'Reflectance from the DLS only (no panel; less accurate)',
                'radiance': 'Radiance (no panel, no DLS)'},
     'label': 'What is written',
     'help': ("What the images of the results folder hold.\n"
                  "# Choices (the name that is saved is in brackets)\n"
                  "- Reflectance, calibrated with the reference panel (panel_reflectance): the panel sets the scale, and "
                  "the DLS optionally corrects the changes of the light (DLS page). The most accurate.\n"
                  "- Reflectance from the DLS only (dls_reflectance): pi x radiance / DLS irradiance, without a panel, "
                  "for flights without a photo of the panel. It relies on the absolute calibration of the DLS, which can "
                  "differ from the camera by tens of percent in some bands. When panel captures are given, they are not "
                  "used for the correction: the log says how far the DLS-only reflectance of the panel is from its known "
                  "reflectance. Images whose irradiance has no scale are read in uW/cm2/nm, as MicaSense does.\n"
                  "- Radiance (radiance): W/m2/sr/nm, from the metadata of each image alone, for example to calibrate it "
                  "later with targets on the ground. The panel and the DLS are not used.")},
    {'key': 'INPUTS.output_data_type', 'group': 'Output', 'kind': 'choice', 'choices': ['float32', 'float64'],
     'default': 'float32', 'labels': {'float32': '32-bit float (half the size)', 'float64': '64-bit float'},
     'label': 'Number format of the images',
     'help': ("How the values are stored in the images that are written.\n"
                  "- 32-bit float: about 7 significant digits, far more than the precision of the camera; the files are "
                  "half the size.\n"
                  "- 64-bit float: about 16 digits; the format of the versions before this option.")},
    {'key': 'INPUTS.reference_panel_capture', 'group': 'Reference panel', 'kind': 'text', 'default': 'IMG_0005',
     'label': 'Reference panel captures', 'help': ("All the photos of the reference panel of the flight, before and after it: choose them from the "
                  "captures of the images folder, or type their names separated by commas (for example IMG_0005, "
                  "IMG_0006, IMG_0422).\n"
                  "The program sorts them by their time: those taken before the first capture to correct are the "
                  "panel before the flight, and those taken after the last one are the panel after it. Several "
                  "photos of one moment are averaged, which lessens the effect of one bad photo; Check panel captures, "
                  "below, shows how much each one departs from the others.\n"
                  "The panel is photographed with its QR code in view. The program finds the QR code and, from it, the "
                  "region of the panel in each band; the regions can be checked, accepted or drawn by hand with Check / "
                  "draw panel regions (Reference panel page).\n"
                  "These captures also give the irradiance of the panel (E_panel) for the DLS correction.")},
    {'key': 'INPUTS.reference_panel_capture_after', 'group': 'Reference panel', 'kind': 'text', 'default': '',
     'dialog': True, 'label': 'Panel captures after the flight (earlier configurations)',
     'help': ("Kept for the configurations of earlier versions, which had a separate field for the panel after the "
                  "flight: its captures are the panel after the flight whatever their time. The UI moves them to "
                  "Reference panel captures, where the moments are found from the times.")},
    {'key': 'PANEL.panels_used', 'kind': 'choice', 'choices': list(rct.PANELS_USED), 'default': 'first',
     'labels': {'first': 'Before the flight only', 'last': 'After the flight only',
                'mean': 'Average of before and after', 'interpolate_time': 'Before and after, weighted by time',
                'nearest_time': 'Before or after, the nearer in time',
                'nearest_illumination': 'Before or after, the nearer in light (uses the DLS)',
                'interpolate_illumination': 'Before and after, weighted by light (uses the DLS)',
                'adjacent_time': 'Closest single capture before and after each image, by time (as Metashape)'},
     'group': 'Reference panel', 'label': 'Panel selection approach',
     'help': ("How the panel captures calibrate the images. The panel captures are those before the flight and those "
                  "after it (found from their times); the captures of one moment are averaged, except in the last "
                  "choice.\n"
                  "# Choices (the name that is saved is in brackets)\n"
                  "- Before the flight only (first): only the panel captures before the flight calibrate the images; "
                  "those after it are only compared with them.\n"
                  "- After the flight only (last): only the panel captures after the flight calibrate the images.\n"
                  "- Average of before and after (mean): the two moments weigh the same.\n"
                  "- Before and after, weighted by time (interpolate_time): each image is weighted by its time between "
                  "the two moments (times from EXIF:DateTimeOriginal).\n"
                  "- Before or after, the nearer in time (nearest_time): each image uses the moment closest in time to "
                  "it.\n"
                  "- Before or after, the nearer in light (nearest_illumination): each image uses the moment whose light "
                  "was the most like its own, compared with the DLS irradiance of all the bands (their level and how it "
                  "is spread over the bands).\n"
                  "- Before and after, weighted by light (interpolate_illumination): the same comparison, but both "
                  "moments are used, each weighted by 1 / (difference of the light) squared: a moment with the light of "
                  "the image counts alone, and the weight changes smoothly with the light, so there is no step between "
                  "neighboring images that would use different moments.\n"
                  "The two choices by the light use the DLS data of the images and of the panel captures: choosing one "
                  "turns on 'Correct illumination changes with the DLS' (DLS page), and turning that off takes the choice "
                  "back to 'Before the flight only'.\n"
                  "- Closest single capture before and after each image, by time (adjacent_time): of all the panel "
                  "captures, only the last one taken before the image and the first one after it are used, weighted by "
                  "time, without averaging; an image before the first or after the last uses the closest. This is what "
                  "Metashape 2.3.1 does.\n"
                  "# Panel captures of one moment only\n"
                  "When all the panel captures are of one moment (all before the flight, for example), there is no "
                  "other moment to choose or weigh, so every choice calibrates all the images with that moment: the "
                  "mean of its captures, band by band, each capture with the same weight (with the DLS correction, each "
                  "one first adjusted to the light of the image). The exception is 'Closest single capture' "
                  "(adjacent_time): each image uses only the capture of that moment taken closest in time to it, "
                  "without averaging. The run says in a flag that only one moment was used.\n"
                  "This choice changes the result: it decides which panel data are used.\n"
                  "With the DLS correction on, each panel capture is first adjusted to the light of the image with its own "
                  "DLS reading (panel factor x E_panel / E_image), and then they are combined.")},
    {'key': 'PANEL.panel_orientation', 'kind': 'text', 'default': 'all', 'group': 'Reference panel',
     'label': 'Panel captures used, by orientation',
     'help': ("The panel can be photographed with the camera facing the sun, with its back to it or with the sun at its "
                  "side. The panel reflects almost equally in every direction (close to a Lambertian surface), so "
                  "turning the camera alone should change little, but the orientations can still give different "
                  "factors: the shadow of the operator or of the aircraft, light reflected onto the panel by the "
                  "operator, a panel or camera that is not level, and, with the DLS correction, a DLS that faces the "
                  "sun differently.\n"
                  "The orientation of each panel capture is read from its metadata: the heading of the camera "
                  "(XMP:Yaw) against the direction of the sun (XMP:SolarAzimuth), 0 degrees facing the sun and 180 with "
                  "the back to it. The captures whose headings are within 20 degrees of each other are one orientation, "
                  "and the list offers the orientations found among the panel captures.\n"
                  "- All the panel captures (all): every capture listed is used. When one moment has captures of "
                  "several orientations and they differ more than the spread limit (set in Check panel captures), a "
                  "flag says so.\n"
                  "- One orientation (its heading is saved, for example 30): only the captures within 20 degrees of it "
                  "are used, so that one protocol is followed.\n"
                  "The heading is only as good as XMP:Yaw: check it in Check panel captures.")},
    {'key': 'INPUTS.image_captures', 'group': 'Images to correct', 'kind': 'text', 'default': '',
     'label': 'Select captures',
     'help': ("The captures to correct: type their names separated by commas, or choose them from the images folder "
                  "with Select...\n"
                  "Empty: all the captures of the folder except the panel captures and the captures to leave out.")},
    {'key': 'INPUTS.excluded_captures', 'group': 'Images to correct', 'kind': 'text', 'default': '',
     'label': 'Captures to leave out',
     'help': ("Captures of the images folder that must not be corrected, separated by commas, or chosen with Select...: "
                  "for example other photos of the panel that are not used, or photos taken on the ground before the "
                  "take-off.\n"
                  "The panel captures are always left out, so they do not need to be listed here.")},
    {'key': 'INPUTS.file_pattern', 'group': 'Images to correct', 'kind': 'text', 'default': rct.CAPTURE_FILE_PATTERN,
     'label': 'File name pattern',
     'help': ("How the program tells which files belong to the same capture. The camera writes one file per band "
                  "(IMG_0005_1.tif ... IMG_0005_6.tif).\n"
                  "# Writing the pattern\n"
                  "- {capture}: the name of the capture, the part shared by the files of its bands.\n"
                  "- {index}: the number of the file in the capture (optional).\n"
                  "- *: any other text.\n"
                  "For example {capture}_{index}.tif. The match is not case sensitive. The band of a file is read from "
                  "its metadata, never from its name.\n"
                  "Change it only if your files are named differently; Suggest from files writes it from the names in "
                  "the folder. A regular expression with a group named capture also works.")},
    {'key': 'INPUTS.ignored_bands', 'kind': 'text', 'default': '',
     'label': 'Bands to ignore',
     'help': ("Names (as written by the camera, XMP:BandName) separated by commas, of bands that are in the files "
                  "but must not be processed, for example a thermal band.")},
    {'key': 'PANEL.reflectance_source', 'kind': 'choice', 'choices': ['values', 'spectral_file'],
     'labels': {'values': 'Values typed in the table of the bands', 'spectral_file': 'Spectral response file (.csv)'},
     'default': 'values', 'group': 'Reflectance of the panel',
     'label': 'Source of the panel reflectance',
     'help': ("Where the reflectance of the panel in each band comes from.\n"
                  "- Values typed in the table of the bands: the 'Panel reflectance' column of the table above. The "
                  "spectral file is not used.\n"
                  "- Spectral response file: computed from the .csv file of the panel, below. The reflectance column of "
                  "the table is not used.\n"
                  "The spectral file reproduces the printed values within about 0.3 %. Use it when you have the file of "
                  "your panel.")},
    {'key': 'PANEL.reflectance_by_band', 'kind': 'band_values', 'group': 'Reflectance of the panel', 'range': (0, 1),
     'default': {'Blue': 0.477, 'Green': 0.478, 'Panchro': 0.479, 'Red': 0.478, 'Red edge': 0.478, 'NIR': 0.477},
     'label': 'Panel reflectance by band', 'help': ("Values provided by the manufacturer with the reference panel. The band names must be those written "
                  "by the camera (XMP:BandName).")},
    {'key': 'PANEL.spectral_file', 'kind': 'file', 'default': '', 'group': 'Reflectance of the panel',
     'label': 'Spectral response file',
     'help': ("The spectral response of the panel: its reflectance at each wavelength, in a .csv file provided by "
                  "the manufacturer.\n"
                  "Browse... chooses the file and opens a window with a preview and a plot, where its format is detected "
                  "and can be changed (delimiter, header rows, units, ...); nothing changes until the file is accepted "
                  "there. Preview and format... opens the same window for the file that is already chosen.\n"
                  "The reflectance of a band is the mean of the curve over its spectral window: the center of the band "
                  "plus or minus half of its bandwidth, from the table of the bands.")},
    {'key': 'PANEL.spectral_delimiter', 'kind': 'choice', 'choices': list(panel_spectrum.DELIMITERS), 'dialog': True,
     'default': 'auto', 'group': 'Reflectance of the panel', 'label': 'Delimiter',
     'help': 'Character that separates the columns; auto detects it.'},
    {'key': 'PANEL.spectral_delimiter_char', 'kind': 'text', 'default': '', 'dialog': True,
     'group': 'Reflectance of the panel', 'label': 'Custom delimiter',
     'help': "The separator when the delimiter is custom (one or more characters)."},
    {'key': 'PANEL.spectral_skip_rows', 'kind': 'text', 'default': 'auto', 'dialog': True,
     'group': 'Reflectance of the panel', 'label': 'Header rows to skip',
     'help': "auto: the rows at the start that are not numbers; or the number of rows."},
    {'key': 'PANEL.spectral_skip_footer', 'kind': 'int', 'default': 0, 'dialog': True,
     'group': 'Reflectance of the panel', 'label': 'Footer rows to skip',
     'help': "Number of rows at the end of the file that are not data."},
    {'key': 'PANEL.spectral_comment_prefix', 'kind': 'text', 'default': '', 'dialog': True,
     'group': 'Reflectance of the panel', 'label': 'Comment prefix',
     'help': "Lines that start with this text are ignored, for example #."},
    {'key': 'PANEL.spectral_exclude_rows', 'kind': 'text', 'default': '', 'dialog': True,
     'group': 'Reflectance of the panel', 'label': 'Lines to exclude',
     'help': "Line numbers of the file that are not used, for example 5, 10-12."},
    {'key': 'PANEL.spectral_wavelength_min', 'kind': 'text', 'default': '', 'dialog': True,
     'group': 'Reflectance of the panel', 'label': 'Smallest wavelength used (nm)',
     'help': "Empty: no limit."},
    {'key': 'PANEL.spectral_wavelength_max', 'kind': 'text', 'default': '', 'dialog': True,
     'group': 'Reflectance of the panel', 'label': 'Largest wavelength used (nm)',
     'help': "Empty: no limit."},
    {'key': 'PANEL.spectral_reflectance_scale', 'kind': 'float', 'default': 1.0, 'dialog': True,
     'group': 'Reflectance of the panel', 'label': 'Reflectance multiplier',
     'help': "Applied after the reflectance unit, for example 0.001 for per mille."},
    {'key': 'PANEL.spectral_wavelength_column', 'kind': 'int', 'default': 1, 'dialog': True,
     'group': 'Reflectance of the panel', 'label': 'Wavelength column', 'help': 'Column number, from 1.'},
    {'key': 'PANEL.spectral_reflectance_column', 'kind': 'int', 'default': 2, 'dialog': True,
     'group': 'Reflectance of the panel', 'label': 'Reflectance column', 'help': 'Column number, from 1.'},
    {'key': 'PANEL.spectral_wavelength_unit', 'kind': 'choice', 'choices': list(panel_spectrum.WAVELENGTH_UNITS),
     'default': 'nm', 'dialog': True, 'group': 'Reflectance of the panel', 'label': 'Wavelength unit',
     'help': 'nm or um (micrometers).'},
    {'key': 'PANEL.spectral_reflectance_unit', 'kind': 'choice', 'choices': list(panel_spectrum.REFLECTANCE_UNITS),
     'default': 'fraction', 'dialog': True, 'group': 'Reflectance of the panel', 'label': 'Reflectance unit',
     'help': 'fraction (0 to 1) or percent.'},
    {'key': 'PANEL.spectral_decimal', 'kind': 'choice', 'choices': list(panel_spectrum.DECIMAL_MARKS), 'default': '.',
     'dialog': True, 'group': 'Reflectance of the panel', 'label': 'Decimal mark', 'help': 'Point or comma.'},
    {'key': 'PANEL.band_center_wavelengths', 'kind': 'band_values', 'range': (0, 5000), 'dialog': True,
     'default': {band: window[0] for band, window in panel_spectrum.REDEDGE_P_BANDS.items()},
     'group': 'Reflectance of the panel', 'label': 'Center wavelength of each band (nm)',
     'help': 'Spectral window of each band: the reflectance of a band is the mean of the curve over '
             'center +/- bandwidth / 2.'},
    {'key': 'PANEL.band_bandwidths', 'kind': 'band_values', 'range': (0, 5000), 'dialog': True,
     'default': {band: window[1] for band, window in panel_spectrum.REDEDGE_P_BANDS.items()},
     'group': 'Reflectance of the panel', 'label': 'Bandwidth of each band (nm)',
     'help': 'Width of the spectral window of each band.'},
    {'key': 'PANEL.spectral_min_coverage', 'kind': 'float', 'default': 0.5, 'dialog': True,
     'group': 'Reflectance of the panel', 'label': 'Smallest covered part of a band window',
     'help': 'Fraction (0 to 1] of the window of a band that must have valid samples in the file.'},
    {'key': 'PANEL.percent_inner_area', 'kind': 'float', 'default': 0.7, 'group': 'Panel measurement',
     'label': 'Inner area of the panel', 'help': ("The size of the square that is measured on the panel, as a fraction of the "
                  "side of the panel: above 0 and up to 1. With 0.7 it is a square in the middle of the panel with 70 % "
                  "of its width and of its height (49 % of its area); with 1 it is about the whole panel.\n"
                  "# Why not all of it\n"
                  "The edge of the panel can include its border, a shadow or the ground, which would change the radiance "
                  "that is measured. A smaller square in the middle avoids that, as long as it still has enough pixels.\n"
                  "# Where it is used\n"
                  "- The region that is found from the QR code is drawn with this size. A band that has no accepted region "
                  "is measured on it.\n"
                  "- A band whose region was accepted or drawn in Check / draw panel regions is measured on that region, "
                  "exactly as it was saved: this value does not change it.\n"
                  "- The tool has the same value: change it there to see the region that it gives before you accept it.")},
    {'key': 'PANEL.saturation_threshold', 'kind': 'int', 'default': 65000, 'group': 'Panel measurement',
     'label': 'Saturation threshold', 'help': ("Pixels above this value are left out of the panel radiance: they are saturated and do not measure "
                  "the light correctly.\n"
                  "The raw values of the camera go up to 65535 (16 bits); 65000 leaves out the pixels at the limit.")},
    {'key': 'PANEL.manual_regions_file', 'kind': 'file', 'default': '', 'group': 'Reference panel regions',
     'label': 'Panel regions drawn by hand (optional)',
     'help': ("File (.json) with the panel regions that were accepted or drawn in Check / draw panel regions. "
                  "Empty: panel_regions.json in the results folder.\n"
                  "The file does not have to exist yet: Browse... lets you choose the folder and name a new one, and the "
                  "tool creates it. While the file is the panel_regions.json of the results folder it follows that folder "
                  "when you change it; a file of another name or folder stays where you put it.\n"
                  "A band with a region in the file uses it instead of the region found from the QR code; the bands "
                  "without one are detected automatically.\n"
                  "The buttons below check the inputs and open the tool that accepts or draws the regions.")},
    {'key': 'CORRECTION.use_dls', 'group': 'Correction with the DLS', 'kind': 'bool', 'default': False,
     'label': 'Correct illumination changes with the DLS',
     'help': ("Multiply the reflectance of each band (Panchro included) by the DLS ratio E_panel / E_image of the "
                  "panel that calibrates it.\n"
                  "Off: only the panel is used, so changes of the light between the panel photo and an image are not "
                  "corrected.\n"
                  "On: every band of the image and of the panel captures used needs a valid irradiance "
                  "(XMP:HorizontalIrradiance); otherwise the run stops with an error.")},
    {'key': 'CHECKS.band_ratio_tolerance', 'group': 'Checks of the DLS data (limits)', 'kind': 'float', 'default': rct.DLS_CHECKS['band_ratio_tolerance'],
     'label': 'Band ratio tolerance', 'help': ("Largest departure accepted (0.03 = 3 %) of the DLS ratio of a band from the median of the ratios of "
                  "all the bands of the same capture.\n"
                  "The light changes all the bands together, so within one capture the ratio E_panel / E_image should "
                  "be almost the same in every band. A band that departs more than this points to a problem with the "
                  "irradiance of that band in that capture.")},
    {'key': 'CHECKS.ratio_range_min', 'group': 'Checks of the DLS data (limits)', 'kind': 'float', 'default': rct.DLS_CHECKS['ratio_range'][0],
     'label': 'Smallest plausible DLS ratio', 'help': ("Ratios below this are flagged. A ratio of 0.5 means that the DLS estimated twice as much light at the "
                  "image as at the panel.\n"
                  "That is hardly credible within one flight. It happens, for example, if the panel was photographed in "
                  "shade or under cloud and the image was taken in full sun, or if the irradiance of the image is "
                  "wrong.\n"
                  "The correction is still applied; the flag asks you to look at that capture.")},
    {'key': 'CHECKS.ratio_range_max', 'group': 'Checks of the DLS data (limits)', 'kind': 'float', 'default': rct.DLS_CHECKS['ratio_range'][1],
     'label': 'Largest plausible DLS ratio', 'help': ("Ratios above this are flagged. A ratio of 2 means that the DLS estimated half as much light at the "
                  "image as at the panel.\n"
                  "It is credible when the panel was photographed in full sun and the image was taken under heavy cloud "
                  "or in shade. It also happens when the DLS was covered or pointing away from the sun at that moment, "
                  "and then the irradiance of the image is wrong.\n"
                  "The correction is still applied; the flag asks you to look at that capture.")},
    {'key': 'CHECKS.max_sun_angle_discrepancy_deg', 'group': 'Checks of the DLS data (limits)', 'kind': 'float',
     'default': rct.DLS_CHECKS['max_sun_angle_discrepancy_deg'],
     'label': 'Largest sun angle discrepancy (deg)',
     'help': ("Largest difference accepted, in degrees, between two measurements of the angle of the sun from the "
                  "vertical (the solar zenith).\n"
                  "# How it works\n"
                  "- The DLS measures where the direct sunlight comes from, in its own frame "
                  "(XMP:EstimatedDirectLightVector). If the DLS is level, the angle between that direction and the "
                  "vertical is the solar zenith.\n"
                  "- The camera also writes the solar elevation (XMP:SolarElevation). The solar zenith is 90 degrees "
                  "minus it, and it does not depend on the DLS.\n"
                  "- The two agree when the DLS is level. If the aircraft was rolled or pitched, the DLS was tilted and "
                  "they differ: the tilt is at least as large as the difference.\n"
                  "A tilted DLS measures the irradiance on a tilted surface instead of a horizontal one, so the DLS "
                  "ratio of that capture is less reliable. The check is made on the panel capture and on the image "
                  "capture.")},
    {'key': 'CHECKS.jump_threshold', 'group': 'Checks of the DLS data (limits)', 'kind': 'float', 'default': rct.DLS_CHECKS['jump_threshold'],
     'label': 'Irradiance jump threshold', 'help': ("Change (0.1 = 10 %) of the irradiance of a band between consecutive captures that is reported.\n"
                  "- A spike: the irradiance jumps and comes back, for example a shaded or tilted DLS in one capture.\n"
                  "- A step: the change stays, for example the sun going behind a cloud.")},
    {'key': 'CHECKS.panel_drift_tolerance', 'group': 'Checks of the DLS data (limits)', 'kind': 'float', 'default': rct.DLS_CHECKS['panel_drift_tolerance'],
     'label': 'Panel drift tolerance', 'help': ("Compares the two panel captures, band by band. Largest change accepted (0.05 = 5 %) in each of "
                  "these:\n"
                  "- The panel factor: the relation between the reflectance of the panel and what the camera read from "
                  "it. It changes if the light was not the same in the two photos, or if the panel was shaded, dirty or "
                  "held differently.\n"
                  "- The agreement between the DLS and the camera: the change of the DLS irradiance between the two "
                  "photos, divided by the change of the irradiance that the camera sees on the panel. It is 1 if the "
                  "DLS followed the light that the panel received, and it is not when the DLS was tilted or shaded in "
                  "one of the photos.\n"
                  "Only used with panel captures before and after the flight. A flag does not change the result; whether "
                  "those after the flight also calibrate the images is chosen in 'Panel selection approach'.\n"
                  "With several captures before or after the flight, their means are compared.")},
    {'key': 'CHECKS.panel_capture_spread_tolerance', 'group': 'Checks of the DLS data (limits)', 'kind': 'float',
     'default': 0.02, 'dialog': True, 'label': 'Spread of the panel captures of one moment',
     'help': ("When several captures of the panel are taken one after the other (before or after the flight), they are "
                  "averaged. Largest spread accepted (0.02 = 2 %, standard deviation / mean) of what is averaged, band by "
                  "band:\n"
                  "- without the DLS correction, the panel factor: it changes if the light changed between the photos, "
                  "or if the panel was shaded or held differently in one of them;\n"
                  "- with the DLS correction, the panel factor x the DLS irradiance, which does not depend on the light: "
                  "it changes when the DLS was shaded or tilted in one of the photos.\n"
                  "A flag does not change the result: leave out the captures that disagree, or use the panel captures "
                  "of the other moment.")},
    {'key': 'CHECKS.panel_illumination_tolerance', 'group': 'Checks of the DLS data (limits)', 'kind': 'float',
     'default': rct.DLS_CHECKS['panel_illumination_tolerance'], 'label': 'Light of the panel: largest difference',
     'help': ("Only used when the panels are chosen by their light (nearest_illumination or interpolate_illumination). "
                  "Largest difference accepted (0.15 = about 15 %) between the DLS irradiance of an image and that of the "
                  "panel that is the most like it. A capture beyond it is flagged: no panel was photographed in light "
                  "like its own, and another panel capture under that light would help. A flag does not change the "
                  "result.\n"
                  "It is not the limit for the panel captures of one moment (before or after the flight), which are "
                  "compared with each other with a smaller limit (2 %), set in Check panel captures (Inputs page).")},
    {'key': 'CHECKS.max_camera_tilt_deg', 'group': 'Acquisition geometry (limits)', 'kind': 'float', 'default': rct.GEOMETRY_CHECKS['max_camera_tilt_deg'],
     'label': 'Largest camera tilt (deg)',
     'help': ("Largest angle accepted between the optical axis of the camera and the vertical, in degrees, in the "
                  "panel capture and in each image capture. It is worked out from XMP:Roll and XMP:Pitch.\n"
                  "A camera that does not look straight down sees the ground at an angle, and surfaces that are not "
                  "diffuse (soil, leaves) look different from it.")},
    {'key': 'CHECKS.panel_heading_tolerance_deg', 'group': 'Acquisition geometry (limits)', 'kind': 'float', 'default': rct.GEOMETRY_CHECKS['panel_heading_tolerance_deg'],
     'label': 'Panel: largest angle from facing the sun or away (deg)',
     'help': ("How far from the line of the sun the heading of the camera may be in a panel capture, in degrees. "
                  "The angle between the heading and the sun is 0 when the camera faces the sun and 180 when it has its "
                  "back to it, and the check accepts the angles within this tolerance of 0 or of 180.\n"
                  "With 30, the angles from 0 to 30 and from 150 to 180 are accepted.")},
    {'key': 'CHECKS.image_heading_tolerance_deg', 'group': 'Acquisition geometry (limits)', 'kind': 'float', 'default': rct.GEOMETRY_CHECKS['image_heading_tolerance_deg'],
     'label': 'Images: largest angle from the sun at the side (deg)',
     'help': ("How far from 90 degrees the angle between the heading of the camera and the sun may be in the images "
                  "to correct, in degrees. 90 is the sun at the side of the camera.\n"
                  "With 30, the angles from 60 to 120 are accepted.")},
    {'key': 'CHECKS.heading_offset_deg', 'group': 'Acquisition geometry (limits)', 'kind': 'float', 'default': rct.GEOMETRY_CHECKS['heading_offset_deg'],
     'label': 'Correction of the yaw (deg)',
     'help': ("Added to XMP:Yaw before it is compared with the direction of the sun, in degrees.\n"
                  "The direction of the sun is measured from true north. If the yaw of your camera is measured from "
                  "magnetic north, put here the magnetic declination of the place (east is positive). If you do not "
                  "know it, leave 0 and use wide limits.")},
]

FIELD_KEYS = [field['key'] for field in FIELDS]
FIELD_BY_KEY = {field['key']: field for field in FIELDS}


class ConfigError(ValueError):
    """ Raised when the configuration is not valid """


def default_config():
    """ A copy of the default value of every key """
    return json.loads(json.dumps({field['key']: field['default'] for field in FIELDS}))


def configuration_from_json(data):
    """
    The values of a configuration in what a json file holds: the values themselves (a configuration or a profile) or what a
    run wrote with them: radiometric_result.json ('config') or the provenance record of a capture (<capture>_provenance.json,
    'parameters' / 'config'), so that the settings of a past run can be used again
    """
    if isinstance(data, dict):
        if isinstance(data.get('config'), dict):
            return data['config']
        parameters = data.get('parameters')
        if isinstance(parameters, dict) and isinstance(parameters.get('config'), dict):
            return parameters['config']
    return data


def load_config(path):
    """
    Default configuration with the values of a json file on top (a configuration, a profile, or the result or provenance file
    of a run: see configuration_from_json); unknown keys are an error
    """
    saved = configuration_from_json(json.loads(Path(path).read_text(encoding='utf-8')))
    return merge_config(saved)


def merge_config(values):
    if not isinstance(values, dict):
        raise ConfigError("A configuration is a json object with the keys of the configuration")
    unknown = sorted(set(values) - set(FIELD_KEYS))
    if unknown:
        raise ConfigError(f"Unknown configuration keys: {unknown}")
    config = default_config()
    config.update(values)
    return config


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) != float('inf')


def resolve_path(root, value):
    path = Path(value)
    return path if path.is_absolute() else Path(root) / path


def images_source(config, root='.'):
    """
    The images folder(s) of the configuration (INPUTS.images_folder, one or several) with the copy chosen for each repeated
    capture (INPUTS.repeated_capture_folders), as rct.ImageFolders: what the functions that read captures take
    """
    folders = [resolve_path(root, folder) for folder in rct.folder_list(config['INPUTS.images_folder'])]
    chosen = {name: resolve_path(root, folder) for name, folder in config['INPUTS.repeated_capture_folders'].items()}
    return rct.ImageFolders(folders, chosen)


CONFIG_FOLDER = environment.CONFIG_FOLDER                 # in the project folder: the configuration, the profiles, the own presets
DEFAULT_CONFIG_NAME = environment.DEFAULT_CONFIG_NAME


def default_config_path(root='.'):
    """
    The configuration file that the window and the tools use when none is given: config/radiometric_inputs.json in the
    project folder; the file that earlier versions kept in the project folder itself is used while the new one does not exist
    """
    root = Path(root)
    path = root / CONFIG_FOLDER / DEFAULT_CONFIG_NAME
    legacy = root / DEFAULT_CONFIG_NAME
    return legacy if not path.is_file() and legacy.is_file() else path


def exiftool_executable(config, root='.'):
    """ ExifTool to use: the configured file, else the environment variable exiftoolpath, else None (system PATH) """
    if config['INPUTS.exiftool_path']:
        return str(resolve_path(root, config['INPUTS.exiftool_path']))
    return os.environ.get('exiftoolpath')


def find_exiftool(config, root='.'):
    """
    The ExifTool that will be used and where it was looked for, in this order: INPUTS.exiftool_path, the environment
    variable exiftoolpath, the system PATH. Returns (path, source); the path is None if it is not there
    """
    return environment.locate_exiftool(config['INPUTS.exiftool_path'], root)


exiftool_version = environment.exiftool_version               # the version that 'exiftool -ver' prints, None if it cannot be run
pyexiftool_installed = environment.pyexiftool_installed       # whether the Python package PyExifTool (import exiftool) is there


def read_capture_images(config, root, capture, exiftool_path=None):
    """ {band: rct.Image} of one capture, with the file pattern, the bands and the ignored bands of the configuration """
    images_folder = images_source(config, root)
    if exiftool_path is None:
        exiftool_path = exiftool_executable(config, root)
    return _read_capture(images_folder, capture, panel_bands(config), exiftool_path,
                         config['INPUTS.file_pattern'], capture_names(config['INPUTS.ignored_bands']))


def detect_bands(config, root, capture=None):
    """
    Read the bands from the metadata of the files of a capture (default: the panel capture)
    Outputs: the dictionary of rct.read_band_definitions
    """
    images_folder = images_source(config, root)
    capture = capture or (panel_capture_groups(config)[0] or [''])[0]
    paths = rct.find_capture_paths(images_folder, capture, config['INPUTS.file_pattern'])
    return rct.read_band_definitions(paths, exiftool_executable(config, root))


def capture_names(text):
    """ Names separated by commas -> list """
    return [name.strip() for name in str(text).split(',') if name.strip()]


def panel_capture_groups(config):
    """
    The panel captures before and after the flight: two lists of names (INPUTS.reference_panel_capture and
    INPUTS.reference_panel_capture_after, each one capture or several separated by commas; the second may be empty)
    """
    return (list(dict.fromkeys(capture_names(config['INPUTS.reference_panel_capture']))),
            list(dict.fromkeys(capture_names(config['INPUTS.reference_panel_capture_after']))))


def spectral_format(config):
    """ Format of the spectral response file from the configuration; ConfigError if it is not valid """
    skip = config['PANEL.spectral_skip_rows'].strip().lower()
    try:
        skip = 'auto' if skip == 'auto' else int(skip)
    except ValueError:
        raise ConfigError("PANEL.spectral_skip_rows must be 'auto' or a whole number")
    limits = []
    for key in ('PANEL.spectral_wavelength_min', 'PANEL.spectral_wavelength_max'):
        text = config[key].strip()
        try:
            limits.append(float(text) if text else None)
        except ValueError:
            raise ConfigError(f"{key} must be a number or empty")
    fmt = {'delimiter': config['PANEL.spectral_delimiter'], 'delimiter_char': config['PANEL.spectral_delimiter_char'],
           'skip_rows': skip, 'skip_footer': int(config['PANEL.spectral_skip_footer']),
           'comment_prefix': config['PANEL.spectral_comment_prefix'], 'exclude_rows': config['PANEL.spectral_exclude_rows'],
           'wavelength_min': limits[0], 'wavelength_max': limits[1],
           'reflectance_scale': float(config['PANEL.spectral_reflectance_scale']),
           'wavelength_column': int(config['PANEL.spectral_wavelength_column']),
           'reflectance_column': int(config['PANEL.spectral_reflectance_column']),
           'wavelength_unit': config['PANEL.spectral_wavelength_unit'],
           'reflectance_unit': config['PANEL.spectral_reflectance_unit'], 'decimal': config['PANEL.spectral_decimal']}
    problems = panel_spectrum.validate_format(fmt)
    if problems:
        raise ConfigError('Spectral response format: ' + '; '.join(problems))
    return fmt


def panel_bands(config):
    """ Names of the bands that are processed: those of the typed reflectances, or those that have a spectral window """
    if config['PANEL.reflectance_source'] == 'spectral_file':
        return list(config['PANEL.band_center_wavelengths'])
    return list(config['PANEL.reflectance_by_band'])


def panel_reflectance_levels(config, root='.'):
    """
    Reflectance of the panel by band
    Outputs: ({band: reflectance}, information). The values are the typed ones, or the mean of the spectral response
             file over the window of each band (the information then has the file, its hash, the format and, for each
             band, the window, the samples and the covered part of the window)
    """
    if config['PANEL.reflectance_source'] == 'values':
        return dict(config['PANEL.reflectance_by_band']), {'source': 'values'}
    fmt = spectral_format(config)
    path = resolve_path(root, config['PANEL.spectral_file'])
    spectrum = panel_spectrum.load_spectrum(path, fmt)
    windows = {band: (center, config['PANEL.band_bandwidths'][band])
               for band, center in config['PANEL.band_center_wavelengths'].items()}
    stats = panel_spectrum.band_reflectances(spectrum, windows, config['PANEL.spectral_min_coverage'])
    info = {'source': 'spectral_file', 'file': spectrum['file'], 'sha256': spectrum['sha256'], 'format': fmt,
            'min_coverage': config['PANEL.spectral_min_coverage'], 'warnings': spectrum['warnings'], 'bands': stats}
    return {band: values['reflectance'] for band, values in stats.items()}, info


def load_panel_regions(path):
    """
    Read the regions of the reference panel drawn by hand
    Outputs: {capture name: {band name: [[x, y], ...]}}, the vertices being pixels of the undistorted image
    """
    saved = json.loads(Path(path).read_text(encoding='utf-8'))
    if saved.get('version') != REGIONS_VERSION or saved.get('geometry') != 'undistorted':
        raise ConfigError(f"{path}: not a panel regions file (version {REGIONS_VERSION}, undistorted geometry)")
    regions = saved.get('captures')
    if not isinstance(regions, dict) or not all(isinstance(bands, dict) for bands in regions.values()):
        raise ConfigError(f"{path}: 'captures' must be {{capture: {{band: [[x, y], ...]}}}}")
    return regions


def load_panel_origins(path):
    """
    Where each region of the file came from: {capture name: {band name: 'accepted_automatic' or 'edited'}}.
    'accepted_automatic' is a region found from the QR code that the user accepted as it was; files that do not say it
    give an empty dictionary
    """
    saved = json.loads(Path(path).read_text(encoding='utf-8'))
    origins = saved.get('origins', {})
    return origins if isinstance(origins, dict) else {}


def save_panel_regions(path, capture, regions, origins=None):
    """
    Save the regions of one capture ({band: [[x, y], ...]}, None or missing bands are not saved) and, optionally, where
    each came from ({band: 'accepted_automatic' or 'edited'}), keeping the other captures of the file. The file is
    replaced atomically
    """
    path = Path(path)
    captures = load_panel_regions(path) if path.is_file() else {}
    all_origins = load_panel_origins(path) if path.is_file() else {}
    kept = {band: [[int(round(x)), int(round(y))] for x, y in polygon]
            for band, polygon in regions.items() if polygon is not None}
    if kept:
        captures[capture] = kept
        known = {band: origin for band, origin in (origins or {}).items() if band in kept and origin}
        if known:
            all_origins[capture] = known
        else:
            all_origins.pop(capture, None)
    else:
        captures.pop(capture, None)
        all_origins.pop(capture, None)
    content = {'version': REGIONS_VERSION, 'geometry': 'undistorted', 'captures': captures}
    if all_origins:
        content['origins'] = all_origins
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(content, indent=2), encoding='utf-8')
    os.replace(temporary, path)
    return path


# Windows refuses paths of 260 characters or more unless long paths are turned on; the longest file of the results folder
# is about this long (a capture name, '_provenance.json', and the temporary radiometric_result.json.tmp)
WINDOWS_PATH_LIMIT = 259
RESULT_NAME_ALLOWANCE = 40


def windows_long_paths_enabled():
    """ Whether Windows accepts paths longer than 260 characters (LongPathsEnabled in the registry); True elsewhere """
    if os.name != 'nt':
        return True
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\FileSystem') as key:
            return winreg.QueryValueEx(key, 'LongPathsEnabled')[0] == 1
    except OSError:
        return False


def path_length_problems(results_folder):
    """
    A problem when the files of the results folder would have paths too long for Windows. The path is measured as it is
    written (made absolute, not resolved): Windows applies the limit to it, also on a drive mapped to a long folder
    """
    folder = os.path.abspath(str(results_folder))
    if len(folder) + 1 + RESULT_NAME_ALLOWANCE <= WINDOWS_PATH_LIMIT or windows_long_paths_enabled():
        return []
    return [f"The results folder path is too long for Windows: {len(folder)} characters, "
            f"and with the names of the files it would pass the limit of {WINDOWS_PATH_LIMIT + 1}. Choose a shorter "
            f"folder (or turn on long paths in Windows)"]


def validate_config(config, root='.', regions_file_may_be_missing=False):
    """
    Return the list of problems of a configuration (empty if it is valid). Paths are resolved from root
    regions_file_may_be_missing: the file of the panel regions (PANEL.manual_regions_file) does not have to exist yet, for the
    tool that creates it; a run needs it
    """
    problems = []
    for key in sorted(set(config) - set(FIELD_KEYS)):
        problems.append(f"Unknown configuration key {key}")
    if problems:
        return problems
    for field in FIELDS:
        key, kind, value = field['key'], field['kind'], config.get(field['key'], field['default'])
        if kind in ('folder', 'file', 'text') and not isinstance(value, str):
            problems.append(f"{key} must be text")
        elif kind == 'folders' and not (isinstance(value, str) or
                                        isinstance(value, list) and all(isinstance(item, str) for item in value)):
            problems.append(f"{key} must be a path or a list of paths")
        elif kind == 'capture_folders' and not (isinstance(value, dict) and
                                                all(isinstance(item, str) for item in value.values())):
            problems.append(f"{key} must be a dictionary {{capture name: folder}}")
        elif kind == 'bool' and not isinstance(value, bool):
            problems.append(f"{key} must be true or false")
        elif kind == 'float' and not _is_number(value):
            problems.append(f"{key} must be a number")
        elif kind == 'int' and (not _is_number(value) or int(value) != value):
            problems.append(f"{key} must be an integer")
        elif kind == 'choice' and value not in field['choices']:
            problems.append(f"{key} must be one of {field['choices']}, got {value!r}")
        elif kind == 'band_values':
            low, high = field.get('range', (0, 1))
            if not isinstance(value, dict) or not value:
                problems.append(f"{key} must be a dictionary {{band name: number}}")
            else:
                for band, number in value.items():
                    if not _is_number(number) or not low < number <= high:
                        problems.append(f"{key}['{band}'] must be a number in ({low}, {high}], got {number!r}")
    if problems:
        return problems

    if not 0 < config['PANEL.percent_inner_area'] <= 1:
        problems.append("PANEL.percent_inner_area must be in (0, 1]")
    before_names, after_names = panel_capture_groups(config)
    output = config['INPUTS.output_product']
    # the panel calibrates the images, or (reflectance from the DLS alone) is only compared with the DLS when it is given
    calibrates = output == 'panel_reflectance'
    needs_panel = calibrates or (output == 'dls_reflectance' and bool(before_names))
    orientation = str(config['PANEL.panel_orientation']).strip().lower()
    try:
        heading_ok = orientation == 'all' or 0 <= float(orientation) <= 180
    except ValueError:
        heading_ok = False
    if not heading_ok:
        problems.append(f"PANEL.panel_orientation must be 'all' or a heading from 0 to 180 degrees, got "
                        f"{config['PANEL.panel_orientation']!r}")
    if calibrates and config['PANEL.panels_used'] in rct.PANELS_BY_ILLUMINATION and not config['CORRECTION.use_dls']:
        problems.append(f"PANEL.panels_used is '{config['PANEL.panels_used']}', which chooses the panel by the light read "
                        "by the DLS, but CORRECTION.use_dls is off: turn the DLS correction on or choose another way to "
                        "use the panels")
    if not 0 < config['PANEL.spectral_min_coverage'] <= 1:
        problems.append("PANEL.spectral_min_coverage must be in (0, 1]")
    band_sets = {frozenset(config[key]) for key in ('PANEL.reflectance_by_band', 'PANEL.band_center_wavelengths',
                                                      'PANEL.band_bandwidths')}
    ignored = capture_names(config['INPUTS.ignored_bands'])
    try:
        rct.compile_file_pattern(config['INPUTS.file_pattern'])
        pattern_problem = None
    except ValueError as error:
        pattern_problem = f"INPUTS.file_pattern: {error}"
        problems.append(pattern_problem)
    if len(band_sets) > 1:
        problems.append("PANEL.reflectance_by_band, PANEL.band_center_wavelengths and PANEL.band_bandwidths must "
                        "have the same bands")
    elif set(ignored) & set(config['PANEL.reflectance_by_band']):
        problems.append(f"INPUTS.ignored_bands has bands that are also processed: "
                        f"{sorted(set(ignored) & set(config['PANEL.reflectance_by_band']))}")
    elif needs_panel and config['PANEL.reflectance_source'] == 'spectral_file':
        spectral_file = config['PANEL.spectral_file'].strip()
        if not spectral_file:
            problems.append("PANEL.spectral_file is empty (PANEL.reflectance_source is spectral_file)")
        elif not resolve_path(root, spectral_file).is_file():
            problems.append(f"Spectral response file not found: {resolve_path(root, spectral_file)}")
        else:
            try:
                panel_reflectance_levels(config, root)
            except (ValueError, OSError) as error:
                problems.append(str(error))
    if config['PANEL.saturation_threshold'] <= 0:
        problems.append("PANEL.saturation_threshold must be positive")
    for key in ('CHECKS.band_ratio_tolerance', 'CHECKS.max_sun_angle_discrepancy_deg', 'CHECKS.jump_threshold',
                'CHECKS.panel_drift_tolerance', 'CHECKS.panel_illumination_tolerance',
                'CHECKS.panel_capture_spread_tolerance', 'CHECKS.ratio_range_min',
                'CHECKS.ratio_range_max'):
        if config[key] <= 0:
            problems.append(f"{key} must be positive")
    if config['CHECKS.ratio_range_min'] >= config['CHECKS.ratio_range_max']:
        problems.append("CHECKS.ratio_range_min must be smaller than CHECKS.ratio_range_max")
    for key in ('CHECKS.max_camera_tilt_deg', 'CHECKS.panel_heading_tolerance_deg', 'CHECKS.image_heading_tolerance_deg'):
        if not 0 < config[key] <= 90:
            problems.append(f"{key} must be between 0 and 90 degrees")
    if not -180 <= config['CHECKS.heading_offset_deg'] <= 180:
        problems.append("CHECKS.heading_offset_deg must be between -180 and 180 degrees")

    images = images_source(config, root)
    readable = bool(images.folders)
    if not images.folders:
        problems.append("INPUTS.images_folder is empty")
    for folder in images.folders:
        if not folder.is_dir():
            problems.append(f"Images folder not found: {folder}")
            readable = False
    if not config['INPUTS.results_folder'].strip():
        problems.append("INPUTS.results_folder is empty")
    else:
        problems.extend(path_length_problems(resolve_path(root, config['INPUTS.results_folder'])))
    exiftool_path, exiftool_source = find_exiftool(config, root)
    if exiftool_path is None:
        if config['INPUTS.exiftool_path'].strip():
            problems.append(f"ExifTool not found: {resolve_path(root, config['INPUTS.exiftool_path'])}")
        else:
            problems.append(f"ExifTool was not found in {exiftool_source}: install it (exiftool.org), or choose it in "
                            f"INPUTS.exiftool_path")
    if not pyexiftool_installed():
        problems.append("The Python package pyexiftool is not installed (pip install pyexiftool)")
    if needs_panel and config['PANEL.manual_regions_file']:
        regions_file = resolve_path(root, config['PANEL.manual_regions_file'])
        if not regions_file.is_file():
            if not regions_file_may_be_missing:
                problems.append(f"Panel regions file not found: {regions_file} (draw and accept the regions with Check / "
                                "draw panel regions, or empty the field to find the panel from its QR code)")
        else:
            try:
                regions = load_panel_regions(regions_file)
                for capture, bands in regions.items():
                    for band in bands:
                        if band not in panel_bands(config):
                            problems.append(f"{regions_file}: band '{band}' of {capture} is not one of the panel bands "
                                            f"{panel_bands(config)}")
            except (ValueError, OSError) as error:
                problems.append(str(error))
    if calibrates and not before_names:
        problems.append("INPUTS.reference_panel_capture is empty")
    excluded = capture_names(config['INPUTS.excluded_captures'])
    typed = capture_names(config['INPUTS.image_captures'])
    for text, names in (("both selected and left out", [name for name in typed if name in excluded]),
                        ("both panel captures and captures to correct",
                         [name for name in typed if name in before_names + after_names])):
        if names:
            problems.append(f"Captures {text}: {', '.join(names)}")
    if readable and pattern_problem is None:
        found = rct.list_capture_names(images, config['INPUTS.file_pattern'])
        for name in before_names + after_names + typed + excluded:
            if name not in found:
                problems.append(f"Capture '{name}' not found in {images}")
        used = before_names + after_names + captures_to_correct(config, found)
        if not typed and not captures_to_correct(config, found):
            problems.append(f"No captures to correct in {images}")
        unresolved = rct.unresolved_captures(images, config['INPUTS.file_pattern'], used)
        if unresolved:
            problems.append(f"Captures in more than one images folder, without the copy to use chosen: "
                            f"{', '.join(unresolved)} (Browse... of the images folder, Review repeated captures; or "
                            "INPUTS.repeated_capture_folders)")
    return problems


def captures_to_correct(config, found):
    """
    The captures that a run corrects: those of INPUTS.image_captures or, when it is empty, all those found in the images
    folder except the panel captures and the captures to leave out (INPUTS.excluded_captures)
    """
    typed = capture_names(config['INPUTS.image_captures'])
    if typed:
        return typed
    before_names, after_names = panel_capture_groups(config)
    left_out = set(capture_names(config['INPUTS.excluded_captures']) + before_names + after_names)
    return [name for name in found if name not in left_out]


def _progress(callback, stage, message, current=0, total=0):
    if callback is not None:
        callback({'stage': stage, 'message': message, 'current': current, 'total': total})


def irradiance_series_changes(series_by_band, threshold):
    """
    Spikes and steps of the horizontal irradiance along the captures
    Inputs: {band: [(capture name, irradiance), ...]} in acquisition order
    Outputs: {band: [event with the capture name, ...]}; bands with fewer than 3 captures or with invalid values
             are skipped
    """
    changes = {}
    for band, series in series_by_band.items():
        if len(series) < 3:
            continue
        try:
            events = rct.classify_irradiance_changes([value for _, value in series], threshold)
        except ValueError:
            continue
        changes[band] = [dict(event, capture=series[event['index']][0]) for event in events]
    return changes


def run(config, root='.', progress=None):
    """
    Run the workflow
    Inputs: configuration (see FIELDS; missing keys take the default), the folder from which the relative paths are
            resolved and, optionally, a function that receives {'stage', 'message', 'current', 'total'}
    Outputs: result dictionary, also saved as radiometric_result.json in the results folder. 'status' is 'completed',
             'completed_with_errors' (some captures failed) or 'failed' (nothing was produced)
    """
    result = {'status': 'failed', 'stages': list(STAGES), 'errors': [], 'flags': [], 'captures': {},
              'panel': {}, 'outputs': []}
    results_folder = None
    limits, geometry_limits = dict(rct.DLS_CHECKS), dict(rct.GEOMETRY_CHECKS)
    try:
        config = merge_config(config)
        result['config'] = config
        problems = validate_config(config, root)
        if problems:
            result['errors'] = problems
            return result
        root = Path(root)
        results_folder = resolve_path(root, config['INPUTS.results_folder'])
        results_folder.mkdir(parents=True, exist_ok=True)
        rct.DLS_CHECKS.update({'band_ratio_tolerance': config['CHECKS.band_ratio_tolerance'],
                               'ratio_range': (config['CHECKS.ratio_range_min'], config['CHECKS.ratio_range_max']),
                               'max_sun_angle_discrepancy_deg': config['CHECKS.max_sun_angle_discrepancy_deg'],
                               'jump_threshold': config['CHECKS.jump_threshold'],
                               'panel_drift_tolerance': config['CHECKS.panel_drift_tolerance'],
                               'panel_illumination_tolerance': config['CHECKS.panel_illumination_tolerance']})
        rct.GEOMETRY_CHECKS.update({key: config['CHECKS.' + key] for key in geometry_limits})
        _run_stages(config, root, results_folder, result, progress)
    except Exception as error:   # the result must always say what happened
        result['errors'].append(f"{type(error).__name__}: {error}")
        result['traceback'] = traceback.format_exc()
        result['status'] = 'failed'
    finally:
        rct.DLS_CHECKS.clear()
        rct.DLS_CHECKS.update(limits)
        rct.GEOMETRY_CHECKS.clear()
        rct.GEOMETRY_CHECKS.update(geometry_limits)
    if results_folder is not None:
        result['result_file'] = str(results_folder / RESULT_FILE_NAME)
        _write_result(results_folder / RESULT_FILE_NAME, result)
    return result


def _light(images, name):
    """ {band: DLS irradiance} of a capture; MetadataError that names the capture if a band has none """
    try:
        return {band: image.dls_irradiance() for band, image in images.items()}
    except rct.MetadataError as error:
        raise rct.MetadataError(f"{name}: {error}") from error


# The panel captures of one moment (before or after the flight) are a group: {capture name: {band: rct.Image}}, each
# capture after the detection of the panel. A group of several captures is used as their mean
def _group_light(group):
    """ {band: DLS irradiance} of a group of panel captures, the mean of its captures """
    lights = [_light(images, name) for name, images in group.items()]
    return {band: float(np.mean([light[band] for light in lights])) for band in lights[0]}


def _capture_time(images, name):
    return rct.capture_time(next(iter(images.values())).metadata, name)


def _group_time(group):
    """ The time of a group of panel captures, the mean of the times of its captures """
    return float(np.mean([_capture_time(images, name) for name, images in group.items()]))


def _second_panel_weight(mode, image, first_group, second_group, name):
    """
    Weight of the second group of panel captures for one image (see rct.panel_weight)
    Inputs: the mode, the capture as {band: rct.Image} and its name, and the two groups of panel captures (the second one
            None when there is none)
    Outputs: (weight, distances), where distances is {'first': ..., 'second': ...}, how different the light of the image is
             from that of each group (see rct.illumination_distance), for the modes by illumination, and None for the others
    """
    if second_group is None or mode == 'first':
        return 0.0, None
    if mode in rct.PANELS_BY_ILLUMINATION:
        light = _light(image, name)
        distances = (rct.illumination_distance(light, _group_light(first_group)),
                     rct.illumination_distance(light, _group_light(second_group)))
        return rct.panel_weight(mode, distances=distances), {'first': distances[0], 'second': distances[1]}
    if mode not in ('interpolate_time', 'nearest_time'):
        return rct.panel_weight(mode), None
    return rct.panel_weight(mode, _capture_time(image, name), _group_time(first_group), _group_time(second_group)), None


def _group_mean(group):
    """
    The mean of a group of panel captures, band by band, in the form that rct.compare_panel_captures reads: the panel
    factor, the irradiance estimated from the panel and the DLS irradiance (MetadataError if a capture has none)
    """
    def mean_light(band):
        return float(np.mean([images[band].dls_irradiance() for images in group.values()]))
    bands = next(iter(group.values()))
    return {band: SimpleNamespace(
        panel_audit={key: float(np.mean([images[band].panel_audit[key] for images in group.values()]))
                     for key in ('radiance_to_reflectance_factor', 'panel_irradiance_estimate')},
        dls_irradiance=lambda band=band: mean_light(band)) for band in bands}


def group_spread(group, use_dls):
    """
    How much the captures of a group disagree, band by band: std / mean of what is averaged, the panel factor or, with the
    DLS, the panel factor x DLS irradiance (a constant of the camera that does not depend on the light). {} for one capture
    """
    if len(group) < 2:
        return {}
    return {band: float(np.std(list(values.values())) / np.mean(list(values.values())))
            for band, values in _averaged_values(group, use_dls).items()}


def _averaged_values(group, use_dls):
    """ {band: {capture: what is averaged}}: the panel factor or, with the DLS, the panel factor x the DLS irradiance """
    return {band: {name: images[band].panel_audit['radiance_to_reflectance_factor'] *
                   (images[band].dls_irradiance() if use_dls else 1.0) for name, images in group.items()}
            for band in next(iter(group.values()))}


# Panel captures whose headings against the sun are within this many degrees of each other are one orientation
ORIENTATION_TOLERANCE_DEG = 20.0


def orientation_kind(heading):
    """ What a heading against the sun means: facing the sun (under 60 degrees), with the back to it (over 120) or at the side """
    if heading is None:
        return 'unknown'
    return 'facing_sun' if heading < 60 else 'back_to_sun' if heading > 120 else 'side'


ORIENTATION_TEXT = {'facing_sun': 'facing the sun', 'back_to_sun': 'with the back to the sun',
                    'side': 'with the sun at the side', 'unknown': 'heading unknown'}


def orientation_groups(headings):
    """
    The orientations found among panel captures: headings against the sun (see rct.heading_to_sun_deg) closer than
    ORIENTATION_TOLERANCE_DEG to the previous one are one group
    Inputs: {capture: heading in degrees, or None}
    Outputs: [{'heading' (the mean, rounded), 'kind' (see orientation_kind), 'captures'}], by heading; the captures without
             a heading are left out
    """
    groups = []
    for heading, capture in sorted((heading, capture) for capture, heading in headings.items() if heading is not None):
        if groups and heading - groups[-1]['last'] <= ORIENTATION_TOLERANCE_DEG:
            groups[-1]['values'].append(heading)
            groups[-1]['captures'].append(capture)
        else:
            groups.append({'values': [heading], 'captures': [capture]})
        groups[-1]['last'] = heading
    return [{'heading': int(round(float(np.mean(group['values'])))), 'kind': orientation_kind(float(np.mean(group['values']))),
             'captures': group['captures']} for group in groups]


def orientation_label(group):
    """ The text of an orientation group, for example 'heading 63 deg (with the sun at the side), 5 captures' """
    count = len(group['captures'])
    return (f"heading {group['heading']} deg ({ORIENTATION_TEXT[group['kind']]}), {count} capture"
            f"{'s' if count != 1 else ''}")


def orientation_selected(chosen, heading):
    """ Whether a panel capture of this heading is used when PANEL.panel_orientation is chosen ('all', or a heading) """
    if str(chosen).strip().lower() == 'all':
        return True
    return heading is not None and abs(heading - float(chosen)) <= ORIENTATION_TOLERANCE_DEG


def orientation_difference(group, headings, use_dls):
    """
    When a moment has panel captures of several orientations: how much each orientation differs from the one with the most
    captures, band by band, on what is averaged (see group_spread)
    Outputs: {heading of the orientation: {band: mean of it / mean of the largest - 1}}; {} with one orientation
    """
    groups = orientation_groups({name: headings.get(name) for name in group})
    if len(groups) < 2:
        return {}
    values = _averaged_values(group, use_dls)
    largest = max(groups, key=lambda item: len(item['captures']))
    difference = {}
    for other in groups:
        if other is largest:
            continue
        difference[other['heading']] = {
            band: float(np.mean([by_capture[name] for name in other['captures']]) /
                        np.mean([by_capture[name] for name in largest['captures']]) - 1)
            for band, by_capture in values.items()}
    return {'reference_heading': largest['heading'], 'differences': difference}


def _flight_times(config, root, flight_names):
    """ The times of the first and of the last capture to correct (None when there is none or it has no time) """
    if not flight_names:
        return None
    images_folder = images_source(config, root)
    times = []
    for name in (flight_names[0], flight_names[-1]):
        paths = rct.find_capture_paths(images_folder, name, config['INPUTS.file_pattern'])
        metadata = rct._read_metadata(paths[:1], exiftool_executable(config, root))[0]
        try:
            times.append(rct.capture_time(metadata, name))
        except rct.MetadataError:
            return None
    return min(times), max(times)


def panel_moment(time, flight):
    """
    The moment of a panel capture: 'before' the first capture to correct or 'after' the last one; one taken during the
    flight goes to the closer end. Without the times of the flight, 'before'
    """
    if flight is None or time is None:
        return 'before'
    first, last = flight
    if time < first:
        return 'before'
    if time > last:
        return 'after'
    return 'before' if time - first <= last - time else 'after'


def calibrate_panel_captures(config, root, levels, flight_names, calibrates, use_dls, result, progress=None):
    """
    Read and measure the panel captures (INPUTS.reference_panel_capture), find their moments and orientations, check them
    and choose those that are used
    - Moments: the captures listed in INPUTS.reference_panel_capture_after (configurations of earlier versions) are after
      the flight; when that key is empty, each capture is before or after the flight by its time against the captures to
      correct (see panel_moment).
    - Orientations: grouped by their heading against the sun (see orientation_groups); PANEL.panel_orientation keeps all or
      the captures of one heading.
    - Checks (when the panel calibrates the images): the spread of each moment, the capture that departs from its moment and
      the difference of the orientations of a moment, as flags.
    Inputs: the configuration, the folder of the relative paths, the reflectance of the panel by band, the captures to
            correct, whether the panel calibrates the images (else a panel that fails is only a flag), whether the DLS
            correction is used, the result dictionary (result['panel'] and result['flags'] are filled) and the progress
            callback
    Outputs: {'before': {capture: {band: rct.Image}}, 'after': {...}}, the moments that have captures; when only the 'after'
             moment has them, they are the first moment ('before')
    """
    images_folder = images_source(config, root)
    exiftool_path = exiftool_executable(config, root)
    ignored = capture_names(config['INPUTS.ignored_bands'])
    bands = list(levels)
    regions, origins = {}, {}
    if config['PANEL.manual_regions_file']:
        regions_file = resolve_path(root, config['PANEL.manual_regions_file'])
        if regions_file.is_file():
            regions, origins = load_panel_regions(regions_file), load_panel_origins(regions_file)
    before_names, after_names = panel_capture_groups(config)
    explicit_after = bool(after_names)
    flight = None if explicit_after else _flight_times(config, root, flight_names)
    measured, info = {}, {}
    for name in before_names + [name for name in after_names if name not in before_names]:
        _progress(progress, 'PANEL_CALIBRATION', f"Detecting the reference panel in {name}")
        objects = _read_capture(images_folder, name, bands, exiftool_path, config['INPUTS.file_pattern'], ignored)
        failed = False
        for band, image in objects.items():
            try:
                image.detect_reference_panel(levels, config['PANEL.percent_inner_area'], config['PANEL.saturation_threshold'],
                                             manual_bounds=regions.get(name, {}).get(band),
                                             region_origin=origins.get(name, {}).get(band))
            except Exception as error:
                if calibrates:
                    raise RuntimeError(f"Panel of band '{band}' in {name}: {error}") from error
                result['flags'].append(f"{name}: the panel of band '{band}' was not measured, so it is not compared "
                                       f"with the DLS ({error})")
                failed = True
                break
        if failed:
            continue
        with warnings.catch_warnings(record=True):
            warnings.simplefilter('always')
            geometry = rct.check_panel_geometry(objects)
        result['flags'].extend(f"{name}: {flag}" for flag in geometry['flags'])
        try:
            time = _capture_time(objects, name)
        except rct.MetadataError:
            time = None
        moments = ([moment for moment, listed in (('before', before_names), ('after', after_names)) if name in listed]
                   if explicit_after else [panel_moment(time, flight)])
        heading = geometry['values'].get('heading_to_sun_deg')
        measured[name] = objects
        info[name] = {'moments': moments, 'time': str(next(iter(objects.values())).metadata.get('EXIF:DateTimeOriginal')),
                      'heading': heading, 'orientation': orientation_kind(heading),
                      'used': orientation_selected(config['PANEL.panel_orientation'], heading),
                      'bands': {band: rct._jsonable(rct.compact_panel_audit(image.panel_audit))
                                for band, image in objects.items()},
                      'geometry': rct._jsonable(geometry['values'])}
    if not measured:
        return {}
    groups = orientation_groups({name: item['heading'] for name, item in info.items()})
    result['panel']['orientations'] = [dict(group, label=orientation_label(group)) for group in groups]
    used = [name for name in measured if info[name]['used']]
    if not used:
        raise RuntimeError(f"None of the panel captures {', '.join(measured)} has the heading chosen in "
                           f"PANEL.panel_orientation ({config['PANEL.panel_orientation']} deg, +-"
                           f"{ORIENTATION_TOLERANCE_DEG:g}); the headings found are: "
                           f"{'; '.join(orientation_label(group) for group in groups) or 'none'}")
    panels = {}
    for moment in ('before', 'after'):
        names_of_moment = [name for name in measured if moment in info[name]['moments']]
        if not names_of_moment:
            continue
        result['panel'][moment] = {'captures': {name: {key: value for key, value in info[name].items() if key != 'moments'}
                                                for name in names_of_moment}}
        group = {name: measured[name] for name in names_of_moment if info[name]['used']}
        if group:
            panels[moment] = group
    if 'before' not in panels:                       # only captures after the flight: they are the first moment
        panels = {'before': panels['after']}
        result['panel']['before'] = result['panel'].pop('after')
        result['flags'].append(f"The panel captures used ({', '.join(panels['before'])}) are all after the flight: "
                               f"they calibrate the images alone")
    if calibrates:
        _check_moments(config, panels, info, use_dls, result)
    return panels


def _check_moments(config, panels, info, use_dls, result):
    """ The flags of the moments: orientations that differ, a capture that departs, a pair that disagrees """
    spread_limit = config['CHECKS.panel_capture_spread_tolerance']
    averaged = 'panel factor x DLS irradiance' if use_dls else 'panel factor'
    for moment, group in panels.items():
        group_names = list(group)
        record = result['panel'].setdefault(moment, {'captures': {}})
        try:
            difference = orientation_difference(group, {name: info[name]['heading'] for name in group}, use_dls)
        except rct.MetadataError:
            difference = {}
        if difference:
            record['orientation_difference'] = difference
            reference = difference['reference_heading']
            for heading, by_band in difference['differences'].items():
                band, value = max(by_band.items(), key=lambda item: abs(item[1]))
                if abs(value) > spread_limit:
                    result['flags'].append(f"Panel captures of the {moment} moment: those with heading {heading} deg differ "
                                           f"{100 * value:+.1f} % (band '{band}') from those with heading {reference} deg "
                                           f"(limit {100 * spread_limit:g} %). Choose one orientation (Panel captures "
                                           f"used, by orientation) or check the photos")
        try:
            spread, departures = group_spread(group, use_dls), group_departures(group, use_dls)
        except rct.MetadataError as error:
            spread, departures = {}, {}
            result['flags'].append(f"Panel captures {', '.join(group_names)}: their spread was not checked ({error})")
        if spread:
            record['spread'] = spread
        if departures:                               # three or more captures: the ones that depart are named
            record['departures'] = departures
            for name, bands_departure in departures.items():
                band, value = max(bands_departure.items(), key=lambda item: abs(item[1]))
                if abs(value) > spread_limit:
                    result['flags'].append(f"Panel capture {name}: its {averaged} departs {100 * value:+.1f} % (band "
                                           f"'{band}') from the median of the panel captures of its moment "
                                           f"({', '.join(group_names)}); limit {100 * spread_limit:g} %. Leave it out "
                                           f"if it is wrong")
        elif spread:                                 # two captures: which one is wrong cannot be told
            for band, value in spread.items():
                if value > spread_limit:
                    result['flags'].append(f"Panel captures {', '.join(group_names)}: the {averaged} of band '{band}' varies "
                                           f"{100 * value:.1f} % between them (limit {100 * spread_limit:g} %)")


def check_panel_captures(config, root='.', progress=None):
    """
    Measure and check the panel captures without correcting anything, for the table of the UI (progress: as in run)
    Outputs: (rows, the flags): one row per panel capture {'capture', 'time', 'moment', 'heading', 'orientation', 'used',
             'departure' (its largest from the median of its moment, with three or more captures, or None), 'spread' (the
             largest spread of the two captures of its moment, or None), 'band', 'alone' (the only capture of its
             moment), 'flag' (text, '' when nothing is wrong; see panel_capture_flag)}
    """
    config = merge_config(config)
    levels, _ = panel_reflectance_levels(config, root)
    images_folder = images_source(config, root)
    flight_names = captures_to_correct(config, rct.list_capture_names(images_folder, config['INPUTS.file_pattern']))
    result = {'panel': {}, 'flags': []}
    use_dls = config['CORRECTION.use_dls'] and config['INPUTS.output_product'] == 'panel_reflectance'
    with warnings.catch_warnings(record=True):
        warnings.simplefilter('always')
        calibrate_panel_captures(config, root, levels, flight_names, True, use_dls, result, progress)
    limit = config['CHECKS.panel_capture_spread_tolerance']
    rows = []
    for moment in ('before', 'after'):
        record = result['panel'].get(moment, {})
        for name, item in record.get('captures', {}).items():
            departure, spread, band = None, None, None
            if name in record.get('departures', {}):
                band, departure = max(record['departures'][name].items(), key=lambda pair: abs(pair[1]))
            elif record.get('spread') and len(record['captures']) == 2:
                band, spread = max(record['spread'].items(), key=lambda pair: pair[1])
            row = {'capture': name, 'time': item['time'], 'moment': moment, 'heading': item['heading'],
                   'orientation': ORIENTATION_TEXT[item['orientation']], 'used': item['used'], 'departure': departure,
                   'spread': spread, 'band': band, 'alone': len(record['captures']) == 1}
            row['flag'] = panel_capture_flag(row, limit)
            rows.append(row)
    return rows, result['flags']


def panel_capture_flag(row, limit):
    """
    The flag of a row of check_panel_captures under a spread limit (CHECKS.panel_capture_spread_tolerance), '' when nothing
    is wrong: a capture of three or more that departs from the median of its moment, the two captures of a moment that
    disagree, or a capture of an orientation that is not used
    """
    if not row['used']:
        return 'not used: another orientation was chosen'
    if row['departure'] is not None and abs(row['departure']) > limit:
        return f"departs more than {100 * limit:g} % from the others"
    if row['spread'] is not None and row['spread'] > limit:
        return f"the two captures of this moment disagree {100 * row['spread']:.1f} %"
    return ''


def adjacent_panel_shares(panels, image, name):
    """
    The panel capture taken last before the image and the one taken first after it, among all the panel captures of both
    moments, weighted by time (as Metashape 2.3.1 does); an image before the first or after the last uses the closest
    Outputs: {capture: weight}
    """
    times = sorted((_capture_time(images, capture), capture) for group in panels.values()
                   for capture, images in group.items())
    time = _capture_time(image, name)
    before = [item for item in times if item[0] <= time]
    after = [item for item in times if item[0] >= time]
    if not before or not after:
        return {(after or before)[0 if after else -1][1]: 1.0}
    (first_time, first), (second_time, second) = before[-1], after[0]
    if first == second or second_time == first_time:
        return {first: 1.0}
    weight = (time - first_time) / (second_time - first_time)
    return {first: 1.0 - weight, second: weight}


def group_departures(group, use_dls):
    """
    How far each capture of a group of three or more is from the others, band by band: what is averaged (see group_spread)
    divided by its median over the group, minus 1 (-0.05 = 5 % below). With two captures the one that is wrong cannot be
    told, so {} (the spread of the group is checked instead)
    Outputs: {capture: {band: departure}}
    """
    if len(group) < 3:
        return {}
    departures = {name: {} for name in group}
    for band, values in _averaged_values(group, use_dls).items():
        median = float(np.median(list(values.values())))
        for name, value in values.items():
            departures[name][band] = float(value / median - 1)
    return departures


def _nearest_capture(group, image, name):
    """ The capture of a group taken closest in time to the image (the first one when the times are not known) """
    try:
        time = _capture_time(image, name)
        return min(group, key=lambda capture: abs(_capture_time(group[capture], capture) - time))
    except rct.MetadataError:
        return next(iter(group))


def _read_capture(images_folder, name, bands, exiftool_path, pattern=None, ignored=()):
    files = rct.read_band_files(rct.find_capture_paths(images_folder, name, pattern), bands, exiftool_path, ignored)
    return {band: rct.Image(*files[band]) for band in bands}


def compact_reflectance_info(info):
    """ What the reports say about where the reflectance of the panel came from, without what the configuration says """
    compact = {key: value for key, value in info.items() if key not in ('format', 'min_coverage', 'bands')}
    if 'bands' in info:
        compact['bands'] = {band: {key: values[key] for key in ('samples', 'coverage')}
                            for band, values in info['bands'].items()}
    return compact



class _Done(Exception):
    """ The capture was written and checked by a path that does not use the panel """


def _irradiance_series(series, bands, objects, name):
    """ Add the DLS irradiance of a capture to the series of each band (the bands without it are skipped) """
    for band in bands:
        try:
            series[band].append((name, objects[band].dls_irradiance()))
        except rct.MetadataError:
            pass


def _write_without_panel(output, data_type, objects, results_folder, entry, written):
    """
    Write a capture as reflectance from the DLS alone or as radiance (no panel)
    Outputs: (the records of the bands, the checks of the capture, its geometry)
    """
    # the factors of all the bands first, so that nothing is written for a capture that cannot be corrected
    factors = ({band: rct.dls_reflectance_factor(image) for band, image in objects.items()}
               if output == 'dls_reflectance' else {})
    records = []
    for band, image in objects.items():
        if output == 'radiance':
            data, factor, details = image.digital_numbers_to_radiance(), None, None
        else:
            factor, details = factors[band]
            data = image.digital_numbers_to_reflectance(factor)
        data = data.astype(data_type, copy=False)
        output_path = results_folder / Path(image.image_path).name
        tifffile.imwrite(output_path, data)
        records.append(rct.band_record(output_path, image, data, [], factor, dls_only=details,
                                       output='radiance' if output == 'radiance' else 'reflectance'))
        written.append(str(output_path))
        entry['outputs'].append(output_path.name)
    geometry = rct.check_image_geometry(objects)
    if output == 'dls_reflectance':                 # the DLS of the image is checked (its tilt), without a panel
        checks = rct.check_dls_capture({}, objects)
    else:
        checks = {'flags': [], 'flag_details': [], 'unavailable': []}
    checks['geometry'] = {'image': geometry['values']}
    checks['flags'] = list(checks['flags']) + geometry['flags']
    checks['flag_details'] = list(checks['flag_details']) + geometry['flag_details']
    checks['unavailable'] = list(checks['unavailable']) + geometry['unavailable']
    return records, checks, geometry


def dls_only_panel_check(panels):
    """
    How far the reflectance from the DLS alone is from the known reflectance of the panel, in each panel capture and band:
    pi x mean radiance of the panel / DLS irradiance, divided by the reflectance of the panel, minus 1 (0.10 = 10 % too high)
    Outputs: {capture: {band: difference, or None when the capture has no usable irradiance}}
    """
    check = {}
    for group in panels.values():
        for name, images in group.items():
            check[name] = {}
            for band, image in images.items():
                audit = image.panel_audit
                try:
                    reflectance = np.pi * audit['radiance_mean'] / rct.irradiance_si(image)[0]
                    check[name][band] = float(reflectance / audit['reflectance_level'] - 1)
                except rct.MetadataError:
                    check[name][band] = None
    return check


def _run_stages(config, root, results_folder, result, progress):
    images_folder = images_source(config, root)
    exiftool_path = exiftool_executable(config, root)
    ignored = capture_names(config['INPUTS.ignored_bands'])
    before_names, after_names = panel_capture_groups(config)
    output, data_type = config['INPUTS.output_product'], np.dtype(config['INPUTS.output_data_type'])
    calibrates = output == 'panel_reflectance'                 # else the panel, if given, is only compared with the DLS
    needs_panel = calibrates or (output == 'dls_reflectance' and bool(before_names))
    if needs_panel:
        levels, reflectance_info = panel_reflectance_levels(config, root)
        reflectance_info = compact_reflectance_info(reflectance_info)
        result['panel']['reflectance'] = rct._jsonable({'levels': levels, **reflectance_info})
    else:
        levels, reflectance_info = dict(config['PANEL.reflectance_by_band']), {}
    result['output'] = {'product': output, 'data_type': str(data_type)}
    bands = list(levels)
    use_dls = config['CORRECTION.use_dls'] and calibrates

    # LOAD_CAPTURES
    names = captures_to_correct(config, rct.list_capture_names(images_folder, config['INPUTS.file_pattern']))
    written_as = {'panel_reflectance': f"reflectance with the panel capture(s) {', '.join(before_names)}",
                  'dls_reflectance': 'reflectance from the DLS only', 'radiance': 'radiance'}[output]
    _progress(progress, 'LOAD_CAPTURES', f"{len(names)} capture(s) to correct, written as {written_as} ({data_type})",
              0, len(names))

    # PANEL_CALIBRATION
    panels = {}
    if needs_panel:
        panels = calibrate_panel_captures(config, root, levels, names, calibrates, use_dls, result, progress)
    if calibrates and 'after' not in panels and config['PANEL.panels_used'] not in ('first', 'adjacent_time'):
        label = FIELD_BY_KEY['PANEL.panels_used']['labels'][config['PANEL.panels_used']]
        result['flags'].append(f"There are panel captures of one moment only: '{label}' uses them alone")
    if output == 'dls_reflectance' and panels:
        result['panel']['dls_only_check'] = dls_only_panel_check(panels)
    if calibrates and 'after' in panels:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            comparison = rct.compare_panel_captures(_group_mean(panels['before']), _group_mean(panels['after']))
        result['panel']['comparison'] = rct._jsonable(comparison)
        result['flags'].extend(comparison['flags'])

    # REFLECTANCE and DLS_CHECKS of each capture
    series = {band: [] for band in bands}
    events, checked = [], 0                      # the flags of the captures, to be counted by criterion (group_flag_events)
    parameters = {'config': config}
    if needs_panel:
        parameters.update({'panel_reflectance_levels': levels, 'panel_reflectance_source': reflectance_info})
    for number, name in enumerate(names, start=1):
        _progress(progress, 'REFLECTANCE', f"Correcting {name}", number - 1, len(names))
        entry = {'outputs': [], 'errors': []}
        result['captures'][name] = entry
        written, flags, details = [], [], []
        checked_panel = before_names[0] if before_names else None
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter('always')
                objects = _read_capture(images_folder, name, bands, exiftool_path, config['INPUTS.file_pattern'], ignored)
                if not calibrates:
                    records, checks, geometry = _write_without_panel(output, data_type, objects, results_folder, entry,
                                                                     written)
                    flags, details = list(checks['flags']), list(checks['flag_details'])
                    checked += 1
                    entry['geometry'] = rct._jsonable(geometry['values'])
                    provenance = rct.write_provenance(results_folder / (name + '_provenance.json'), records,
                                                      parameters=parameters, checks=checks)
                    entry['provenance'] = Path(provenance).name
                    _irradiance_series(series, bands, objects, name)
                    result['outputs'].extend(written)
                    raise _Done
                second = panels.get('after')
                if config['PANEL.panels_used'] == 'adjacent_time':
                    shares = adjacent_panel_shares(panels, objects, name)
                    weight = sum(share for capture, share in shares.items() if capture in (second or {}))
                    distances = None
                    entry['panels'] = {'used': 'adjacent_time', 'weight_of_the_second_panel': weight,
                                       'captures': shares}
                    weighted = [(group[capture], share) for group in panels.values() for capture, share in shares.items()
                                if capture in group]
                else:
                    shares = None
                    weight, distances = _second_panel_weight(config['PANEL.panels_used'], objects, panels['before'],
                                                             second, name)
                    entry['panels'] = {'used': config['PANEL.panels_used'] if second is not None else 'first',
                                       'weight_of_the_second_panel': weight}
                    if distances:
                        entry['panels']['light_difference'] = distances
                    # the weight of a group is shared by its captures
                    weighted = [(capture, (1 - weight) / len(panels['before'])) for capture in panels['before'].values()]
                    if second is not None:
                        weighted += [(capture, weight / len(second)) for capture in second.values()]
                # the factors of all the bands first, so that nothing is written for a capture that cannot be corrected
                factors = {band: rct.combine_panels([(capture[band], share) for capture, share in weighted],
                                                    objects[band] if use_dls else None) for band in bands}
                records = []
                for band in bands:
                    image = objects[band]
                    factor, used = factors[band]
                    reflectance = image.digital_numbers_to_reflectance(factor).astype(data_type, copy=False)
                    output_path = results_folder / Path(image.image_path).name
                    tifffile.imwrite(output_path, reflectance)
                    records.append(rct.band_record(output_path, image, reflectance, used, factor))
                    written.append(str(output_path))
                    entry['outputs'].append(output_path.name)           # the files are in the results folder
                # the DLS checks compare the image with the panel capture closest in time of the group that weighs most
                # in its calibration (the first group if both weigh the same)
                label = 'after' if weight > 0.5 else 'before'
                if shares:                                   # the closest captures: the one that weighs most
                    checked_panel = max(shares, key=shares.get)
                    label = 'after' if checked_panel in (second or {}) else 'before'
                else:
                    checked_panel = _nearest_capture(panels[label], objects, name)
                checks = rct.check_dls_capture(panels[label][checked_panel], objects)
                checks['compared_with_panel'] = checked_panel
                geometry = rct.check_image_geometry(objects)
                checks['geometry'] = {'panel': result['panel'][label]['captures'][checked_panel]['geometry'],
                                      'image': geometry['values']}
                checks['flags'] = list(checks['flags']) + geometry['flags']
                checks['unavailable'] = list(checks['unavailable']) + geometry['unavailable']
                flags, details = list(checks['flags']), checks['flag_details'] + geometry['flag_details']
                if distances:
                    checks['panel_light_difference'] = distances
                    nearest, tolerance = min(distances.values()), rct.DLS_CHECKS['panel_illumination_tolerance']
                    if nearest > tolerance:
                        flags.append(f"Light of the capture differs {100 * nearest:.0f} % from that of the nearest panel "
                                     f"capture (limit {100 * tolerance:g} %)")
                        details.append({'criterion': 'panel_illumination', 'scope': 'image', 'band': None,
                                        'value': nearest, 'limit': tolerance})
                checked += 1
                entry['geometry'] = rct._jsonable(geometry['values'])
                entry['dls'] = rct._jsonable({key: checks[key] for key in ('median_ratio',) if key in checks})
                provenance = rct.write_provenance(results_folder / (name + '_provenance.json'), records,
                                                  parameters=parameters, checks=checks)
                entry['provenance'] = Path(provenance).name
            _irradiance_series(series, bands, objects, name)
            result['outputs'].extend(written)
        except _Done:
            pass
        except Exception as error:
            entry['errors'].append(f"{type(error).__name__}: {error}")
            result['errors'].append(f"{name}: {error}")
        for number, flag in enumerate(flags):
            detail = details[number] if number < len(details) else None
            if detail is None:
                result['flags'].append(f"{name}: {flag}")
            elif detail['scope'] == 'panel':                         # the same in every capture: once, with the panel
                flag = f"{checked_panel}: {flag}"
                if flag not in result['flags']:
                    result['flags'].append(flag)
            else:
                events.append((name, detail))

    groups = group_flag_events(events, checked, names)

    _progress(progress, 'DLS_CHECKS', "Checking the irradiance along the captures", len(names), len(names))
    result['irradiance_changes'] = irradiance_series_changes(series, config['CHECKS.jump_threshold'])
    groups += group_irradiance_changes(result['irradiance_changes'], config['CHECKS.jump_threshold'], len(names), names)
    result['flag_groups'] = [{key: value for key, value in group.items() if key != 'message'} for group in groups]
    result['flags'].extend(group['message'] for group in groups)

    _progress(progress, 'PROVENANCE', "Saving the result file", len(names), len(names))
    produced = any(entry['outputs'] for entry in result['captures'].values())
    failed = any(entry['errors'] for entry in result['captures'].values())
    result['status'] = 'failed' if not produced else ('completed_with_errors' if failed else 'completed')
    result['summary'] = build_summary(result, config)


# The flags of the captures are counted by criterion: 'DLS tilt over 10 deg in 386 of 409 captures (...): IMG_0010 to IMG_0044, ...'
FLAG_CRITERIA = (                                # (criterion, label from the limit, name of the value, unit, decimals)
    ('camera_tilt', lambda limit: f"Camera tilt over {limit:g} deg", 'tilt', ' deg', 1),
    ('heading', lambda limit: f"Camera heading more than {limit:g} deg from the sun at the side (90 deg)",
     'heading', ' deg from the sun', 0),
    ('dls_tilt', lambda limit: f"DLS tilt over {limit:g} deg", 'tilt of at least', ' deg', 1),
    ('ratio_range', lambda limit: f"DLS ratio outside {limit[0]:g} to {limit[1]:g}", 'ratio', '', 3),
    ('band_ratio_departure', lambda limit: f"DLS ratio of a band more than {100 * limit:g} % from the median of the bands",
     'departure', ' %', 1),
    ('panel_illumination', lambda limit: f"Light more than {100 * limit:g} % different from the nearest panel capture",
     'difference', ' %', 1))


def capture_list_text(names, order, texts=None):
    """
    The captures of a flag as text, all of them, in the order of the run (order: the names of all the captures): three or
    more neighbors are written as 'first to last'. texts: {capture: text written after its name}; the captures are then
    written one by one
    """
    position = {name: number for number, name in enumerate(order)}
    names = sorted(set(names), key=position.__getitem__)
    items, index = [], 0
    while index < len(names):
        end = index
        while not texts and end + 1 < len(names) and position[names[end + 1]] == position[names[end]] + 1:
            end += 1
        if end - index >= 2:
            items.append(f"{names[index]} to {names[end]}")
        else:
            items.extend(name + (f" ({texts[name]})" if texts else '') for name in names[index:end + 1])
        index = end + 1
    return ', '.join(items)


def group_flag_events(events, total, order):
    """
    Count the flags of the captures by criterion
    Inputs: [(capture, detail)] with the 'flag_details' of rct.check_dls_capture and rct.check_image_geometry; the number
            of captures that were checked and the names of all the captures in the order of the run
    Outputs: one dictionary per criterion that was flagged, in the order of FLAG_CRITERIA: {'criterion', 'label', 'count',
             'of', 'captures' (all of them), 'bands', 'min', 'max' (of the values) and 'message'}, the text for the log
             with the count, the range of the values and the captures (see capture_list_text)
    """
    groups = []
    for criterion, label, value_name, unit, digits in FLAG_CRITERIA:
        found = [(capture, detail) for capture, detail in events if detail['criterion'] == criterion]
        if not found:
            continue
        captures = sorted({capture for capture, _ in found}, key=order.index)
        scale = 100 if criterion in ('band_ratio_departure', 'panel_illumination') else 1
        values = [abs(detail['value']) * scale for _, detail in found]
        bands = sorted({detail['band'] for _, detail in found if detail['band']})
        low, high = min(values), max(values)
        values_text = (f"{low:.{digits}f}{unit}" if round(low, digits) == round(high, digits)
                       else f"{low:.{digits}f} to {high:.{digits}f}{unit}")
        label_text = label(found[0][1]['limit'])
        message = (f"{label_text} in {len(captures)} of {total} capture{'s' if total != 1 else ''} "
                   f"({value_name} {values_text}{'; bands ' + ', '.join(bands) if bands else ''}): "
                   f"{capture_list_text(captures, order)}")
        groups.append({'criterion': criterion, 'label': label_text, 'count': len(captures), 'of': total,
                       'captures': captures, 'bands': bands, 'min': low, 'max': high, 'message': message})
    return groups


def group_irradiance_changes(changes, threshold, total, order):
    """
    The spikes and steps of the irradiance (see irradiance_series_changes) counted by kind over the bands, in the form of
    group_flag_events; each capture says the largest change and in how many bands it was found
    """
    groups = []
    for kind in ('spike', 'step'):
        found = {}
        for events in changes.values():
            for event in events:
                if event['kind'] == kind:
                    found.setdefault(event['capture'], []).append(event['change'])
        if not found:
            continue
        texts = {capture: f"{100 * max(values, key=abs):+.1f} %, {len(values)} band{'s' if len(values) > 1 else ''}"
                 for capture, values in found.items()}
        captures = sorted(found, key=order.index)
        label = f"Irradiance {kind} of more than {100 * threshold:g} % from the previous capture"
        message = (f"{label} in {len(captures)} of {total} capture{'s' if total != 1 else ''}: "
                   f"{capture_list_text(captures, order, texts=texts)}")
        groups.append({'criterion': f'irradiance_{kind}', 'label': label, 'count': len(captures), 'of': total,
                       'captures': captures, 'changes': {capture: found[capture] for capture in captures},
                       'message': message})
    return groups


def _statistics(values):
    """ {'n', 'min', 'median', 'max'} of the numbers that are not None, or None if there are none """
    numbers = [value for value in values if value is not None]
    if not numbers:
        return None
    return {'n': len(numbers), 'min': min(numbers), 'median': statistics.median(numbers), 'max': max(numbers)}


def build_summary(result, config):
    """
    What the run found, in numbers, for the end of the log and the result file: how many captures were corrected, the
    geometry and the DLS of the panel captures, and the minimum, median and maximum over the corrected captures of the tilt of
    the camera, the angle between its heading and the sun, the elevation of the sun, the tilt of the DLS and the DLS ratio,
    each with how many captures are outside the limits of the configuration
    """
    done = [entry for entry in result['captures'].values() if entry.get('outputs') and not entry.get('errors')]
    tilt_limit, tolerance = config['CHECKS.max_camera_tilt_deg'], config['CHECKS.image_heading_tolerance_deg']
    dls_limit = config['CHECKS.max_sun_angle_discrepancy_deg']
    low, high = config['CHECKS.ratio_range_min'], config['CHECKS.ratio_range_max']

    def column(group, key):
        return [entry[group].get(key) for entry in done if entry.get(group)]
    images = {}
    for name, values, outside in (
            ('camera_tilt_deg', column('geometry', 'camera_tilt_deg'), lambda value: value > tilt_limit),
            ('heading_to_sun_deg', column('geometry', 'heading_to_sun_deg'), lambda value: abs(value - 90) > tolerance),
            ('sun_elevation_deg', column('geometry', 'sun_elevation_deg'), None),
            ('dls_tilt_deg', column('geometry', 'dls_tilt_deg'), lambda value: value > dls_limit),
            ('dls_ratio', column('dls', 'median_ratio'), lambda value: not low <= value <= high)):
        stats = _statistics(values)
        if stats:
            stats['outside'] = None if outside is None else sum(outside(value) for value in values if value is not None)
            images[name] = stats
    panels = {name: dict(capture['geometry'], capture=name, orientation=capture.get('orientation'),
                         used=capture.get('used', True))
              for label in ('before', 'after') for name, capture in result['panel'].get(label, {}).get('captures', {}).items()
              if capture.get('geometry')}
    weights = [entry['panels']['weight_of_the_second_panel'] for entry in done if entry.get('panels')]
    mode = config['PANEL.panels_used']
    panel_weights = None
    if weights and mode != 'first' and 'after' in result['panel']:
        panel_weights = {'mode': mode, 'first_only': sum(weight == 0 for weight in weights),
                         'second_only': sum(weight == 1 for weight in weights),
                         'both': sum(0 < weight < 1 for weight in weights)}
    return {'captures': len(result['captures']), 'corrected': len(done), 'files': len(result['outputs']),
            'flags': len(result['flags']), 'panel_weights': panel_weights,
            'irradiance_changes': sum(len(events) for events in result.get('irradiance_changes', {}).values()),
            'limits': {'camera_tilt_deg': tilt_limit, 'heading_tolerance_deg': tolerance, 'dls_tilt_deg': dls_limit,
                       'dls_ratio': [low, high]},
            'panels': panels, 'images': rct._jsonable(images)}


def summary_lines(result):
    """ The summary of a run (see build_summary) as lines of text for the log; [] if the run has none """
    summary = result.get('summary')
    if not summary:
        return []

    def number(value, digits=0):
        return 'unknown' if value is None else f"{value:.{digits}f}"

    lines = [f"Summary: {summary['corrected']} of {summary['captures']} capture(s) corrected, {summary['files']} file(s) "
             f"written, {summary['flags']} flag(s)."]
    for panel in summary['panels'].values():
        orientation = {'facing_sun': 'facing the sun', 'back_to_sun': 'the back to the sun',
                       'side': 'the sun at the side'}.get(panel.get('orientation'), 'orientation unknown')
        lines.append(f"Panel {panel['capture']}: camera tilt {number(panel.get('camera_tilt_deg'), 1)} deg; heading "
                     f"{number(panel.get('heading_to_sun_deg'))} deg from the sun ({orientation}; 0 = facing it, 180 = the "
                     f"back to it); DLS tilt {number(panel.get('dls_tilt_deg'), 1)} deg; sun elevation "
                     f"{number(panel.get('sun_elevation_deg'))} deg" +
                     ('.' if panel.get('used', True) else '; not used (Panel captures used, by orientation).'))
    for capture, bands in (result.get('panel', {}).get('dls_only_check') or {}).items():
        values = ', '.join(f"{band} {100 * value:+.0f} %" for band, value in bands.items() if value is not None)
        lines.append(f"Reflectance from the DLS only of the panel in {capture}, against its known reflectance: "
                     f"{values or 'no irradiance'}.")
    used = summary.get('panel_weights')
    if used:
        lines.append(f"Panels ({used['mode']}): the panel before the flight alone in {used['first_only']} capture(s), after it alone in "
                     f"{used['second_only']}, both in {used['both']}.")
    images, limits = summary['images'], summary['limits']
    descriptions = (
        ('camera_tilt_deg', 1, "camera tilt (deg)", f"limit {limits['camera_tilt_deg']:g}"),
        ('heading_to_sun_deg', 0, "camera heading from the sun (deg; 90 = the sun at the side)",
         f"limit {limits['heading_tolerance_deg']:g} from 90"),
        ('sun_elevation_deg', 0, "sun elevation (deg)", None),
        ('dls_tilt_deg', 1, "DLS tilt (deg, at least)", f"limit {limits['dls_tilt_deg']:g}"),
        ('dls_ratio', 3, "DLS ratio, panel / image", f"range {limits['dls_ratio'][0]:g} to {limits['dls_ratio'][1]:g}"))
    for key, digits, label, limit in descriptions:
        stats = images.get(key)
        if not stats:
            continue
        line = (f"Images ({stats['n']}): {label}: min {number(stats['min'], digits)}, median "
                f"{number(stats['median'], digits)}, max {number(stats['max'], digits)}")
        if limit:
            line += f"; {stats['outside']} outside the {limit}"
        lines.append(line + '.')
    if summary.get('irradiance_changes'):
        lines.append(f"Irradiance along the captures: {summary['irradiance_changes']} spike(s) or step(s) found.")
    return lines


def _write_result(path, result):
    """ Write the result atomically, so that a reader never finds a partial file """
    path = Path(path)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(rct._jsonable(result), indent=2), encoding='utf-8')
    os.replace(temporary, path)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Radiometric correction of MicaSense RedEdge-P captures")
    parser.add_argument('--config', help="json file with the values to change from the defaults (also the "
                                         "radiometric_result.json or a provenance file of a past run, to repeat it)")
    parser.add_argument('--request-json', help="json request: {'version': 1, 'root': folder, 'config': {...}}")
    parser.add_argument('--root', help="folder from which the relative paths are resolved (default: current folder)")
    parser.add_argument('--write-default-config', metavar='PATH', help="write the default configuration and exit")
    parser.add_argument('--detect-bands', nargs='?', const='', metavar='CAPTURE',
                        help="print the bands read from the metadata of a capture (default: the panel capture) and exit")
    arguments = parser.parse_args(argv)

    if arguments.write_default_config:
        Path(arguments.write_default_config).write_text(json.dumps(default_config(), indent=2), encoding='utf-8')
        print(f"Default configuration written to {Path(arguments.write_default_config).resolve()}")
        return 0
    root, values = arguments.root or '.', {}
    try:
        if arguments.request_json:
            request = json.loads(Path(arguments.request_json).read_text(encoding='utf-8'))
            if request.get('version') != REQUEST_VERSION:
                raise ConfigError(f"Unsupported request version {request.get('version')!r}")
            root, values = request.get('root', root), request.get('config', {})
        elif arguments.config:
            values = configuration_from_json(json.loads(Path(arguments.config).read_text(encoding='utf-8')))
    except (OSError, ValueError) as error:
        print(f"Invalid input: {error}", file=sys.stderr)
        return 2

    if arguments.detect_bands is not None:
        try:
            detected = detect_bands(merge_config(values), root, arguments.detect_bands or None)
        except Exception as error:
            print(f"ERROR: {type(error).__name__}: {error}", file=sys.stderr)
            return 1
        print(json.dumps(rct._jsonable(detected), indent=2))
        return 0

    def report(event):
        print(f"[{event['stage']}] {event['message']}", flush=True)

    result = run(values, root, report)
    for error in result['errors']:
        print(f"ERROR: {error}", file=sys.stderr)
    for flag in result['flags']:
        print(f"FLAG: {flag}")
    for line in summary_lines(result):
        print(line)
    if 'result_file' in result:
        print(f"RADIOMETRIC_RESULT_JSON={Path(result['result_file']).resolve()}")
    print(f"Status: {result['status']}")
    return 0 if result['status'] == 'completed' else 1


if __name__ == '__main__':
    sys.exit(main())
