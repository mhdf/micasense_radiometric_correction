# Optional DLS correction

[Back to the README](../README.md)

<div align="justify">

## What the DLS correction does

The panel capture calibrates the camera for the light of that moment. If the light changes between the panel capture and an image (a cloud, the height of the sun), the reflectance of the image is wrong by the same proportion. The DLS (downwelling light sensor) records the light during every capture and so shows how it changed. It does not measure the irradiance on a horizontal surface directly: it measures the light at its own sensor and estimates the horizontal irradiance (`XMP:HorizontalIrradiance`, one value per band) from those readings, which is why a tilted or shaded DLS gives a worse estimate (see [Checks of the DLS data](#checks-of-the-dls-data)).

The DLS ratio of a band is `E_panel / E_image`: the irradiance estimated at the panel capture divided by the one estimated at the image capture. An image taken in more light than the panel (ratio below 1) would look too bright, and multiplying by the ratio corrects it. A ratio of 0.885 means that the DLS estimated 13 % more light at the image than at the panel, so the reflectance is lowered by 11.5 %. The ratio is 1 at the panel capture. With `CORRECTION.use_dls` on, the reflectance of each image is:

```
reflectance(image) = radiance(image) * panel_reflectance / panel_radiance * E_panel / E_image
```

- At the panel capture the ratio is 1, so the result reproduces the panel-only conversion. Panel only is the default and never reads the DLS data.
- It is a relative correction: it assumes that the panel and the DLS saw the same illumination when the panel was photographed. The scale to SI units (`XMP:IrradianceScaleToSIUnits`) is applied when it exists; it cancels in the ratio, so it must be in both captures or in none.
- Each image uses only data written in files: its own irradiance (the metadata of that image) and that of the panel capture(s) used. There is no flight-wide reference, no averaging with neighboring images and no smoothing along the flight.
- The Panchro band is corrected like the others, with the irradiance written in its own file (in the example images its ratio, 0.8842, agrees with those of the multispectral bands, 0.883 to 0.888). A missing or invalid irradiance in any band, Panchro included, raises an error: no ratio is estimated from other bands.
- The correction does not account for the orientation or the shading of the DLS, so check the data as described below before relying on it.

## Checks of the DLS data

The limits are in `rct.DLS_CHECKS`; they are first guesses, not validated values, so adjust them to your data. Each check issues a `RuntimeWarning` and the messages are saved in the provenance record. The checks only raise flags; they never change a result.

- `rct.check_dls_capture(panel_images, images)`, for one capture. With two panel captures, the workflow compares each image with the panel that weighs most in its calibration (the first one when both weigh the same) and records its name (`compared_with_panel`):
  - the DLS ratio of every band must be within 3 % of the median of the bands (the light changes all the bands together);
  - the ratio must be inside 0.5 to 2;
  - the sun angle: the solar zenith is 90 degrees minus the solar elevation (`XMP:SolarElevation`, as written in the file), and the DLS writes the direction of the direct sunlight in its own frame (`XMP:EstimatedDirectLightVector`). With a level DLS the angle between that direction and the axis of the DLS equals the zenith, so a difference above 10 degrees means that the DLS was tilted by at least that much, or that the angles are inconsistent. This was checked only against the example panel capture (0.2 degrees, with a level DLS); the image capture of the example shows about 15 degrees and is flagged;
  - the DLS irradiance divided by the irradiance estimated from the panel (pi x radiance / reflectance) is reported per band. It differs between bands (about 18 % in the example, probably a calibration difference between camera and DLS), so it is not judged on one capture.
- The panel captures of one moment, when there are several, must agree within 2 % on what is averaged (`CHECKS.panel_capture_spread_tolerance`, see [several captures of one moment](reference-panel.md#several-captures-of-one-moment)).
- `rct.compare_panel_captures(before, after)`, for a panel photographed twice (for example before and after the flight): the panel factor of each band, and the DLS irradiance divided by the panel estimate, must not change by more than 5 %. The first change shows that the light or the panel changed, the second that the DLS did not follow the light seen by the camera.
- `rct.classify_irradiance_changes(irradiances)`, for the irradiance of one band along the flight in acquisition order: a *spike* is a single capture that jumps and comes back (a shaded or tilted DLS), a *step* is a change that persists (the sun going behind a cloud).

## Geometry of the acquisition

How the camera was pointed in each capture. The limits are in `rct.GEOMETRY_CHECKS` and in the configuration (`CHECKS.max_camera_tilt_deg`, `CHECKS.panel_heading_tolerance_deg`, `CHECKS.image_heading_tolerance_deg` and `CHECKS.heading_offset_deg`); they are first guesses. They only raise flags (`RuntimeWarning`), never change the result, and what is missing in the metadata is reported as unavailable instead of failing.

- **Camera tilt** (`rct.camera_tilt_deg()`): the angle between the optical axis of the camera and the vertical, from `XMP:Roll` and `XMP:Pitch`. Limit: 5 degrees, for the panel capture and for each image capture.
- **Heading and the sun** (`rct.heading_to_sun_deg()`): the angle between the heading of the camera (`XMP:Yaw` plus `CHECKS.heading_offset_deg`, the direction of the top of the image) and the direction of the sun (`XMP:SolarAzimuth`), from 0 to 180 degrees without a side: 0 is facing the sun, 90 the sun at the side, 180 the back to the sun. The panel is expected close to 0 or to 180 (within 30 degrees) and the images to correct close to 90 (within 30 degrees). `XMP:SolarAzimuth` was checked against the position of the sun calculated from the GPS position and the time of the example (the time written by the camera is UTC); whether `XMP:Yaw` is referred to true or to magnetic north is not known, which is what the offset is for.
- It is the heading of the camera that counts, not the direction of the flight lines. If the aircraft keeps its heading, the angle is the same on every line; if its nose follows the lines of a crossed pattern, one set of lines differs by 90 degrees from the other, and the sun cannot be at the side in both.
- `rct.check_panel_geometry(panel_images)` and `rct.check_image_geometry(images)` do the checks. The flags of a panel capture are written once in the result, and the values of the panel and of each capture are saved in the `geometry` of the checks of the provenance record.
- In the example, the panel capture shows a heading 87 degrees from the sun and is flagged (it was photographed with the sun at the side, not facing it or with the back to it) and the image capture shows 83 degrees, which is accepted.

## The flags of a run are counted by criterion

A criterion that fails in many captures is written once, with how many captures failed and which ones, not once per capture. In the example flight (409 captures; only a few of its captures are in `images/raw`, the complete flight is not included) the line is, for instance:

```
DLS tilt over 10 deg in 386 of 409 captures (tilt of at least 10.1 to 22.0 deg): IMG_0010 to IMG_0044, IMG_0047, IMG_0050 to IMG_0100, ...
```

- Every capture is listed, in the order of the run, and three or more neighbors are written as `first to last` to keep it short. The `flag_groups` of `radiometric_result.json` have the same captures in a list (criterion, label, count, number of captures, captures, range of the values and, for the criteria checked in each band, the bands).
- The criteria that are counted are the tilt of the camera, its heading against the sun, the tilt of the DLS, the DLS ratio outside its plausible range and the ratio of a band that departs from the other bands, and the spikes and steps of the irradiance (one line for each kind, with the largest change and the number of bands in which it was found).
- The flags of the panel captures (heading, tilt, the DLS of the panel and the comparison of two panels) are not counted: they are written once with the name of the panel capture. The provenance record of each capture still has all of its own flags.

## The summary at the end of a run

At the end of the log, and in the command line output, the run says what it found over the whole run: how many captures were corrected, the geometry and the DLS of the panel captures and, over the corrected captures, the minimum, median and maximum of the tilt of the camera, the angle between its heading and the sun, the elevation of the sun, the tilt of the DLS and the DLS ratio, each with how many captures are outside the limits, and how many spikes or steps of the irradiance were found. The numbers are also in the `summary` of `radiometric_result.json`.

</div>
