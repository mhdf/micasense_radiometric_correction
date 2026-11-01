"""
Tests for the metadata validation and band-file mapping in radiometric_correction_tools.py

Run from the project folder with:  python -m unittest tests.test_radiometric_correction_tools -v
The tests that read the example images need ExifTool; set the environment variable
'exiftoolpath' if it is not on the PATH.
"""

import os
import tempfile
import unittest
import warnings

import numpy as np
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import radiometric_correction_tools as rct

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / 'images' / 'raw'
BANDS = ['Blue', 'Green', 'Panchro', 'Red', 'Red edge', 'NIR']
REFLECTANCE_LEVELS = { "Blue": 0.477, "Green": 0.478, "Panchro": 0.479,
                       "Red": 0.478, "Red edge": 0.478, "NIR": 0.477 }


def fake_metadata(band, capture='A'):
    return {'XMP:BandName': band, 'XMP:CaptureId': capture}


class MapBandFilesTests(unittest.TestCase):

    def test_maps_by_band_name_not_by_file_order(self):
        paths = ['a_1.tif', 'a_2.tif', 'a_3.tif']
        metadata = [fake_metadata('Red'), fake_metadata('Blue'), fake_metadata('Green')]
        mapping = rct.map_band_files(paths, metadata, ['Blue', 'Green', 'Red'])
        self.assertEqual(mapping['Red'][0], 'a_1.tif')
        self.assertEqual(mapping['Blue'][0], 'a_2.tif')
        self.assertEqual(mapping['Green'][0], 'a_3.tif')

    def test_duplicate_band_is_rejected(self):
        with self.assertRaisesRegex(rct.MetadataError, "Band 'Blue' appears twice"):
            rct.map_band_files(['a_1.tif', 'a_2.tif'], [fake_metadata('Blue'), fake_metadata('Blue')])

    def test_missing_band_is_reported(self):
        with self.assertRaisesRegex(rct.MetadataError, "missing: \\['Green'\\]"):
            rct.map_band_files(['a_1.tif'], [fake_metadata('Blue')], ['Blue', 'Green'])

    def test_unexpected_band_is_reported(self):
        with self.assertRaisesRegex(rct.MetadataError, "unexpected: \\['Red'\\]"):
            rct.map_band_files(['a_1.tif', 'a_2.tif'], [fake_metadata('Blue'), fake_metadata('Red')], ['Blue'])

    def test_mixed_captures_are_rejected(self):
        with self.assertRaisesRegex(rct.MetadataError, 'different captures'):
            rct.map_band_files(['a_1.tif', 'b_1.tif'],
                               [fake_metadata('Blue', 'A'), fake_metadata('Green', 'B')])

    def test_missing_band_name_names_the_file(self):
        with self.assertRaisesRegex(rct.MetadataError, "a_1.tif: missing metadata field 'XMP:BandName'"):
            rct.map_band_files(['a_1.tif'], [{}])


class FilePatternTests(unittest.TestCase):

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.folder.cleanup()

    def touch(self, *names):
        for name in names:
            (Path(self.folder.name) / name).write_bytes(b'')

    def test_the_default_pattern_groups_the_files_of_a_capture_in_numeric_order(self):
        self.touch('IMG_0001_1.tif', 'IMG_0001_2.tif', 'IMG_0001_10.tif', 'IMG_0002_1.tif', 'notes.txt', 'IMG_x.tif')
        self.assertEqual(rct.list_capture_names(self.folder.name), ['IMG_0001', 'IMG_0002'])
        names = [Path(path).name for path in rct.find_capture_paths(self.folder.name, 'IMG_0001')]
        self.assertEqual(names, ['IMG_0001_1.tif', 'IMG_0001_2.tif', 'IMG_0001_10.tif'])    # 10 after 2, not after 1

    def test_the_extension_is_not_case_sensitive(self):
        self.touch('DJI_0001_1.TIF', 'DJI_0001_2.Tif')
        self.assertEqual(rct.list_capture_names(self.folder.name), ['DJI_0001'])

    def test_another_naming_scheme(self):
        self.touch('DJI_0001_MS_G.TIF', 'DJI_0001_MS_R.TIF', 'DJI_0002_MS_G.TIF', 'DJI_0001.JPG')
        pattern = r'(?P<capture>DJI_\d+)_MS_(?P<band>[A-Z]+)\.tif$'
        self.assertEqual(rct.list_capture_names(self.folder.name, pattern), ['DJI_0001', 'DJI_0002'])
        names = [Path(path).name for path in rct.find_capture_paths(self.folder.name, 'DJI_0001', pattern)]
        self.assertEqual(names, ['DJI_0001_MS_G.TIF', 'DJI_0001_MS_R.TIF'])             # no index: by name

    def test_a_missing_capture_names_the_pattern(self):
        self.touch('IMG_0001_1.tif')
        with self.assertRaisesRegex(FileNotFoundError, "'IMG_9'.*pattern"):
            rct.find_capture_paths(self.folder.name, 'IMG_9')

    def test_several_folders_are_read_as_one_and_a_repeated_capture_uses_the_copy_chosen(self):
        flight, panels = Path(self.folder.name) / 'flight', Path(self.folder.name) / 'panels'
        flight.mkdir()
        panels.mkdir()
        for folder, names in ((flight, ('IMG_0005_1.tif', 'IMG_0005_2.tif', 'IMG_0010_1.tif', 'IMG_0010_2.tif')),
                              (panels, ('IMG_0003_1.tif', 'IMG_0005_1.tif', 'IMG_0005_2.tif'))):
            for name in names:
                (folder / name).write_bytes(b'')
        self.assertEqual(rct.folder_list(f"{flight} | {panels} | {flight}"), [str(flight), str(panels)])
        self.assertEqual(rct.folder_list([flight, '']), [str(flight)])
        self.assertEqual(rct.folder_list(''), [])
        both = [str(flight), str(panels)]
        self.assertEqual(rct.list_capture_names(both), ['IMG_0003', 'IMG_0005', 'IMG_0010'])     # once each
        self.assertEqual(list(rct.repeated_captures(both)), ['IMG_0005'])
        self.assertEqual(list(rct.repeated_captures(both)['IMG_0005']), [flight, panels])
        self.assertEqual(rct.unresolved_captures(both), ['IMG_0005'])
        self.assertEqual(rct.unresolved_captures(both, names=['IMG_0010']), [])                # only those used
        with self.assertRaisesRegex(ValueError, "'IMG_0005' is in more than one images folder"):
            rct.find_capture_paths(both, 'IMG_0005')
        self.assertEqual(Path(rct.find_capture_paths(both, 'IMG_0003')[0]).parent, panels)
        chosen = rct.ImageFolders(both, {'IMG_0005': panels, 'IMG_0010': flight})               # IMG_0010 is not repeated
        self.assertEqual(rct.unresolved_captures(chosen), [])
        self.assertEqual([Path(path).parent for path in rct.find_capture_paths(chosen, 'IMG_0005')], [panels, panels])
        self.assertEqual(rct.list_capture_names(chosen), ['IMG_0003', 'IMG_0005', 'IMG_0010'])
        elsewhere = rct.ImageFolders(both, {'IMG_0005': Path(self.folder.name)})                # not one of the copies
        self.assertEqual(rct.unresolved_captures(elsewhere), ['IMG_0005'])
        info = rct.preview_file_pattern(chosen)
        self.assertEqual((info['files'], info['matched'], info['captures']), (7, 7, 3))
        self.assertEqual(info['files_per_capture'], {1: 1, 2: 3})                               # each copy counted once
        self.assertEqual(str(chosen), f"{flight} | {panels}")

    def test_a_template_is_read_like_the_regular_expression_it_stands_for(self):
        self.touch('IMG_0001_1.tif', 'IMG_0001_2.TIF', 'IMG_0002_1.tif', 'DJI_0001_MS_G.TIF', 'DJI_0001_MS_R.TIF', 'x.txt')
        self.assertEqual(rct.list_capture_names(self.folder.name, '{capture}_{index}.tif'), ['IMG_0001', 'IMG_0002'])
        self.assertEqual(rct.list_capture_names(self.folder.name, 'IMG_{capture}_{index}.tif'), ['0001', '0002'])
        self.assertEqual(rct.list_capture_names(self.folder.name, '{capture}_MS_*.tif'), ['DJI_0001'])
        names = [Path(path).name for path in rct.find_capture_paths(self.folder.name, 'IMG_0001', '{capture}_{index}.tif')]
        self.assertEqual(names, ['IMG_0001_1.tif', 'IMG_0001_2.TIF'])
        with self.assertRaisesRegex(ValueError, 'only once'):
            rct.compile_file_pattern('{capture}_{capture}.tif')

    def test_invalid_patterns_are_reported(self):
        with self.assertRaisesRegex(ValueError, 'not a valid regular expression'):
            rct.compile_file_pattern('(unclosed')
        with self.assertRaisesRegex(ValueError, "needs {capture}"):
            rct.compile_file_pattern(r'(.+)_(\d+)\.tif$')
        self.assertIsNotNone(rct.compile_file_pattern(None))


