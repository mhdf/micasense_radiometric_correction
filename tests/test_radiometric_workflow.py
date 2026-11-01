"""
Tests for radiometric_workflow.py (configuration, validation and runner), using the example images

Run from the project folder with:  python -m unittest tests.test_radiometric_workflow -v
"""

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
import tifffile

import radiometric_correction_tools as rct
from radiometric_app import radiometric_workflow as wf

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / 'images' / 'raw'


def config_for(results_folder, **changes):
    """ The example images: IMG_0005 shows the panel, IMG_0010 is corrected (IMG_0422 is the second panel capture) """
    values = {'INPUTS.images_folder': str(RAW), 'INPUTS.results_folder': str(results_folder),
              'INPUTS.image_captures': 'IMG_0010'}
    values.update(changes)
    return values


class ConfigurationTests(unittest.TestCase):

    def test_a_results_folder_too_long_for_windows_is_a_clear_problem(self):
        long_folder = Path(tempfile.gettempdir()) / ('x' * 230)
        with mock.patch.object(wf, 'windows_long_paths_enabled', return_value=False):
            problems = wf.path_length_problems(long_folder)
            self.assertEqual(len(problems), 1)
            self.assertIn('too long for Windows', problems[0])
            self.assertEqual(wf.path_length_problems(Path(tempfile.gettempdir()) / 'results'), [])
        with mock.patch.object(wf, 'windows_long_paths_enabled', return_value=True):
            self.assertEqual(wf.path_length_problems(long_folder), [])
        # the path as it is written counts, not where a mapped drive leads (os.path.abspath does not resolve)
        with mock.patch.object(wf, 'windows_long_paths_enabled', return_value=False), \
                mock.patch.object(wf.os.path, 'abspath', return_value='R:\\results\\reflectance'):
            self.assertEqual(wf.path_length_problems('R:\\results\\reflectance'), [])

    def test_the_configuration_is_found_in_what_a_run_wrote(self):
        values = {'PANEL.percent_inner_area': 0.5}
        self.assertEqual(wf.configuration_from_json(values), values)                      # a configuration or a profile
        self.assertEqual(wf.configuration_from_json({'status': 'completed', 'config': values}), values)       # the result file
        self.assertEqual(wf.configuration_from_json({'method': 'panel_only', 'parameters': {'config': values}}), values)
        self.assertEqual(wf.configuration_from_json({'parameters': {'levels': 1}}), {'parameters': {'levels': 1}})
        self.assertEqual(wf.configuration_from_json([1, 2]), [1, 2])

    def test_something_that_is_not_a_json_object_is_not_a_configuration(self):
        with self.assertRaisesRegex(wf.ConfigError, 'json object'):
            wf.merge_config([1, 2])
        with self.assertRaisesRegex(wf.ConfigError, 'json object'):
            wf.merge_config('text')

    def test_defaults_cover_every_field_and_survive_json(self):
        config = wf.default_config()
        self.assertEqual(list(config), wf.FIELD_KEYS)
        self.assertEqual(json.loads(json.dumps(config)), config)
        self.assertEqual(len(set(wf.FIELD_KEYS)), len(wf.FIELD_KEYS))

    def test_default_config_is_a_copy(self):
        config = wf.default_config()
        config['PANEL.reflectance_by_band']['Blue'] = 0.1
        self.assertEqual(wf.default_config()['PANEL.reflectance_by_band']['Blue'], 0.477)

    def test_unknown_key_is_an_error(self):
        with self.assertRaisesRegex(wf.ConfigError, 'INPUTS.typo'):
            wf.merge_config({'INPUTS.typo': 1})

    def test_the_defaults_are_valid_for_the_example_folder(self):
        self.assertEqual(wf.validate_config(wf.default_config(), ROOT), [])

    def test_problems_are_listed_with_their_key(self):
        # wrong types are reported first
        config = wf.merge_config({'PANEL.saturation_threshold': 'high', 'CORRECTION.use_dls': 'yes',
                                  'PANEL.reflectance_by_band': {'Blue': 2.0}})
        text = '\n'.join(wf.validate_config(config, ROOT))
        for expected in ('PANEL.saturation_threshold must be an integer', 'CORRECTION.use_dls must be true or false',
                         "PANEL.reflectance_by_band['Blue']"):
            self.assertIn(expected, text)
        # then the values and the captures
        config = wf.merge_config({'PANEL.percent_inner_area': 1.5, 'CHECKS.ratio_range_min': 3.0,
                                  'INPUTS.reference_panel_capture': 'IMG_9999'})
        text = '\n'.join(wf.validate_config(config, ROOT))
        self.assertIn('PANEL.percent_inner_area must be in (0, 1]', text)
        self.assertIn('CHECKS.ratio_range_min must be smaller', text)
        self.assertIn("Capture 'IMG_9999' not found", text)

    def test_exiftool_is_looked_for_in_the_configuration_the_environment_and_the_path(self):
        import os
        with tempfile.TemporaryDirectory() as folder:
            program = Path(folder) / 'exiftool.exe'
            program.write_text('x')
            self.assertEqual(wf.find_exiftool(wf.merge_config({'INPUTS.exiftool_path': str(program)})),
                             (str(program), 'INPUTS.exiftool_path'))
            self.assertEqual(wf.find_exiftool(wf.merge_config({'INPUTS.exiftool_path': str(program) + '.no'})),
                             (None, 'INPUTS.exiftool_path'))
            with mock.patch.dict(os.environ, {'exiftoolpath': str(program)}):
                self.assertEqual(wf.find_exiftool(wf.default_config()),
                                 (str(program), 'the environment variable exiftoolpath'))
            with mock.patch.dict(os.environ, {'exiftoolpath': ''}), mock.patch('shutil.which', return_value=None):
                self.assertEqual(wf.find_exiftool(wf.default_config()), (None, 'the system PATH'))
            with mock.patch.dict(os.environ, {'exiftoolpath': ''}), mock.patch('shutil.which', return_value='C:/x/exiftool'):
                self.assertEqual(wf.find_exiftool(wf.default_config()), ('C:/x/exiftool', 'the system PATH'))

    def test_the_version_comes_from_the_program(self):
        done = mock.Mock(stdout='12.70\n', returncode=0)
        with mock.patch('subprocess.run', return_value=done):
            self.assertEqual(wf.exiftool_version('exiftool'), '12.70')
        with mock.patch('subprocess.run', return_value=mock.Mock(stdout='', returncode=1)):
            self.assertIsNone(wf.exiftool_version('exiftool'))
        with mock.patch('subprocess.run', side_effect=FileNotFoundError):
            self.assertIsNone(wf.exiftool_version('exiftool'))

    def test_a_missing_exiftool_or_package_is_a_problem_of_the_configuration(self):
        with mock.patch.object(wf, 'find_exiftool', return_value=(None, 'the system PATH')), \
                mock.patch.object(wf, 'pyexiftool_installed', return_value=False):
            text = '\n'.join(wf.validate_config(wf.default_config(), ROOT))
        self.assertIn('ExifTool was not found in the system PATH', text)
        self.assertIn('pyexiftool is not installed', text)

    def test_missing_folder_and_exiftool_are_reported(self):
        config = wf.merge_config({'INPUTS.images_folder': 'does/not/exist', 'INPUTS.exiftool_path': 'nope.exe'})
        text = '\n'.join(wf.validate_config(config, ROOT))
        self.assertIn('Images folder not found', text)
        self.assertIn('ExifTool not found', text)

    def test_captures_are_listed_from_the_file_names(self):
        self.assertEqual(rct.list_capture_names(RAW), ['IMG_0003', 'IMG_0005', 'IMG_0009', 'IMG_0010', 'IMG_0422', 'IMG_0423'])
        self.assertEqual(wf.capture_names(' A , B,,C '), ['A', 'B', 'C'])

    def test_panel_regions_are_saved_and_loaded_keeping_other_captures(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'regions.json'
            wf.save_panel_regions(path, 'IMG_0005', {'Blue': [[1, 2], [30, 2], [30, 40]], 'Red': None})
            wf.save_panel_regions(path, 'IMG_0100', {'Green': [[5, 5], [20, 5], [20, 20]]})
            self.assertEqual(wf.load_panel_regions(path), {'IMG_0005': {'Blue': [[1, 2], [30, 2], [30, 40]]},
                                                           'IMG_0100': {'Green': [[5, 5], [20, 5], [20, 20]]}})
            wf.save_panel_regions(path, 'IMG_0005', {'Blue': None})      # nothing left for this capture
            self.assertEqual(list(wf.load_panel_regions(path)), ['IMG_0100'])
            self.assertFalse(Path(str(path) + '.tmp').exists())

    def test_the_origin_of_each_region_is_saved_next_to_it(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'regions.json'
            square = [[1, 2], [30, 2], [30, 40]]
            wf.save_panel_regions(path, 'IMG_0005', {'Blue': square, 'Red': square},
                                  {'Blue': 'accepted_automatic', 'Red': 'edited', 'NIR': 'edited'})
            wf.save_panel_regions(path, 'IMG_0100', {'Green': square}, {'Green': 'edited'})
            self.assertEqual(wf.load_panel_origins(path), {'IMG_0005': {'Blue': 'accepted_automatic', 'Red': 'edited'},
                                                           'IMG_0100': {'Green': 'edited'}})      # only for saved bands
            wf.save_panel_regions(path, 'IMG_0005', {'Blue': square})                              # no origins given
            self.assertEqual(wf.load_panel_origins(path), {'IMG_0100': {'Green': 'edited'}})
            wf.save_panel_regions(path, 'IMG_0100', {})
            self.assertEqual(wf.load_panel_origins(path), {})
            self.assertNotIn('origins', json.loads(path.read_text(encoding='utf-8')))

    def test_a_file_without_origins_is_still_read(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'old.json'
            path.write_text(json.dumps({'version': 1, 'geometry': 'undistorted',
                                        'captures': {'IMG_0005': {'Blue': [[1, 2], [30, 2], [30, 40]]}}}), encoding='utf-8')
            self.assertEqual(wf.load_panel_origins(path), {})
            self.assertEqual(list(wf.load_panel_regions(path)['IMG_0005']), ['Blue'])

    def test_a_file_that_is_not_a_regions_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'regions.json'
            for content in ({'version': 9, 'geometry': 'undistorted', 'captures': {}},
                            {'version': 1, 'geometry': 'original', 'captures': {}},
                            {'version': 1, 'geometry': 'undistorted', 'captures': {'A': [1]}}):
                path.write_text(json.dumps(content), encoding='utf-8')
                with self.assertRaises(wf.ConfigError):
                    wf.load_panel_regions(path)

    def test_regions_file_problems_are_reported_by_the_validation(self):
        config = wf.merge_config({'PANEL.manual_regions_file': 'missing.json'})
        self.assertIn('Panel regions file not found', '\n'.join(wf.validate_config(config, ROOT)))
        self.assertEqual(wf.validate_config(config, ROOT, regions_file_may_be_missing=True), [])     # the tool creates it
        self.assertIn('Check / draw panel regions', '\n'.join(wf.validate_config(config, ROOT)))      # and says what to do
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'regions.json'
            wf.save_panel_regions(path, 'IMG_0005', {'Infrared': [[1, 2], [30, 2], [30, 40]]})
            config = wf.merge_config({'PANEL.manual_regions_file': str(path)})
            self.assertIn("band 'Infrared' of IMG_0005 is not one of the panel bands",
                          '\n'.join(wf.validate_config(config, ROOT)))

    def write_spectrum(self, folder, level=0.5, start=400, stop=900, name='panel.csv', header=False):
        path = Path(folder) / name
        text = ('Wavelength (nm),Reflectance\n' if header else '') + \
               ''.join(f"{w},{level}\n" for w in range(start, stop + 1))
        path.write_text(text, encoding='utf-8')
        return path

    def spectral_config(self, path, **changes):
        values = {'PANEL.reflectance_source': 'spectral_file', 'PANEL.spectral_file': str(path)}
        values.update(changes)
        return wf.merge_config(values)

    def test_levels_come_from_the_typed_values_or_from_the_spectral_file(self):
        config = wf.default_config()
        levels, info = wf.panel_reflectance_levels(config)
        self.assertEqual(levels, config['PANEL.reflectance_by_band'])
        self.assertEqual(info, {'source': 'values'})
        with tempfile.TemporaryDirectory() as folder:
            config = self.spectral_config(self.write_spectrum(folder, 0.5, header=True))
            levels, info = wf.panel_reflectance_levels(config)
        self.assertEqual(list(levels), list(wf.BAND_NAMES))
        self.assertTrue(all(abs(value - 0.5) < 1e-12 for value in levels.values()))
        self.assertEqual(info['source'], 'spectral_file')
        self.assertEqual(len(info['sha256']), 64)
        self.assertEqual(info['bands']['Blue']['window_nm'], [459.0, 491.0])
        self.assertEqual(wf.panel_bands(config), list(wf.BAND_NAMES))

    def test_the_format_options_are_used(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'panel.csv'
            path.write_text(''.join(f"{w / 1000:.3f};{'50,0'}\n" for w in range(400, 901)), encoding='utf-8')
            config = self.spectral_config(path, **{'PANEL.spectral_delimiter': 'semicolon', 'PANEL.spectral_decimal': ',',
                                                  'PANEL.spectral_wavelength_unit': 'um',
                                                  'PANEL.spectral_reflectance_unit': 'percent'})
            levels, _ = wf.panel_reflectance_levels(config)
        self.assertAlmostEqual(levels['Red'], 0.5)

    def test_spectral_file_problems_are_reported(self):
        def problems(config):
            return '\n'.join(wf.validate_config(config, ROOT))
        self.assertIn('PANEL.spectral_file is empty',
                      problems(wf.merge_config({'PANEL.reflectance_source': 'spectral_file'})))
        self.assertIn('Spectral response file not found', problems(self.spectral_config('missing.csv')))
        with tempfile.TemporaryDirectory() as folder:
            path = self.write_spectrum(folder, 0.5, 400, 700)           # ends at 700 nm: the NIR window is not covered
            self.assertIn("Band 'Red edge'", problems(self.spectral_config(path)))
            self.assertIn("Band 'NIR'", problems(self.spectral_config(path)))      # all the bands are listed
            self.assertIn('Spectral response format', problems(self.spectral_config(
                path, **{'PANEL.spectral_reflectance_column': 1})))
            self.assertIn("PANEL.spectral_skip_rows must be 'auto'", problems(self.spectral_config(
                path, **{'PANEL.spectral_skip_rows': 'two'})))
            self.assertIn('same bands', problems(self.spectral_config(
                path, **{'PANEL.band_bandwidths': {'Blue': 32.0}})))
            self.assertIn('PANEL.spectral_min_coverage', problems(self.spectral_config(
                path, **{'PANEL.spectral_min_coverage': 0})))
            self.assertIn('PANEL.reflectance_source must be one of', problems(
                wf.merge_config({'PANEL.reflectance_source': 'both'})))
            self.assertIn("PANEL.band_bandwidths['Blue'] must be a number in (0, 5000]", problems(
                wf.merge_config({'PANEL.band_bandwidths': dict(wf.default_config()['PANEL.band_bandwidths'], Blue=-1)})))

    def test_the_reading_options_of_the_configuration_reach_the_format(self):
        config = wf.merge_config({'PANEL.spectral_delimiter': 'custom', 'PANEL.spectral_delimiter_char': '|',
                                  'PANEL.spectral_skip_footer': 2, 'PANEL.spectral_comment_prefix': '#',
                                  'PANEL.spectral_exclude_rows': '5-6', 'PANEL.spectral_wavelength_min': '400',
                                  'PANEL.spectral_wavelength_max': ' ', 'PANEL.spectral_reflectance_scale': 0.01})
        fmt = wf.spectral_format(config)
        self.assertEqual((fmt['delimiter'], fmt['delimiter_char'], fmt['skip_footer']), ('custom', '|', 2))
        self.assertEqual((fmt['comment_prefix'], fmt['exclude_rows']), ('#', '5-6'))
        self.assertEqual((fmt['wavelength_min'], fmt['wavelength_max'], fmt['reflectance_scale']), (400.0, None, 0.01))
        self.assertEqual(wf.spectral_format(wf.default_config())['wavelength_min'], None)

    def test_bad_reading_options_are_reported(self):
        for key, value, message in (('PANEL.spectral_wavelength_min', 'low', 'must be a number or empty'),
                                    ('PANEL.spectral_delimiter', 'custom', 'custom delimiter needs'),
                                    ('PANEL.spectral_exclude_rows', 'x', 'exclude_rows'),
                                    ('PANEL.spectral_skip_footer', -1, 'skip_footer'),
                                    ('PANEL.spectral_reflectance_scale', 0.0, 'reflectance_scale')):
            with tempfile.TemporaryDirectory() as folder:
                path = self.write_spectrum(folder)
                config = self.spectral_config(path, **{key: value})
                self.assertIn(message, '\n'.join(wf.validate_config(config, ROOT)), key)

    def test_levels_from_a_file_that_needs_the_options(self):
        with tempfile.TemporaryDirectory() as folder:
            lines = ['# panel', 'wavelength|reflectance'] + [f"{w}|{500 if w < 700 else 700}" for w in range(400, 901)] + ['end|x']
            path = Path(folder) / 'panel.txt'
            path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
            config = self.spectral_config(path, **{
                'PANEL.spectral_delimiter': 'custom', 'PANEL.spectral_delimiter_char': '|',
                'PANEL.spectral_comment_prefix': '#', 'PANEL.spectral_skip_footer': 1,
                'PANEL.spectral_reflectance_scale': 0.001, 'PANEL.spectral_wavelength_max': '880'})
            self.assertEqual(wf.validate_config(config, ROOT), [])
            levels, info = wf.panel_reflectance_levels(config)
        self.assertAlmostEqual(levels['Blue'], 0.5)
        self.assertAlmostEqual(levels['Red edge'], 0.7)

    def test_a_low_coverage_limit_accepts_a_file_that_ends_early(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.write_spectrum(folder, 0.5, 400, 848)           # like the example: NIR window 61 % covered
            self.assertEqual(wf.validate_config(self.spectral_config(path), ROOT), [])
            strict = self.spectral_config(path, **{'PANEL.spectral_min_coverage': 0.9})
            self.assertIn("Band 'NIR'", '\n'.join(wf.validate_config(strict, ROOT)))

    def test_irrelevant_values_are_not_checked_for_the_unused_source(self):
        # the typed values are not used when the spectral file is the source, and the file is not read for 'values'
        config = wf.merge_config({'PANEL.spectral_file': 'missing.csv'})
        self.assertEqual(wf.validate_config(config, ROOT), [])

    def test_the_band_lists_must_describe_the_same_bands(self):
        defaults = wf.default_config()
        config = wf.merge_config({'PANEL.band_center_wavelengths': {'Blue': 475.0}})
        self.assertIn('must have the same bands', '\n'.join(wf.validate_config(config, ROOT)))
        config = wf.merge_config({'PANEL.reflectance_by_band': {'Blue': 0.477, 'Coastal': 0.5}})
        self.assertIn('must have the same bands', '\n'.join(wf.validate_config(config, ROOT)))
        self.assertEqual(wf.validate_config(defaults, ROOT), [])

    def test_ignored_bands_cannot_also_be_processed(self):
        config = wf.merge_config({'INPUTS.ignored_bands': 'NIR, LWIR'})
        self.assertIn("INPUTS.ignored_bands has bands that are also processed: ['NIR']",
                      '\n'.join(wf.validate_config(config, ROOT)))

    def test_a_bad_file_pattern_is_reported_with_its_key(self):
        for pattern, message in (('(unclosed', 'not a valid regular expression'), (r'(.+)_(\d+)\.tif$', 'needs {capture}')):
            config = wf.merge_config({'INPUTS.file_pattern': pattern})
            problems = '\n'.join(wf.validate_config(config, ROOT))
            self.assertIn('INPUTS.file_pattern', problems)
            self.assertIn(message, problems)

    def test_a_pattern_that_matches_nothing_means_that_the_capture_is_not_found(self):
        config = wf.merge_config({'INPUTS.file_pattern': r'(?P<capture>.+)_B(?P<index>\d+)\.tif$'})
        self.assertIn("Capture 'IMG_0005' not found", '\n'.join(wf.validate_config(config, ROOT)))

    def test_the_bands_of_a_capture_can_be_detected_from_the_images(self):
        result = wf.detect_bands(wf.default_config(), ROOT)
        self.assertEqual([band['name'] for band in result['bands']],
                         ['Blue', 'Green', 'Red', 'NIR', 'Red edge', 'Panchro'])
        self.assertEqual(wf.detect_bands(wf.default_config(), ROOT, 'IMG_0010')['camera'], 'MicaSense RedEdge-P')
        with self.assertRaises(FileNotFoundError):
            wf.detect_bands(wf.default_config(), ROOT, 'IMG_9999')

    def test_the_panel_approach_and_the_orientation_are_validated(self):
        for mode in ('last', 'mean', 'interpolate_time', 'nearest_time', 'adjacent_time'):
            # the moments are found from the times at the run: one moment is used alone, with a flag
            self.assertEqual(wf.validate_config(wf.merge_config({'PANEL.panels_used': mode}), ROOT), [])
        self.assertIn('PANEL.panels_used must be one of', '\n'.join(wf.validate_config(
            wf.merge_config({'PANEL.panels_used': 'later'}), ROOT)))
        for good in ('all', 'ALL', '30', '172.5'):
            self.assertEqual(wf.validate_config(wf.merge_config({'PANEL.panel_orientation': good}), ROOT), [], good)
        for bad in ('facing', '200', ''):
            self.assertIn('PANEL.panel_orientation must be', '\n'.join(wf.validate_config(
                wf.merge_config({'PANEL.panel_orientation': bad}), ROOT)), bad)

    def test_the_choices_of_the_panel_by_the_light_need_the_dls_correction(self):
        for mode in rct.PANELS_BY_ILLUMINATION:
            values = {'PANEL.panels_used': mode, 'INPUTS.reference_panel_capture_after': 'IMG_0005'}
            problems = '\n'.join(wf.validate_config(wf.merge_config(dict(values, **{'CORRECTION.use_dls': False})), ROOT))
            self.assertIn(f"PANEL.panels_used is '{mode}'", problems)
            self.assertIn('CORRECTION.use_dls is off', problems)
            self.assertEqual(wf.validate_config(wf.merge_config(dict(values, **{'CORRECTION.use_dls': True})), ROOT), [])
        for mode in ('first', 'last', 'mean', 'interpolate_time', 'nearest_time'):     # the others do not
            values = {'PANEL.panels_used': mode, 'INPUTS.reference_panel_capture_after': 'IMG_0005'}
            self.assertEqual(wf.validate_config(wf.merge_config(values), ROOT), [])

    def test_irradiance_series_changes_names_the_capture(self):
        series = {'Blue': [('c1', 100.0), ('c2', 101.0), ('c3', 150.0), ('c4', 100.0), ('c5', 100.0)],
                  'Green': [('c1', 100.0), ('c2', 100.0)],
                  'Red': [('c1', 100.0), ('c2', -1.0), ('c3', 100.0)]}
        changes = wf.irradiance_series_changes(series, 0.1)
        self.assertEqual(list(changes), ['Blue'])
        self.assertEqual([(e['capture'], e['kind']) for e in changes['Blue']], [('c3', 'spike')])


class RunTests(unittest.TestCase):
    """ The runs are done once and shared: each processes the six bands of the example capture """

    @classmethod
    def setUpClass(cls):
        cls.folder = Path(tempfile.mkdtemp())
        cls.panel_only = wf.run(config_for(cls.folder / 'panel_only'), ROOT)
        cls.with_dls = wf.run(config_for(cls.folder / 'dls', **{'CORRECTION.use_dls': True}), ROOT)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.folder, ignore_errors=True)

    def test_panel_only_run_completes_and_names_outputs_like_the_sources(self):
        result = self.panel_only
        self.assertEqual(result['status'], 'completed', result['errors'])
        names = sorted(Path(path).name for path in result['outputs'])
        self.assertEqual(names, [f'IMG_0010_{number}.tif' for number in range(1, 7)])
        self.assertEqual(list(result['captures']), ['IMG_0010'])
        self.assertEqual(result['stages'], list(wf.STAGES))

    def test_panel_only_output_equals_the_library_result(self):
        folder = self.folder / 'panel_only'
        panel_files = rct.read_band_files(rct.find_capture_paths(RAW, 'IMG_0005'), list(wf.BAND_NAMES))
        panel = rct.Image(*panel_files['Blue'])
        factor = panel.detect_reference_panel(wf.default_config()['PANEL.reflectance_by_band'], 0.7, 65000)[1]
        target = rct.Image(*rct.read_band_files(rct.find_capture_paths(RAW, 'IMG_0010'),
                                                list(wf.BAND_NAMES))['Blue'])
        expected = target.digital_numbers_to_reflectance(factor)
        saved = tifffile.imread(folder / 'IMG_0010_1.tif')
        self.assertEqual(saved.dtype, np.float32)                                # the default number format
        self.assertTrue(np.array_equal(saved, expected.astype(np.float32)))

    def test_result_and_provenance_files_are_saved(self):
        folder = self.folder / 'panel_only'
        saved = json.loads((folder / wf.RESULT_FILE_NAME).read_text(encoding='utf-8'))
        self.assertEqual(saved['status'], 'completed')
        self.assertEqual(Path(saved['result_file']), folder / wf.RESULT_FILE_NAME)
        self.assertEqual(set(saved['panel']['before']['captures']['IMG_0005']['bands']), set(wf.BAND_NAMES))
        provenance = json.loads((folder / 'IMG_0010_provenance.json').read_text(encoding='utf-8'))
        self.assertEqual(provenance['method'], 'panel_only')
        self.assertEqual(provenance['parameters']['config']['PANEL.percent_inner_area'], 0.7)
        self.assertFalse((folder / (wf.RESULT_FILE_NAME + '.tmp')).exists())

    def test_the_settings_of_a_run_can_be_read_back_from_its_result_and_provenance_files(self):
        folder = self.folder / 'panel_only'
        result = json.loads((folder / wf.RESULT_FILE_NAME).read_text(encoding='utf-8'))
        self.assertEqual(wf.load_config(folder / wf.RESULT_FILE_NAME), result['config'])          # all the values of the run
        self.assertEqual(wf.load_config(folder / 'IMG_0010_provenance.json'), result['config'])
        self.assertEqual(wf.load_config(folder / wf.RESULT_FILE_NAME)['INPUTS.results_folder'], str(folder))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):                                                 # the command line repeats a run
            code = wf.main(['--config', str(folder / wf.RESULT_FILE_NAME), '--root', str(ROOT), '--detect-bands'])
        self.assertEqual(code, 0)
        self.assertIn('Blue', output.getvalue())

    def test_a_run_with_the_choice_of_the_panel_by_the_light_is_refused_without_the_dls_correction(self):
        result = wf.run(config_for(self.folder / 'light_without_dls', **{
            'PANEL.panels_used': 'nearest_illumination', 'INPUTS.reference_panel_capture_after': 'IMG_0005'}), ROOT)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('CORRECTION.use_dls is off', result['errors'][0])
        self.assertEqual(result['outputs'], [])

    def test_the_reports_do_not_repeat_themselves(self):
        folder = self.folder / 'panel_only'
        text = (folder / wf.RESULT_FILE_NAME).read_text(encoding='utf-8')
        flag = 'sun angle differs'
        self.assertEqual(text.count(flag), 0)                                   # the flags of the captures are counted instead
        saved = json.loads(text)
        self.assertEqual(len(saved['flags']), 2)                                # the heading of the panel and the DLS tilt
        self.assertEqual([group['criterion'] for group in saved['flag_groups']], ['dls_tilt'])
        self.assertEqual(saved['flag_groups'][0]['captures'], ['IMG_0010'])
        self.assertNotIn('message', saved['flag_groups'][0])                    # it is in the flags
        entry = saved['captures']['IMG_0010']
        for key in ('flags', 'checks'):
            self.assertNotIn(key, entry)                                        # they are in the provenance record
        self.assertEqual(entry['outputs'], [f'IMG_0010_{number}.tif' for number in (1, 2, 6, 3, 5, 4)])
        self.assertEqual(entry['provenance'], 'IMG_0010_provenance.json')
        self.assertEqual(list(entry['dls']), ['median_ratio'])
        self.assertAlmostEqual(entry['geometry']['dls_tilt_deg'], 14.99, places=1)
        for audit in saved['panel']['before']['captures']['IMG_0005']['bands'].values():
            for key in ('image_path', 'band_name', 'sat_threshold', 'percent_inner_area', 'geometry'):
                self.assertNotIn(key, audit)
        for key in ('format', 'min_coverage'):
            self.assertNotIn(key, saved['panel']['reflectance'])
        provenance_text = (folder / 'IMG_0010_provenance.json').read_text(encoding='utf-8')
        self.assertEqual(provenance_text.count(flag), 1)
        provenance = json.loads(provenance_text)
        self.assertNotIn('dls_checks_limits', provenance)
        self.assertNotIn('ratio_sources', provenance['checks'])
        self.assertNotIn('unavailable', provenance['checks'])
        self.assertEqual(set(provenance['capture']), {'image_capture_id', 'panel_capture_ids', 'camera_attitude_as_written'})
        self.assertEqual(provenance['checks']['compared_with_panel'], 'IMG_0005')
        self.assertIsInstance(provenance['capture']['camera_attitude_as_written']['Pitch'], float)
        for band in provenance['bands']:
            self.assertNotIn('image_capture_id', band)
            for key in ('attitude_as_written', 'sun_angle_discrepancy_deg'):
                self.assertNotIn(key, band['dls'])
            panel = band['panels'][0]
            for key in ('capture_id', 'dls_ratio', 'adjusted_factor'):                # the DLS is not used in this run
                self.assertNotIn(key, panel)
            self.assertNotIn('image_path', panel['audit'])
            self.assertEqual(band['factor_applied'], panel['factor'])
            self.assertIn('image_horizontal_irradiance', band['dls'])           # what the DLS read is still there

    def test_dls_run_corrects_every_band_including_panchro(self):
        folder = self.folder / 'dls'
        provenance = json.loads((folder / 'IMG_0010_provenance.json').read_text(encoding='utf-8'))
        self.assertEqual(provenance['method'], 'panel_and_dls')
        ratios = {band['band']: band['panels'][0]['dls_ratio'] for band in provenance['bands']}
        self.assertEqual(set(ratios), set(wf.BAND_NAMES))
        self.assertAlmostEqual(ratios['Blue'], 138.05218450296033 / 156.04327208007237)
        self.assertAlmostEqual(ratios['Panchro'], 99.211212701455025 / 112.20932502581493)
        ratio = tifffile.imread(folder / 'IMG_0010_6.tif').mean() / \
                tifffile.imread(self.folder / 'panel_only' / 'IMG_0010_6.tif').mean()
        self.assertAlmostEqual(ratio, ratios['Panchro'], delta=1e-6)                     # 32-bit images

    def test_dls_run_reports_the_tilt_flag_of_the_example(self):
        flags = self.with_dls['flags']
        self.assertEqual(len(flags), 2)
        tilt = [flag for flag in flags if 'DLS tilt' in flag]
        self.assertEqual(tilt, ['DLS tilt over 10 deg in 1 of 1 capture (tilt of at least 15.0 deg): IMG_0010'])

    def test_thresholds_of_the_configuration_are_used_and_then_restored(self):
        limits_before, geometry_before = dict(rct.DLS_CHECKS), dict(rct.GEOMETRY_CHECKS)
        result = wf.run(config_for(self.folder / 'loose', **{'CHECKS.max_sun_angle_discrepancy_deg': 30.0,
                                                             'CHECKS.panel_heading_tolerance_deg': 90.0}), ROOT)
        self.assertEqual(result['flags'], [])
        self.assertEqual(rct.DLS_CHECKS, limits_before)
        self.assertEqual(rct.GEOMETRY_CHECKS, geometry_before)
        strict = wf.run(config_for(self.folder / 'strict', **{'CHECKS.max_camera_tilt_deg': 1.0,
                                                              'CHECKS.image_heading_tolerance_deg': 5.0}), ROOT)
        text = '\n'.join(strict['flags'])
        self.assertIn('Camera tilt over 1 deg in 1 of 1 capture (tilt 1.4 deg): IMG_0010', text)
        self.assertIn('Camera heading more than 5 deg from the sun at the side (90 deg) in 1 of 1 capture '
                      '(heading 83 deg from the sun): IMG_0010', text)
        self.assertEqual(rct.GEOMETRY_CHECKS, geometry_before)

    def test_the_geometry_of_the_panel_and_of_the_images_is_checked_once_for_each(self):
        result = self.panel_only
        panel = result['panel']['before']['captures']['IMG_0005']['geometry']
        self.assertAlmostEqual(panel['camera_tilt_deg'], 0.075, places=2)
        self.assertAlmostEqual(panel['heading_to_sun_deg'], 87.0, places=1)
        flags = [flag for flag in result['flags'] if 'panel capture' in flag and 'Heading' in flag]
        self.assertEqual(len(flags), 1)
        self.assertTrue(flags[0].startswith('IMG_0005: '))
        provenance = json.loads((self.folder / 'panel_only' / 'IMG_0010_provenance.json').read_text(encoding='utf-8'))
        geometry = provenance['checks']['geometry']
        self.assertEqual(sorted(geometry), ['image', 'panel'])
        self.assertAlmostEqual(geometry['image']['heading_to_sun_deg'], 82.8, places=1)
        self.assertAlmostEqual(geometry['panel']['heading_to_sun_deg'], 87.0, places=1)       # what the panel was like
        self.assertFalse([flag for flag in provenance['checks']['flags'] if 'panel capture' in flag and 'Heading' in flag])  # its flag is in the result, once

    def test_the_images_can_be_in_several_folders_and_a_repeated_capture_uses_the_copy_chosen(self):
        panels, flight = self.folder / 'several_panels', self.folder / 'several_flight'
        panels.mkdir()
        flight.mkdir()
        for path in RAW.glob('IMG_0005_*.tif'):
            shutil.copy(path, panels / path.name)
            shutil.copy(path, flight / path.name)                     # the panel capture was also copied with the flight
        for path in RAW.glob('IMG_0010_*.tif'):
            shutil.copy(path, flight / path.name)
        values = config_for(self.folder / 'several_out', **{'INPUTS.images_folder': [str(panels), str(flight)]})
        problems = wf.validate_config(wf.merge_config(values), ROOT)
        self.assertTrue(any('more than one images folder' in problem and 'IMG_0005' in problem for problem in problems),
                        problems)
        values['INPUTS.repeated_capture_folders'] = {'IMG_0005': str(panels)}
        self.assertEqual(wf.validate_config(wf.merge_config(values), ROOT), [])
        result = wf.run(values, ROOT)
        self.assertEqual(result['status'], 'completed', result['errors'])
        self.assertEqual(sorted(Path(path).name for path in result['outputs']),
                         [f'IMG_0010_{number}.tif' for number in range(1, 7)])
        provenance = (self.folder / 'several_out' / 'IMG_0010_provenance.json').read_text(encoding='utf-8')
        self.assertIn('several_panels', provenance)                   # the panel files of the copy that was chosen
        self.assertEqual(result['config']['INPUTS.images_folder'], [str(panels), str(flight)])
        missing = config_for(self.folder / 'x', **{'INPUTS.images_folder': f"{panels} | {self.folder / 'missing'}"})
        self.assertIn(f"Images folder not found: {self.folder / 'missing'}", wf.validate_config(wf.merge_config(missing), ROOT))
        self.assertIn('INPUTS.images_folder must be a path or a list of paths',
                      wf.validate_config(wf.merge_config(dict(values, **{'INPUTS.images_folder': 3})), ROOT))

    def test_the_flags_of_the_panel_are_not_repeated_for_every_capture(self):
        images = self.folder / 'two_captures'
        images.mkdir()
        for path in RAW.glob('*.tif'):
            shutil.copy(path, images / path.name)
            if path.name.startswith('IMG_0010'):
                shutil.copy(path, images / path.name.replace('IMG_0010', 'IMG_0011'))
        result = wf.run({'INPUTS.images_folder': str(images), 'INPUTS.results_folder': str(self.folder / 'two_captures_out'),
                         'INPUTS.image_captures': 'IMG_0010, IMG_0011', 'CHECKS.max_sun_angle_discrepancy_deg': 0.1}, ROOT)
        self.assertEqual(result['status'], 'completed', result['errors'])
        self.assertEqual(sum('Heading of the camera in the panel capture' in flag for flag in result['flags']), 1)
        self.assertEqual(sum('DLS of the panel capture' in flag for flag in result['flags']), 1)   # 0.2 deg > 0.1: once
        self.assertEqual(sum('DLS of the image capture' in flag for flag in result['flags']), 0)    # they are counted instead
        self.assertIn('DLS tilt over 0.1 deg in 2 of 2 captures (tilt of at least 15.0 deg): IMG_0010, IMG_0011',
                      result['flags'])
        self.assertEqual(result['flag_groups'][0]['captures'], ['IMG_0010', 'IMG_0011'])
        self.assertEqual(result['summary']['corrected'], 2)

    def test_flags_of_the_same_criterion_are_counted_with_the_captures_listed(self):
        order = [f'IMG_{number:04d}' for number in range(10, 30)]
        tilt = lambda value: {'criterion': 'dls_tilt', 'scope': 'image', 'band': None, 'value': value, 'limit': 10.0}
        events = [(name, tilt(10.0 + number)) for number, name in enumerate(order[:6])]          # IMG_0010 to IMG_0015
        events += [(order[8], tilt(20.0)), (order[12], tilt(12.0)), (order[13], tilt(11.0))]
        events += [(order[4], {'criterion': 'camera_tilt', 'scope': 'image', 'band': None, 'value': 6.2, 'limit': 5.0})]
        groups = wf.group_flag_events(events, 20, order)
        self.assertEqual([group['criterion'] for group in groups], ['camera_tilt', 'dls_tilt'])   # the order of FLAG_CRITERIA
        self.assertEqual(groups[0]['message'], 'Camera tilt over 5 deg in 1 of 20 captures (tilt 6.2 deg): IMG_0014')
        self.assertEqual(groups[1]['message'], 'DLS tilt over 10 deg in 9 of 20 captures (tilt of at least 10.0 to 20.0 deg): '
                                               'IMG_0010 to IMG_0015, IMG_0018, IMG_0022, IMG_0023')
        self.assertEqual((groups[1]['count'], groups[1]['of'], groups[1]['min'], groups[1]['max']), (9, 20, 10.0, 20.0))
        self.assertEqual(groups[1]['captures'], [order[number] for number in (0, 1, 2, 3, 4, 5, 8, 12, 13)])

    def test_flags_of_the_bands_are_counted_by_capture_and_name_the_bands(self):
        order = ['A_1', 'A_2', 'A_3']
        ratio = lambda band, value: {'criterion': 'ratio_range', 'scope': 'image', 'band': band, 'value': value,
                                     'limit': [0.5, 2.0]}
        events = [('A_2', ratio('Blue', 0.31)), ('A_2', ratio('Green', 0.4)), ('A_3', ratio('Blue', 2.4))]
        group = wf.group_flag_events(events, 3, order)[0]
        self.assertEqual(group['count'], 2)                                      # captures, not (capture, band) pairs
        self.assertEqual(group['bands'], ['Blue', 'Green'])
        self.assertEqual(group['message'], 'DLS ratio outside 0.5 to 2 in 2 of 3 captures (ratio 0.310 to 2.400; bands Blue, '
                                           'Green): A_2, A_3')
        departure = {'criterion': 'band_ratio_departure', 'scope': 'image', 'band': 'Red', 'value': -0.125, 'limit': 0.1}
        message = wf.group_flag_events([('A_1', departure)], 1, order)[0]['message']
        self.assertEqual(message, 'DLS ratio of a band more than 10 % from the median of the bands in 1 of 1 capture '
                                  '(departure 12.5 %; bands Red): A_1')

    def test_the_list_of_captures_has_all_of_them_and_neighbors_are_written_as_ranges(self):
        order = [f'IMG_{number:04d}' for number in range(1, 101)]
        self.assertEqual(wf.capture_list_text(order[:2], order), 'IMG_0001, IMG_0002')
        self.assertEqual(wf.capture_list_text(order[:3], order), 'IMG_0001 to IMG_0003')
        self.assertEqual(wf.capture_list_text(order[9:60], order), 'IMG_0010 to IMG_0060')
        scattered = order[::2]                                                    # 50 captures, none of them neighbors
        text = wf.capture_list_text(scattered, order)
        self.assertEqual(text.split(', '), scattered)                             # no limit: every capture is there
        self.assertEqual(wf.capture_list_text(['IMG_0007', 'IMG_0003'], order), 'IMG_0003, IMG_0007')    # in the order of the run
        self.assertEqual(wf.capture_list_text(order[:3], order, texts={n: '+1 %' for n in order[:3]}),
                         'IMG_0001 (+1 %), IMG_0002 (+1 %), IMG_0003 (+1 %)')

    def test_the_spikes_and_steps_are_counted_over_the_bands(self):
        order = [f'IMG_{number:04d}' for number in range(10, 20)]
        changes = {'Blue': [{'index': 3, 'kind': 'step', 'change': -0.114, 'capture': 'IMG_0013'}],
                   'Green': [{'index': 3, 'kind': 'step', 'change': -0.105, 'capture': 'IMG_0013'},
                             {'index': 6, 'kind': 'spike', 'change': 0.2, 'capture': 'IMG_0016'}]}
        groups = wf.group_irradiance_changes(changes, 0.1, 10, order)
        self.assertEqual([group['criterion'] for group in groups], ['irradiance_spike', 'irradiance_step'])
        self.assertEqual(groups[0]['message'], 'Irradiance spike of more than 10 % from the previous capture in 1 of 10 '
                                               'captures: IMG_0016 (+20.0 %, 1 band)')
        self.assertEqual(groups[1]['message'], 'Irradiance step of more than 10 % from the previous capture in 1 of 10 '
                                               'captures: IMG_0013 (-11.4 %, 2 bands)')
        self.assertEqual(wf.group_irradiance_changes({}, 0.1, 10, order), [])

    def test_the_summary_of_the_run(self):
        summary = self.panel_only['summary']
        self.assertEqual((summary['captures'], summary['corrected'], summary['files'], summary['flags']), (1, 1, 6, 2))
        self.assertEqual(summary['panels']['IMG_0005']['capture'], 'IMG_0005')
        images = summary['images']
        self.assertEqual(set(images), {'camera_tilt_deg', 'heading_to_sun_deg', 'sun_elevation_deg', 'dls_tilt_deg',
                                       'dls_ratio'})
        self.assertEqual(images['camera_tilt_deg']['outside'], 0)
        self.assertEqual(images['dls_tilt_deg']['outside'], 1)
        self.assertEqual(images['sun_elevation_deg']['outside'], None)
        lines = wf.summary_lines(self.panel_only)
        self.assertEqual(lines[0], 'Summary: 1 of 1 capture(s) corrected, 6 file(s) written, 2 flag(s).')
        self.assertTrue(lines[1].startswith('Panel IMG_0005: camera tilt 0.1 deg; heading 87 deg from the sun'))
        self.assertTrue(any(line.startswith('Images (1): DLS tilt') and '1 outside the limit 10' in line for line in lines))
        self.assertEqual(wf.summary_lines({'errors': ['x']}), [])
        saved = json.loads((self.folder / 'panel_only' / wf.RESULT_FILE_NAME).read_text(encoding='utf-8'))
        self.assertEqual(saved['summary']['corrected'], 1)

    def test_the_statistics_use_the_median_and_skip_what_is_missing(self):
        self.assertEqual(wf._statistics([3.0, None, 1.0, 2.0]), {'n': 3, 'min': 1.0, 'median': 2.0, 'max': 3.0})
        self.assertIsNone(wf._statistics([None, None]))

    def test_the_limits_of_the_geometry_have_to_make_sense(self):
        for key, value in (('CHECKS.max_camera_tilt_deg', 0.0), ('CHECKS.panel_heading_tolerance_deg', 120.0),
                           ('CHECKS.image_heading_tolerance_deg', -5.0), ('CHECKS.heading_offset_deg', 200.0)):
            text = '\n'.join(wf.validate_config(wf.merge_config({key: value}), ROOT))
            self.assertIn(key, text)
        self.assertEqual(wf.validate_config(wf.merge_config({'CHECKS.heading_offset_deg': -22.0}), ROOT), [])

    def test_two_panel_captures_are_compared(self):
        result = wf.run(config_for(self.folder / 'two_panels', **{'INPUTS.reference_panel_capture_after': 'IMG_0005'}), ROOT)
        comparison = result['panel']['comparison']
        self.assertEqual(comparison['flags'], [])
        self.assertTrue(all(entry['factor_ratio'] == 1.0 for entry in comparison['bands'].values()))

    def test_a_region_drawn_by_hand_replaces_the_automatic_one_for_that_band_only(self):
        automatic = self.panel_only['panel']['before']['captures']['IMG_0005']['bands']
        regions = self.folder / 'regions.json'
        wf.save_panel_regions(regions, 'IMG_0005', {'Blue': automatic['Blue']['panel_bounds']})
        result = wf.run(config_for(self.folder / 'manual_blue', **{'PANEL.manual_regions_file': str(regions)}), ROOT)
        self.assertEqual(result['status'], 'completed', result['errors'])
        bands = result['panel']['before']['captures']['IMG_0005']['bands']
        self.assertEqual(bands['Blue']['region_source'], 'manual')
        self.assertEqual({band for band, audit in bands.items() if audit['region_source'] == 'automatic'},
                         set(wf.BAND_NAMES) - {'Blue'})
        self.assertEqual(bands['Blue']['radiance_to_reflectance_factor'], automatic['Blue']['radiance_to_reflectance_factor'])

    def test_regions_drawn_by_hand_allow_a_panel_without_a_readable_qr_code(self):
        regions = self.folder / 'regions_no_qr.json'
        square = [[600, 300], [700, 300], [700, 400], [600, 400]]
        wf.save_panel_regions(regions, 'IMG_0010', {band: square for band in wf.BAND_NAMES})
        result = wf.run(config_for(self.folder / 'no_qr', **{'INPUTS.reference_panel_capture': 'IMG_0010',
                                                             'INPUTS.image_captures': 'IMG_0005',
                                                             'PANEL.manual_regions_file': str(regions)}), ROOT)
        self.assertEqual(result['status'], 'completed', result['errors'])
        audits = result['panel']['before']['captures']['IMG_0010']['bands']               # the "panel" is IMG_0010 here
        self.assertTrue(all(audit['region_source'] == 'manual' for audit in audits.values()))

    def test_the_origin_of_the_regions_reaches_the_audit_of_the_panel(self):
        automatic = self.panel_only['panel']['before']['captures']['IMG_0005']['bands']
        regions = self.folder / 'regions_origin.json'
        wf.save_panel_regions(regions, 'IMG_0005', {band: audit['panel_bounds'] for band, audit in automatic.items()},
                              {'Blue': 'accepted_automatic', 'Green': 'edited'})
        result = wf.run(config_for(self.folder / 'origin', **{'PANEL.manual_regions_file': str(regions)}), ROOT)
        self.assertEqual(result['status'], 'completed', result['errors'])
        bands = result['panel']['before']['captures']['IMG_0005']['bands']
        self.assertEqual((bands['Blue']['region_source'], bands['Blue']['region_origin']), ('manual', 'accepted_automatic'))
        self.assertEqual(bands['Green']['region_origin'], 'edited')
        self.assertIsNone(bands['Red']['region_origin'])                              # a file that does not say it
        self.assertEqual(bands['Blue']['radiance_to_reflectance_factor'], automatic['Blue']['radiance_to_reflectance_factor'])
        self.assertIsNone(automatic['Blue'].get('region_origin'))                     # the automatic run has none

    def test_an_invalid_region_names_the_band(self):
        regions = self.folder / 'regions_bad.json'
        wf.save_panel_regions(regions, 'IMG_0005', {'Green': [[1, 1], [9000, 1], [9000, 50]]})
        result = wf.run(config_for(self.folder / 'bad_region', **{'PANEL.manual_regions_file': str(regions)}), ROOT)
        self.assertEqual(result['status'], 'failed')
        self.assertIn("Panel of band 'Green' in IMG_0005", result['errors'][0])
        self.assertIn('not inside', result['errors'][0])

    def test_a_run_with_the_spectral_file_uses_its_reflectance_and_records_it(self):
        folder = self.folder / 'spectral_inputs'
        folder.mkdir()
        path = folder / 'panel.csv'
        path.write_text(''.join(f"{w},0.5\n" for w in range(400, 901)), encoding='utf-8')
        result = wf.run(config_for(self.folder / 'spectral', **{'PANEL.reflectance_source': 'spectral_file',
                                                                'PANEL.spectral_file': str(path)}), ROOT)
        self.assertEqual(result['status'], 'completed', result['errors'])
        reflectance = result['panel']['reflectance']
        self.assertEqual(reflectance['source'], 'spectral_file')
        self.assertTrue(all(abs(level - 0.5) < 1e-12 for level in reflectance['levels'].values()))
        values_run = self.panel_only['panel']['before']['captures']['IMG_0005']['bands']
        spectral_run = result['panel']['before']['captures']['IMG_0005']['bands']
        for band in wf.BAND_NAMES:
            self.assertAlmostEqual(spectral_run[band]['radiance_to_reflectance_factor'] /
                                   values_run[band]['radiance_to_reflectance_factor'],
                                   0.5 / wf.default_config()['PANEL.reflectance_by_band'][band])
        provenance = json.loads((self.folder / 'spectral' / 'IMG_0010_provenance.json').read_text(encoding='utf-8'))
        self.assertEqual(provenance['parameters']['panel_reflectance_source']['sha256'], reflectance['sha256'])
        self.assertEqual(provenance['parameters']['panel_reflectance_levels']['Blue'], 0.5)

    def test_a_spectral_file_that_does_not_cover_a_band_stops_the_run_before_processing(self):
        folder = self.folder / 'short_inputs'
        folder.mkdir()
        path = folder / 'panel.csv'
        path.write_text(''.join(f"{w},0.5\n" for w in range(400, 700)), encoding='utf-8')
        result = wf.run(config_for(self.folder / 'short', **{'PANEL.reflectance_source': 'spectral_file',
                                                             'PANEL.spectral_file': str(path)}), ROOT)
        self.assertEqual(result['status'], 'failed')
        self.assertIn("Band 'Red edge'", result['errors'][0])
        self.assertIn("Band 'NIR'", result['errors'][0])
        self.assertEqual(result['outputs'], [])

    def test_fewer_bands_can_be_processed_if_the_others_are_ignored(self):
        three = ('Blue', 'Green', 'Red')
        defaults = wf.default_config()
        subset = {'PANEL.reflectance_by_band': {band: defaults['PANEL.reflectance_by_band'][band] for band in three},
                  'PANEL.band_center_wavelengths': {band: defaults['PANEL.band_center_wavelengths'][band] for band in three},
                  'PANEL.band_bandwidths': {band: defaults['PANEL.band_bandwidths'][band] for band in three}}
        result = wf.run(config_for(self.folder / 'three_bands', **subset,
                                   **{'INPUTS.ignored_bands': 'Red edge, NIR, Panchro'}), ROOT)
        self.assertEqual(result['status'], 'completed', result['errors'])
        self.assertEqual(sorted(Path(path).name for path in result['outputs']),
                         ['IMG_0010_1.tif', 'IMG_0010_2.tif', 'IMG_0010_3.tif'])
        self.assertEqual(set(result['panel']['before']['captures']['IMG_0005']['bands']), set(three))
        provenance = json.loads((self.folder / 'three_bands' / 'IMG_0010_provenance.json').read_text(encoding='utf-8'))
        self.assertEqual([band['band'] for band in provenance['bands']], list(three))
        # a band gives the same result whether the others are processed or not
        for name in ('IMG_0010_1.tif', 'IMG_0010_3.tif'):
            self.assertTrue(np.array_equal(tifffile.imread(self.folder / 'three_bands' / name),
                                           tifffile.imread(self.folder / 'panel_only' / name)))

    def test_extra_bands_in_the_files_stop_the_capture_unless_they_are_ignored(self):
        defaults = wf.default_config()
        subset = {key: {'Blue': defaults[key]['Blue']} for key in ('PANEL.reflectance_by_band',
                  'PANEL.band_center_wavelengths', 'PANEL.band_bandwidths')}
        result = wf.run(config_for(self.folder / 'one_band', **subset), ROOT)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('unexpected', result['errors'][0])
        self.assertIn('NIR', result['errors'][0])

    def test_a_band_that_is_not_in_the_files_is_a_missing_band(self):
        coastal = {'PANEL.reflectance_by_band': {'Coastal': 0.5}, 'PANEL.band_center_wavelengths': {'Coastal': 444.0},
                   'PANEL.band_bandwidths': {'Coastal': 28.0}, 'INPUTS.ignored_bands': 'Blue, Green, Red, NIR, Red edge, Panchro'}
        result = wf.run(config_for(self.folder / 'coastal', **coastal), ROOT)
        self.assertEqual(result['status'], 'failed')
        self.assertIn("missing: ['Coastal']", result['errors'][0])

    def test_files_with_another_naming_scheme_give_the_same_result(self):
        images = self.folder / 'renamed_images'
        images.mkdir()
        for path in RAW.glob('*.tif'):
            number = int(path.stem.rsplit('_', 1)[1])
            new_name = path.stem.rsplit('_', 1)[0].replace('IMG_', 'FLIGHT-') + f'-band{number + 4}.TIF'    # 5..10
            shutil.copy(path, images / new_name)
        pattern = r'(?P<capture>FLIGHT-\d+)-band(?P<index>\d+)\.tif$'
        result = wf.run({'INPUTS.images_folder': str(images), 'INPUTS.results_folder': str(self.folder / 'renamed'),
                         'INPUTS.reference_panel_capture': 'FLIGHT-0005', 'INPUTS.image_captures': 'FLIGHT-0010',
                         'INPUTS.file_pattern': pattern}, ROOT)
        self.assertEqual(result['status'], 'completed', result['errors'])
        names = sorted(Path(path).name for path in result['outputs'])
        self.assertEqual(names, sorted(f'FLIGHT-0010-band{number}.TIF' for number in range(5, 11)))
        # the band comes from the metadata: file band5 is the Blue band, as IMG_0010_1.tif was
        self.assertTrue(np.array_equal(tifffile.imread(self.folder / 'renamed' / 'FLIGHT-0010-band5.TIF'),
                                       tifffile.imread(self.folder / 'panel_only' / 'IMG_0010_1.tif')))

    def run_with_two_panels(self, name, mode, **changes):
        values = {'INPUTS.reference_panel_capture_after': 'IMG_0005', 'PANEL.panels_used': mode}
        values.update(changes)
        return wf.run(config_for(self.folder / name, **values), ROOT)

    def test_the_mean_of_two_equal_panels_gives_the_result_of_one(self):
        result = self.run_with_two_panels('mean_panels', 'mean')
        self.assertEqual(result['status'], 'completed', result['errors'])
        self.assertEqual(result['captures']['IMG_0010']['panels'], {'used': 'mean', 'weight_of_the_second_panel': 0.5})
        for number in range(1, 7):
            self.assertTrue(np.array_equal(tifffile.imread(self.folder / 'mean_panels' / f'IMG_0010_{number}.tif'),
                                           tifffile.imread(self.folder / 'panel_only' / f'IMG_0010_{number}.tif')))
        provenance = json.loads((self.folder / 'mean_panels' / 'IMG_0010_provenance.json').read_text(encoding='utf-8'))
        blue = provenance['bands'][0]
        self.assertEqual([panel['weight'] for panel in blue['panels']], [0.5, 0.5])
        self.assertTrue(all(panel['file'].endswith('IMG_0005_1.tif') for panel in blue['panels']))
        self.assertEqual(blue['factor_applied'], blue['panels'][0]['factor'])

    def test_the_last_panel_alone_calibrates_the_images(self):
        result = self.run_with_two_panels('last_panel', 'last')
        self.assertEqual(result['status'], 'completed', result['errors'])
        self.assertEqual(result['captures']['IMG_0010']['panels'], {'used': 'last', 'weight_of_the_second_panel': 1.0})
        self.assertEqual(result['summary']['panel_weights'], {'mode': 'last', 'first_only': 0, 'second_only': 1, 'both': 0})
        for number in range(1, 7):                                               # the second panel is a copy of the first
            self.assertTrue(np.array_equal(tifffile.imread(self.folder / 'last_panel' / f'IMG_0010_{number}.tif'),
                                           tifffile.imread(self.folder / 'panel_only' / f'IMG_0010_{number}.tif')))
        provenance = json.loads((self.folder / 'last_panel' / 'IMG_0010_provenance.json').read_text(encoding='utf-8'))
        self.assertEqual([panel['weight'] for panel in provenance['bands'][0]['panels']], [1.0])
        single = self.run_with_two_panels('last_without_second', 'last', **{'INPUTS.reference_panel_capture_after': ''})
        self.assertEqual(single['status'], 'completed', single['errors'])          # one moment only: used alone
        self.assertIn("There are panel captures of one moment only: 'After the flight only' uses them alone", single['flags'])

    def test_with_the_dls_the_last_panel_gives_the_reflectance_of_its_own_light(self):
        result = self.run_with_a_second_light('last_dls', 'last', 0.5)            # the second panel read half the irradiance
        self.assertEqual(result['status'], 'completed', result['errors'])
        for number in range(1, 7):
            last = tifffile.imread(self.folder / 'last_dls' / f'IMG_0010_{number}.tif')
            first = tifffile.imread(self.folder / 'dls' / f'IMG_0010_{number}.tif')
            self.assertTrue(np.allclose(last, 0.5 * first, rtol=0.01, atol=0), number)    # R = L f E_panel / E_image
        # the DLS checks compare the image with the panel that calibrates it: the last one, with half the light
        provenance = json.loads((self.folder / 'last_dls' / 'IMG_0010_provenance.json').read_text(encoding='utf-8'))
        self.assertEqual(provenance['checks']['compared_with_panel'], 'IMG_0007')
        self.assertAlmostEqual(result['captures']['IMG_0010']['dls']['median_ratio'], 0.5 * 0.884, places=2)
        self.assertTrue(any('outside the plausible range' in flag for flag in provenance['checks']['flags']))

    def test_the_mean_with_the_dls_uses_the_irradiance_of_each_panel(self):
        result = self.run_with_two_panels('mean_dls', 'mean', **{'CORRECTION.use_dls': True})
        self.assertEqual(result['status'], 'completed', result['errors'])
        for number in range(1, 7):
            mean = tifffile.imread(self.folder / 'mean_dls' / f'IMG_0010_{number}.tif')
            single = tifffile.imread(self.folder / 'dls' / f'IMG_0010_{number}.tif')
            self.assertTrue(np.allclose(mean, single, rtol=1e-12, atol=0), number)

    def test_the_first_panel_alone_is_still_the_default_and_leaves_the_second_for_the_comparison(self):
        result = self.run_with_two_panels('first_panel', 'first')
        self.assertEqual(result['captures']['IMG_0010']['panels'], {'used': 'first', 'weight_of_the_second_panel': 0.0})
        self.assertIn('comparison', result['panel'])
        provenance = json.loads((self.folder / 'first_panel' / 'IMG_0010_provenance.json').read_text(encoding='utf-8'))
        self.assertEqual(len(provenance['bands'][0]['panels']), 1)                # a panel that was not used is not repeated

    def test_a_second_panel_that_is_not_used_when_there_is_only_one(self):
        entry = self.panel_only['captures']['IMG_0010']
        self.assertEqual(entry['panels'], {'used': 'first', 'weight_of_the_second_panel': 0.0})

    def test_the_weight_follows_the_time_of_the_image_between_the_panels(self):
        import exiftool
        images = self.folder / 'later_panel_images'
        images.mkdir()
        for path in RAW.glob('*.tif'):
            shutil.copy(path, images / path.name)
            if path.name.startswith('IMG_0005'):
                shutil.copy(path, images / path.name.replace('IMG_0005', 'IMG_0007'))   # the panel again, "after the flight"
        with exiftool.ExifToolHelper() as et:
            for path in images.glob('IMG_0007_*.tif'):
                et.execute('-overwrite_original', '-EXIF:DateTimeOriginal=2023:02:02 13:37:44', str(path))
        result = wf.run({'INPUTS.images_folder': str(images), 'INPUTS.results_folder': str(self.folder / 'timed'),
                         'INPUTS.reference_panel_capture': 'IMG_0005', 'INPUTS.reference_panel_capture_after': 'IMG_0007',
                         'INPUTS.image_captures': 'IMG_0010', 'PANEL.panels_used': 'interpolate_time'}, ROOT)
        self.assertEqual(result['status'], 'completed', result['errors'])
        # IMG_0005 at 13:26:38, IMG_0010 at 13:30:11, IMG_0007 at 13:37:44: 213 s of 666 s
        self.assertAlmostEqual(result['captures']['IMG_0010']['panels']['weight_of_the_second_panel'], 213 / 666)

    def run_with_a_later_panel(self, name, mode, time):
        import exiftool
        images = self.folder / (name + '_images')
        images.mkdir()
        for path in RAW.glob('*.tif'):
            shutil.copy(path, images / path.name)
            if path.name.startswith('IMG_0005'):
                shutil.copy(path, images / path.name.replace('IMG_0005', 'IMG_0007'))
        with exiftool.ExifToolHelper() as et:
            for path in images.glob('IMG_0007_*.tif'):
                et.execute('-overwrite_original', '-EXIF:DateTimeOriginal=' + time, str(path))
        return wf.run({'INPUTS.images_folder': str(images), 'INPUTS.results_folder': str(self.folder / name),
                       'INPUTS.reference_panel_capture': 'IMG_0005', 'INPUTS.reference_panel_capture_after': 'IMG_0007',
                       'INPUTS.image_captures': 'IMG_0010', 'PANEL.panels_used': mode}, ROOT)

    def test_the_panel_closest_in_time_is_the_one_that_is_used(self):
        # IMG_0005 at 13:26:38 and IMG_0010 at 13:30:11: the second panel at 13:37:44 is farther, at 13:31:00 it is nearer
        far = self.run_with_a_later_panel('nearest_far', 'nearest_time', '2023:02:02 13:37:44')
        self.assertEqual(far['status'], 'completed', far['errors'])
        self.assertEqual(far['captures']['IMG_0010']['panels'], {'used': 'nearest_time', 'weight_of_the_second_panel': 0.0})
        near = self.run_with_a_later_panel('nearest_near', 'nearest_time', '2023:02:02 13:31:00')
        self.assertEqual(near['status'], 'completed', near['errors'])
        self.assertEqual(near['captures']['IMG_0010']['panels'], {'used': 'nearest_time', 'weight_of_the_second_panel': 1.0})
        provenance = json.loads((self.folder / 'nearest_near' / 'IMG_0010_provenance.json').read_text(encoding='utf-8'))
        panels = provenance['bands'][0]['panels']
        self.assertEqual([(Path(panel['file']).name, panel['weight']) for panel in panels], [('IMG_0007_1.tif', 1.0)])

    def test_the_time_of_the_images_is_needed_to_interpolate(self):
        original = wf._read_capture

        def without_time(images_folder, name, bands, exiftool_path, *arguments):
            objects = original(images_folder, name, bands, exiftool_path, *arguments)
            if name == 'IMG_0010':
                for image in objects.values():
                    image.metadata['EXIF:DateTimeOriginal'] = None
            return objects

        with mock.patch.object(wf, '_read_capture', without_time):
            result = self.run_with_two_panels('no_time', 'interpolate_time')
        self.assertEqual(result['status'], 'failed')
        self.assertIn('IMG_0010: EXIF:DateTimeOriginal', result['captures']['IMG_0010']['errors'][0])

    def run_with_a_second_light(self, name, mode, factor, remove_irradiance_of=None, **changes):
        """
        The second panel is IMG_0007, a copy of IMG_0005 whose DLS irradiance is multiplied by factor in every band: the
        image IMG_0010 has 1.13 times the irradiance of IMG_0005 (the DLS ratio is 0.884)
        """
        images = self.folder / (name + '_images')
        images.mkdir()
        for path in RAW.glob('*.tif'):
            shutil.copy(path, images / path.name)
            if path.name.startswith('IMG_0005'):
                shutil.copy(path, images / path.name.replace('IMG_0005', 'IMG_0007'))
        original = wf._read_capture

        def changed(images_folder, capture, bands, exiftool_path, *arguments):
            objects = original(images_folder, capture, bands, exiftool_path, *arguments)
            for image in objects.values():
                if capture == 'IMG_0007':
                    image.metadata['XMP:HorizontalIrradiance'] = float(image.metadata['XMP:HorizontalIrradiance']) * factor
                if capture == remove_irradiance_of:
                    image.metadata['XMP:HorizontalIrradiance'] = None
            return objects

        values = {'INPUTS.images_folder': str(images), 'INPUTS.results_folder': str(self.folder / name),
                  'INPUTS.reference_panel_capture': 'IMG_0005', 'INPUTS.reference_panel_capture_after': 'IMG_0007',
                  'INPUTS.image_captures': 'IMG_0010', 'PANEL.panels_used': mode, 'CORRECTION.use_dls': True}
        values.update(changes)
        with mock.patch.object(wf, '_read_capture', changed):
            return wf.run(values, ROOT)

    def test_the_panel_with_the_light_of_the_image_is_the_one_that_is_used(self):
        near = self.run_with_a_second_light('light_near', 'nearest_illumination', 1.13)
        self.assertEqual(near['status'], 'completed', near['errors'])
        panels = near['captures']['IMG_0010']['panels']
        self.assertEqual((panels['used'], panels['weight_of_the_second_panel']), ('nearest_illumination', 1.0))
        self.assertGreater(panels['light_difference']['first'], 0.1)                  # IMG_0005 had 12 % less light
        self.assertLess(panels['light_difference']['second'], 0.01)
        soft = self.run_with_a_second_light('light_soft', 'interpolate_illumination', 1.13)
        weight = soft['captures']['IMG_0010']['panels']['weight_of_the_second_panel']
        first, second = (soft['captures']['IMG_0010']['panels']['light_difference'][key] for key in ('first', 'second'))
        self.assertAlmostEqual(weight, first ** 2 / (first ** 2 + second ** 2))
        self.assertGreater(weight, 0.99)
        provenance = json.loads((self.folder / 'light_near' / 'IMG_0010_provenance.json').read_text(encoding='utf-8'))
        self.assertEqual([panel['weight'] for panel in provenance['bands'][0]['panels']], [1.0])
        self.assertAlmostEqual(provenance['bands'][0]['panels'][0]['dls_ratio'], 1.0, delta=0.02)   # the light of the image
        self.assertEqual(sorted(provenance['checks']['panel_light_difference']), ['first', 'second'])

    def test_a_panel_with_other_light_is_not_used(self):
        far = self.run_with_a_second_light('light_far', 'nearest_illumination', 0.5)
        self.assertEqual(far['captures']['IMG_0010']['panels']['weight_of_the_second_panel'], 0.0)
        soft = self.run_with_a_second_light('light_far_soft', 'interpolate_illumination', 0.5)
        panels = soft['captures']['IMG_0010']['panels']
        first, second = panels['light_difference']['first'], panels['light_difference']['second']
        self.assertGreater(second, 0.5)
        self.assertAlmostEqual(panels['weight_of_the_second_panel'], first ** 2 / (first ** 2 + second ** 2))
        self.assertLess(panels['weight_of_the_second_panel'], 0.05)

    def test_with_the_light_of_the_image_the_dls_and_the_panel_alone_give_the_same_reflectance(self):
        result = self.run_with_a_second_light('light_dls', 'nearest_illumination', 1.13, **{'CORRECTION.use_dls': True})
        self.assertEqual(result['status'], 'completed', result['errors'])
        for number in range(1, 7):
            with_dls = tifffile.imread(self.folder / 'light_dls' / f'IMG_0010_{number}.tif')
            panel_only = tifffile.imread(self.folder / 'panel_only' / f'IMG_0010_{number}.tif')
            self.assertTrue(np.allclose(with_dls, panel_only, rtol=0.01, atol=0), number)    # the DLS ratio is about 1

    def test_a_capture_far_from_every_panel_is_flagged_and_the_limit_can_be_changed(self):
        quiet = self.run_with_a_second_light('light_default', 'nearest_illumination', 0.5)
        self.assertFalse([flag for flag in quiet['flags'] if 'nearest panel capture' in flag])    # 12 % is within 15 %
        strict = self.run_with_a_second_light('light_strict', 'nearest_illumination', 0.5,
                                              **{'CHECKS.panel_illumination_tolerance': 0.05})
        flags = [flag for flag in strict['flags'] if 'nearest panel capture' in flag]
        self.assertEqual(len(flags), 1)
        self.assertRegex(flags[0], r'^Light more than 5 % different from the nearest panel capture in 1 of 1 capture '
                                   r'\(difference 12\.\d %\): IMG_0010$')
        self.assertIn('panel_illumination', [group['criterion'] for group in strict['flag_groups']])
        self.assertEqual(rct.DLS_CHECKS['panel_illumination_tolerance'], 0.15)                    # restored after the run

    def test_the_summary_says_which_panels_were_used(self):
        result = self.run_with_a_second_light('light_summary', 'nearest_illumination', 1.13)
        self.assertEqual(result['summary']['panel_weights'], {'mode': 'nearest_illumination', 'first_only': 0,
                                                              'second_only': 1, 'both': 0})
        self.assertIn('Panels (nearest_illumination): the panel before the flight alone in 0 capture(s), after it alone in 1, both in 0.',
                      wf.summary_lines(result))
        soft = self.run_with_a_second_light('light_summary_soft', 'interpolate_illumination', 0.9)
        self.assertEqual(soft['summary']['panel_weights']['both'], 1)
        self.assertIsNone(self.panel_only['summary']['panel_weights'])                          # one panel: nothing to say

    def test_the_dls_data_are_needed_to_compare_the_light(self):
        result = self.run_with_a_second_light('light_missing', 'nearest_illumination', 1.0, remove_irradiance_of='IMG_0010')
        self.assertEqual(result['status'], 'failed')
        error = result['captures']['IMG_0010']['errors'][0]
        self.assertIn('IMG_0010', error)
        self.assertIn('HorizontalIrradiance', error)

    def test_progress_reports_the_stages_in_order(self):
        events = []
        wf.run(config_for(self.folder / 'progress'), ROOT, events.append)
        stages = [event['stage'] for event in events]
        self.assertEqual([stage for i, stage in enumerate(stages) if i == 0 or stage != stages[i - 1]], list(wf.STAGES))

    def test_invalid_configuration_fails_without_processing(self):
        result = wf.run(config_for(self.folder / 'invalid', **{'INPUTS.reference_panel_capture': 'IMG_9999'}), ROOT)
        self.assertEqual(result['status'], 'failed')
        self.assertIn("Capture 'IMG_9999' not found", result['errors'][0])
        self.assertEqual(result['outputs'], [])

    def test_a_capture_without_a_panel_fails_with_a_clear_message(self):
        result = wf.run(config_for(self.folder / 'no_panel', **{'INPUTS.reference_panel_capture': 'IMG_0010',
                                                                'INPUTS.image_captures': 'IMG_0005'}), ROOT)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('no QR code found', result['errors'][0])
        self.assertIn("Panel of band", result['errors'][0])

    def test_a_missing_band_file_fails_that_capture_only(self):
        images = self.folder / 'incomplete_images'
        images.mkdir()
        for path in RAW.glob('*.tif'):
            if path.name != 'IMG_0010_3.tif':
                shutil.copy(path, images / path.name)
        result = wf.run({'INPUTS.images_folder': str(images), 'INPUTS.results_folder': str(self.folder / 'incomplete'),
                         'INPUTS.reference_panel_capture': 'IMG_0003, IMG_0005, IMG_0009, IMG_0423'}, ROOT)
        self.assertEqual(result['status'], 'completed_with_errors')
        self.assertIn("missing: ['Red']", result['captures']['IMG_0010']['errors'][0])
        self.assertEqual(sorted(Path(path).name for path in result['outputs']),            # the other capture is corrected
                         [f'IMG_0422_{number}.tif' for number in range(1, 7)])

    def test_the_closest_panel_capture_before_and_after_reproduces_metashape(self):
        # one capture in each moment: the same as the interpolation in time
        for mode in ('adjacent_time', 'interpolate_time'):
            result = self.run_with_two_panels(f'two_{mode}', mode, **{'INPUTS.reference_panel_capture_after': 'IMG_0422',
                                                                     'CORRECTION.use_dls': True})
            self.assertEqual(result['status'], 'completed', result['errors'])
        for number in range(1, 7):
            self.assertTrue(np.allclose(tifffile.imread(self.folder / 'two_adjacent_time' / f'IMG_0010_{number}.tif'),
                                        tifffile.imread(self.folder / 'two_interpolate_time' / f'IMG_0010_{number}.tif'),
                                        rtol=1e-6, atol=0), number)

        def capture(time):
            return {'Blue': SimpleNamespace(metadata={'EXIF:DateTimeOriginal': f'2023:02:02 {time}'})}
        panels = {'before': {'P1': capture('13:00:00'), 'P2': capture('13:00:10')},
                  'after': {'P3': capture('13:20:10'), 'P4': capture('13:20:20')}}
        shares = wf.adjacent_panel_shares(panels, capture('13:10:10'), 'I')
        self.assertEqual(set(shares), {'P2', 'P3'})                              # the others do not count, as in Metashape
        self.assertAlmostEqual(shares['P3'], 0.5)
        self.assertEqual(wf.adjacent_panel_shares(panels, capture('12:59:00'), 'I'), {'P1': 1.0})
        self.assertEqual(wf.adjacent_panel_shares(panels, capture('13:30:00'), 'I'), {'P4': 1.0})
        self.assertEqual(wf.adjacent_panel_shares({'before': panels['before']}, capture('13:10:10'), 'I'), {'P2': 1.0})
        config = wf.merge_config(config_for(self.folder / 'x', **{'PANEL.panels_used': 'adjacent_time'}))
        self.assertEqual(wf.validate_config(config, ROOT), [])                   # a second moment is not needed

    def test_the_orientation_of_the_panel_captures(self):
        self.assertEqual([wf.orientation_kind(heading) for heading in (20, 87, 170, None)],
                         ['facing_sun', 'side', 'back_to_sun', 'unknown'])
        groups = wf.orientation_groups({'A': 62.8, 'B': 62.6, 'C': 30.0, 'D': 30.1, 'E': 171.0, 'F': None})
        self.assertEqual([(group['heading'], group['kind'], group['captures']) for group in groups],
                         [(30, 'facing_sun', ['C', 'D']), (63, 'side', ['B', 'A']), (171, 'back_to_sun', ['E'])])
        self.assertEqual(wf.orientation_label(groups[0]), 'heading 30 deg (facing the sun), 2 captures')
        self.assertTrue(wf.orientation_selected('all', None))
        self.assertTrue(wf.orientation_selected('30', 45.0))                    # within 20 degrees
        self.assertFalse(wf.orientation_selected('30', 62.8))
        self.assertFalse(wf.orientation_selected('30', None))

        def capture(factor):
            return {'Blue': SimpleNamespace(panel_audit={'radiance_to_reflectance_factor': factor})}
        group = {'F1': capture(2.0), 'F2': capture(2.0), 'B1': capture(2.1)}
        difference = wf.orientation_difference(group, {'F1': 20.0, 'F2': 21.0, 'B1': 170.0}, False)
        self.assertEqual(difference['reference_heading'], 20)                    # the orientation with the most captures
        self.assertAlmostEqual(difference['differences'][170]['Blue'], 0.05)
        self.assertEqual(wf.orientation_difference(group, {'F1': 20.0, 'F2': 21.0, 'B1': 25.0}, False), {})
        # the example panel capture has the sun at its side (87 degrees): a heading of 30 keeps none
        side = wf.run(config_for(self.folder / 'side'), ROOT)
        self.assertEqual(side['panel']['before']['captures']['IMG_0005']['orientation'], 'side')
        self.assertEqual([group['heading'] for group in side['panel']['orientations']], [87])
        facing = wf.run(config_for(self.folder / 'facing', **{'PANEL.panel_orientation': '30'}), ROOT)
        self.assertEqual(facing['status'], 'failed')
        self.assertIn('None of the panel captures IMG_0005 has the heading chosen', facing['errors'][0])
        self.assertIn('heading 87 deg (with the sun at the side), 1 capture', facing['errors'][0])

    def test_the_moments_of_the_panel_captures_are_found_from_their_times(self):
        self.assertEqual([wf.panel_moment(t, (100, 200)) for t in (50, 120, 190, 250)],
                         ['before', 'before', 'after', 'after'])
        self.assertEqual(wf.panel_moment(250, None), 'before')
        # all the panel captures in one field: IMG_0422 was taken after IMG_0010, so it is the panel after the flight
        result = wf.run(config_for(self.folder / 'one_field', **{'INPUTS.reference_panel_capture': 'IMG_0005, IMG_0422',
                                                                  'PANEL.panels_used': 'interpolate_time'}), ROOT)
        self.assertEqual(result['status'], 'completed', result['errors'])
        self.assertEqual((list(result['panel']['before']['captures']), list(result['panel']['after']['captures'])),
                         (['IMG_0005'], ['IMG_0422']))
        two_fields = self.run_with_two_panels('two_fields', 'interpolate_time',
                                              **{'INPUTS.reference_panel_capture_after': 'IMG_0422'})
        self.assertAlmostEqual(result['captures']['IMG_0010']['panels']['weight_of_the_second_panel'],
                               two_fields['captures']['IMG_0010']['panels']['weight_of_the_second_panel'])
        # only one moment: an approach that needs two uses it alone, with a flag
        alone = wf.run(config_for(self.folder / 'alone', **{'PANEL.panels_used': 'mean'}), ROOT)
        self.assertEqual(alone['status'], 'completed', alone['errors'])
        self.assertIn("There are panel captures of one moment only: 'Average of before and after' uses them alone",
                      alone['flags'])

    def test_the_panel_captures_of_the_example_flight_that_depart_are_flagged(self):
        # three panel captures before the flight (20 s apart) and two after it, as they were taken
        config = config_for(self.folder / 'example_panels', **{
            'INPUTS.reference_panel_capture': 'IMG_0003, IMG_0005, IMG_0009, IMG_0422, IMG_0423'})
        for use_dls, flagged in ((False, ['IMG_0009']), (True, ['IMG_0003', 'IMG_0009'])):
            rows, _ = wf.check_panel_captures(dict(config, **{'CORRECTION.use_dls': use_dls}), ROOT)
            self.assertEqual([(row['capture'], row['moment']) for row in rows],
                             [('IMG_0003', 'before'), ('IMG_0005', 'before'), ('IMG_0009', 'before'),
                              ('IMG_0422', 'after'), ('IMG_0423', 'after')])
            self.assertEqual([row['capture'] for row in rows if row['flag']], flagged, use_dls)

    def test_the_panel_captures_can_be_checked_without_a_run(self):
        rows, flags = wf.check_panel_captures(config_for(self.folder / 'check', **{
            'INPUTS.reference_panel_capture': 'IMG_0005, IMG_0422'}), ROOT)
        self.assertEqual([(row['capture'], row['moment']) for row in rows], [('IMG_0005', 'before'), ('IMG_0422', 'after')])
        self.assertEqual(rows[0]['orientation'], 'with the sun at the side')
        self.assertTrue(all(row['used'] for row in rows))
        self.assertFalse((self.folder / 'check').exists())                       # nothing is written

    def test_a_panel_capture_that_departs_from_its_moment_is_named(self):
        # three copies of the panel capture; the DLS of IMG_0007 read 10 % more light than in the other two
        images = self.folder / 'three_panels_images'
        images.mkdir()
        for path in RAW.glob('*.tif'):
            shutil.copy(path, images / path.name)
            if path.name.startswith('IMG_0005'):
                for copy in ('IMG_0006', 'IMG_0007'):
                    shutil.copy(path, images / path.name.replace('IMG_0005', copy))
        original = wf._read_capture

        def changed(images_folder, capture, bands, exiftool_path, *arguments):
            objects = original(images_folder, capture, bands, exiftool_path, *arguments)
            if capture == 'IMG_0007':
                for image in objects.values():
                    image.metadata['XMP:HorizontalIrradiance'] = float(image.metadata['XMP:HorizontalIrradiance']) * 1.1
            return objects

        for use_dls in (False, True):
            values = {'INPUTS.images_folder': str(images), 'INPUTS.results_folder': str(self.folder / f'three_{use_dls}'),
                      'INPUTS.reference_panel_capture': 'IMG_0005, IMG_0006, IMG_0007',
                      'INPUTS.image_captures': 'IMG_0010', 'CORRECTION.use_dls': use_dls}
            with mock.patch.object(wf, '_read_capture', changed):
                result = wf.run(values, ROOT)
            self.assertEqual(result['status'], 'completed', result['errors'])
            departures = result['panel']['before']['departures']
            named = [flag for flag in result['flags'] if flag.startswith('Panel capture ')]
            if use_dls:                                    # only what is averaged with the DLS sees the difference
                self.assertAlmostEqual(departures['IMG_0007']['Blue'], 0.1)
                self.assertEqual(len(named), 1)
                self.assertIn('Panel capture IMG_0007: its panel factor x DLS irradiance departs +10.0 %', named[0])
            else:
                self.assertEqual(named, [])
                self.assertAlmostEqual(departures['IMG_0007']['Blue'], 0.0)
            self.assertFalse(any(flag.startswith('Panel captures ') for flag in result['flags']))   # three: named

    def copies_of_the_panel(self, name, copies, factor):
        """ An images folder with copies of IMG_0005 (same time); the DLS of the last copy read factor x the light """
        images = self.folder / name
        images.mkdir()
        for path in RAW.glob('*.tif'):
            shutil.copy(path, images / path.name)
            if path.name.startswith('IMG_0005'):
                for copy in copies:
                    shutil.copy(path, images / path.name.replace('IMG_0005', copy))
        original = wf._read_capture

        def changed(images_folder, capture, bands, exiftool_path, *arguments):
            objects = original(images_folder, capture, bands, exiftool_path, *arguments)
            if capture == copies[-1]:
                for image in objects.values():
                    image.metadata['XMP:HorizontalIrradiance'] = float(image.metadata['XMP:HorizontalIrradiance']) * factor
            return objects
        return images, changed

    def test_several_panel_captures_of_one_moment_are_averaged_and_their_spread_is_checked(self):
        # two captures of the panel before the flight; the DLS of IMG_0006 read 10 % more light: only what is averaged with
        # the DLS sees the difference, and with two captures the pair is flagged (the one that is wrong cannot be told)
        images, changed = self.copies_of_the_panel('two_panels_images', ['IMG_0006'], 1.1)
        for use_dls in (False, True):
            folder = self.folder / f'group_{use_dls}'
            values = {'INPUTS.images_folder': str(images), 'INPUTS.results_folder': str(folder),
                      'INPUTS.reference_panel_capture': 'IMG_0005, IMG_0006', 'INPUTS.image_captures': 'IMG_0010',
                      'CORRECTION.use_dls': use_dls}
            with mock.patch.object(wf, '_read_capture', changed):
                result = wf.run(values, ROOT)
            self.assertEqual(result['status'], 'completed', result['errors'])
            self.assertEqual(list(result['panel']['before']['captures']), ['IMG_0005', 'IMG_0006'])
            self.assertEqual(set(result['panel']['before']['spread']), set(wf.BAND_NAMES))
            pair = [flag for flag in result['flags'] if flag.startswith('Panel captures IMG_0005, IMG_0006: the ')]
            self.assertEqual(bool(pair), use_dls, result['flags'])
            provenance = json.loads((folder / 'IMG_0010_provenance.json').read_text(encoding='utf-8'))
            blue = provenance['bands'][0]
            self.assertEqual([panel['weight'] for panel in blue['panels']], [0.5, 0.5])
            expected = np.mean([panel['adjusted_factor' if use_dls else 'factor'] for panel in blue['panels']])
            self.assertAlmostEqual(blue['factor_applied'], expected)
            self.assertEqual(provenance['checks']['compared_with_panel'], 'IMG_0005')     # the closest in time to IMG_0010
        summary = wf.summary_lines(result)
        self.assertTrue(any(line.startswith('Panel IMG_0006:') for line in summary))

    def test_captures_to_leave_out_are_not_corrected(self):
        result = wf.run(config_for(self.folder / 'left_out', **{'INPUTS.image_captures': '',
                                                                 'INPUTS.excluded_captures': 'IMG_0003, IMG_0009, IMG_0422, IMG_0423'}),
                        ROOT)
        self.assertEqual(result['status'], 'completed', result['errors'])
        self.assertEqual(list(result['captures']), ['IMG_0010'])
        config = wf.merge_config(config_for(self.folder / 'x', **{'INPUTS.excluded_captures': 'IMG_0010, IMG_9999'}))
        problems = wf.validate_config(config, ROOT)
        self.assertIn('Captures both selected and left out: IMG_0010', problems)
        self.assertTrue(any("Capture 'IMG_9999' not found" in problem for problem in problems))

    def test_missing_irradiance_in_one_band_fails_the_dls_correction_of_the_capture(self):
        original = wf._read_capture

        def without_panchro_irradiance(images_folder, name, bands, exiftool_path, *arguments):
            objects = original(images_folder, name, bands, exiftool_path, *arguments)
            if name == 'IMG_0010':
                objects['Panchro'].metadata['XMP:HorizontalIrradiance'] = None
            return objects

        with mock.patch.object(wf, '_read_capture', without_panchro_irradiance):
            result = wf.run(config_for(self.folder / 'no_irradiance', **{'CORRECTION.use_dls': True}), ROOT)
            panel_only = wf.run(config_for(self.folder / 'no_irr_panel'), ROOT)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('HorizontalIrradiance', result['captures']['IMG_0010']['errors'][0])
        self.assertEqual(result['outputs'], [])      # nothing is saved for a capture that cannot be corrected
        self.assertEqual(panel_only['status'], 'completed', panel_only['errors'])


class CommandLineTests(unittest.TestCase):

    def run_main(self, arguments):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = wf.main(arguments)
        return code, out.getvalue(), err.getvalue()

    def test_default_configuration_can_be_written_and_loaded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'inputs.json'
            code, _, _ = self.run_main(['--write-default-config', str(path)])
            self.assertEqual(code, 0)
            self.assertEqual(wf.load_config(path), wf.default_config())

    def test_request_json_runs_and_prints_the_result_path(self):
        with tempfile.TemporaryDirectory() as folder:
            request = Path(folder) / 'request.json'
            request.write_text(json.dumps({'version': 1, 'root': str(ROOT),
                                           'config': config_for(Path(folder) / 'out')}), encoding='utf-8')
            code, out, err = self.run_main(['--request-json', str(request)])
            self.assertEqual(code, 0, err)
            self.assertIn('Status: completed', out)
            line = [line for line in out.splitlines() if line.startswith('RADIOMETRIC_RESULT_JSON=')][0]
            self.assertTrue(Path(line.split('=', 1)[1]).is_file())

    def test_wrong_request_version_and_missing_file_return_2(self):
        with tempfile.TemporaryDirectory() as folder:
            request = Path(folder) / 'request.json'
            request.write_text(json.dumps({'version': 99}), encoding='utf-8')
            self.assertEqual(self.run_main(['--request-json', str(request)])[0], 2)
            self.assertEqual(self.run_main(['--config', str(Path(folder) / 'missing.json')])[0], 2)

    def test_the_bands_of_a_capture_can_be_printed(self):
        code, out, err = self.run_main(['--detect-bands', 'IMG_0010', '--root', str(ROOT)])
        self.assertEqual(code, 0, err)
        detected = json.loads(out)
        self.assertEqual(detected['camera'], 'MicaSense RedEdge-P')
        self.assertEqual([band['name'] for band in detected['bands']], ['Blue', 'Green', 'Red', 'NIR', 'Red edge', 'Panchro'])
        code, out, err = self.run_main(['--detect-bands', '--root', str(ROOT)])          # the panel capture by default
        self.assertEqual(code, 0, err)
        self.assertEqual(len(json.loads(out)['bands']), 6)
        code, out, err = self.run_main(['--detect-bands', 'IMG_9999', '--root', str(ROOT)])
        self.assertEqual(code, 1)
        self.assertIn('FileNotFoundError', err)

    def test_invalid_configuration_returns_1_and_prints_the_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'inputs.json'
            path.write_text(json.dumps({'PANEL.percent_inner_area': 5}), encoding='utf-8')
            code, _, err = self.run_main(['--config', str(path), '--root', str(ROOT)])
            self.assertEqual(code, 1)
            self.assertIn('PANEL.percent_inner_area must be in (0, 1]', err)


if __name__ == '__main__':
    unittest.main()
