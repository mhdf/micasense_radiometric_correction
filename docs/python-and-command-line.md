# Python library and workflow runner

[Back to the README](../README.md)

<div align="justify">

The processing needs the images, the reference panel and the metadata of the images, which is read with [ExifTool](https://exiftool.org/), so the field labels written by the camera must be kept.

## Example: raw images to reflectance with the Python library

The example below processes the example images in this repository (`images/raw`): the capture `IMG_0005` shows the reference panel and `IMG_0010` is the capture to be corrected. First, import the required libraries and the module.

```
# Standard library imports
import os
from pathlib import Path

# Third-party library imports
import tifffile

# Module for radiometric correction
#  Set the root directory for your working environment first
root_path = Path.cwd()
images_folder = 'images/raw'
results_folder = 'results/reflectance'
# Import module
import radiometric_correction_tools as rct

# Path to ExifTool - None uses the one found in the system PATH
exiftool_path = os.environ.get('exiftoolpath')
```

Next, provide the information required for processing: (i) the names of the captures with the reference panel and with the images to be transformed from raw Digital Numbers (DNs) to reflectance, (ii) the reflectance levels of the different bands in the reference panel, a value provided by MicaSense with each reference panel, and (iii) whether the DLS should be used. The files of each capture are matched to the bands using the band name written in their metadata (`XMP:BandName`), not the order of the files: in the example images the file `_3` is the Red band and `_6` is the Panchro band. Each result keeps the name of its source file, so `IMG_0010_3.tif` is the Red band both before and after the correction.

```
# Name of the captures: the reference panel and the images to be corrected
reference_panel_name = 'IMG_0005'
image_to_process_name = 'IMG_0010'

# Reflectance level for the different bands in the reference panel -
# these values are measured and provided by MicaSense, marked in the reference panel,
# but they can also be requested via the MicaSense technical support.
# obs.: the names must be the band names written by the camera (XMP:BandName), the order does not matter.
reflectance_level_reference_panel = { "Blue": 0.477, "Green": 0.478, "Panchro": 0.479, \
                                      "Red": 0.478, "Red edge": 0.478, "NIR": 0.477 }

# Use the Downwelling Light Sensor (DLS) to correct changes in illumination between the panel
# capture and each image? (False = panel only). All bands, Panchro included, use the irradiance written in their files.
use_dls = False

# Find the files of each capture and match them to the bands using the metadata (not the file order)
bands = list( reflectance_level_reference_panel.keys() )
reference_panel_files = rct.read_band_files( rct.find_capture_paths(root_path / images_folder, reference_panel_name),
                                             expected_bands=bands, exiftool_path=exiftool_path )
image_files = rct.read_band_files( rct.find_capture_paths(root_path / images_folder, image_to_process_name),
                                   expected_bands=bands, exiftool_path=exiftool_path )
```

Finally, iterate over the bands, saving the images after transforming them from DNs to radiance and then to reflectance.

```
# Images of both captures (the panel is detected inside the loop)
reference_panel_objs = { band: rct.Image( *reference_panel_files[band] ) for band in bands }
image_objs = { band: rct.Image( *image_files[band] ) for band in bands }

band_records = []
for band in bands: # Iterate over bands

    # Reference panel image (path and metadata of the file of this band)
    reference_panel_obj = reference_panel_objs[band]

    # Identify automatically the coordinates for the reference panel in the undistorted image and
    # calculate the factor used to convert radiance to reflectance
    panel_bounds, radiance_to_reflectance_factor, _, _ = \
            reference_panel_obj.detect_reference_panel( reflectance_level_reference_panel,
                                                        percent_inner_area=0.7, sat_threshold=65000 )
    print( reference_panel_obj.panel_audit )  # what was used to calibrate this band

    # Image to be processed
    image_obj = image_objs[band]

    # Factor applied to the image: the panel factor and, with the DLS, the DLS ratio E_panel / E_image.
    # The list holds the panels and their weights (one panel here, weight 1). A missing or invalid
    # irradiance, Panchro included, raises an error: nothing is estimated from other bands
    factor_applied, panels_used = rct.combine_panels( [(reference_panel_obj, 1.0)],
                                                      image_obj if use_dls else None )

    # Reflectance image
    image_reflectance = image_obj.digital_numbers_to_reflectance( factor_applied )

    ##
    # Save reflectance image
    output_path = root_path / results_folder / Path(image_files[band][0]).name
    tifffile.imwrite( output_path, image_reflectance )  # keeps the float values (no rescaling to integers)
    band_records.append( rct.band_record(output_path, image_obj, image_reflectance, panels_used, factor_applied) )

# Checks of the DLS data of the capture (the messages are also issued as warnings)
checks = rct.check_dls_capture( reference_panel_objs, image_objs )
print( checks['flags'] )

# Record of what was used to produce the results, saved with them
rct.write_provenance( root_path / results_folder / (image_to_process_name + '_provenance.json'), band_records,
                      parameters={ 'reflectance_level_reference_panel': reflectance_level_reference_panel,
                                   'percent_inner_area': 0.7, 'sat_threshold': 65000, 'use_dls': use_dls },
                      checks=checks )
```

In summary, `rct.Image(image, metadata)` creates an object with the image and its information; `detect_reference_panel(...)` finds the reference panel on the undistorted radiance image and calculates the factor that converts radiance to reflectance; `rct.combine_panels(...)` gives the factor applied to the image (with the DLS, the panel factor times the DLS ratio; with two panels, their weighted mean); and `digital_numbers_to_reflectance(factor)` transforms the target image to radiance and multiplies it by that factor. `rct.find_capture_paths()` and `rct.read_band_files()` list the files of a capture and match them to the bands.

## Checks and calibration record

- Metadata are checked when an image is created: a missing or invalid field (non-numeric, wrong number of values, non-positive exposure or ISO and so on) raises a `rct.MetadataError` that names the file and the field.
- `detect_reference_panel()` raises a clear error if the QR code is not found, if the band has no reflectance level or if all the panel pixels are saturated; saturated pixels (above `sat_threshold`) are left out of the panel mean. Afterwards `panel_audit` holds what was used to calibrate the band: the panel region, the number of pixels and of saturated pixels, the mean and standard deviation of the radiance, the reflectance level and the final factor.
- `rct.band_record()` and `rct.write_provenance()` save a json file with what was used to produce the results (`IMG_0010_provenance.json` in the example): the ids of the image and panel captures and the attitude of the camera; for each band, the image file, the factor applied and the panels used, each with its file, weight, factor, audit and DLS irradiance (recorded even when the DLS is not used) and, when the DLS is used, its DLS ratio and adjusted factor; the DLS irradiance of the image; the checks of the capture; the parameters of the run (the complete configuration, with the reflectance of the panel); and the software used: the versions of Python, numpy and opencv and the SHA-256 of `radiometric_correction_tools.py`, a fingerprint of the content of that file (see [Panel reflectance](reference-panel.md#panel-reflectance-from-its-spectral-response-file)), so that the exact code that made the results can be identified.

## Workflow runner (no user interface)

`radiometric_app/radiometric_workflow.py` runs the whole example above, for all the captures of a folder, from a configuration with fixed keys. It has no user interface (the UI is built on top of it, from `FIELDS`, which describes each key: default, kind of value, label and description).

```
python -m radiometric_app.radiometric_workflow --write-default-config config/radiometric_inputs.json   # write the defaults, then edit them
python -m radiometric_app.radiometric_workflow --config config/radiometric_inputs.json                   # run
python -m radiometric_app.radiometric_workflow --request-json request.json                               # run from a request (see below)
```

- The configuration is a flat json file with the keys of `FIELDS` (`INPUTS.*` folders, captures and what is written, `PANEL.*` reflectance levels and panel measurement, `CORRECTION.use_dls`, `CHECKS.*` limits). Only the keys to change are needed; unknown keys are an error. Relative paths are resolved from `--root` (default: the current folder). Labels and descriptions may change in future versions, the keys will not.
- With `INPUTS.image_captures` empty, every capture in the images folder except the panel capture(s) and those of `INPUTS.excluded_captures` is corrected. The panel is detected once and used for all of them. The panel captures are before or after the flight by their time; the panel after the flight is compared with the one before and, depending on `PANEL.panels_used`, can also calibrate the images (see [two panel captures](reference-panel.md#two-panel-captures-before-and-after-the-flight)). `radiometric_workflow.check_panel_captures(config, root)` measures and checks the panel captures without correcting anything.
- The stages are `LOAD_CAPTURES`, `PANEL_CALIBRATION`, `REFLECTANCE`, `DLS_CHECKS` and `PROVENANCE`. A capture that fails (for example a missing band file, or a missing DLS irradiance when `CORRECTION.use_dls` is on) is reported and the others continue.
- Each corrected capture gets its `<capture>_provenance.json`. The whole run gets `radiometric_result.json` in the results folder, which says what happened and where to look: the status (`completed`, `completed_with_errors` or `failed`), the errors, the flags of all the checks (see [the flags of a run](dls-correction.md#the-flags-of-a-run-are-counted-by-criterion); `flag_groups` has the structured form), the complete configuration, the panel calibration, the spikes and steps of the irradiance and, for each capture, its output files, the panels used, its errors and a summary of its DLS checks. `outputs` has the complete path of every output file. The command prints `RADIOMETRIC_RESULT_JSON=<path>` and returns 0 only for `completed` (1 for errors, 2 for an unreadable request or configuration file).
- A request is `{"version": 1, "root": "<folder>", "config": {...}}`, so that another program can start the workflow as a separate process (the libraries needed here are usually not available in the Python of a photogrammetry program) and read the result file.
- From Python: `radiometric_workflow.run(config, root, progress)`, where `progress` receives `{'stage', 'message', 'current', 'total'}`.

</div>