class GeometryTests(unittest.TestCase):

    def test_the_tilt_of_the_camera_comes_from_the_roll_and_the_pitch(self):
        self.assertAlmostEqual(rct.camera_tilt_deg({'XMP:Roll': 0.0, 'XMP:Pitch': 0.0}), 0.0)
        self.assertAlmostEqual(rct.camera_tilt_deg({'XMP:Roll': 3.0, 'XMP:Pitch': 0.0}), 3.0)
        self.assertAlmostEqual(rct.camera_tilt_deg({'XMP:Roll': '3.0', 'XMP:Pitch': '4.0'}), 4.9985, places=3)
        self.assertIsNone(rct.camera_tilt_deg({'XMP:Roll': 3.0}))

    def test_the_angle_between_the_heading_and_the_sun_has_no_side(self):
        azimuth = lambda degrees: {'XMP:SolarAzimuth': np.radians(degrees)}
        self.assertAlmostEqual(rct.heading_to_sun_deg(dict(azimuth(100), **{'XMP:Yaw': 10.0})), 90.0)
        self.assertAlmostEqual(rct.heading_to_sun_deg(dict(azimuth(100), **{'XMP:Yaw': 190.0})), 90.0)
        self.assertAlmostEqual(rct.heading_to_sun_deg(dict(azimuth(10), **{'XMP:Yaw': 350.0})), 20.0)    # across north
        self.assertAlmostEqual(rct.heading_to_sun_deg(dict(azimuth(10), **{'XMP:Yaw': -10.0})), 20.0)
        self.assertAlmostEqual(rct.heading_to_sun_deg(dict(azimuth(10), **{'XMP:Yaw': 10.0})), 0.0)       # facing the sun
        self.assertAlmostEqual(rct.heading_to_sun_deg(dict(azimuth(10), **{'XMP:Yaw': 190.0})), 180.0)    # the back to it
        self.assertAlmostEqual(rct.heading_to_sun_deg(dict(azimuth(100), **{'XMP:Yaw': 30.0}), offset=60.0), 10.0)
        self.assertIsNone(rct.heading_to_sun_deg({'XMP:Yaw': 10.0}))
        self.assertAlmostEqual(rct.sun_elevation_deg({'XMP:SolarElevation': np.radians(66.0)}), 66.0)
        self.assertIsNone(rct.sun_elevation_deg({}))

    def images(self, **metadata):
        from types import SimpleNamespace
        values = {'XMP:Roll': 0.0, 'XMP:Pitch': 0.0, 'XMP:Yaw': 0.0, 'XMP:SolarAzimuth': np.radians(90.0),
                  'XMP:SolarElevation': np.radians(60.0)}
        values.update(metadata)
        return {band: SimpleNamespace(metadata=values, dls_sun_angle_discrepancy_deg=lambda: 2.0) for band in ('Blue', 'Red')}

    def test_the_panel_is_expected_facing_the_sun_or_with_the_back_to_it(self):
        facing = rct.check_panel_geometry(self.images(**{'XMP:Yaw': 80.0}))             # 10 deg from facing the sun
        self.assertEqual(facing['flags'], [])
        self.assertAlmostEqual(facing['values']['heading_departure_deg'], 10.0)
        back = rct.check_panel_geometry(self.images(**{'XMP:Yaw': 280.0}))              # 170: 10 deg from the back to it
        self.assertEqual(back['flags'], [])
        side = rct.check_panel_geometry(self.images(**{'XMP:Yaw': 0.0}))                # the sun at the side
        self.assertEqual(len(side['flags']), 1)
        self.assertIn('90 deg from the sun', side['flags'][0])
        self.assertIn('panel capture', side['flags'][0])

    def test_the_images_are_expected_with_the_sun_at_the_side(self):
        side = rct.check_image_geometry(self.images())
        self.assertEqual(side['flags'], [])
        self.assertAlmostEqual(side['values']['heading_departure_deg'], 0.0)
        facing = rct.check_image_geometry(self.images(**{'XMP:Yaw': 80.0}))
        self.assertEqual(len(facing['flags']), 1)
        self.assertIn('image capture', facing['flags'][0])
        self.assertIn('within 30 deg of 90', facing['flags'][0])

    def test_a_tilted_camera_is_flagged_and_the_limits_can_be_changed(self):
        tilted = rct.check_image_geometry(self.images(**{'XMP:Roll': 8.0}))
        self.assertEqual(len(tilted['flags']), 1)
        self.assertIn('Camera tilt of the image capture: 8.0 deg', tilted['flags'][0])
        with mock.patch.dict(rct.GEOMETRY_CHECKS, {'max_camera_tilt_deg': 10.0}):
            self.assertEqual(rct.check_image_geometry(self.images(**{'XMP:Roll': 8.0}))['flags'], [])
        self.assertEqual(len(rct.check_image_geometry(self.images(**{'XMP:Yaw': -80.0}))['flags']), 1)    # 170 from the sun
        with mock.patch.dict(rct.GEOMETRY_CHECKS, {'heading_offset_deg': 80.0}):               # the yaw is corrected first
            self.assertEqual(rct.check_image_geometry(self.images(**{'XMP:Yaw': -80.0}))['flags'], [])

    def test_each_flag_has_details_to_count_the_same_criterion_over_the_captures(self):
        tilted = rct.check_image_geometry(self.images(**{'XMP:Roll': 8.0, 'XMP:Yaw': 80.0}))
        self.assertEqual(len(tilted['flags']), len(tilted['flag_details']))
        self.assertEqual([detail['criterion'] for detail in tilted['flag_details']], ['camera_tilt', 'heading'])
        tilt, heading = tilted['flag_details']
        self.assertEqual((tilt['scope'], tilt['limit']), ('image', 5.0))
        self.assertAlmostEqual(tilt['value'], 8.0, places=1)
        self.assertEqual((heading['scope'], heading['limit']), ('image', 30.0))
        self.assertAlmostEqual(heading['value'], 10.0)
        panel = rct.check_panel_geometry(self.images(**{'XMP:Yaw': 0.0}))
        self.assertEqual([(detail['criterion'], detail['scope']) for detail in panel['flag_details']], [('heading', 'panel')])
        self.assertEqual(rct.check_image_geometry(self.images())['flag_details'], [])

    def test_what_is_missing_is_reported_and_not_flagged(self):
        from types import SimpleNamespace
        empty = {'Blue': SimpleNamespace(metadata={}, dls_sun_angle_discrepancy_deg=lambda: None)}
        result = rct.check_image_geometry(empty)
        self.assertEqual(result['flags'], [])
        self.assertEqual(len(result['unavailable']), 2)
        self.assertIsNone(result['values']['camera_tilt_deg'])

    def test_the_values_are_the_median_of_the_bands(self):
        from types import SimpleNamespace
        images = {band: SimpleNamespace(metadata={'XMP:Roll': roll, 'XMP:Pitch': 0.0}, dls_sun_angle_discrepancy_deg=lambda: None)
                  for band, roll in (('A', 1.0), ('B', 2.0), ('C', 9.0))}
        self.assertAlmostEqual(rct.measure_geometry(images)['camera_tilt_deg'], 2.0)

    def test_the_flags_are_issued_as_warnings(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            rct.check_image_geometry(self.images(**{'XMP:Roll': 8.0}))
        self.assertTrue(any(issubclass(warning.category, RuntimeWarning) and 'Camera tilt' in str(warning.message)
                            for warning in caught))


class PatternPreviewTests(unittest.TestCase):

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.folder.cleanup()

    def touch(self, *names):
        for name in names:
            (Path(self.folder.name) / name).write_bytes(b'')

    def test_the_pattern_is_written_as_a_name(self):
        self.assertEqual(rct.pattern_template(rct.CAPTURE_FILE_PATTERN), '{capture}_{index}.tif')
        self.assertEqual(rct.pattern_template(r'(?P<capture>.+)_(?P<index>\d+)\.tif$'), '<capture>_<index>.tif')
        self.assertEqual(rct.pattern_template(r'^(?P<capture>DJI_\d+)_MS_(?P<band>[A-Z]+)\.tif$'), '<capture>_MS_<band>.tif')

    def test_the_preview_counts_files_and_captures(self):
        self.touch('IMG_0001_1.tif', 'IMG_0001_2.tif', 'IMG_0002_1.tif', 'notes.txt')
        info = rct.preview_file_pattern(self.folder.name)
        self.assertEqual((info['files'], info['matched'], info['captures']), (4, 3, 2))
        self.assertEqual(info['files_per_capture'], {1: 1, 2: 1})
        self.assertEqual(info['examples'], [('IMG_0001_1.tif', 'IMG_0001', 1), ('IMG_0002_1.tif', 'IMG_0002', 1)])
        self.assertEqual(info['unmatched'], ['notes.txt'])
        with self.assertRaises(ValueError):
            rct.preview_file_pattern(self.folder.name, '(unclosed')

    def test_a_pattern_is_suggested_from_the_names(self):
        names = ['IMG_0001_1.tif', 'IMG_0001_2.tif', 'IMG_0002_1.tif', 'notes.txt']
        pattern = rct.suggest_file_pattern(names)
        self.assertEqual(pattern, '{capture}_{index}.tif')
        self.touch(*names)
        self.assertEqual(rct.list_capture_names(self.folder.name, pattern), ['IMG_0001', 'IMG_0002'])
        dashed = rct.suggest_file_pattern(['flight-A-0001-1.TIF', 'flight-A-0001-2.TIF'])
        self.assertEqual(dashed, '{capture}-{index}.tif')
        self.assertTrue(rct.compile_file_pattern(dashed).match('flight-A-0001-3.tif'))      # the extension is not case sensitive
        self.assertIsNone(rct.suggest_file_pattern(['readme.txt', 'photo.jpg']))
        self.assertIsNone(rct.suggest_file_pattern([]))


class IgnoredBandsTests(unittest.TestCase):

    def test_ignored_bands_are_left_out_of_the_mapping_and_the_checks(self):
        metadata = [fake_metadata('Blue'), fake_metadata('LWIR', 'OTHER-CAPTURE-ID'), fake_metadata('Green')]
        mapping = rct.map_band_files(['a_1.tif', 'a_2.tif', 'a_3.tif'], metadata, ['Blue', 'Green'], ignore_bands=['LWIR'])
        self.assertEqual(list(mapping), ['Blue', 'Green'])

    def test_an_extra_band_that_is_not_ignored_is_still_an_error(self):
        metadata = [fake_metadata('Blue'), fake_metadata('LWIR')]
        with self.assertRaisesRegex(rct.MetadataError, "unexpected: \\['LWIR'\\]"):
            rct.map_band_files(['a_1.tif', 'a_2.tif'], metadata, ['Blue'])


class BandDefinitionTests(unittest.TestCase):

    @staticmethod
    def metadata(name, center, width, rig, **extra):
        return dict({'XMP:BandName': name, 'XMP:CentralWavelength': center, 'XMP:WavelengthFWHM': width,
                     'XMP:RigCameraIndex': rig, 'EXIF:Make': 'MicaSense', 'EXIF:Model': 'Altum',
                     'EXIF:SerialNumber': 'AL06-1'}, **extra)

    def read(self, metadata_list):
        paths = [f'f_{i}.tif' for i in range(len(metadata_list))]
        with mock.patch.object(rct, '_read_metadata', return_value=metadata_list):
            return rct.read_band_definitions(paths)

    def test_bands_come_in_the_order_of_the_rig_with_their_windows(self):
        result = self.read([self.metadata('NIR', 842, 57, 3), self.metadata('Blue', 475, 32, 0),
                            self.metadata('Green', 560, 27, 1)])
        self.assertEqual([band['name'] for band in result['bands']], ['Blue', 'Green', 'NIR'])
        self.assertEqual(result['bands'][2]['center_nm'], 842.0)
        self.assertEqual(result['bands'][2]['bandwidth_nm'], 57.0)
        self.assertEqual((result['camera'], result['serial']), ('MicaSense Altum', 'AL06-1'))

    def test_a_thermal_band_is_suggested_to_be_left_out(self):
        result = self.read([self.metadata('Blue', 475, 32, 0), self.metadata('LWIR', 11000, 6000, 5)])
        self.assertEqual([band['thermal'] for band in result['bands']], [False, True])
        result = self.read([self.metadata('Thermal', 10, 1, 0)])
        self.assertTrue(result['bands'][0]['thermal'])

    def test_missing_fields_and_duplicate_names_are_reported(self):
        broken = self.metadata('Blue', 475, 32, 0)
        del broken['XMP:WavelengthFWHM']
        with self.assertRaisesRegex(rct.MetadataError, "XMP:WavelengthFWHM"):
            self.read([broken])
        with self.assertRaisesRegex(rct.MetadataError, 'more than one file'):
            self.read([self.metadata('Blue', 475, 32, 0), self.metadata('Blue', 475, 32, 1)])

    def test_files_without_a_rig_index_come_after_the_others_by_name(self):
        result = self.read([self.metadata('B', 500, 10, None), self.metadata('A', 400, 10, None),
                            self.metadata('C', 600, 10, 0)])
        self.assertEqual([band['name'] for band in result['bands']], ['C', 'A', 'B'])


class TwoPanelsTests(unittest.TestCase):

    def test_the_weight_of_the_second_panel(self):
        self.assertEqual(rct.panel_weight('first', 50, 0, 100), 0.0)
        self.assertEqual(rct.panel_weight('last', 50, 0, 100), 1.0)                       # only the second panel
        self.assertEqual(rct.panel_weight('mean', 50, 0, 100), 0.5)
        self.assertEqual(rct.panel_weight('interpolate_time', 0, 0, 100), 0.0)
        self.assertEqual(rct.panel_weight('interpolate_time', 100, 0, 100), 1.0)
        self.assertEqual(rct.panel_weight('interpolate_time', 25, 0, 100), 0.25)

    def test_the_nearest_panel_in_time_is_used_entirely(self):
        self.assertEqual(rct.panel_weight('nearest_time', 10, 0, 100), 0.0)
        self.assertEqual(rct.panel_weight('nearest_time', 90, 0, 100), 1.0)
        self.assertEqual(rct.panel_weight('nearest_time', 49, 0, 100), 0.0)
        self.assertEqual(rct.panel_weight('nearest_time', 51, 0, 100), 1.0)
        self.assertEqual(rct.panel_weight('nearest_time', 50, 0, 100), 0.0)        # equally close: the first
        self.assertEqual(rct.panel_weight('nearest_time', -30, 0, 100), 0.0)       # outside the two panels
        self.assertEqual(rct.panel_weight('nearest_time', 400, 0, 100), 1.0)
        self.assertEqual(rct.panel_weight('nearest_time', 7, 5, 5), 0.0)           # panels taken at the same time

    def test_outside_the_two_panels_the_nearest_one_is_used(self):
        self.assertEqual(rct.panel_weight('interpolate_time', -30, 0, 100), 0.0)
        self.assertEqual(rct.panel_weight('interpolate_time', 400, 0, 100), 1.0)

    def test_panels_taken_at_the_same_time_count_the_same(self):
        self.assertEqual(rct.panel_weight('interpolate_time', 7, 5, 5), 0.5)

    def test_an_unknown_mode_is_an_error(self):
        with self.assertRaisesRegex(ValueError, 'panels_used'):
            rct.panel_weight('later', 1, 0, 2)

    def test_the_light_of_a_capture_is_compared_with_that_of_a_panel(self):
        light = {'Blue': 140.0, 'Green': 120.0, 'Red': 100.0}
        self.assertEqual(rct.illumination_distance(light, dict(light)), 0.0)
        brighter = {band: 1.1 * value for band, value in light.items()}                 # 10 % more in every band
        self.assertAlmostEqual(rct.illumination_distance(brighter, light), np.log(1.1))
        self.assertAlmostEqual(rct.illumination_distance(light, brighter), np.log(1.1))     # it does not matter which one
        spread = dict(light, Red=120.0)                                                  # only one band is different
        self.assertAlmostEqual(rct.illumination_distance(spread, light), np.sqrt(np.log(1.2) ** 2 / 3))
        self.assertAlmostEqual(rct.illumination_distance(light, {'Red': 100.0, 'NIR': 3.0}), 0.0)    # the bands in both
        with self.assertRaisesRegex(rct.MetadataError, 'no band in common'):
            rct.illumination_distance(light, {'NIR': 50.0})
        with self.assertRaisesRegex(rct.MetadataError, "band 'Blue'"):
            rct.illumination_distance(light, dict(light, Blue=0.0))

    def test_the_panel_with_the_most_similar_light_is_used_entirely(self):
        self.assertEqual(rct.panel_weight('nearest_illumination', distances=(0.3, 0.02)), 1.0)
        self.assertEqual(rct.panel_weight('nearest_illumination', distances=(0.02, 0.3)), 0.0)
        self.assertEqual(rct.panel_weight('nearest_illumination', distances=(0.1, 0.1)), 0.0)    # equally like it: the first
        self.assertEqual(rct.panel_weight('nearest_illumination', distances=(0.0, 0.0)), 0.0)

    def test_the_weights_by_illumination_change_smoothly_with_the_light(self):
        self.assertEqual(rct.panel_weight('interpolate_illumination', distances=(0.1, 0.1)), 0.5)
        self.assertEqual(rct.panel_weight('interpolate_illumination', distances=(0.0, 0.0)), 0.5)
        self.assertEqual(rct.panel_weight('interpolate_illumination', distances=(0.3, 0.0)), 1.0)    # the light of the second
        self.assertEqual(rct.panel_weight('interpolate_illumination', distances=(0.0, 0.3)), 0.0)
        self.assertAlmostEqual(rct.panel_weight('interpolate_illumination', distances=(0.3, 0.1)), 0.9)    # 1/d**2: 100 and 900
        self.assertAlmostEqual(rct.panel_weight('interpolate_illumination', distances=(0.1, 0.3)), 0.1)
        # the light of the image moves away from the second panel (its distance grows) and towards the first
        weights = [rct.panel_weight('interpolate_illumination', distances=(0.3 - x, x)) for x in np.linspace(0.0, 0.3, 61)]
        self.assertTrue(all(b <= a for a, b in zip(weights, weights[1:])))                         # no reversal
        self.assertLess(max(abs(np.diff(weights))), 0.05)                                          # and no step

    def test_the_modes_by_illumination_need_the_distances(self):
        for mode in ('nearest_illumination', 'interpolate_illumination'):
            self.assertIn(mode, rct.PANELS_USED)
            with self.assertRaisesRegex(ValueError, 'distances'):
                rct.panel_weight(mode)

    def test_the_time_of_a_capture_is_read_from_the_metadata(self):
        first = rct.capture_time({'EXIF:DateTimeOriginal': '2023:02:02 13:26:38'})
        second = rct.capture_time({'EXIF:DateTimeOriginal': '2023:02:02 13:30:11'})
        self.assertEqual(second - first, 213.0)
        for bad in ({}, {'EXIF:DateTimeOriginal': 'yesterday'}, {'EXIF:DateTimeOriginal': None}):
            with self.assertRaisesRegex(rct.MetadataError, 'IMG_1: EXIF:DateTimeOriginal'):
                rct.capture_time(bad, 'IMG_1')

    class Fake:
        """ A band of a capture with what combine_panels and band_record read """
        def __init__(self, name, factor=None, irradiance=None):
            self.image_path, self.metadata, self.band_name = name, {'XMP:CaptureId': name[:2]}, 'Blue'
            self.panel_audit = {'radiance_to_reflectance_factor': factor} if factor is not None else None
            self.horizontal_irradiance, self.irradiance_scale_to_si = irradiance, None

        def dls_irradiance(self):
            return self.horizontal_irradiance

        def dls_sun_angle_discrepancy_deg(self):
            return None

    def test_without_the_dls_the_factors_are_averaged_with_the_weight(self):
        first, second = self.Fake('p1.tif', 2.0), self.Fake('p2.tif', 3.0)
        self.assertEqual(rct.combine_panels([(first, 1.0), (second, 0.0)])[0], 2.0)
        self.assertAlmostEqual(rct.combine_panels([(first, 0.5), (second, 0.5)])[0], 2.5)
        self.assertAlmostEqual(rct.combine_panels([(first, 0.75), (second, 0.25)])[0], 2.25)
        factor, used = rct.combine_panels([(first, 0.0), (second, 1.0)])
        self.assertEqual((factor, [entry['panel'] for entry in used]), (3.0, [second]))       # a panel of weight 0 is left out
        self.assertEqual((used[0]['dls_ratio'], used[0]['adjusted_factor']), (None, 3.0))

    def test_with_the_dls_each_panel_is_brought_to_the_light_of_the_image_before_they_are_combined(self):
        image = self.Fake('img.tif', irradiance=125.0)
        first, second = self.Fake('p1.tif', 1.0, 100.0), self.Fake('p2.tif', 0.8, 125.0)
        factor, used = rct.combine_panels([(first, 0.0), (second, 1.0)], image)
        self.assertAlmostEqual(factor, 0.8)                              # the last panel had the light of the image
        self.assertEqual(used[0]['dls_ratio'], 1.0)
        factor, used = rct.combine_panels([(first, 0.5), (second, 0.5)], image)
        self.assertAlmostEqual(factor, 0.5 * 1.0 * 100 / 125 + 0.5 * 0.8)
        self.assertEqual([entry['dls_ratio'] for entry in used], [0.8, 1.0])
        self.assertAlmostEqual(used[0]['adjusted_factor'], 0.8)

    def test_the_weights_of_the_panels_must_add_up_to_one(self):
        with self.assertRaisesRegex(ValueError, 'add up to 1'):
            rct.combine_panels([(self.Fake('p1.tif', 1.0), 0.5)])
        with self.assertRaisesRegex(ValueError, 'at least one panel'):
            rct.combine_panels([(self.Fake('p1.tif', 1.0), 0.0)])

    def test_a_record_names_the_panels_used_and_the_factor_applied(self):
        import numpy as np
        image = self.Fake('img.tif', irradiance=125.0)
        first, second = self.Fake('p1.tif', 1.0, 100.0), self.Fake('p2.tif', 0.8, 125.0)
        factor, used = rct.combine_panels([(first, 0.7), (second, 0.3)], image)
        record = rct.band_record('out.tif', image, np.zeros((2, 2)), used, factor)
        self.assertEqual(record['factor_applied'], factor)
        self.assertEqual([(panel['file'], panel['weight'], panel['dls_ratio']) for panel in record['panels']],
                         [('p1.tif', 0.7, 0.8), ('p2.tif', 0.3, 1.0)])
        self.assertTrue(record['dls']['used'])
        factor, used = rct.combine_panels([(first, 1.0)])
        plain = rct.band_record('out.tif', image, np.zeros((2, 2)), used, factor)
        self.assertEqual((len(plain['panels']), plain['dls']['used']), (1, False))


class ValidatePolygonTests(unittest.TestCase):

    SIZE = (100, 80)

    def test_a_simple_polygon_is_accepted(self):
        rct.validate_polygon([[10, 10], [60, 10], [60, 50], [10, 50]], self.SIZE)
        rct.validate_polygon([[10, 10], [60, 10], [30, 50]], self.SIZE)

    def test_reasons_for_rejection(self):
        cases = [([[10, 10], [20, 20]], 'at least 3 vertices'),
                 ([[10, 10], [20, 20], [30, 30]], 'no area'),
                 ([[10, 10], [60, 10], [60, 90]], 'not inside'),
                 ([[-1, 10], [60, 10], [60, 50]], 'not inside'),
                 ([[10, 10], [60, 50], [60, 10], [10, 50]], 'cross'),
                 ([[10, 10], [float('nan'), 10], [60, 50]], 'non-finite')]
        for points, message in cases:
            with self.assertRaisesRegex(ValueError, message):
                rct.validate_polygon(points, self.SIZE, 'region')

    def test_the_name_is_in_the_message(self):
        with self.assertRaisesRegex(ValueError, 'IMG_0005_1.tif: a region needs'):
            rct.validate_polygon([[1, 1]], self.SIZE, 'IMG_0005_1.tif')


class IrradianceChangesTests(unittest.TestCase):

    def test_stable_irradiance_has_no_events(self):
        self.assertEqual(rct.classify_irradiance_changes([100, 101, 99, 100, 102]), [])

    def test_single_capture_jump_is_a_spike(self):
        events = rct.classify_irradiance_changes([100, 101, 140, 100, 99])
        self.assertEqual([(e['index'], e['kind']) for e in events], [(2, 'spike')])
        self.assertAlmostEqual(events[0]['change'], 140 / 101 - 1)

    def test_persistent_change_is_a_step(self):
        events = rct.classify_irradiance_changes([100, 101, 60, 59, 58])
        self.assertEqual([(e['index'], e['kind']) for e in events], [(2, 'step')])

    def test_spike_and_step_together_and_custom_threshold(self):
        events = rct.classify_irradiance_changes([100, 101, 100, 140, 100, 99, 60, 59, 58])
        self.assertEqual([(e['index'], e['kind']) for e in events], [(3, 'spike'), (6, 'step')])
        self.assertEqual(rct.classify_irradiance_changes([100, 105, 100], threshold=0.03)[0]['kind'], 'spike')

    def test_change_at_the_last_capture_is_a_step(self):
        self.assertEqual([e['kind'] for e in rct.classify_irradiance_changes([100, 100, 50])], ['step'])

    def test_invalid_irradiance_is_rejected(self):
        for bad in ([100, 0, 100], [100, -1], [100, float('nan')]):
            with self.assertRaises(ValueError):
                rct.classify_irradiance_changes(bad)


class ComparePanelCapturesTests(unittest.TestCase):

    @staticmethod
    def fake_panel(factor, irradiance, estimate):
        return SimpleNamespace(panel_audit={'radiance_to_reflectance_factor': factor, 'panel_irradiance_estimate': estimate},
                               dls_irradiance=lambda: irradiance)

    def test_identical_captures_have_no_flags(self):
        before = {'Blue': self.fake_panel(2.0, 100.0, 1.0)}
        with warnings.catch_warnings():
            warnings.simplefilter('error')
            result = rct.compare_panel_captures(before, before)
        self.assertEqual(result['flags'], [])
        self.assertEqual(result['bands']['Blue']['factor_ratio'], 1.0)

    def test_drift_of_the_factor_is_flagged(self):
        before = {'Blue': self.fake_panel(2.0, 100.0, 1.0)}
        after = {'Blue': self.fake_panel(2.2, 90.0, 1 / 1.1)}   # 10 % less light: the factor rises, the camera estimate and the DLS fall
        with self.assertWarns(RuntimeWarning):
            result = rct.compare_panel_captures(before, after)
        self.assertEqual(len(result['flags']), 1)
        self.assertIn('factor changed 10.0 %', result['flags'][0])
        self.assertAlmostEqual(result['bands']['Blue']['dls_over_panel_estimate_ratio'], 0.9 * 1.1)

    def test_dls_not_following_the_camera_is_flagged(self):
        before = {'Blue': self.fake_panel(2.0, 100.0, 1.0)}
        after = {'Blue': self.fake_panel(2.0, 130.0, 1.0)}  # camera saw the same light, the DLS did not
        with self.assertWarns(RuntimeWarning):
            result = rct.compare_panel_captures(before, after)
        self.assertEqual(len(result['flags']), 1)
        self.assertIn('DLS irradiance / panel irradiance estimate changed 30.0 %', result['flags'][0])

    def test_missing_band_is_flagged(self):
        before = {'Blue': self.fake_panel(2.0, 100.0, 1.0)}
        with self.assertWarns(RuntimeWarning):
            result = rct.compare_panel_captures(before, {})
        self.assertIn("'Blue' is missing", result['flags'][0])


class ExampleImageTests(unittest.TestCase):
    """ Use the example images to check the real metadata """

    @classmethod
    def setUpClass(cls):
        try:
            import exiftool
        except ImportError:
            raise unittest.SkipTest('pyexiftool is not installed')
        cls.exiftool_path = os.environ.get('exiftoolpath')
        cls.paths = rct.find_capture_paths(RAW, 'IMG_0010')
        cls.band_files = rct.read_band_files(cls.paths, BANDS, cls.exiftool_path)

    def test_example_bands_do_not_follow_the_readme_dictionary_order(self):
        # file _3 is Red and _6 is Panchro, so the file index cannot be used as band index
        self.assertTrue(self.band_files['Red'][0].endswith('IMG_0010_3.tif'))
        self.assertTrue(self.band_files['Panchro'][0].endswith('IMG_0010_6.tif'))
        self.assertEqual(list(self.band_files), ['Blue', 'Green', 'Red', 'NIR', 'Red edge', 'Panchro'])

    def test_valid_metadata_builds_an_image(self):
        path, metadata = self.band_files['Blue']
        image = rct.Image(path, metadata)
        self.assertEqual(image.band_name, 'Blue')
        self.assertEqual(len(image.distortion_parameters), 5)

    def test_missing_field_names_file_and_field(self):
        path, metadata = self.band_files['Blue']
        metadata = {key: val for key, val in metadata.items() if key != 'EXIF:BlackLevel'}
        with self.assertRaisesRegex(rct.MetadataError, "IMG_0010_1.tif.*'EXIF:BlackLevel'"):
            rct.Image(path, metadata)

    def test_nonpositive_exposure_is_rejected(self):
        path, metadata = self.band_files['Blue']
        with self.assertRaisesRegex(rct.MetadataError, "'EXIF:ExposureTime' must be positive"):
            rct.Image(path, dict(metadata, **{'EXIF:ExposureTime': 0}))

    def test_wrong_number_of_calibration_coefficients_is_rejected(self):
        path, metadata = self.band_files['Blue']
        with self.assertRaisesRegex(rct.MetadataError, "'XMP:RadiometricCalibration' has 2 values"):
            rct.Image(path, dict(metadata, **{'XMP:RadiometricCalibration': [1.0, 2.0]}))

    def test_non_numeric_field_is_rejected(self):
        path, metadata = self.band_files['Blue']
        with self.assertRaisesRegex(rct.MetadataError, "'EXIF:ISOSpeed' is not numeric"):
            rct.Image(path, dict(metadata, **{'EXIF:ISOSpeed': 'abc'}))

    @staticmethod
    def micasense_radiance(image):
        """
        The radiance as MicaSense computes it (micasense/imageprocessing, Image.vignette with the 2D polynomial and
        Image.radiance), with its transposes, written with the values of rct.Image
        """
        image_raw = np.copy(image.image).T
        x_dim, y_dim = image.image.shape[1], image.image.shape[0]
        x, y = np.meshgrid(np.arange(x_dim), np.arange(y_dim))
        x, y = x.T, y.T
        xv, yv = x.T / x_dim, y.T / y_dim
        p2 = np.zeros_like(xv, dtype=float)
        for i, c in enumerate(image.k):
            p2 += c * xv ** image.e[2 * i] * yv ** image.e[2 * i + 1]
        V = (1. / p2).T
        R = 1.0 / (1.0 + image.a2 * y / image.exposure_time - image.a3 * y)
        L = V * R * (image_raw - image.black_levels.mean())
        L[L < 0] = 0
        return (L.astype(float) / (image.gain * image.exposure_time) * image.a1 / float(2 ** image.bits_per_pixel)).T

    def test_the_radiance_is_the_one_of_micasense_in_every_band(self):
        for band in BANDS:
            image = rct.Image(*self.band_files[band])
            self.assertTrue(np.allclose(image.digital_numbers_to_radiance(), self.micasense_radiance(image),
                                        rtol=1e-12, atol=0), band)

    # the radial vignetting of a RedEdge-MX (MicaSense's sample capture REDEDGE-MX/IMG_0001_1.tif, Blue)
    RADIAL = {'XMP:VignettingCenter': [623.6301, 470.2927],
              'XMP:VignettingPolynomial': [1.001285e-06, 5.614210e-07, -5.962064e-09, 1.862037e-11, -2.530487e-14,
                                           1.209421e-17]}

    def radial_image(self, **changes):
        path, metadata = self.band_files['Blue']
        metadata = {key: value for key, value in metadata.items() if 'VignettingPolynomial2D' not in key}
        return rct.Image(path, dict(metadata, **self.RADIAL, **changes))

    def test_the_radial_vignetting_of_older_cameras_is_the_one_of_micasense(self):
        image = self.radial_image()
        self.assertEqual(image.vignetting_model, 'radial')
        # micasense Image.vignette(), radial branch, with its transposes
        x, y = np.meshgrid(np.arange(image.image.shape[1]), np.arange(image.image.shape[0]))
        x, y = x.T, y.T
        polynomial = list(image.vignetting_polynomial)
        polynomial.reverse()
        polynomial.append(1.)
        r = np.hypot((x - image.vignetting_center[0]), (y - image.vignetting_center[1]))
        expected = (1. / np.polyval(np.array(polynomial), r)).T
        self.assertTrue(np.allclose(image.vignetting_correction(), expected, rtol=1e-12, atol=0))
        centre = image.vignetting_correction()[470, 624]                       # half a pixel from the centre
        self.assertAlmostEqual(centre, 1.0, delta=1e-5)

    def test_the_vignetting_correction_is_computed_once_for_a_band(self):
        first, second = rct.Image(*self.band_files['Blue']), rct.Image(*self.band_files['Blue'])
        self.assertIs(first.vignetting_correction(), second.vignetting_correction())
        self.assertFalse(first.vignetting_correction().flags.writeable)         # shared: nobody can change it
        self.assertIsNot(first.vignetting_correction(), rct.Image(*self.band_files['Green']).vignetting_correction())

    def test_the_statistics_of_a_region_are_those_of_the_whole_image_mask(self):
        from skimage import measure
        image = rct.Image(*self.band_files['Blue'])
        rows, columns = image.image.shape
        for region in ([[730, 211], [834, 215], [829, 319], [727, 316]],            # a panel region
                       [[-50, -40], [120.5, -10], [100, 90.7], [-20, 60]],            # partly outside the image
                       [[1400, 1000], [1600, 1000], [1600, 1200], [1400, 1200]]):    # at the corner
            mask = measure.grid_points_in_poly((rows, columns), np.fliplr(np.asarray(region, dtype=float)))
            pixels = image.image[mask]
            mean, std, count, saturated = image.region_stats(region, sat_threshold=60000)
            self.assertEqual((count, saturated), (mask.sum(), (pixels > 60000).sum()), region)
            self.assertAlmostEqual(mean, pixels.mean())
            self.assertAlmostEqual(std, pixels.std())

    def test_an_image_without_a_vignetting_model_is_a_clear_error(self):
        path, metadata = self.band_files['Blue']
        metadata = {key: value for key, value in metadata.items() if 'VignettingPolynomial2D' not in key}
        with self.assertRaisesRegex(rct.MetadataError, 'no vignetting model'):
            rct.Image(path, metadata)

    def test_the_exposure_of_legacy_rededge_firmware_is_corrected_as_micasense_does(self):
        self.assertEqual(self.radial_image(**{'EXIF:ExposureTime': 1 / 6329, 'EXIF:Model': 'RedEdge-M'}).exposure_time,
                         0.000274)
        self.assertAlmostEqual(self.radial_image(**{'EXIF:ExposureTime': 1 / 6329, 'EXIF:Model': 'Altum'}).exposure_time,
                               1 / 6329)

    def test_the_dls_of_old_firmware_is_not_used(self):
        old = self.radial_image(**{'EXIF:Model': 'RedEdge-M', 'EXIF:Software': 'v5.0.2'})
        with self.assertRaisesRegex(rct.MetadataError, 'not reliable .*at least v5.1.7'):
            old.dls_irradiance()
        self.assertTrue(self.radial_image(**{'EXIF:Model': 'RedEdge-M', 'EXIF:Software': 'v7.5.0-beta6'})
                        .horizontal_irradiance_trusted())
        self.assertTrue(rct.Image(*self.band_files['Blue']).horizontal_irradiance_trusted())     # RedEdge-P

    def test_the_vignetting_correction_is_larger_along_the_long_side_of_the_image(self):
        # the image is wider than tall, so the middle of the left edge is further from the centre than the middle of the top
        image = rct.Image(*self.band_files['Blue'])
        correction = image.vignetting_correction()
        height, width = correction.shape
        self.assertEqual((width, height), (1456, 1088))
        self.assertAlmostEqual(correction[height // 2, width // 2], 1.0, delta=0.02)
        self.assertGreater(correction[height // 2, 0], correction[0, width // 2] + 0.3)
        self.assertEqual(image.row_gradient_correction().shape, (height, 1))

    def test_panel_detection_records_an_audit(self):
        panel_files = rct.read_band_files(rct.find_capture_paths(RAW, 'IMG_0005'), BANDS, self.exiftool_path)
        path, metadata = panel_files['Blue']
        panel = rct.Image(path, metadata)
        _, factor, _, _ = panel.detect_reference_panel(REFLECTANCE_LEVELS, percent_inner_area=0.7, sat_threshold=65000)
        audit = panel.panel_audit
        self.assertEqual(audit['band_name'], 'Blue')
        self.assertEqual(audit['radiance_to_reflectance_factor'], factor)
        self.assertAlmostEqual(audit['reflectance_level'] / audit['radiance_mean'], factor)
        self.assertEqual(audit['n_saturated'], 0)
        self.assertGreater(audit['n_pixels'], 0)
        # 2.0312 before the axes of the vignetting correction were fixed: the panel is in the upper part of the photo
        # (row 265 of 1088), where the swapped correction made the radiance 18 % too high
        self.assertAlmostEqual(factor, 2.3933485545501494, delta=1e-6 * 2.3933485545501494)
        self.assertLess(audit['radiance_std'] / audit['radiance_mean'], 0.03)

    def test_panel_detection_without_qr_code_is_a_clear_error(self):
        path, metadata = self.band_files['Blue']        # target capture, no reference panel
        with self.assertRaisesRegex(ValueError, 'IMG_0010_1.tif: no QR code found'):
            rct.Image(path, metadata).detect_reference_panel(REFLECTANCE_LEVELS, 0.7)

    def test_panel_band_without_reflectance_level_is_a_clear_error(self):
        panel_files = rct.read_band_files(rct.find_capture_paths(RAW, 'IMG_0005'), BANDS, self.exiftool_path)
        path, metadata = panel_files['Blue']
        levels = {band: val for band, val in REFLECTANCE_LEVELS.items() if band != 'Blue'}
        with self.assertRaisesRegex(KeyError, "band 'Blue' not in the reference panel"):
            rct.Image(path, metadata).detect_reference_panel(levels, 0.7)

    # --- DLS correction ---------------------------------------------------------------------------------------

    def panel_and_target(self, band='Blue', **target_changes):
        panel_files = rct.read_band_files(rct.find_capture_paths(RAW, 'IMG_0005'), BANDS, self.exiftool_path)
        panel = rct.Image(*panel_files[band])
        factor = panel.detect_reference_panel(REFLECTANCE_LEVELS, 0.7)[1]
        path, metadata = self.band_files[band]
        target = rct.Image(path, dict(metadata, **target_changes))
        return panel, target, factor

    def test_dls_ratio_is_irradiance_at_panel_over_irradiance_at_image(self):
        panel, target, _ = self.panel_and_target('Blue')
        self.assertAlmostEqual(rct.dls_irradiance_ratio(panel, target), 138.05218450296033 / 156.04327208007237)

    def test_dls_reproduces_panel_only_result_at_the_panel_capture(self):
        panel, _, factor = self.panel_and_target('Blue')
        applied, used = rct.combine_panels([(panel, 1.0)], panel)
        self.assertEqual((used[0]['dls_ratio'], applied), (1.0, factor))

    def test_panel_only_mode_is_the_original_conversion(self):
        _, target, factor = self.panel_and_target('Blue')
        self.assertTrue((target.digital_numbers_to_reflectance(factor) ==
                         target.digital_numbers_to_radiance() * factor).all())

    def test_doubling_the_image_irradiance_halves_the_reflectance(self):
        panel, target, factor = self.panel_and_target('Blue')
        _, doubled, _ = self.panel_and_target('Blue', **{'XMP:HorizontalIrradiance': 2 * target.horizontal_irradiance})
        stable = target.digital_numbers_to_reflectance(rct.combine_panels([(panel, 1.0)], target)[0])
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', RuntimeWarning)   # ratio 0.44 is outside the plausible range
            brighter = doubled.digital_numbers_to_reflectance(rct.combine_panels([(panel, 1.0)], doubled)[0])
        self.assertTrue(abs(brighter - stable / 2).max() < 1e-12)

    def test_common_irradiance_scale_cancels_in_the_ratio(self):
        panel, target, _ = self.panel_and_target('Blue')
        scale = {'XMP:IrradianceScaleToSIUnits': 0.01}
        panel_scaled = rct.Image(panel.image_path, dict(panel.metadata, **scale))
        target_scaled = rct.Image(target.image_path, dict(target.metadata, **scale))
        self.assertAlmostEqual(rct.dls_irradiance_ratio(panel_scaled, target_scaled),
                               rct.dls_irradiance_ratio(panel, target))

    def test_scale_present_in_only_one_image_is_rejected(self):
        panel, target, _ = self.panel_and_target('Blue', **{'XMP:IrradianceScaleToSIUnits': 0.01})
        with self.assertRaisesRegex(rct.MetadataError, 'exists in only one'):
            rct.dls_irradiance_ratio(panel, target)

    def test_missing_nonpositive_or_nonnumeric_irradiance_is_rejected(self):
        panel, target, _ = self.panel_and_target('Blue')
        for value, message in [(None, 'missing metadata field'), (0, 'must be positive'),
                               (-3.0, 'must be positive'), (float('nan'), 'no finite values'),
                               ('abc', 'not numeric')]:
            bad = rct.Image(target.image_path, dict(target.metadata, **{'XMP:HorizontalIrradiance': value}))
            with self.assertRaisesRegex(rct.MetadataError, message):
                rct.dls_irradiance_ratio(panel, bad)

    def test_dls_without_irradiance_does_not_affect_panel_only_mode(self):
        metadata = {key: val for key, val in self.band_files['Blue'][1].items() if 'Irradiance' not in key}
        image = rct.Image(self.band_files['Blue'][0], metadata)
        self.assertIsNotNone(image.digital_numbers_to_reflectance(2.0))

    def test_different_bands_are_rejected(self):
        panel, _, _ = self.panel_and_target('Blue')
        green = rct.Image(*self.band_files['Green'])
        with self.assertRaisesRegex(rct.MetadataError, 'needs the same band'):
            rct.dls_irradiance_ratio(panel, green)

    # --- Panchro, checks of the DLS data ----------------------------------------------------------------------

    @classmethod
    def capture_dicts(cls):
        """ Images of both captures for all the bands, with the panel detected (done once) """
        if not hasattr(cls, '_P'):
            panel_files = rct.read_band_files(rct.find_capture_paths(RAW, 'IMG_0005'), BANDS, cls.exiftool_path)
            cls._P = {band: rct.Image(*panel_files[band]) for band in BANDS}
            for panel in cls._P.values():
                panel.detect_reference_panel(REFLECTANCE_LEVELS, 0.7)
            cls._T = {band: rct.Image(*cls.band_files[band]) for band in BANDS}
        return cls._P, cls._T

    @staticmethod
    def changed(image, **changes):
        return rct.Image(image.image_path, dict(image.metadata, **changes))

    def test_panchro_uses_its_own_irradiance_like_the_other_bands(self):
        panel, target = self.capture_dicts()
        expected = 99.211212701455025 / 112.20932502581493
        self.assertAlmostEqual(rct.dls_irradiance_ratio(panel['Panchro'], target['Panchro']), expected)
        _, used = rct.combine_panels([(panel['Panchro'], 1.0)], target['Panchro'])
        self.assertAlmostEqual(used[0]['dls_ratio'], expected)

    def test_the_reflectance_from_the_dls_alone_uses_the_irradiance_in_si_units(self):
        _, target = self.capture_dicts()
        blue = target['Blue']                                 # no XMP:IrradianceScaleToSIUnits: uW/cm2/nm, as MicaSense
        factor, details = rct.dls_reflectance_factor(blue)
        self.assertEqual((details['scale'], details['scale_source']), (0.01, 'assumed'))
        self.assertAlmostEqual(factor, np.pi / (0.01 * 156.04327208007237))
        scaled = self.changed(blue, **{'XMP:IrradianceScaleToSIUnits': 0.02})
        self.assertEqual(rct.irradiance_si(scaled)[1:], (0.02, 'metadata'))
        self.assertAlmostEqual(rct.irradiance_si(scaled)[0], 0.02 * 156.04327208007237)

    def test_without_a_panel_only_the_dls_of_the_image_is_checked(self):
        _, target = self.capture_dicts()
        with warnings.catch_warnings(record=True):
            warnings.simplefilter('always')
            checks = rct.check_dls_capture({}, target)
        self.assertEqual(checks['band_ratios'], {})
        self.assertNotIn('panel', checks['sun_angle_discrepancy_deg'])
        self.assertIn('image', checks['sun_angle_discrepancy_deg'])
        self.assertEqual(checks['unavailable'], [])

    def test_capture_ratios_use_the_metadata_of_every_band(self):
        panel, target = self.capture_dicts()
        ratios = rct.capture_dls_ratios(panel, target)
        self.assertEqual(set(ratios), set(BANDS))
        self.assertTrue(all(source == 'metadata' for _, source in ratios.values()))

    def test_missing_irradiance_is_an_error_in_every_band_including_panchro(self):
        panel, target = self.capture_dicts()
        for band in BANDS:
            broken = dict(target, **{band: self.changed(target[band], **{'XMP:HorizontalIrradiance': None})})
            with self.assertRaisesRegex(rct.MetadataError, "XMP:HorizontalIrradiance"):
                rct.capture_dls_ratios(panel, broken)

    def test_invalid_irradiance_is_an_error_in_panchro(self):
        panel, target = self.capture_dicts()
        for value in (0, -5.0, float('nan'), 'abc'):
            broken = dict(target, Panchro=self.changed(target['Panchro'], **{'XMP:HorizontalIrradiance': value}))
            with self.assertRaises(rct.MetadataError):
                rct.capture_dls_ratios(panel, broken)

    def test_ratio_outside_the_plausible_range_gives_a_warning(self):
        panel, target = self.capture_dicts()
        bright = self.changed(target['Blue'], **{'XMP:HorizontalIrradiance': 10 * target['Blue'].horizontal_irradiance})
        with self.assertWarnsRegex(RuntimeWarning, 'outside the plausible range'):
            rct.dls_irradiance_ratio(panel['Blue'], bright)

    def test_example_capture_bands_agree_and_the_image_dls_is_flagged_for_its_sun_angle(self):
        panel, target = self.capture_dicts()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            checks = rct.check_dls_capture(panel, target)
        self.assertEqual(checks['unavailable'], [])
        self.assertTrue(all(abs(dep) < 0.01 for dep in checks['band_ratio_departures'].values()))
        self.assertLess(checks['sun_angle_discrepancy_deg']['panel'], 1)
        self.assertGreater(checks['sun_angle_discrepancy_deg']['image'], 10)
        self.assertEqual(len(checks['flags']), 1)
        self.assertIn('sun angle', checks['flags'][0])
        self.assertEqual(len(caught), 1)
        self.assertEqual([(detail['criterion'], detail['scope'], detail['limit']) for detail in checks['flag_details']],
                         [('dls_tilt', 'image', rct.DLS_CHECKS['max_sun_angle_discrepancy_deg'])])
        self.assertAlmostEqual(checks['flag_details'][0]['value'], checks['sun_angle_discrepancy_deg']['image'])
        # the irradiance estimated from the panel is reported, not judged
        self.assertEqual(set(checks['dls_over_panel_estimate']), set(BANDS))

    def test_a_band_that_departs_from_the_others_is_flagged(self):
        panel, target = self.capture_dicts()
        odd = self.changed(target['Green'], **{'XMP:HorizontalIrradiance': 1.1 * target['Green'].horizontal_irradiance})
        with warnings.catch_warnings(record=True):
            warnings.simplefilter('always')
            checks = rct.check_dls_capture(panel, dict(target, Green=odd))
        self.assertTrue(any("Band 'Green'" in flag and 'median' in flag for flag in checks['flags']))
        departure = [detail for detail in checks['flag_details'] if detail['criterion'] == 'band_ratio_departure']
        self.assertEqual([detail['band'] for detail in departure], ['Green'])

    def test_sun_angle_discrepancy_is_none_without_the_fields(self):
        _, target = self.capture_dicts()
        metadata = {key: val for key, val in target['Blue'].metadata.items() if key != 'XMP:SolarElevation'}
        self.assertIsNone(rct.Image(target['Blue'].image_path, metadata).dls_sun_angle_discrepancy_deg())

    def test_checks_are_recorded_in_the_provenance(self):
        import json, tempfile
        panel, target = self.capture_dicts()
        with warnings.catch_warnings(record=True):
            warnings.simplefilter('always')
            checks = rct.check_dls_capture(panel, target)
        factor, used = rct.combine_panels([(panel['Blue'], 1.0)])
        record = rct.band_record('out/x.tif', target['Blue'], target['Blue'].digital_numbers_to_radiance(), used, factor)
        self.assertIn('sun_angle_discrepancy_deg', record['dls'])
        self.assertEqual(set(record['dls']['attitude_as_written']), {'Roll', 'Pitch', 'Yaw'})
        with tempfile.TemporaryDirectory() as folder:
            saved = json.loads(rct.write_provenance(Path(folder) / 'p.json', [record], checks=checks).read_text(encoding='utf-8'))
        self.assertEqual(saved['checks']['flags'], checks['flags'])
        self.assertNotIn('dls_checks_limits', saved)                              # the limits are in the configuration
        self.assertNotIn('ratio_sources', saved['checks'])
        self.assertNotIn('flag_details', saved['checks'])                         # they are for the count over the captures

    # --- Provenance record ------------------------------------------------------------------------------------

    def test_provenance_of_a_panel_only_run(self):
        panel, target, factor = self.panel_and_target('Blue')
        applied, used = rct.combine_panels([(panel, 1.0)])
        reflectance = target.digital_numbers_to_reflectance(applied)
        record = rct.band_record('out/IMG_0010_1.tif', target, reflectance, used, applied)
        self.assertEqual(record['band'], 'Blue')
        self.assertTrue(record['panels'][0]['file'].endswith('IMG_0005_1.tif'))
        self.assertTrue(record['image_file'].endswith('IMG_0010_1.tif'))
        self.assertEqual(record['output_dtype'], 'float64')
        self.assertFalse(record['dls']['used'])
        self.assertIsNone(record['panels'][0]['dls_ratio'])
        # the irradiances are recorded even when they are not used
        self.assertAlmostEqual(record['dls']['image_horizontal_irradiance'], 156.04327208007237)
        self.assertEqual((record['factor_applied'], record['panels'][0]['factor']), (factor, factor))
        self.assertEqual(record['panels'][0]['audit']['radiance_to_reflectance_factor'], factor)

    def test_provenance_of_a_dls_run_is_saved_as_json(self):
        import json, tempfile
        panel, target, factor = self.panel_and_target('Blue')
        applied, used = rct.combine_panels([(panel, 1.0)], target)
        reflectance = target.digital_numbers_to_reflectance(applied)
        record = rct.band_record('out/IMG_0010_1.tif', target, reflectance, used, applied)
        self.assertTrue(record['dls']['used'])
        self.assertAlmostEqual(record['panels'][0]['dls_ratio'], 138.05218450296033 / 156.04327208007237)
        self.assertAlmostEqual(record['factor_applied'], factor * 138.05218450296033 / 156.04327208007237)
        self.assertTrue(np.allclose(reflectance, target.digital_numbers_to_radiance() * factor *
                                    rct.dls_irradiance_ratio(panel, target)))
        with tempfile.TemporaryDirectory() as folder:
            path = rct.write_provenance(Path(folder) / 'provenance.json', [record],
                                        parameters={'percent_inner_area': 0.7, 'levels': REFLECTANCE_LEVELS})
            saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(saved['method'], 'panel_and_dls')
        self.assertEqual(saved['parameters']['percent_inner_area'], 0.7)
        self.assertEqual(len(saved['software']['radiometric_correction_tools_sha256']), 64)
        self.assertEqual(saved['bands'][0]['panels'][0]['audit']['panel_bounds'],
                         record['panels'][0]['audit']['panel_bounds'])

    def test_the_records_of_the_bands_are_written_without_repetitions(self):
        import copy
        panel = {'file': 'p1.tif', 'capture_id': 'p', 'weight': 1.0, 'factor': 2.0, 'dls_ratio': None,
                 'adjusted_factor': 2.0, 'horizontal_irradiance': 100.0, 'irradiance_scale_to_si': None,
                 'audit': {'image_path': 'p1.tif', 'band_name': 'Blue', 'geometry': 'undistorted', 'sat_threshold': 1,
                           'percent_inner_area': 0.7, 'barcode_data': None, 'n_pixels': 5,
                           'radiance_to_reflectance_factor': 2.0}}
        first = {'band': 'Blue', 'image_capture_id': 'a', 'factor_applied': 2.0, 'panels': [panel],
                 'dls': {'used': False, 'attitude_as_written': {'Roll': 1.0, 'Pitch': '0.5', 'Yaw': 2.0},
                         'sun_angle_discrepancy_deg': 3.0, 'image_irradiance_scale_to_si': None,
                         'image_horizontal_irradiance': 110.0}}
        second = copy.deepcopy(first)
        second['band'] = 'Green'
        capture, bands = rct.compact_band_records([first, second])
        self.assertEqual(capture, {'image_capture_id': 'a', 'panel_capture_ids': ['p'],
                                   'camera_attitude_as_written': {'Roll': 1.0, 'Pitch': 0.5, 'Yaw': 2.0}})
        self.assertEqual(bands[0]['panels'], [{'file': 'p1.tif', 'weight': 1.0, 'factor': 2.0,
                                               'horizontal_irradiance': 100.0, 'audit': {'n_pixels': 5}}])
        self.assertEqual(bands[0]['dls'], {'used': False, 'image_horizontal_irradiance': 110.0})
        self.assertEqual(set(bands[0]), {'band', 'factor_applied', 'panels', 'dls'})
        self.assertIn('capture_id', first['panels'][0])                         # the records that were given are not changed
        # what is not the same in all the bands stays where it is
        second['image_capture_id'] = 'b'
        capture, bands = rct.compact_band_records([first, second])
        self.assertNotIn('image_capture_id', capture)
        self.assertEqual((bands[0]['image_capture_id'], bands[1]['image_capture_id']), ('a', 'b'))
        # with the DLS the ratio and the adjusted factor of each panel are kept
        used = copy.deepcopy(first)
        used['dls']['used'] = True
        used['panels'][0].update({'dls_ratio': 0.9, 'adjusted_factor': 1.8})
        capture, bands = rct.compact_band_records([used])
        self.assertEqual((bands[0]['panels'][0]['dls_ratio'], bands[0]['panels'][0]['adjusted_factor']), (0.9, 1.8))

    def test_the_checks_are_written_without_what_says_nothing(self):
        checks = {'band_ratios': {'Blue': 0.9}, 'ratio_sources': {'Blue': 'metadata'}, 'unavailable': [], 'flags': []}
        self.assertEqual(rct.compact_checks(checks), {'band_ratios': {'Blue': 0.9}, 'flags': []})
        self.assertEqual(rct.compact_checks({'unavailable': ['x'], 'flags': []}), {'unavailable': ['x'], 'flags': []})
        self.assertEqual(rct.compact_checks(None), {})

    def test_method_is_panel_only_without_dls(self):
        import json, tempfile
        panel, target, factor = self.panel_and_target('Blue')
        applied, used = rct.combine_panels([(panel, 1.0)])
        reflectance = target.digital_numbers_to_reflectance(applied)
        record = rct.band_record('out/IMG_0010_1.tif', target, reflectance, used, applied)
        with tempfile.TemporaryDirectory() as folder:
            saved = json.loads(rct.write_provenance(Path(folder) / 'p.json', [record]).read_text(encoding='utf-8'))
        self.assertEqual(saved['method'], 'panel_only')

    # --- Reference panel region drawn by hand -----------------------------------------------------------------

    def panel_image(self, band='Blue'):
        panel_files = rct.read_band_files(rct.find_capture_paths(RAW, 'IMG_0005'), BANDS, self.exiftool_path)
        return rct.Image(*panel_files[band])

    def test_a_manual_region_equal_to_the_automatic_one_gives_the_same_factor(self):
        automatic = self.panel_image()
        bounds, factor, barcode_data, source = automatic.detect_reference_panel(REFLECTANCE_LEVELS, 0.7)
        self.assertEqual(automatic.panel_audit['region_source'], 'automatic')
        manual = self.panel_image()
        m_bounds, m_factor, m_data, m_source = manual.detect_reference_panel(REFLECTANCE_LEVELS, 0.7, manual_bounds=bounds.tolist())
        self.assertEqual(m_factor, factor)
        self.assertEqual((m_data, m_source), (None, 'manual'))
        self.assertEqual(manual.panel_audit['region_source'], 'manual')
        self.assertEqual(manual.panel_audit['geometry'], 'undistorted')
        self.assertEqual(manual.panel_audit['n_pixels'], automatic.panel_audit['n_pixels'])

    def test_a_manual_region_needs_no_qr_code(self):
        _, target = self.capture_dicts()
        image = rct.Image(target['Blue'].image_path, target['Blue'].metadata)
        self.assertIsNone(image.locate_reference_panel(0.7))
        _, factor, _, source = image.detect_reference_panel(REFLECTANCE_LEVELS, 0.7,
                                                              manual_bounds=[[600, 300], [700, 300], [700, 400], [600, 400]])
        self.assertEqual(source, 'manual')
        self.assertGreater(factor, 0)

    def test_an_invalid_manual_region_is_rejected(self):
        for bounds, message in (([[10, 10], [20, 20]], 'at least 3 vertices'),
                                ([[10, 10], [5000, 10], [5000, 50]], 'not inside'),
                                ([[10, 10], [60, 50], [60, 10], [10, 50]], 'cross')):
            with self.assertRaisesRegex(ValueError, message):
                self.panel_image().detect_reference_panel(REFLECTANCE_LEVELS, 0.7, manual_bounds=bounds)

    def test_locate_reference_panel_returns_the_region_without_calibrating(self):
        image = self.panel_image()
        bounds, barcode_data, geometry = image.locate_reference_panel(0.7)
        self.assertEqual(geometry, 'undistorted')
        self.assertEqual(bounds.shape, (4, 2))
        self.assertFalse(hasattr(image, 'panel_audit'))

    def test_region_statistics_and_display_image(self):
        image = self.panel_image()
        bounds, _, _ = image.locate_reference_panel(0.7)
        stats = image.panel_region_stats(bounds)
        self.assertEqual(stats['n_saturated'], 0)
        self.assertLess(stats['variation_percent'], 5)
        outside = image.panel_region_stats([[0, 0], [3, 0], [3, 3]])   # corner of the undistorted image: black border
        self.assertTrue(outside is None or outside['variation_percent'] >= 0)
        shown = image.display_image()
        self.assertEqual(shown.dtype, np.uint8)
        self.assertEqual(shown.shape, image.image.shape)
        self.assertGreater(shown.max(), 200)

    def test_the_bands_of_the_example_capture_are_read_from_its_metadata(self):
        result = rct.read_band_definitions(rct.find_capture_paths(RAW, 'IMG_0010'), self.exiftool_path)
        self.assertEqual(result['camera'], 'MicaSense RedEdge-P')
        table = [(band['name'], band['center_nm'], band['bandwidth_nm']) for band in result['bands']]
        self.assertEqual(table, [('Blue', 475.0, 32.0), ('Green', 560.0, 27.0), ('Red', 668.0, 14.0),
                                 ('NIR', 842.0, 57.0), ('Red edge', 717.0, 12.0), ('Panchro', 634.5, 463.0)])
        self.assertFalse(any(band['thermal'] for band in result['bands']))
        self.assertTrue(result['bands'][0]['file'].endswith('IMG_0010_1.tif'))

    def test_missing_capture_is_reported(self):
        with self.assertRaises(FileNotFoundError):
            rct.find_capture_paths(RAW, 'IMG_9999')


if __name__ == '__main__':
    unittest.main()
