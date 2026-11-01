"""
Tests for radiometric_window.py: the values being edited are tested without a display; the window with a hidden Tk
root (skipped if Tk cannot open one)

Run from the project folder with:  python -m unittest tests.test_radiometric_window -v
"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from radiometric_app import panel_spectrum as ps
from radiometric_app import radiometric_window as rw
from radiometric_app import radiometric_workflow as wf

ROOT = Path(__file__).resolve().parent.parent


def rct_pattern():
    import radiometric_correction_tools as rct
    return rct.CAPTURE_FILE_PATTERN


class ConfigModelTests(unittest.TestCase):

    def test_defaults_are_valid_and_are_not_saved(self):
        model = rw.ConfigModel()
        config, errors = model.config()
        self.assertEqual(config, wf.default_config())
        self.assertEqual(errors, {})
        self.assertEqual(model.overrides(), {})

    def test_every_field_has_a_raw_value_of_the_right_shape(self):
        model = rw.ConfigModel()
        for field in wf.FIELDS:
            raw = model.get(field['key'])
            expected = {'band_values': dict, 'bool': bool}.get(field['kind'], str)
            self.assertIsInstance(raw, expected, field['key'])

    def test_numbers_are_converted_and_bad_text_is_reported_by_key(self):
        model = rw.ConfigModel()
        model.set('PANEL.percent_inner_area', '0.5')
        model.set('PANEL.saturation_threshold', '65000.5')
        model.set('CHECKS.jump_threshold', 'abc')
        model.set_band('PANEL.reflectance_by_band', 'Red', 'x')
        config, errors = model.config()
        self.assertEqual(config['PANEL.percent_inner_area'], 0.5)
        self.assertEqual(set(errors), {'PANEL.saturation_threshold', 'CHECKS.jump_threshold',
                                       'PANEL.reflectance_by_band'})
        self.assertEqual(errors['PANEL.saturation_threshold'], 'must be an integer')
        self.assertEqual(errors['CHECKS.jump_threshold'], 'must be a number')
        self.assertIn('Red', errors['PANEL.reflectance_by_band'])
        # an unreadable value keeps the default and is never saved
        self.assertEqual(config['PANEL.saturation_threshold'], 65000)
        self.assertEqual(model.overrides(), {'PANEL.percent_inner_area': 0.5})

    def test_one_images_folder_is_saved_as_a_path_and_several_as_a_list(self):
        model = rw.ConfigModel({'INPUTS.images_folder': ['a/raw', 'b/raw'],
                                'INPUTS.repeated_capture_folders': {'IMG_0005': 'b/raw'}})
        self.assertEqual(model.get('INPUTS.images_folder'), 'a/raw | b/raw')
        self.assertEqual(model.config()[0]['INPUTS.images_folder'], ['a/raw', 'b/raw'])
        self.assertEqual(model.config()[0]['INPUTS.repeated_capture_folders'], {'IMG_0005': 'b/raw'})
        model.set('INPUTS.images_folder', ' a/raw | ')
        self.assertEqual(model.config()[0]['INPUTS.images_folder'], 'a/raw')
        model.set('INPUTS.repeated_capture_folders', '{not json')
        self.assertIn('INPUTS.repeated_capture_folders', model.config()[1])

    def test_an_integer_typed_with_a_decimal_point_is_accepted(self):
        model = rw.ConfigModel()
        model.set('PANEL.saturation_threshold', '60000.0')
        self.assertEqual(model.config()[0]['PANEL.saturation_threshold'], 60000)

    def test_only_changed_values_are_saved_and_loaded_back(self):
        model = rw.ConfigModel()
        model.set('INPUTS.reference_panel_capture', ' IMG_0007 ')
        model.set('CORRECTION.use_dls', True)
        model.set_band('PANEL.reflectance_by_band', 'Blue', '0.5')
        with tempfile.TemporaryDirectory() as folder:
            path = model.save(Path(folder) / 'inputs.json')
            saved = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(set(saved), {'INPUTS.reference_panel_capture', 'CORRECTION.use_dls',
                                          'PANEL.reflectance_by_band'})
            self.assertEqual(saved['INPUTS.reference_panel_capture'], 'IMG_0007')
            loaded = rw.ConfigModel.load(path)
            self.assertEqual(loaded.config()[0], model.config()[0])
            self.assertFalse(Path(str(path) + '.tmp').exists())

    def test_unknown_keys_in_a_saved_file_are_an_error(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'inputs.json'
            path.write_text(json.dumps({'INPUTS.typo': 1}), encoding='utf-8')
            with self.assertRaises(wf.ConfigError):
                rw.ConfigModel.load(path)

    def test_a_key_that_does_not_exist_cannot_be_set(self):
        with self.assertRaises(KeyError):
            rw.ConfigModel().set('INPUTS.typo', 'x')

    def test_choice_fields_hold_one_of_their_values(self):
        model = rw.ConfigModel()
        self.assertEqual(model.config()[0]['PANEL.reflectance_source'], 'values')
        model.set('PANEL.reflectance_source', 'spectral_file')
        self.assertEqual(model.overrides(), {'PANEL.reflectance_source': 'spectral_file'})

    def test_every_field_belongs_to_a_page(self):
        pages = {page for page, _ in rw.SECTIONS}
        self.assertTrue(all(rw.page_of(field['key']) in pages for field in wf.FIELDS))
        self.assertEqual(rw.page_of('CHECKS.jump_threshold'), 'CORRECTION')           # the DLS page has the checks too
        self.assertEqual(rw.page_of('PANEL.manual_regions_file'), 'PANEL')                  # with the panel measurement
        self.assertEqual(rw.page_of('PANEL.percent_inner_area'), 'PANEL')


class WindowTests(unittest.TestCase):

    def setUp(self):
        import tkinter as tk
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"no display for Tk: {error}")
        self.root.withdraw()
        rw.apply_dark_theme(self.root)
        self.folder = tempfile.TemporaryDirectory()
        self.config_path = Path(self.folder.name) / 'inputs.json'
        self.frame = rw.RadiometricFrame(self.root, self.config_path, ROOT)
        self.frame.widget.pack(fill='both', expand=True)
        self.root.geometry('1000x700')
        self.root.update()

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass
        self.folder.cleanup()

    def pump(self, condition, timeout=240):
        end = time.time() + timeout
        while not condition() and time.time() < end:
            self.root.update()
            time.sleep(0.05)
        self.assertTrue(condition(), 'timed out')

    def results(self, name):
        return str(Path(self.folder.name) / name)

    def test_pages_and_fields_are_built_from_the_schema(self):
        self.assertEqual(self.frame.navigation.size(), len(rw.SECTIONS))
        self.assertEqual(set(self.frame.variables), set(wf.FIELD_KEYS))
        self.assertEqual(set(self.frame.pages), {prefix for prefix, _ in rw.SECTIONS})

    def test_typing_changes_the_model_and_marks_invalid_values(self):
        self.frame.set_value('PANEL.percent_inner_area', '0.4')
        self.assertEqual(self.frame.model.config()[0]['PANEL.percent_inner_area'], 0.4)
        self.assertEqual(str(self.frame.entries['PANEL.percent_inner_area'].cget('style')), 'TEntry')
        self.frame.set_value('PANEL.percent_inner_area', '0,4x')
        self.assertEqual(str(self.frame.entries['PANEL.percent_inner_area'].cget('style')), 'Invalid.TEntry')
        self.frame.set_value('PANEL.reflectance_by_band', {'Red': 'oops'})
        names = [row['name'] for row in self.frame.band_table.get_rows()]
        styles = {name: str(row['widgets']['reflectance'].cget('style'))
                  for name, row in zip(names, self.frame.band_table.rows)}
        self.assertEqual(styles['Red'], 'Invalid.TEntry')
        self.assertEqual(styles['Blue'], 'TEntry')

    def test_the_check_box_is_a_boolean(self):
        self.frame.set_value('CORRECTION.use_dls', True)
        self.assertIs(self.frame.model.config()[0]['CORRECTION.use_dls'], True)

    def test_save_writes_only_the_changes(self):
        self.frame.set_value('INPUTS.reference_panel_capture', 'IMG_0007')
        self.assertTrue(self.frame.save())
        self.assertEqual(json.loads(self.config_path.read_text(encoding='utf-8')),
                         {'INPUTS.reference_panel_capture': 'IMG_0007'})
        self.assertIn('Saved configuration updated', self.frame.status.cget('text'))
        self.assertIn(str(self.config_path), self.frame.status.cget('text'))
        self.assertIn('opens with these values the next time', self.frame.status.cget('text'))
        self.assertEqual(self.frame.buttons['save'].cget('text'), 'Update saved configuration')

    def test_a_saved_configuration_is_loaded_and_a_bad_one_is_reported(self):
        self.config_path.write_text(json.dumps({'CORRECTION.use_dls': True}), encoding='utf-8')
        frame = rw.RadiometricFrame(self.root, self.config_path, ROOT)
        self.assertTrue(frame.variables['CORRECTION.use_dls'].get())
        self.config_path.write_text('{not json', encoding='utf-8')
        frame = rw.RadiometricFrame(self.root, self.config_path, ROOT)
        self.assertEqual(frame.model.overrides(), {})
        self.assertTrue(any('was not used' in line for line in frame.log_lines()))

    def python_report(self, compatible=True, tk=True):
        return {'executable': r'C:\envs\a\python.exe', 'version': '3.10.1' if compatible else '3.8.0',
                'python_compatible': compatible, 'tkinter': {'ok': tk, 'detail': 'Tk 8.6' if tk else 'no display'}}

    def test_the_inputs_page_has_the_python_section_with_the_saved_python(self):
        frame = rw.RadiometricFrame(self.root, Path(self.folder.name) / 'python.json', self.folder.name)
        self.assertEqual(frame.python_var.get(), sys.executable)                  # nothing saved: the one that is running
        wf.environment.save_python_environment(self.folder.name, r'C:\envs\a\python.exe')
        frame = rw.RadiometricFrame(self.root, Path(self.folder.name) / 'python.json', self.folder.name)
        self.assertEqual(frame.python_var.get(), r'C:\envs\a\python.exe')
        self.assertTrue(self.inside(self.frame.buttons['python_check'], 'INPUTS'))
        self.assertEqual(self.frame.buttons['python_use'].cget('text'), 'Use it for the next launches')
        self.assertEqual(self.frame.buttons['python_check'].cget('text'), 'Check this Python')

    def test_check_this_python_says_what_it_has_and_what_it_lacks(self):
        items = [{'name': 'shapely', 'status': 'missing', 'version': None, 'detail': 'polygons', 'fix': 'pip install shapely',
                  'level': 'required'}]
        self.frame.python_var.set(r'C:\envs\a\python.exe')
        with mock.patch.object(rw.environment, 'inspect_python', return_value=self.python_report()), \
                mock.patch.object(rw.environment, 'check_python_environment', return_value=items) as check:
            self.frame.buttons['python_check'].invoke()
            self.assertIn('disabled', self.frame.buttons['python_use'].state())      # while it runs
            self.pump(lambda: 'shapely' in self.frame.python_status.cget('text'))
        self.assertEqual(check.call_args[0][0], r'C:\envs\a\python.exe')
        text = self.frame.python_status.cget('text')
        self.assertIn('Python 3.10.1: C:\\envs\\a\\python.exe', text)
        self.assertIn('Tkinter: works', text)
        self.assertIn('shapely: missing (polygons)', text)
        self.assertIn(r'C:\envs\a\python.exe -m pip install shapely', text)          # for that Python, not for this one
        self.assertEqual(str(self.frame.python_status.cget('style')), 'Error.TLabel')
        self.assertNotIn('disabled', self.frame.buttons['python_use'].state())
        with mock.patch.object(rw.environment, 'inspect_python', return_value=self.python_report()), \
                mock.patch.object(rw.environment, 'check_python_environment', return_value=[]):
            self.frame.buttons['python_check'].invoke()
            self.pump(lambda: 'in place' in self.frame.python_status.cget('text'))
        self.assertEqual(str(self.frame.python_status.cget('style')), 'Ok.TLabel')

    def test_a_python_that_cannot_open_a_window_is_not_asked_about_its_packages(self):
        with mock.patch.object(rw.environment, 'inspect_python', return_value=self.python_report(compatible=False, tk=False)), \
                mock.patch.object(rw.environment, 'check_python_environment') as check:
            self.frame.buttons['python_check'].invoke()
            self.pump(lambda: 'Tkinter' in self.frame.python_status.cget('text'))
        check.assert_not_called()
        text = self.frame.python_status.cget('text')
        self.assertIn('does not work - no display', text)
        self.assertIn('Python 3.10 or newer', text)

    def test_what_cannot_be_run_is_shown_as_an_error(self):
        with mock.patch.object(rw.environment, 'inspect_python', side_effect=FileNotFoundError('Python executable does not exist: x')):
            self.frame.buttons['python_check'].invoke()
            self.pump(lambda: 'does not exist' in self.frame.python_status.cget('text'))
        self.assertEqual(str(self.frame.python_status.cget('style')), 'Error.TLabel')
        self.assertNotIn('disabled', self.frame.buttons['python_check'].state())

    def test_use_it_for_the_next_launches_saves_it_and_leaves_this_window_alone(self):
        frame = rw.RadiometricFrame(self.root, Path(self.folder.name) / 'use.json', self.folder.name)
        frame.python_var.set(r'C:\envs\a\python.exe')
        with mock.patch.object(rw.environment, 'inspect_python', return_value=self.python_report()):
            frame.buttons['python_use'].invoke()
            self.pump(lambda: 'Saved' in frame.python_status.cget('text'))
        self.assertEqual(wf.environment.read_python_environment(self.folder.name), {'python': r'C:\envs\a\python.exe'})
        self.assertIn('keeps the Python that it is running', frame.python_status.cget('text'))
        with mock.patch.object(rw.environment, 'inspect_python', return_value=self.python_report(compatible=False)):
            frame.buttons['python_use'].invoke()
            self.pump(lambda: 'needs Python 3.10' in frame.python_status.cget('text'))
        self.assertEqual(wf.environment.read_python_environment(self.folder.name), {'python': r'C:\envs\a\python.exe'})

    def test_a_folder_is_chosen_in_the_window_with_the_picture_of_the_images_and_a_file_in_the_dialog_of_the_system(self):
        images = Path(self.folder.name) / 'flight' / 'raw'
        images.mkdir(parents=True)
        panels = Path(self.folder.name) / 'panels'
        self.frame.set_value('INPUTS.images_folder', str(images))
        answer = ([str(images), str(panels)], {'IMG_0005': str(panels)})
        with mock.patch('radiometric_app.folder_browser.ask_folders', return_value=answer) as ask:
            self.frame._browse('INPUTS.images_folder', 'folders')
        arguments = ask.call_args[0]
        self.assertEqual(arguments[1:], ('Images folder', images, [images], {}, rct_pattern()))
        self.assertIs(arguments[0], self.frame.parent.winfo_toplevel())
        self.assertEqual(self.frame.variables['INPUTS.images_folder'].get(), f"{images} | {panels}")
        config = self.frame.model.config()[0]
        self.assertEqual(config['INPUTS.images_folder'], [str(images), str(panels)])
        self.assertEqual(config['INPUTS.repeated_capture_folders'], {'IMG_0005': str(panels)})
        with mock.patch('radiometric_app.folder_browser.ask_folders', return_value=None) as ask:    # cancelled: nothing changes
            self.frame._browse('INPUTS.images_folder', 'folders')
        self.assertEqual(ask.call_args[0][3:5], ([images, panels], {'IMG_0005': str(panels)}))     # what is there is shown
        self.assertEqual(self.frame.variables['INPUTS.images_folder'].get(), f"{images} | {panels}")
        self.frame.set_value('INPUTS.images_folder', str(images.parent))
        missing = Path(self.folder.name) / 'not_yet' / 'results'                                    # it does not exist yet
        self.frame.set_value('INPUTS.results_folder', str(missing))
        with mock.patch('radiometric_app.folder_browser.ask_folder', return_value='') as ask:
            self.frame._browse('INPUTS.results_folder', 'folder')
        self.assertEqual(ask.call_args[0][1], 'Results folder')
        self.assertEqual(ask.call_args[0][2], Path(self.folder.name))                               # the nearest one that exists
        with mock.patch('tkinter.filedialog.askdirectory') as system_folder, \
                mock.patch('tkinter.filedialog.askopenfilename', return_value=str(images / 'x.exe')) as system_file:
            self.frame._browse('INPUTS.exiftool_path', 'file')
        system_folder.assert_not_called()
        system_file.assert_called_once()
        self.assertEqual(self.frame.variables['INPUTS.exiftool_path'].get(), str(images / 'x.exe'))

    def test_the_regions_file_in_the_results_folder_follows_the_results_folder(self):
        folder = Path(self.folder.name)
        old, new = folder / 'old_results', folder / 'new_results'
        self.frame.set_value('INPUTS.results_folder', str(old).replace('\\', '/'))             # the style of the stored paths
        self.frame.set_value('PANEL.manual_regions_file', str(old / 'panel_regions.json'))       # with the other slashes
        self.frame.set_value('INPUTS.results_folder', str(new).replace('\\', '/'))
        self.assertEqual(Path(self.frame.model.config()[0]['PANEL.manual_regions_file']), new / 'panel_regions.json')
        self.frame.set_value('INPUTS.results_folder', 'relative/results')                        # and again, with a relative one
        self.assertEqual(Path(self.frame.model.config()[0]['PANEL.manual_regions_file']),
                         Path('relative/results') / 'panel_regions.json')
        self.frame.set_value('INPUTS.results_folder', str(new))
        self.assertEqual(Path(self.frame.model.config()[0]['PANEL.manual_regions_file']), new / 'panel_regions.json')

    def test_a_regions_file_that_is_elsewhere_or_has_another_name_stays_where_it_is(self):
        folder = Path(self.folder.name)
        self.frame.set_value('INPUTS.results_folder', str(folder / 'results'))
        for chosen in (folder / 'elsewhere' / 'panel_regions.json', folder / 'results' / 'my_regions.json'):
            self.frame.set_value('PANEL.manual_regions_file', str(chosen))
            self.frame.set_value('INPUTS.results_folder', str(folder / 'other'))
            self.assertEqual(self.frame.model.config()[0]['PANEL.manual_regions_file'], str(chosen))
            self.frame.set_value('INPUTS.results_folder', str(folder / 'results'))
        self.frame.set_value('PANEL.manual_regions_file', '')                                    # empty stays empty
        self.frame.set_value('INPUTS.results_folder', str(folder / 'third'))
        self.assertEqual(self.frame.model.config()[0]['PANEL.manual_regions_file'], '')

    def test_a_profile_keeps_its_regions_file_whatever_the_results_folder_was(self):
        folder = Path(self.folder.name)
        self.frame.set_value('INPUTS.results_folder', str(folder / 'one'))
        self.frame.set_value('PANEL.manual_regions_file', str(folder / 'one' / 'panel_regions.json'))
        profile = folder / 'profile.json'
        profile.write_text(json.dumps({'INPUTS.results_folder': str(folder / 'two'),
                                       'PANEL.manual_regions_file': str(folder / 'kept' / 'regions.json')}), encoding='utf-8')
        self.assertTrue(self.frame.load_profile(profile))
        config = self.frame.model.config()[0]
        self.assertEqual(Path(config['PANEL.manual_regions_file']), folder / 'kept' / 'regions.json')

    def test_the_regions_file_is_chosen_with_the_dialog_that_names_a_file_so_that_it_need_not_exist(self):
        folder = Path(self.folder.name)
        results = folder / 'results'
        results.mkdir()
        self.frame.set_value('INPUTS.results_folder', str(results / 'not_yet'))               # a folder that does not exist
        chosen = folder / 'elsewhere' / 'my_regions.json'
        with mock.patch('tkinter.filedialog.asksaveasfilename', return_value=str(chosen)) as ask, \
                mock.patch('tkinter.filedialog.askopenfilename') as open_one:
            self.frame._browse('PANEL.manual_regions_file', 'file')
        open_one.assert_not_called()
        options = ask.call_args[1]
        self.assertEqual(options['initialdir'], results)                                       # the nearest one that exists
        self.assertEqual(options['initialfile'], 'panel_regions.json')
        self.assertIs(options['confirmoverwrite'], False)                                      # it is not written here
        self.assertEqual(options['defaultextension'], '.json')
        self.assertEqual(self.frame.variables['PANEL.manual_regions_file'].get(), str(chosen))   # a new file can be chosen
        with mock.patch('tkinter.filedialog.asksaveasfilename', return_value='') as ask:       # cancelled: nothing changes
            self.frame._browse('PANEL.manual_regions_file', 'file')
        self.assertEqual(ask.call_args[1]['initialdir'], folder)                               # it opens where the file is
        self.assertEqual(ask.call_args[1]['initialfile'], 'my_regions.json')
        self.assertEqual(self.frame.variables['PANEL.manual_regions_file'].get(), str(chosen))

    def test_browse_for_the_python_fills_the_field(self):
        with mock.patch('tkinter.filedialog.askopenfilename', return_value=r'D:\p\python.exe'):
            self.frame.browse_python()
        self.assertEqual(self.frame.python_var.get(), r'D:\p\python.exe')

    def test_check_environment_lists_the_computer_in_the_log(self):
        def entry(name, status='ok', fix=None):
            return {'name': name, 'status': status, 'version': '1.0', 'detail': 'what for', 'fix': fix, 'level': 'required'}
        button = self.frame.buttons['environment']
        self.assertEqual(button.cget('text'), 'Check environment')
        items = [entry('Python'), entry('shapely', 'missing', 'pip install shapely')]
        with mock.patch.object(rw.environment, 'check_environment', return_value=items) as check:
            self.frame.check_environment()
            self.assertIn('disabled', button.state())                                  # while it is asked
            self.pump(lambda: self.frame.summary.cget('text') != '')
        self.assertEqual(check.call_args.kwargs['deep'], True)
        self.assertNotIn('disabled', button.state())
        lines = self.frame.log_lines()
        self.assertTrue(any(line.startswith('ok       Python') for line in lines))
        self.assertTrue(any(line.startswith('MISSING  shapely') for line in lines))
        self.assertTrue(any('pip install shapely' in line for line in lines))
        self.assertIn('1 thing(s) to install or fix', self.frame.summary.cget('text'))
        with mock.patch.object(rw.environment, 'check_environment', return_value=items[:1]):
            self.frame.check_environment()
            self.pump(lambda: self.frame.summary.cget('text') == 'The computer is ready.')
        self.assertIn('Everything the program needs is in place.', self.frame.log_lines())
        self.assertFalse(any('MISSING' in line for line in self.frame.log_lines()))    # the log was cleared first

    def test_check_inputs_accepts_the_example_and_names_the_problems(self):
        config, problems = self.frame.check_inputs()
        self.assertEqual(problems, [])
        self.assertIn('The inputs are valid.', self.frame.log_lines())
        self.frame.set_value('INPUTS.reference_panel_capture', 'IMG_9999')
        self.frame.set_value('CHECKS.jump_threshold', 'abc')
        _, problems = self.frame.check_inputs()
        self.assertTrue(any('CHECKS.jump_threshold' in problem and 'must be a number' in problem for problem in problems))
        self.frame.set_value('CHECKS.jump_threshold', '0.1')
        _, problems = self.frame.check_inputs()
        self.assertTrue(any("Capture 'IMG_9999' not found" in problem for problem in problems))

    def test_running_the_workflow_reports_progress_and_the_result(self):
        self.frame.set_value('INPUTS.results_folder', self.results('run'))
        self.frame.set_value('INPUTS.image_captures', 'IMG_0010')                    # IMG_0422 is the second panel capture
        self.frame.set_value('CORRECTION.use_dls', True)
        self.frame.start_run()
        self.assertTrue(self.frame.running)
        self.assertEqual(str(self.frame.buttons['run'].cget('state')), 'disabled')
        self.pump(lambda: not self.frame.running)
        result = self.frame.last_result
        self.assertEqual(result['status'], 'completed', result['errors'])
        lines = '\n'.join(self.frame.log_lines())
        for stage in wf.STAGES[:4]:
            self.assertIn(f'[{stage}]', lines)
        self.assertIn('FLAG: DLS tilt over 10 deg in 1 of 1 capture', lines)
        self.assertIn('Status: completed. 6 image(s) written', self.frame.summary.cget('text'))
        self.assertEqual(str(self.frame.buttons['run'].cget('state')), 'normal')
        self.assertEqual(len(list(Path(self.results('run')).glob('IMG_0010_*.tif'))), 6)
        self.assertTrue(self.config_path.is_file())       # the run saves the configuration first

    def test_a_run_with_invalid_inputs_does_not_start(self):
        self.frame.set_value('PANEL.saturation_threshold', 'high')
        self.frame.start_run()
        self.assertFalse(self.frame.running)
        self.assertIsNone(self.frame.last_result)
        self.assertTrue(any('must be an integer' in line for line in self.frame.log_lines()))

    def accepted(self, polygons, origins):
        from radiometric_app import panel_selector
        return panel_selector.SelectionResult(polygons, origins)

    def test_the_accepted_regions_are_saved_with_their_origin_and_the_regions_file_is_set(self):
        self.frame.set_value('INPUTS.results_folder', self.results('regions'))
        polygon = [[700, 200], [850, 200], [850, 320], [700, 320]]
        bands = list(wf.default_config()['PANEL.reflectance_by_band'])
        selection = {'IMG_0005': self.accepted({band: polygon for band in bands},
                                               {band: ('accepted_automatic' if band != 'Blue' else 'edited')
                                                for band in bands})}
        with mock.patch('radiometric_app.panel_selector.select_panel_regions', return_value=selection) as selector:
            self.frame.draw_regions()
            self.pump(lambda: selector.called and not self.frame.running)
        path = Path(self.results('regions')) / 'panel_regions.json'
        self.assertEqual(wf.load_panel_regions(path), {'IMG_0005': {band: polygon for band in bands}})
        origins = wf.load_panel_origins(path)['IMG_0005']
        self.assertEqual((origins['Blue'], origins['Green']), ('edited', 'accepted_automatic'))
        self.assertEqual(self.frame.model.config()[0]['PANEL.manual_regions_file'], str(path))
        text = '\n'.join(self.frame.log_lines())
        self.assertIn('IMG_0005: 6 region(s), 5 as found from the QR code, 1 drawn or edited', text)
        self.assertIn('Saved the accepted regions', self.frame.inputs_status.cget('text'))
        saved = json.loads(self.config_path.read_text(encoding='utf-8'))
        self.assertEqual(saved['PANEL.manual_regions_file'], str(path))

    def test_a_regions_file_that_does_not_exist_yet_does_not_stop_the_tool_that_creates_it(self):
        path = Path(self.results('new_regions')) / 'sub' / 'my_regions.json'
        self.frame.set_value('INPUTS.results_folder', self.results('new_regions'))
        self.frame.set_value('PANEL.manual_regions_file', str(path))
        config, problems = self.frame.check_inputs_here()
        self.assertEqual(problems, [])
        self.assertIn('does not exist yet', self.frame.inputs_status.cget('text'))
        self.assertEqual(str(self.frame.inputs_status.cget('style')), 'Ok.TLabel')
        _, run_problems = self.frame.problems()                                      # a run does need the file
        self.assertTrue(any('Panel regions file not found' in problem for problem in run_problems))
        bands = list(wf.BAND_NAMES)
        polygon = [[10, 10], [20, 10], [20, 20], [10, 20]]
        selection = {'IMG_0005': self.accepted({band: polygon for band in bands}, {band: 'edited' for band in bands})}
        with mock.patch('radiometric_app.panel_selector.select_panel_regions', return_value=selection) as selector:
            self.frame.draw_regions()
            self.pump(lambda: selector.called and not self.frame.running)
        self.assertEqual(wf.load_panel_regions(path), {'IMG_0005': {band: polygon for band in bands}})   # created, with its folder
        self.assertEqual(self.frame.problems()[1], [])                                # now a run can use it

    def test_the_explanations_of_the_inner_area_and_of_the_profiles_say_what_they_do(self):
        field = next(field for field in wf.FIELDS if field['key'] == 'PANEL.percent_inner_area')
        text = rw.field_help(field)
        self.assertIn('70 % of its width and of its height (49 % of its area)', text)
        self.assertIn('# Why not all of it', text)
        self.assertIn('exactly as it was saved', text)
        save, profile = rw.PROFILE_NOTES['save'][1], rw.PROFILE_NOTES['save_profile'][1]
        self.assertIn('done by itself when you run', save)
        self.assertIn('Running does not make a profile', profile)
        self.assertIn('radiometric_result.json', profile)
        load = rw.PROFILE_NOTES['load_profile'][1]
        for name in ('radiometric_result.json', '<capture>_provenance.json', 'panel_regions.json'):
            self.assertIn(name, load)

    def test_the_inner_area_of_the_tool_is_kept_in_the_configuration(self):
        from radiometric_app.panel_selector import SelectionResults
        self.frame.set_value('INPUTS.results_folder', self.results('inner'))
        bands = list(wf.BAND_NAMES)
        polygon = [[10, 10], [20, 10], [20, 20], [10, 20]]
        selection = SelectionResults({'IMG_0005': self.accepted({band: polygon for band in bands},
                                                                {band: 'edited' for band in bands})})
        selection.percent_inner_area = 0.5
        with mock.patch('radiometric_app.panel_selector.select_panel_regions', return_value=selection) as selector:
            self.frame.draw_regions()
            self.pump(lambda: selector.called and not self.frame.running)
        self.assertEqual(selector.call_args[0][5], 0.7)                                # the tool opens with the field
        self.assertEqual(self.frame.model.config()[0]['PANEL.percent_inner_area'], 0.5)
        self.assertTrue(any('PANEL.percent_inner_area was set to 0.5' in line for line in self.frame.log_lines()))
        saved = json.loads(self.config_path.read_text(encoding='utf-8'))
        self.assertEqual(saved['PANEL.percent_inner_area'], 0.5)

    def test_cancelling_the_dialog_saves_nothing(self):
        self.frame.set_value('INPUTS.results_folder', self.results('cancel'))
        with mock.patch('radiometric_app.panel_selector.select_panel_regions', return_value=None) as selector:
            self.frame.draw_regions()
            self.pump(lambda: selector.called and not self.frame.running)
        self.assertFalse((Path(self.results('cancel')) / 'panel_regions.json').exists())
        self.assertIn('Cancelled: no region was saved.', self.frame.log_lines())
        self.assertEqual(self.frame.inputs_status.cget('text'), 'Cancelled: no region was saved.')

    def test_the_dialog_receives_the_saved_regions_their_origins_and_the_panel_parameters(self):
        self.frame.set_value('INPUTS.results_folder', self.results('again'))
        self.frame.set_value('PANEL.percent_inner_area', '0.6')
        path = Path(self.results('again')) / 'panel_regions.json'
        path.parent.mkdir(parents=True)
        wf.save_panel_regions(path, 'IMG_0005', {'Red': [[10, 10], [50, 10], [50, 50]]}, {'Red': 'edited'})
        with mock.patch('radiometric_app.panel_selector.select_panel_regions', return_value=None) as selector:
            self.frame.draw_regions()
            self.pump(lambda: selector.called and not self.frame.running)
        names, load, required, saved, origins, percent, saturation = selector.call_args[0]
        self.assertEqual(names, ['IMG_0005'])                                     # only the panel captures are listed
        self.assertEqual(required, ['IMG_0005'])
        self.assertEqual(saved, {'IMG_0005': {'Red': [[10, 10], [50, 10], [50, 50]]}})
        self.assertEqual(origins, {'IMG_0005': {'Red': 'edited'}})
        self.assertEqual((percent, saturation), (0.6, 65000))
        preloaded = selector.call_args.kwargs['preloaded']
        self.assertEqual(list(preloaded), ['IMG_0005'])
        self.assertEqual(set(preloaded['IMG_0005']), set(wf.default_config()['PANEL.reflectance_by_band']))
        other = load('IMG_0010')                                                  # the others are read when they are opened
        self.assertEqual(set(other), set(preloaded['IMG_0005']))

    def test_both_panel_captures_are_required_and_their_regions_are_saved(self):
        self.frame.set_value('INPUTS.results_folder', self.results('two'))
        self.frame.set_value('INPUTS.reference_panel_capture_after', 'IMG_0005')
        polygon = [[700, 200], [850, 200], [850, 320], [700, 320]]
        bands = list(wf.default_config()['PANEL.reflectance_by_band'])
        selection = {name: self.accepted({band: polygon for band in bands}, {band: 'edited' for band in bands})
                     for name in ('IMG_0005', 'IMG_0010')}
        with mock.patch('radiometric_app.panel_selector.select_panel_regions', return_value=selection) as selector:
            self.frame.draw_regions()
            self.pump(lambda: selector.called and not self.frame.running)
        self.assertEqual(selector.call_args[0][2], ['IMG_0005'])                   # the same capture twice is one
        path = Path(self.results('two')) / 'panel_regions.json'
        self.assertEqual(sorted(wf.load_panel_regions(path)), ['IMG_0005', 'IMG_0010'])
        self.assertEqual(wf.load_panel_origins(path)['IMG_0010']['Blue'], 'edited')
        self.assertIn('IMG_0010: 6 region(s)', self.frame.inputs_status.cget('text'))

    # --- checking the inputs before the tool is opened ----------------------------------------------------------

    def test_the_inputs_can_be_checked_next_to_the_tool_without_leaving_the_page(self):
        self.frame.show_page('INPUTS')
        config, problems = self.frame.check_inputs_here()
        self.assertEqual(problems, [])
        self.assertIn('The inputs are valid', self.frame.inputs_status.cget('text'))
        self.assertEqual(str(self.frame.inputs_status.cget('style')), 'Ok.TLabel')
        self.frame.set_value('INPUTS.reference_panel_capture', 'IMG_9999')
        _, problems = self.frame.check_inputs_here()
        self.assertTrue(any("Capture 'IMG_9999' not found" in problem for problem in problems))
        self.assertIn("Capture 'IMG_9999' not found", self.frame.inputs_status.cget('text'))
        self.assertEqual(str(self.frame.inputs_status.cget('style')), 'Error.TLabel')
        self.assertEqual([line for line in self.frame.log_lines() if line], [])      # the Run page log is not used

    def test_the_check_button_is_next_to_the_tool_on_the_reference_panel_page(self):
        self.assertTrue(self.inside(self.frame.buttons['check_here'], 'PANEL'))
        self.assertEqual(self.frame.buttons['check_here'].cget('text'), 'Check inputs')
        self.assertEqual(self.frame.buttons['check_here'].master, self.frame.buttons['regions'].master)

    def test_the_tool_is_not_opened_when_the_inputs_have_problems(self):
        self.frame.set_value('INPUTS.reference_panel_capture', 'IMG_9999')
        with mock.patch('radiometric_app.panel_selector.select_panel_regions') as selector:
            self.frame.draw_regions()
        self.assertFalse(self.frame.running)
        self.assertFalse(selector.called)
        self.assertIn("Capture 'IMG_9999' not found", self.frame.inputs_status.cget('text'))
        self.assertEqual(self.frame.navigation.curselection(), (0,))      # still on the Inputs page

    def test_problems_in_the_band_table_are_shown_next_to_the_tool_too(self):
        self.frame.band_table.add_row({'name': 'Blue', 'center': '1', 'width': '1', 'reflectance': '0.5'})
        _, problems = self.frame.check_inputs_here()
        self.assertTrue(any('used by more than one band' in problem for problem in problems))
        self.assertIn('used by more than one band', self.frame.inputs_status.cget('text'))

    def test_both_buttons_are_disabled_while_the_workflow_is_busy(self):
        self.frame._set_busy(True)
        self.assertTrue(self.frame.buttons['check_here'].instate(['disabled']))
        self.assertTrue(self.frame.buttons['regions'].instate(['disabled']))
        self.frame._set_busy(False)
        self.assertFalse(self.frame.buttons['check_here'].instate(['disabled']))

    # --- spectral response of the panel and profiles ---------------------------------------------------------

    def write_spectrum(self, name='panel.csv', level=0.5, start=400, stop=900):
        path = Path(self.folder.name) / name
        path.write_text(''.join(f"{w},{level}\n" for w in range(start, stop + 1)), encoding='utf-8')
        return path

    def enabled(self, widgets):
        return [not widget.instate(['disabled']) for widget in widgets]

    def test_only_the_controls_of_the_chosen_source_can_be_edited(self):
        values = self.frame.band_table.reflectance_entries()
        spectral = self.frame.source_widgets['spectral_file']
        self.assertEqual(len(values), 6)
        self.assertEqual(len(spectral), 3)                      # entry, Browse and Import
        self.assertTrue(all(self.enabled(values)))
        self.assertFalse(any(self.enabled(spectral)))
        self.frame.set_value('PANEL.reflectance_source', 'spectral_file')
        self.assertFalse(any(self.enabled(values)))
        self.assertTrue(all(self.enabled(spectral)))

    def test_choices_are_drop_downs_and_dialog_keys_have_no_widgets_on_the_page(self):
        self.assertEqual(self.frame.entries['PANEL.reflectance_source'].winfo_class(), 'TCombobox')
        for key in ('PANEL.spectral_delimiter', 'PANEL.spectral_skip_rows', 'PANEL.band_center_wavelengths'):
            self.assertIn(key, self.frame.variables)
            self.assertNotIn(key, self.frame.entries)
        self.frame.set_value('PANEL.spectral_delimiter', 'semicolon')
        self.frame.set_value('PANEL.band_bandwidths', {'Blue': '40'})
        config = self.frame.model.config()[0]
        self.assertEqual(config['PANEL.spectral_delimiter'], 'semicolon')
        self.assertEqual(config['PANEL.band_bandwidths']['Blue'], 40.0)

    def test_the_summary_shows_the_values_read_from_the_file_or_the_problem(self):
        path = self.write_spectrum(level=0.5, stop=848)
        self.frame.set_value('PANEL.spectral_file', str(path))
        self.pump(lambda: self.frame.spectral_details() is not None, 5)
        text = self.frame.spectral_details()[1]
        self.assertIn('- Blue: 0.5000', text)
        self.assertIn('NIR 61 %', text)                         # the window of NIR is partly covered
        self.assertEqual(self.frame.spectral_summary.cget('text'), '')
        self.assertFalse(self.frame.spectral_summary.winfo_manager())
        self.frame.set_value('PANEL.spectral_wavelength_column', '9')
        self.pump(lambda: 'data row' in self.frame.spectral_summary.cget('text'), 5)
        self.assertTrue(self.frame.spectral_summary.winfo_manager())
        self.assertIsNone(self.frame.spectral_details())
        self.frame.set_value('PANEL.spectral_wavelength_column', '1')
        self.frame.set_value('PANEL.spectral_min_coverage', '0.9')
        self.pump(lambda: "Band 'NIR'" in self.frame.spectral_summary.cget('text'), 5)
        self.frame.set_value('PANEL.spectral_file', '')
        self.pump(lambda: self.frame.spectral_summary.cget('text') == '', 5)

    def result_of_the_dialog(self, action, **kwargs):
        from radiometric_app import panel_spectrum_dialog as psd
        path = kwargs.pop('path', None) or self.write_spectrum(level=0.48)
        return psd.ImportSession(path, **kwargs).result(action)

    def test_using_the_file_fills_the_fields_and_switches_the_source(self):
        frame = rw.RadiometricFrame(self.root, Path(self.folder.name) / 'inside.json', self.folder.name)
        path = self.write_spectrum(level=0.48)
        result = self.result_of_the_dialog('use_file', path=path, fmt={'skip_rows': 0},
                                           windows=dict(ps.REDEDGE_P_BANDS, Blue=(480.0, 40.0)))
        frame.apply_import(result)
        config = frame.model.config()[0]
        self.assertEqual(config['PANEL.reflectance_source'], 'spectral_file')
        self.assertEqual(frame.band_table.get_rows()[0]['center'], '480')              # the table shows the windows
        self.assertEqual(config['PANEL.spectral_file'], 'panel.csv')       # inside the project folder: relative
        self.assertEqual(config['PANEL.spectral_skip_rows'], '0')
        self.assertEqual((config['PANEL.spectral_skip_footer'], config['PANEL.spectral_comment_prefix']), (0, ''))
        self.assertEqual((config['PANEL.spectral_wavelength_min'], config['PANEL.spectral_reflectance_scale']), ('', 1.0))
        self.assertEqual(config['PANEL.band_center_wavelengths']['Blue'], 480.0)
        self.assertEqual(config['PANEL.band_bandwidths']['Blue'], 40.0)
        self.assertEqual(wf.validate_config(dict(config, **{'INPUTS.images_folder': str(ROOT / 'images' / 'raw')}),
                                            self.folder.name), [])
        levels, info = wf.panel_reflectance_levels(config, self.folder.name)
        self.assertAlmostEqual(levels['Blue'], 0.48)
        self.assertFalse(any(self.enabled(frame.band_table.reflectance_entries())))

    def test_the_reading_options_of_the_dialog_reach_the_fields(self):
        result = self.result_of_the_dialog('use_file', fmt={'comment_prefix': '#', 'skip_footer': 0, 'exclude_rows': '3',
                                                            'wavelength_min': 420.0, 'reflectance_scale': 1.0,
                                                            'delimiter': 'custom', 'delimiter_char': ','})
        self.frame.apply_import(result)
        config = self.frame.model.config()[0]
        self.assertEqual(config['PANEL.spectral_comment_prefix'], '#')
        self.assertEqual(config['PANEL.spectral_exclude_rows'], '3')
        self.assertEqual(config['PANEL.spectral_wavelength_min'], '420.0')
        self.assertEqual((config['PANEL.spectral_delimiter'], config['PANEL.spectral_delimiter_char']), ('custom', ','))
        self.assertEqual(wf.spectral_format(config)['wavelength_min'], 420.0)

    def test_a_file_outside_the_project_folder_keeps_its_full_path(self):
        other = tempfile.TemporaryDirectory()
        try:
            path = Path(other.name) / 'panel.csv'
            path.write_text(''.join(f"{w},0.5\n" for w in range(400, 901)), encoding='utf-8')
            self.frame.apply_import(self.result_of_the_dialog('use_file', path=path))
            self.assertEqual(self.frame.model.config()[0]['PANEL.spectral_file'], str(path))
        finally:
            other.cleanup()

    def test_copying_the_values_fills_the_typed_list_and_returns_to_the_typed_source(self):
        self.frame.set_value('PANEL.reflectance_source', 'spectral_file')
        self.frame.apply_import(self.result_of_the_dialog('copy_values'))
        config = self.frame.model.config()[0]
        self.assertEqual(config['PANEL.reflectance_source'], 'values')
        self.assertTrue(all(value == 0.48 for value in config['PANEL.reflectance_by_band'].values()))
        self.assertEqual(config['PANEL.spectral_file'], '')                # the file is not made the source
        self.assertTrue(any('copied' in line for line in self.frame.log_lines()))

    def test_the_import_button_opens_the_dialog_with_the_current_values_and_applies_the_answer(self):
        self.frame.set_value('PANEL.spectral_delimiter', 'comma')
        self.frame.set_value('PANEL.band_bandwidths', {'Red': '20'})
        answer = self.result_of_the_dialog('use_file')
        with mock.patch('radiometric_app.panel_spectrum_dialog.import_spectrum', return_value=answer) as dialog:
            self.frame.import_spectrum()
        _, path, fmt, windows, coverage = dialog.call_args[0]
        self.assertEqual(path, '')
        self.assertEqual(fmt['delimiter'], 'comma')
        self.assertEqual(windows['Red'], (668.0, 20.0))
        self.assertEqual(coverage, 0.5)
        self.assertEqual(self.frame.model.config()[0]['PANEL.reflectance_source'], 'spectral_file')
        with mock.patch('radiometric_app.panel_spectrum_dialog.import_spectrum', return_value=None):
            self.frame.import_spectrum()                                   # cancelled: nothing changes
        self.assertEqual(self.frame.model.config()[0]['PANEL.reflectance_source'], 'spectral_file')

    def spectral_buttons(self):
        entry, browse, importer = self.frame.source_widgets['spectral_file'][-3:]
        return browse, importer

    def test_browsing_for_the_spectral_file_opens_the_import_dialog_on_it_with_the_format_detected(self):
        self.frame.set_value('PANEL.reflectance_source', 'spectral_file')           # the buttons belong to this source
        browse, importer = self.spectral_buttons()
        self.assertEqual((browse.cget('text'), importer.cget('text')), ('Browse...', 'Preview and format...'))
        path = self.write_spectrum(level=0.48)
        answer = self.result_of_the_dialog('use_file', path=path)
        with mock.patch('tkinter.filedialog.askopenfilename', return_value=str(path)), \
                mock.patch('radiometric_app.panel_spectrum_dialog.import_spectrum', return_value=None) as dialog:
            browse.invoke()                                                    # cancelled in the dialog: nothing changes
        self.assertEqual(dialog.call_args[0][1], str(path))
        self.assertIs(dialog.call_args[1]['detect'], True)
        config = self.frame.model.config()[0]
        self.assertEqual(config['PANEL.spectral_file'], '')
        with mock.patch('tkinter.filedialog.askopenfilename', return_value=str(path)), \
                mock.patch('radiometric_app.panel_spectrum_dialog.import_spectrum', return_value=answer):
            browse.invoke()                                                    # accepted: the path and the format together
        config = self.frame.model.config()[0]
        self.assertEqual(Path(config['PANEL.spectral_file']).name, 'panel.csv')
        self.assertEqual(config['PANEL.reflectance_source'], 'spectral_file')

    def test_the_file_that_is_already_the_source_keeps_its_format_and_a_cancelled_choice_does_nothing(self):
        self.frame.set_value('PANEL.reflectance_source', 'spectral_file')
        browse, importer = self.spectral_buttons()
        path = self.write_spectrum(level=0.48)
        self.frame.set_value('PANEL.spectral_file', str(path))
        with mock.patch('tkinter.filedialog.askopenfilename', return_value=str(path)), \
                mock.patch('radiometric_app.panel_spectrum_dialog.import_spectrum', return_value=None) as dialog:
            browse.invoke()
        self.assertIs(dialog.call_args[1]['detect'], False)                    # the same file: the saved format is used
        with mock.patch('tkinter.filedialog.askopenfilename', return_value=''), \
                mock.patch('radiometric_app.panel_spectrum_dialog.import_spectrum') as dialog:
            browse.invoke()                                                    # the file dialog was cancelled
        dialog.assert_not_called()
        with mock.patch('radiometric_app.panel_spectrum_dialog.import_spectrum', return_value=None) as dialog:
            importer.invoke()                                                  # the preview of the file that is chosen
        self.assertEqual(dialog.call_args[0][1], str(path))
        self.assertIs(dialog.call_args[1]['detect'], False)

    def test_the_settings_of_a_past_run_can_be_loaded_from_its_result_or_provenance_file(self):
        folder = Path(self.folder.name)
        values = dict(wf.default_config(), **{'INPUTS.results_folder': 'past_results', 'PANEL.percent_inner_area': 0.5,
                                              'CORRECTION.use_dls': True})
        result = folder / 'radiometric_result.json'
        result.write_text(json.dumps({'status': 'completed', 'config': values, 'outputs': []}), encoding='utf-8')
        self.assertTrue(self.frame.load_profile(result))
        config = self.frame.model.config()[0]
        self.assertEqual((config['INPUTS.results_folder'], config['PANEL.percent_inner_area'], config['CORRECTION.use_dls']),
                         ('past_results', 0.5, True))
        text = self.frame.status.cget('text')
        self.assertIn('Settings of the run loaded', text)
        self.assertIn('results folder is the one of that run', text)
        self.assertIn('past_results', text)
        other = dict(values, **{'PANEL.percent_inner_area': 0.9, 'CORRECTION.use_dls': False})
        provenance = folder / 'IMG_0010_provenance.json'
        provenance.write_text(json.dumps({'method': 'panel_only', 'parameters': {'config': other}}), encoding='utf-8')
        self.assertTrue(self.frame.load_profile(provenance))
        config = self.frame.model.config()[0]
        self.assertEqual((config['PANEL.percent_inner_area'], config['CORRECTION.use_dls']), (0.9, False))
        profile = folder / 'profile.json'                                                  # a profile is loaded as before
        profile.write_text(json.dumps({'PANEL.percent_inner_area': 0.6}), encoding='utf-8')
        self.assertTrue(self.frame.load_profile(profile))
        self.assertIn('Profile loaded from', self.frame.status.cget('text'))
        self.assertEqual(self.frame.model.config()[0]['PANEL.percent_inner_area'], 0.6)

    def test_a_run_record_with_a_key_that_does_not_exist_any_more_is_refused_and_nothing_changes(self):
        folder = Path(self.folder.name)
        old = folder / 'old_result.json'
        old.write_text(json.dumps({'config': dict(wf.default_config(), **{'OLD.key': 1})}), encoding='utf-8')
        before = dict(self.frame.model.raw)
        self.assertFalse(self.frame.load_profile(old))
        self.assertIn('was not loaded', self.frame.status.cget('text'))
        self.assertIn('OLD.key', self.frame.status.cget('text'))
        self.assertEqual(self.frame.model.raw, before)
        notes = rw.PROFILE_NOTES['load_profile'][1]
        self.assertIn('radiometric_result.json of a past run', notes)

    def test_a_profile_saves_every_value_and_restores_them(self):
        self.frame.set_value('INPUTS.reference_panel_capture', 'IMG_0007')
        self.frame.set_value('CORRECTION.use_dls', True)
        self.frame.set_value('PANEL.reflectance_by_band', {'Blue': '0.5'})
        path = Path(self.folder.name) / 'profiles' / 'mine.json'
        self.assertTrue(self.frame.save_profile(path))
        saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(list(saved), wf.FIELD_KEYS)                         # all the keys, not only the changes
        self.assertEqual(saved['PANEL.reflectance_by_band']['Blue'], 0.5)
        self.frame.set_value('INPUTS.reference_panel_capture', 'IMG_0001')
        self.frame.set_value('CORRECTION.use_dls', False)
        self.assertTrue(self.frame.load_profile(path))
        config = self.frame.model.config()[0]
        self.assertEqual(config['INPUTS.reference_panel_capture'], 'IMG_0007')
        self.assertIs(config['CORRECTION.use_dls'], True)
        self.assertEqual(config['PANEL.reflectance_by_band']['Blue'], 0.5)
        self.assertIs(self.frame.variables['CORRECTION.use_dls'].get(), True)     # the check box follows

    def test_a_profile_with_only_some_keys_changes_the_others_to_the_defaults(self):
        path = Path(self.folder.name) / 'small.json'
        path.write_text(json.dumps({'PANEL.percent_inner_area': 0.4}), encoding='utf-8')
        self.frame.set_value('CORRECTION.use_dls', True)
        self.assertTrue(self.frame.load_profile(path))
        config = self.frame.model.config()[0]
        self.assertEqual(config['PANEL.percent_inner_area'], 0.4)
        self.assertIs(config['CORRECTION.use_dls'], False)

    def test_bad_profiles_are_refused_and_nothing_changes(self):
        path = Path(self.folder.name) / 'bad.json'
        path.write_text(json.dumps({'INPUTS.typo': 1}), encoding='utf-8')
        self.frame.set_value('INPUTS.reference_panel_capture', 'IMG_0007')
        self.assertFalse(self.frame.load_profile(path))
        self.assertFalse(self.frame.load_profile(Path(self.folder.name) / 'missing.json'))
        self.assertEqual(self.frame.model.config()[0]['INPUTS.reference_panel_capture'], 'IMG_0007')
        self.assertTrue(any('INPUTS.typo' in line for line in self.frame.log_lines()))

    def test_a_profile_is_not_saved_while_a_value_cannot_be_read(self):
        self.frame.set_value('PANEL.saturation_threshold', 'high')
        path = Path(self.folder.name) / 'profiles' / 'refused.json'
        self.assertFalse(self.frame.save_profile(path))
        self.assertFalse(path.exists())

    # --- bands of the camera ----------------------------------------------------------------------------------

    def names(self):
        return [row['name'] for row in self.frame.band_table.get_rows()]

    def row(self, name):
        return next(row for row in self.frame.band_table.rows if row['vars']['name'].get() == name)

    def test_the_table_shows_the_bands_of_the_configuration(self):
        rows = self.frame.band_table.get_rows()
        self.assertEqual([r['name'] for r in rows], ['Blue', 'Green', 'Panchro', 'Red', 'Red edge', 'NIR'])
        self.assertEqual(rows[0], {'name': 'Blue', 'center': '475.0', 'width': '32.0', 'reflectance': '0.477', 'process': True})
        self.assertEqual(self.frame.model.get('INPUTS.ignored_bands'), '')
        for key in rw.BAND_KEYS:
            self.assertIs(self.frame.variables[key], self.frame.band_table)

    def test_editing_the_table_writes_the_configuration(self):
        self.row('Blue')['vars']['center'].set('480')
        self.row('Blue')['vars']['reflectance'].set('0.5')
        config = self.frame.model.config()[0]
        self.assertEqual(config['PANEL.band_center_wavelengths']['Blue'], 480.0)
        self.assertEqual(config['PANEL.reflectance_by_band']['Blue'], 0.5)
        self.assertEqual(config['PANEL.band_bandwidths']['Blue'], 32.0)

    def test_a_band_that_is_not_used_moves_to_the_ignored_bands(self):
        self.row('Panchro')['vars']['process'].set(False)
        self.row('NIR')['vars']['process'].set(False)
        config = self.frame.model.config()[0]
        self.assertEqual(config['INPUTS.ignored_bands'], 'Panchro, NIR')
        for key in ('PANEL.reflectance_by_band', 'PANEL.band_center_wavelengths', 'PANEL.band_bandwidths'):
            self.assertEqual(list(config[key]), ['Blue', 'Green', 'Red', 'Red edge'])
        self.assertEqual(wf.validate_config(config, ROOT), [])
        self.row('NIR')['vars']['process'].set(True)                                 # and back
        self.assertEqual(self.frame.model.get('INPUTS.ignored_bands'), 'Panchro')
        self.assertIn('NIR', self.frame.model.get('PANEL.band_center_wavelengths'))

    def test_bands_can_be_added_renamed_and_removed(self):
        table = self.frame.band_table
        new = table.add_row({'name': 'Coastal', 'center': '444', 'width': '28', 'reflectance': '0.47'})
        self.assertEqual(self.frame.model.config()[0]['PANEL.band_center_wavelengths']['Coastal'], 444.0)
        new['vars']['name'].set('Coastal blue')
        config = self.frame.model.config()[0]
        self.assertIn('Coastal blue', config['PANEL.reflectance_by_band'])
        self.assertNotIn('Coastal', config['PANEL.reflectance_by_band'])
        table.remove(new)
        table.remove(self.row('Panchro'))
        config = self.frame.model.config()[0]
        self.assertEqual(list(config['PANEL.reflectance_by_band']), ['Blue', 'Green', 'Red', 'Red edge', 'NIR'])
        self.assertEqual(wf.validate_config(config, ROOT), [])

    def test_problems_in_the_table_stop_the_run_and_are_listed(self):
        self.frame.band_table.add_row({'name': 'Blue', 'center': '1', 'width': '1', 'reflectance': '0.5'})
        config, problems = self.frame.check_inputs()
        self.assertTrue(any("'Blue' is used by more than one band" in problem for problem in problems))
        self.frame.start_run()
        self.assertFalse(self.frame.running)
        self.assertIn('is used by more than one band', self.frame.band_info.cget('text'))

    def test_a_band_without_a_name_is_reported_and_not_written(self):
        self.frame.band_table.add_row({'center': '500', 'width': '10', 'reflectance': '0.5'})
        self.assertEqual(len(self.frame.model.get('PANEL.band_center_wavelengths')), 6)
        _, problems = self.frame.problems()
        self.assertTrue(any('needs a name' in problem for problem in problems))

    def test_setting_the_band_keys_updates_the_table(self):
        self.frame.set_value('PANEL.reflectance_by_band', {'Blue': '0.45'})                 # added to the bands
        self.assertEqual(self.frame.band_table.get_rows()[0]['reflectance'], '0.45')
        self.assertEqual(len(self.names()), 6)
        self.frame.set_value('INPUTS.ignored_bands', 'LWIR, Thermal')
        rows = self.frame.band_table.get_rows()
        self.assertEqual([(r['name'], r['process']) for r in rows[-2:]], [('LWIR', False), ('Thermal', False)])
        self.frame.set_value('PANEL.reflectance_by_band', {'Red': '0.4'}, replace=True)    # replaces the reflectances
        self.assertEqual(self.frame.band_table.get_rows()[0]['reflectance'], '')
        self.assertEqual(self.frame.model.get('PANEL.reflectance_by_band'), {'Red': '0.4'})

    def test_the_typed_reflectance_is_not_needed_when_the_spectral_file_is_the_source(self):
        self.row('Blue')['vars']['reflectance'].set('oops')
        self.assertIn('PANEL.reflectance_by_band', self.frame.model.config()[1])
        self.frame.set_value('PANEL.reflectance_source', 'spectral_file')
        config, errors = self.frame.model.config()
        self.assertNotIn('PANEL.reflectance_by_band', errors)
        self.assertEqual(config['PANEL.reflectance_by_band']['Blue'], 0.5)                 # placeholder, not used
        self.assertEqual(config['PANEL.reflectance_by_band']['Green'], 0.478)              # readable values are kept
        self.assertTrue(all(entry.instate(['disabled']) for entry in self.frame.band_table.reflectance_entries()))
        self.assertEqual(str(self.row('Blue')['widgets']['reflectance'].cget('style')), 'TEntry')

    def test_a_profile_replaces_the_bands(self):
        path = Path(self.folder.name) / 'profiles' / 'three.json'
        path.parent.mkdir()
        path.write_text(json.dumps({
            'PANEL.reflectance_by_band': {'Green': 0.5, 'Red': 0.5, 'NIR': 0.5},
            'PANEL.band_center_wavelengths': {'Green': 550.0, 'Red': 650.0, 'NIR': 800.0},
            'PANEL.band_bandwidths': {'Green': 40.0, 'Red': 40.0, 'NIR': 40.0},
            'INPUTS.ignored_bands': 'LWIR'}), encoding='utf-8')
        self.assertTrue(self.frame.load_profile(path))
        self.assertEqual(self.names(), ['Green', 'Red', 'NIR', 'LWIR'])
        self.assertEqual([r['process'] for r in self.frame.band_table.get_rows()], [True, True, True, False])
        config = self.frame.model.config()[0]
        self.assertEqual(list(config['PANEL.band_center_wavelengths']), ['Green', 'Red', 'NIR'])
        saved = Path(self.folder.name) / 'profiles' / 'back.json'
        self.assertTrue(self.frame.save_profile(saved))
        self.assertEqual(json.loads(saved.read_text(encoding='utf-8'))['INPUTS.ignored_bands'], 'LWIR')

    def test_a_preset_fills_the_table_and_keeps_the_typed_reflectance_of_the_same_bands(self):
        self.assertIn('micasense_rededge_mx', self.frame.presets)
        self.frame.apply_preset('micasense_rededge_mx')
        self.assertEqual(self.names(), ['Blue', 'Green', 'Red', 'NIR', 'Red edge'])
        self.assertEqual(self.frame.band_table.get_rows()[0]['reflectance'], '0.477')      # kept: same name
        self.assertEqual(self.frame.band_table.get_rows()[2]['width'], '14')
        self.assertNotIn('Panchro', self.frame.model.get('PANEL.reflectance_by_band'))      # the panchromatic band is gone
        self.assertIn('Preset: MicaSense RedEdge-M', self.frame.band_info.cget('text'))
        self.assertEqual(self.frame.model.config()[0]['INPUTS.file_pattern'], rct_pattern())

    def test_a_preset_can_leave_a_band_out(self):
        self.frame.apply_preset('micasense_altum')
        self.assertEqual(self.names(), ['Blue', 'Green', 'Red', 'NIR', 'Red edge', 'LWIR'])
        self.assertEqual(self.frame.model.config()[0]['INPUTS.ignored_bands'], 'LWIR')
        self.assertEqual(self.frame.band_table.get_rows()[-1]['process'], False)

    def test_the_selected_preset_is_applied_by_its_label(self):
        label = next(label for label, key in self.frame.preset_labels.items() if key == 'micasense_altum')
        self.frame.preset_var.set(label)
        self.frame.apply_selected_preset()
        self.assertEqual(len(self.names()), 6)
        self.frame.preset_var.set('')
        self.frame.apply_selected_preset()                                                  # nothing selected: nothing
        self.assertEqual(len(self.names()), 6)

    def test_the_bands_are_read_from_the_images_of_the_panel_capture(self):
        self.row('Blue')['vars']['reflectance'].set('0.4')
        self.frame.detect_bands()
        self.assertTrue(self.frame.detecting)
        self.pump(lambda: not self.frame.detecting, 60)
        rows = self.frame.band_table.get_rows()
        self.assertEqual([(r['name'], r['center'], r['width']) for r in rows],
                         [('Blue', '475', '32'), ('Green', '560', '27'), ('Red', '668', '14'),
                          ('NIR', '842', '57'), ('Red edge', '717', '12'), ('Panchro', '634.5', '463')])
        self.assertEqual(rows[0]['reflectance'], '0.4')                                     # typed values are kept
        self.assertEqual(rows[2]['reflectance'], '0.478')
        self.assertTrue(all(r['process'] for r in rows))
        text = self.frame.band_info.cget('text')
        self.assertIn('Read 6 bands from IMG_0005: MicaSense RedEdge-P', text)
        self.assertEqual(self.frame.band_buttons['detect'].instate(['disabled']), False)

    def test_detection_reports_a_capture_that_does_not_exist(self):
        self.frame.set_value('INPUTS.reference_panel_capture', 'IMG_9999')
        self.frame.detect_bands()
        self.pump(lambda: not self.frame.detecting, 60)
        self.assertIn('FileNotFoundError', self.frame.band_info.cget('text'))
        self.assertEqual(len(self.names()), 6)                                              # the table is not changed

    def test_detection_leaves_a_thermal_band_out(self):
        detected = {'camera': 'MicaSense Altum', 'serial': 'AL06-1', 'bands': [
            {'name': 'Blue', 'center_nm': 475.0, 'bandwidth_nm': 32.0, 'thermal': False},
            {'name': 'LWIR', 'center_nm': 11000.0, 'bandwidth_nm': 6000.0, 'thermal': True}]}
        self.frame.apply_detected_bands(detected, 'IMG_0001')
        self.assertEqual(self.names(), ['Blue', 'LWIR'])
        self.assertEqual(self.frame.model.config()[0]['INPUTS.ignored_bands'], 'LWIR')
        text = self.frame.band_info.cget('text')
        self.assertIn('serial AL06-1', text)
        self.assertIn('Not used because they look thermal: LWIR', text)

    def test_fewer_bands_can_be_run_from_the_window(self):
        for name in ('Panchro', 'Red edge', 'NIR'):
            self.row(name)['vars']['process'].set(False)
        self.frame.set_value('INPUTS.results_folder', self.results('three'))
        self.frame.set_value('INPUTS.image_captures', 'IMG_0010')
        self.frame.start_run()
        self.pump(lambda: not self.frame.running)
        result = self.frame.last_result
        self.assertEqual(result['status'], 'completed', result['errors'])
        self.assertEqual(sorted(Path(path).name for path in result['outputs']),
                         ['IMG_0010_1.tif', 'IMG_0010_2.tif', 'IMG_0010_3.tif'])

    def test_a_long_page_scrolls_and_a_short_one_does_not(self):
        self.root.attributes('-alpha', 0.0)
        self.root.deiconify()
        self.root.geometry('1000x420')
        self.frame.show_page('PANEL')
        self.root.update()
        self.assertTrue(self.frame.scroll_pages['PANEL'].scrolls())
        self.root.geometry('1000x1900')
        self.root.update()
        self.assertFalse(self.frame.scroll_pages['PANEL'].scrolls())
        self.assertNotIn('RUN', self.frame.scroll_pages)                                   # the log scrolls by itself

    def inside(self, widget, page):
        return str(widget).startswith(str(self.frame.scroll_pages[page].body))

    def test_the_dls_correction_and_its_checks_are_one_page(self):
        titles = [self.frame.navigation.get(index).strip() for index in range(self.frame.navigation.size())]
        self.assertEqual(titles, ['Inputs', 'Reference panel', 'DLS', 'Run and results'])
        self.assertEqual(self.frame.scroll_pages['CORRECTION'].body.winfo_children()[0].cget('text'),
                         'DLS (Downwelling Light Sensor)')
        keys = ['CORRECTION.use_dls'] + [field['key'] for field in wf.FIELDS if field['key'].startswith('CHECKS.')]
        self.assertEqual(len(keys), 13)
        for key in keys:
            self.assertIn(key, self.frame.variables)
        for key in keys[1:]:
            self.assertTrue(self.inside(self.frame.entries[key], 'CORRECTION'), key)
        self.assertNotIn('CHECKS', self.frame.pages)

    def test_the_dls_page_has_a_heading_for_each_part(self):
        headings = [child.cget('text') for child in self.frame.scroll_pages['CORRECTION'].body.winfo_children()
                    if child.winfo_class() == 'TLabel']
        self.assertIn('Correction with the DLS', headings)
        self.assertIn('Checks of the DLS data (limits)', headings)
        self.assertLess(headings.index('Correction with the DLS'), headings.index('Checks of the DLS data (limits)'))

    def test_the_panel_region_tool_is_on_the_reference_panel_page_after_the_panel_measurement(self):
        self.assertTrue(self.inside(self.frame.entries['PANEL.manual_regions_file'], 'PANEL'))
        self.assertTrue(self.inside(self.frame.buttons['regions'], 'PANEL'))
        self.assertTrue(self.inside(self.frame.buttons['check_here'], 'PANEL'))
        self.assertFalse(self.inside(self.frame.buttons['regions'], 'INPUTS'))
        self.assertNotIn('PANEL.manual_regions_file', [key for key in self.frame.entries
                                                       if self.inside(self.frame.entries[key], 'INPUTS')])
        order = wf.FIELD_KEYS
        for key in ('PANEL.panels_used', 'PANEL.percent_inner_area', 'PANEL.saturation_threshold'):
            self.assertLess(order.index(key), order.index('PANEL.manual_regions_file'))
        headings = [child.cget('text') for child in self.frame.scroll_pages['PANEL'].body.winfo_children()
                    if child.winfo_class() == 'TLabel']
        self.assertLess(headings.index('Panel measurement'), headings.index('Reference panel regions'))
        run_page_buttons = [child.cget('text') for child in self.frame.pages['RUN'].winfo_children()[1].winfo_children()]
        self.assertEqual(run_page_buttons, ['Check inputs', 'Check environment', 'Run', 'Open results folder'])

    def test_the_inputs_page_has_headings_for_its_groups(self):
        headings = [child.cget('text') for child in self.frame.scroll_pages['INPUTS'].body.winfo_children()
                    if child.winfo_class() == 'TLabel']
        self.assertEqual(headings[1:], ['Folders and tools', 'Output', 'Reference panel', 'Images to correct',
                                        'Python environment'])

    def test_the_messages_follow_the_width_of_the_page_and_the_help_column_keeps_its_width(self):
        self.root.attributes('-alpha', 0.0)
        self.root.deiconify()
        self.root.geometry('1300x760')
        self.root.update()
        self.assertGreater(len(self.frame.wrapped_labels), 3)
        narrow = {int(label.cget('wraplength')) for label in self.frame.wrapped_labels}
        panel_width = self.frame.help_panel.widget.winfo_width()
        self.assertEqual(len(narrow), 1)
        self.root.geometry('1800x760')
        self.root.update()
        wide = {int(label.cget('wraplength')) for label in self.frame.wrapped_labels}
        self.assertEqual(len(wide), 1)
        self.assertGreater(wide.pop(), narrow.pop() + 300)
        self.assertEqual(self.frame.help_panel.widget.winfo_width(), panel_width)          # the help column does not grow

    def test_every_field_of_a_page_is_explained_in_the_help_column(self):
        for field in wf.FIELDS:
            key = field['key']
            if field.get('dialog') or key in rw.BAND_KEYS:
                continue
            if key not in self.frame.entries:
                continue                                                      # a check box has no entry
            widget = self.frame.entries[key]
            found = self.frame.help_panel.explanation_of(next(iter(widget.values())) if isinstance(widget, dict) else widget)
            self.assertEqual((found[0], found[1]), (field['label'], rw.field_help(field)), key)
            self.assertNotIn('Default', found[1], key)                              # the defaults are not shown

    def dls_labels(self):
        return [child.cget('text') for child in self.frame.scroll_pages['CORRECTION'].body.winfo_children()
                if child.winfo_class() == 'TLabel']

    def test_the_dls_page_explains_the_ratio_and_where_the_zenith_comes_from(self):
        self.frame.show_page('CORRECTION')
        body = self.frame.scroll_pages['CORRECTION'].body
        notes = [self.frame.help_panel.explanation_of(child)[1] for child in body.winfo_children()
                 if child.winfo_class() == 'TLabel' and self.frame.help_panel.explanation_of(child)]
        helps = [field['help'] for field in wf.FIELDS if field['key'].startswith('CHECKS.')]
        texts = ' '.join([self.frame.help_panel.default[1]] + notes + helps)
        for phrase in ('E_panel / E_image', 'XMP:HorizontalIrradiance', 'a ratio of 0.885 means that the light was 13 % stronger',
                       'the reflectance is multiplied by 0.885', 'XMP:EstimatedDirectLightVector',
                       'The solar zenith is 90 degrees minus it', 'the DLS was tilted and they differ',
                       'Only the metadata of the image and of the panel captures is used'):
            self.assertIn(phrase, texts)
        self.assertIn('Checks of the DLS data (limits)', self.dls_labels())

    def test_each_part_of_the_dls_page_is_explained_by_its_heading(self):
        body = self.frame.scroll_pages['CORRECTION'].body
        headings = {child.cget('text'): child for child in body.winfo_children() if child.winfo_class() == 'TLabel'}
        for group, start in (('Correction with the DLS', 'The panel photo sets the reflectance scale'),
                             ('Checks of the DLS data (limits)', 'These checks look for DLS data')):
            found = self.frame.help_panel.explanation_of(headings[group])
            self.assertEqual(found[0], group)
            self.assertTrue(found[1].startswith(start), group)
        field = self.frame.help_panel.explanation_of(self.frame.entries['CHECKS.jump_threshold'])
        self.assertEqual(field[2], '')                                              # a field does not repeat the note

    def test_the_limits_say_what_they_mean(self):
        helps = {field['key']: field['help'] for field in wf.FIELDS if field['key'].startswith(('CHECKS.', 'CORRECTION.'))}
        self.assertIn('DLS ratio E_panel / E_image', helps['CORRECTION.use_dls'])
        self.assertIn('median of the ratios of all the bands of the same capture', helps['CHECKS.band_ratio_tolerance'])
        self.assertIn('twice as much', helps['CHECKS.ratio_range_min'])
        self.assertIn('half as much', helps['CHECKS.ratio_range_max'])
        self.assertIn('XMP:SolarElevation', helps['CHECKS.max_sun_angle_discrepancy_deg'])
        self.assertIn('the tilt is at least as large as the difference', helps['CHECKS.max_sun_angle_discrepancy_deg'])
        self.assertIn('spike', helps['CHECKS.jump_threshold'])
        self.assertIn('step', helps['CHECKS.jump_threshold'])
        self.assertIn('panel factor', helps['CHECKS.panel_drift_tolerance'])
        self.assertIn('DLS and the camera', helps['CHECKS.panel_drift_tolerance'])
        self.assertIn('flag asks you to look', helps['CHECKS.ratio_range_max'])

    def test_the_limits_of_the_geometry_are_on_the_dls_page_with_their_note(self):
        for key in ('CHECKS.max_camera_tilt_deg', 'CHECKS.panel_heading_tolerance_deg', 'CHECKS.image_heading_tolerance_deg',
                    'CHECKS.heading_offset_deg'):
            self.assertTrue(self.inside(self.frame.entries[key], 'CORRECTION'), key)
        body = self.frame.scroll_pages['CORRECTION'].body
        headings = {child.cget('text'): child for child in body.winfo_children() if child.winfo_class() == 'TLabel'}
        found = self.frame.help_panel.explanation_of(headings['Acquisition geometry (limits)'])
        self.assertIn('0 degrees: the camera faces the sun', found[1])
        self.assertIn('crossed pattern', found[1])
        self.assertIn('magnetic declination', wf.FIELDS[wf.FIELD_KEYS.index('CHECKS.heading_offset_deg')]['help'])

    def test_the_summary_of_the_run_is_written_at_the_end_of_the_log(self):
        result = {'status': 'completed', 'errors': [], 'flags': ['IMG_0010: a flag'], 'outputs': ['a.tif'],
                  'summary': {'captures': 1, 'corrected': 1, 'files': 1, 'flags': 1, 'irradiance_changes': 0,
                              'limits': {'camera_tilt_deg': 5.0, 'heading_tolerance_deg': 30.0, 'dls_tilt_deg': 10.0,
                                         'dls_ratio': [0.5, 2.0]},
                              'panels': {}, 'images': {'camera_tilt_deg': {'n': 1, 'min': 1.0, 'median': 1.0, 'max': 1.0,
                                                                           'outside': 0}}}}
        self.frame._show_result(result)
        lines = self.frame.log_lines()
        self.assertIn('FLAG: IMG_0010: a flag', lines)
        self.assertIn('Summary: 1 of 1 capture(s) corrected, 1 file(s) written, 1 flag(s).', lines)
        self.assertTrue(any(line.startswith('Images (1): camera tilt (deg): min 1.0') for line in lines))
        self.assertLess(lines.index('FLAG: IMG_0010: a flag'),
                        lines.index('Summary: 1 of 1 capture(s) corrected, 1 file(s) written, 1 flag(s).'))

    def panel_labels(self):
        return next(field for field in wf.FIELDS if field['key'] == 'PANEL.panels_used')['labels']

    def pick(self, name):
        """ Choose an item of the list of the panel selection approach, as a click does """
        entry = self.frame.entries['PANEL.panels_used']
        entry.set(self.panel_labels()[name])
        entry.event_generate('<<ComboboxSelected>>')
        self.root.update()

    def test_the_panel_selection_approach_is_a_wide_list_with_descriptive_names(self):
        entry = self.frame.entries['PANEL.panels_used']
        labels = self.panel_labels()
        self.assertEqual(set(labels), set(wf.rct.PANELS_USED))
        self.assertEqual(entry.winfo_class(), 'TCombobox')
        self.assertEqual(str(entry.cget('state')), 'readonly')                          # a list, not a box to type in
        self.assertGreaterEqual(int(entry.cget('width')), max(map(len, labels.values())))
        self.assertEqual(entry.get(), 'Before the flight only')
        self.frame.set_value('PANEL.panels_used', 'nearest_time')
        self.assertEqual(entry.get(), 'Before or after, the nearer in time')
        self.assertEqual(self.frame.model.config()[0]['PANEL.panels_used'], 'nearest_time')   # what is saved is the name
        for name in ('nearest_illumination', 'interpolate_illumination'):
            self.assertIn('uses the DLS', labels[name])
        self.assertEqual(self.frame.entries['PANEL.reflectance_source'].winfo_class(), 'TCombobox')

    def test_choosing_a_panel_selection_by_the_light_turns_the_dls_correction_on(self):
        use = self.frame.variables['CORRECTION.use_dls']
        self.assertFalse(use.get())
        self.frame.set_value('PANEL.panels_used', 'mean')
        self.pick('interpolate_time')                                                   # the others change nothing else
        self.assertFalse(use.get())
        self.assertEqual(self.frame.variables['PANEL.panels_used'].get(), 'interpolate_time')
        self.pick('nearest_illumination')
        self.assertEqual(self.frame.variables['PANEL.panels_used'].get(), 'nearest_illumination')   # it stays chosen
        self.assertTrue(use.get())                                                      # and the DLS correction is on
        self.assertTrue(self.frame.model.config()[0]['CORRECTION.use_dls'])
        text = self.frame.status.cget('text')
        self.assertIn("'Before or after, the nearer in light (uses the DLS)' uses the DLS data", text)
        self.assertIn('was turned on', text)
        self.frame.set_value('CORRECTION.use_dls', False)                               # turning it off takes it back
        self.assertEqual(self.frame.model.config()[0]['PANEL.panels_used'], 'first')
        self.pick('interpolate_illumination')
        self.assertTrue(use.get())
        self.assertEqual(self.frame.model.config()[0]['PANEL.panels_used'], 'interpolate_illumination')
        problems = ' '.join(self.frame.problems()[1])
        self.assertNotIn('CORRECTION.use_dls', problems)                                # the DLS correction is on

    def test_a_dropdown_keeps_its_arrow_after_a_choice_is_made(self):
        self.assertIn(str(self.frame.entries['PANEL.panels_used'].cget('style')), ('', 'TCombobox'))   # the default style
        self.pick('mean')
        self.frame.set_value('PANEL.reflectance_source', 'spectral_file')
        for key in ('PANEL.panels_used', 'PANEL.reflectance_source'):
            entry = self.frame.entries[key]
            self.assertEqual(entry.winfo_class(), 'TCombobox', key)
            self.assertEqual(str(entry.cget('style')), 'TCombobox', key)               # not the style of a box to type in
        entry = self.frame.entries['PANEL.panels_used']
        entry.configure(style='Invalid.TCombobox')                                      # the style for a wrong value exists
        self.assertEqual(self.frame.ttk.Style(self.root).lookup('Invalid.TCombobox', 'fieldbackground', ('readonly',)),
                         '#6b3f36')

    def test_turning_the_dls_off_takes_back_a_choice_of_the_panel_by_the_light_and_says_so(self):
        self.frame.set_value('CORRECTION.use_dls', True)
        self.frame.set_value('PANEL.panels_used', 'interpolate_illumination')
        self.frame.set_value('CORRECTION.use_dls', False)
        self.assertEqual(self.frame.model.config()[0]['PANEL.panels_used'], 'first')
        self.assertEqual(self.frame.variables['PANEL.panels_used'].get(), 'first')
        text = self.frame.status.cget('text')
        self.assertIn("was 'Before and after, weighted by light (uses the DLS)' and is now 'Before the flight only'", text)
        self.assertIn('needs the DLS correction', text)
        self.frame.set_value('CORRECTION.use_dls', True)                         # turning it on changes nothing else
        self.assertEqual(self.frame.model.config()[0]['PANEL.panels_used'], 'first')
        self.frame.set_value('PANEL.panels_used', 'mean')
        self.frame.set_value('CORRECTION.use_dls', False)
        self.assertEqual(self.frame.model.config()[0]['PANEL.panels_used'], 'mean')

    def test_a_profile_that_has_the_choice_without_the_dls_is_not_changed_but_reported(self):
        path = Path(self.folder.name) / 'inconsistent.json'
        path.write_text(json.dumps({'PANEL.panels_used': 'nearest_illumination', 'CORRECTION.use_dls': False,
                                    'INPUTS.reference_panel_capture_after': 'IMG_0005'}), encoding='utf-8')
        self.assertTrue(self.frame.load_profile(path))
        self.assertEqual(self.frame.model.config()[0]['PANEL.panels_used'], 'nearest_illumination')
        _, problems = self.frame.problems()
        self.assertTrue(any('CORRECTION.use_dls is off' in problem for problem in problems))

    def test_the_use_of_the_panels_is_chosen_next_to_the_panel_captures(self):
        for key in ('INPUTS.reference_panel_capture', 'PANEL.panels_used', 'PANEL.panel_orientation'):
            self.assertTrue(self.inside(self.frame.entries[key], 'INPUTS'), key)
        self.assertNotIn('INPUTS.reference_panel_capture_after', self.frame.entries)    # one field for all the captures
        self.assertEqual(self.frame.entries['PANEL.panels_used'].winfo_class(), 'TCombobox')
        labels = self.panel_labels()
        self.assertEqual(list(self.frame.entries['PANEL.panels_used'].cget('values')),
                         [labels[name] for name in wf.rct.PANELS_USED])                  # all of them, the DLS on or off
        self.frame.set_value('PANEL.panels_used', 'mean')
        _, problems = self.frame.problems()
        self.assertEqual(problems, [])                       # the moments are found from the times when it runs

    def test_the_panel_captures_after_the_flight_of_an_earlier_configuration_join_the_panel_captures(self):
        path = Path(self.folder.name) / 'old.json'
        path.write_text(json.dumps({'INPUTS.reference_panel_capture': 'IMG_0005',
                                    'INPUTS.reference_panel_capture_after': 'IMG_0422'}), encoding='utf-8')
        self.assertTrue(self.frame.load_profile(path))
        config = self.frame.model.config()[0]
        self.assertEqual((config['INPUTS.reference_panel_capture'], config['INPUTS.reference_panel_capture_after']),
                         ('IMG_0005, IMG_0422', ''))
        self.assertTrue(any('are now in Reference panel captures' in line for line in self.frame.log_lines()))

    def test_the_orientations_offered_are_those_of_the_panel_captures(self):
        entry = self.frame.entries['PANEL.panel_orientation']
        groups = wf.orientation_groups({'IMG_0005': 87.0, 'IMG_0422': 30.0})
        self.frame._show_orientations(groups, None)
        values = list(entry.cget('values'))
        self.assertEqual(values[0], 'All the panel captures (heading 30: 1; heading 87: 1)')
        self.assertEqual(values[1:], ['Only heading 30 deg (facing the sun), 1 capture',
                                      'Only heading 87 deg (with the sun at the side), 1 capture'])
        self.frame.orientation_shown.set(values[1])
        self.assertEqual(self.frame.model.config()[0]['PANEL.panel_orientation'], '30')
        self.assertIn('2 orientations', self.frame.orientation_info.cget('text'))
        self.frame._show_orientations(wf.orientation_groups({'IMG_0005': 87.0}), None)   # 30 is no longer there: kept, said
        self.assertIn('Heading 30 deg (not among these panel captures)', list(entry.cget('values')))
        self.frame.set_value('PANEL.panel_orientation', 'all')
        self.frame._show_orientations(wf.orientation_groups({'IMG_0005': 87.0}), None)   # one orientation: nothing to choose
        self.assertIn('disabled', entry.state())
        self.assertIn('one orientation', self.frame.orientation_info.cget('text'))

    def test_the_panel_captures_are_checked_in_a_table_and_can_be_left_out(self):
        rows = [{'capture': 'IMG_0005', 'time': '2023:02:02 13:26:38', 'moment': 'before', 'heading': 87.0,
                 'orientation': 'with the sun at the side', 'used': True, 'departure': -0.05, 'spread': None,
                 'band': 'Red', 'alone': False, 'flag': 'departs more than 2 % from the others'},
                {'capture': 'IMG_0422', 'time': '2023:02:02 13:48:36', 'moment': 'after', 'heading': 83.0,
                 'orientation': 'with the sun at the side', 'used': True, 'departure': None, 'spread': None, 'band': None,
                 'alone': True, 'flag': ''}]
        self.frame.set_value('INPUTS.reference_panel_capture', 'IMG_0005, IMG_0422')
        self.frame._show_panel_check(rows, [])
        window = self.frame.panel_check_window
        tree = [child for child in window.winfo_children()[0].winfo_children() if child.winfo_class() == 'Treeview'][0]
        self.assertEqual(tree.get_children(), ('IMG_0005', 'IMG_0422'))
        self.assertEqual(tree.item('IMG_0005')['values'][4], '-5.0 % (Red)')
        self.assertEqual(tree.item('IMG_0005')['tags'], ['flagged'])
        texts = [child.cget('text') for child in window.winfo_children()[0].winfo_children()
                 if child.winfo_class() == 'TLabel']
        self.assertTrue(any('IMG_0422 is the only panel capture after the flight' in text for text in texts), texts)
        self.frame.panel_check_limit.set('10')                                  # the limit is changed in the table
        self.assertEqual(tree.item('IMG_0005')['tags'], '')
        self.assertEqual(tree.set('IMG_0005', 'flag'), '')
        self.assertEqual(self.frame.model.config()[0]['CHECKS.panel_capture_spread_tolerance'], 0.1)   # and in the run
        self.frame.panel_check_limit.set('1')
        self.assertEqual(tree.set('IMG_0005', 'flag'), 'departs more than 1 % from the others')
        self.frame.panel_check_limit.set('x')                                   # what is not a number changes nothing
        self.assertEqual(self.frame.model.config()[0]['CHECKS.panel_capture_spread_tolerance'], 0.01)
        self.frame.leave_out_panel_captures(['IMG_0005'])
        config = self.frame.model.config()[0]
        self.assertEqual(config['INPUTS.reference_panel_capture'], 'IMG_0422')
        self.assertIn('IMG_0005', config['INPUTS.excluded_captures'])                  # so it is not corrected either
        window.destroy()
        with mock.patch.object(rw.wf, 'check_panel_captures', return_value=(rows, [])) as check:
            self.frame.check_panel_captures()
            for _ in range(50):
                self.root.update()
                if check.called and getattr(self.frame, 'panel_check_window', window) is not window:
                    break
                time.sleep(0.05)
        self.assertTrue(check.called)

    def test_each_output_enables_only_the_fields_it_uses(self):
        def enabled(key):
            return all('disabled' not in widget.state() for widget in self.frame.field_widgets[key])
        use = self.frame.variables['CORRECTION.use_dls']
        use.set(False)
        self.frame.set_value('INPUTS.output_product', 'dls_reflectance')
        self.assertTrue(use.get())                                         # the DLS alone always uses the DLS
        self.assertFalse(enabled('CORRECTION.use_dls'))
        self.assertFalse(enabled('PANEL.panels_used'))
        self.assertTrue(enabled('INPUTS.reference_panel_capture'))        # only compared with the DLS, but measured
        self.frame.set_value('INPUTS.output_product', 'radiance')
        self.assertFalse(use.get())
        for key in ('INPUTS.reference_panel_capture', 'PANEL.percent_inner_area', 'CHECKS.band_ratio_tolerance'):
            self.assertFalse(enabled(key), key)
        self.assertTrue(enabled('CHECKS.max_camera_tilt_deg'))           # the geometry of the images is still checked
        self.frame.set_value('INPUTS.output_product', 'panel_reflectance')
        self.assertFalse(use.get())                                        # the choice made before is given back
        for key in ('INPUTS.reference_panel_capture', 'PANEL.panels_used', 'CORRECTION.use_dls'):
            self.assertTrue(enabled(key), key)
        self.frame.set_value('INPUTS.reference_panel_capture_after', 'IMG_0005')
        self.assertEqual(self.frame.problems()[1], [])

    def test_the_frame_can_be_placed_inside_another_window(self):
        import tkinter as tk
        from tkinter import ttk
        host = tk.Toplevel(self.root)
        notebook = ttk.Notebook(host)
        notebook.pack(fill='both', expand=True)
        frame = rw.RadiometricFrame(notebook, Path(self.folder.name) / 'embedded.json', ROOT)
        notebook.add(frame.widget, text='Radiometric')
        host.update()
        self.assertEqual(frame.widget.winfo_parent(), str(notebook))
        host.destroy()


class CaptureChoiceTests(unittest.TestCase):
    setUp = WindowTests.setUp
    tearDown = WindowTests.tearDown

    def test_panel_captures_are_typed_or_chosen_by_looking_at_the_captures(self):
        entry = self.frame.entries['INPUTS.reference_panel_capture']
        self.assertEqual(entry.winfo_class(), 'TEntry')
        self.assertEqual(self.frame.capture_names(), ['IMG_0003', 'IMG_0005', 'IMG_0009', 'IMG_0010', 'IMG_0422', 'IMG_0423'])
        self.assertEqual(self.frame.capture_names(True), [''] + ['IMG_0003', 'IMG_0005', 'IMG_0009', 'IMG_0010', 'IMG_0422', 'IMG_0423'])
        with mock.patch.object(rw, 'pick_captures', return_value=['IMG_0010']) as picker:
            self.frame.choose_panel_capture('INPUTS.reference_panel_capture')
        args, kwargs = picker.call_args
        self.assertEqual(args[1], ['IMG_0003', 'IMG_0005', 'IMG_0009', 'IMG_0010', 'IMG_0422', 'IMG_0423'])
        self.assertEqual(kwargs['selected'], ['IMG_0005'])
        self.assertTrue(kwargs['multiple'])                                      # one capture or several, averaged
        self.assertEqual(self.frame.model.get('INPUTS.reference_panel_capture'), 'IMG_0010')
        self.assertEqual(len(kwargs['paths_of']('IMG_0005')), 6)                  # what the window needs to show the pictures
        self.assertEqual(kwargs['bands'], ['Blue', 'Green', 'Panchro', 'Red', 'Red edge', 'NIR'])
        self.assertIn(kwargs['resolve_band']('Red'), range(6))                    # read from the metadata of a capture
        with mock.patch.object(rw, 'pick_captures', return_value=['IMG_0005', 'IMG_0010']):
            self.frame.choose_panel_capture('INPUTS.reference_panel_capture')
        self.assertEqual(self.frame.model.get('INPUTS.reference_panel_capture'), 'IMG_0005, IMG_0010')
        self.frame.set_value('INPUTS.reference_panel_capture', 'IMG_0010')
        self.frame.set_value('INPUTS.reference_panel_capture_after', 'IMG_0010')
        with mock.patch.object(rw, 'pick_captures', return_value=[]) as picker:
            self.frame.choose_panel_capture('INPUTS.reference_panel_capture_after')
        self.assertIn('before and after the flight', picker.call_args.kwargs['title'])
        self.assertEqual(self.frame.model.get('INPUTS.reference_panel_capture_after'), '')
        with mock.patch.object(rw, 'pick_captures', return_value=None):
            self.frame.choose_panel_capture('INPUTS.reference_panel_capture')
        self.assertEqual(self.frame.model.get('INPUTS.reference_panel_capture'), 'IMG_0010')    # cancelled: unchanged
        self.frame.set_value('INPUTS.images_folder', 'does/not/exist')
        self.assertEqual(self.frame.capture_names(), [])

    def test_captures_to_correct_are_chosen_from_a_list(self):
        with mock.patch.object(rw, 'pick_captures', return_value=['IMG_0010']) as picker:
            self.frame.set_value('INPUTS.reference_panel_capture', 'IMG_0003, IMG_0005, IMG_0009, IMG_0423')
            self.frame.choose_captures()
        self.assertEqual(picker.call_args[0][1], ['IMG_0010', 'IMG_0422'])             # the panel captures are not offered
        self.assertEqual(self.frame.model.get('INPUTS.image_captures'), 'IMG_0010')      # shown in the field, even if it is all
        self.frame.set_value('INPUTS.reference_panel_capture', 'IMG_0001')
        with mock.patch.object(rw, 'pick_captures', return_value=['IMG_0010']):
            self.frame.choose_captures()
        self.assertEqual(self.frame.model.get('INPUTS.image_captures'), 'IMG_0010')
        with mock.patch.object(rw, 'pick_captures', return_value=None):
            self.frame.choose_captures()
        self.assertEqual(self.frame.model.get('INPUTS.image_captures'), 'IMG_0010')
        self.frame._update_captures_summary()
        title, text = self.frame.captures_details()
        self.assertEqual(title, 'Captures to correct')
        self.assertIn('1 capture(s) will be corrected:', text)
        self.assertIn('- IMG_0010', text)
        self.assertEqual(self.frame.captures_summary.cget('text'), '')                 # nothing is wrong
        self.frame.set_value('INPUTS.image_captures', 'IMG_0010, IMG_7')
        self.frame._update_captures_summary()
        self.assertIn('Not found in the images folder: IMG_7', self.frame.captures_summary.cget('text'))
        self.assertTrue(self.frame.captures_summary.winfo_manager())
        self.frame.set_value('INPUTS.image_captures', '')
        self.frame.set_value('INPUTS.reference_panel_capture', 'IMG_0003, IMG_0005, IMG_0009, IMG_0423')
        self.frame._update_captures_summary()
        text = self.frame.captures_details()[1]
        self.assertIn('All the other captures of the folder will be corrected (2):', text)
        self.assertIn('- IMG_0010', text)
        self.assertIn('- IMG_0422', text)
        self.assertFalse(self.frame.captures_summary.winfo_manager())

    def test_captures_to_leave_out_are_chosen_from_a_list_and_not_corrected(self):
        self.frame.set_value('INPUTS.reference_panel_capture', 'IMG_0003, IMG_0005, IMG_0009, IMG_0423')
        self.frame.set_value('INPUTS.image_captures', '')
        with mock.patch.object(rw, 'pick_captures', return_value=['IMG_0422']) as picker:
            self.frame.choose_captures('INPUTS.excluded_captures')
        self.assertEqual(picker.call_args[0][1], ['IMG_0010', 'IMG_0422'])             # the panel captures are not offered
        self.assertEqual(picker.call_args.kwargs['title'], 'Select the captures to leave out')
        self.assertEqual(self.frame.model.get('INPUTS.excluded_captures'), 'IMG_0422')
        with mock.patch.object(rw, 'pick_captures', return_value=['IMG_0010']) as picker:
            self.frame.choose_captures()
        self.assertEqual(picker.call_args[0][1], ['IMG_0010'])                         # nor the one left out
        self.frame.set_value('INPUTS.image_captures', '')
        self.frame._update_captures_summary()
        self.assertIn('All the other captures of the folder will be corrected (1):', self.frame.captures_details()[1])
        self.frame.set_value('INPUTS.image_captures', 'IMG_0422')
        self.frame._update_captures_summary()
        self.assertIn('Both selected and left out: IMG_0422', self.frame.captures_summary.cget('text'))

    def test_the_pattern_details_and_the_suggestion(self):
        self.frame._update_pattern_preview()
        title, text = self.frame.pattern_details()
        self.assertEqual(title, 'In the images folder')
        self.assertIn('{capture}_{index}.tif', text)
        self.assertIn('- IMG_0005_1.tif -> capture IMG_0005, file 1', text)
        self.assertIn('6 captures (6 with 6 files)', text)
        self.assertEqual(self.frame.pattern_preview.cget('text'), '')
        self.frame.set_value('INPUTS.file_pattern', r'(?P<capture>X.+)\.tif$')
        self.frame._update_pattern_preview()
        self.assertIn('No file', self.frame.pattern_preview.cget('text'))
        self.assertTrue(self.frame.pattern_preview.winfo_manager())
        self.frame.suggest_pattern()
        self.assertEqual(self.frame.model.get('INPUTS.file_pattern'), '{capture}_{index}.tif')
        self.frame._update_pattern_preview()
        self.assertFalse(self.frame.pattern_preview.winfo_manager())
        self.frame.set_value('INPUTS.file_pattern', '(unclosed')
        self.frame._update_pattern_preview()
        self.assertIn('not a valid regular expression', self.frame.pattern_preview.cget('text'))
        self.assertIsNone(self.frame.pattern_details())

    def test_the_source_of_the_panel_reflectance_is_a_dropdown_of_descriptions(self):
        box = self.frame.entries['PANEL.reflectance_source']
        self.assertEqual(box.winfo_class(), 'TCombobox')
        self.assertEqual(list(box.cget('values')), ['Values typed in the table of the bands', 'Spectral response file (.csv)'])
        self.assertEqual(box.get(), 'Values typed in the table of the bands')
        box.set('Spectral response file (.csv)')                                    # the stored name is what the program uses
        self.assertEqual(self.frame.model.get('PANEL.reflectance_source'), 'spectral_file')
        self.frame.set_value('PANEL.reflectance_source', 'values')
        self.assertEqual(box.get(), 'Values typed in the table of the bands')
        self.frame.variables['PANEL.reflectance_source'].set('spectral_file')
        self.assertEqual(box.get(), 'Spectral response file (.csv)')


class HelpColumnTests(unittest.TestCase):
    setUp = WindowTests.setUp
    tearDown = WindowTests.tearDown

    def test_the_page_is_explained_when_no_field_is_pointed_at(self):
        panel = self.frame.help_panel
        self.assertEqual(panel.shown[0], 'Inputs')
        self.assertTrue(panel.plain_text().startswith('Inputs'))
        self.frame.show_page('CORRECTION')
        self.assertEqual(panel.shown[0], 'DLS (Downwelling Light Sensor)')
        self.assertIn('E_panel / E_image', panel.plain_text())

    def test_the_text_is_formatted_in_paragraphs_lists_and_sections(self):
        panel = self.frame.help_panel
        panel.point_at(self.frame.entries['CHECKS.jump_threshold'])
        text = panel.plain_text()
        self.assertIn('\u2022\tA spike', text)                                  # a list item
        self.assertIn('Irradiance jump threshold', text)
        self.assertEqual(len(panel.rules), 0)
        tags = set(panel.text.tag_names('2.0'))
        self.assertIn('title', panel.text.tag_names('1.0'))
        self.assertIn('body_p', tags)
        self.assertGreater(int(float(panel.text.tag_cget('body_p', 'lmargin2'))),
                           int(float(panel.text.tag_cget('body_p', 'lmargin1'))))       # the lines after the first hang
        self.assertGreater(int(float(panel.text.tag_cget('body_p', 'spacing3'))), 8)      # space after a paragraph

    def test_the_pointer_shows_the_field_and_leaving_goes_back_to_the_page(self):
        panel = self.frame.help_panel
        entry = self.frame.entries['INPUTS.file_pattern']
        panel.point_at(entry)
        self.assertEqual(panel.shown[0], 'File name pattern')
        self.assertIn('{capture}', panel.plain_text())
        self.assertIn('The pattern reads names written as', panel.plain_text())      # the live information
        panel.point_at(self.frame.content)                      # over nothing that is explained
        self.root.update()
        self.assertEqual(panel.shown[0], 'File name pattern')                         # not at once: no flicker between fields
        end = time.time() + 2
        while panel.shown[0] != 'Inputs' and time.time() < end:
            self.root.update()
            time.sleep(0.02)
        self.assertEqual(panel.shown[0], 'Inputs')

    def test_the_buttons_at_the_bottom_are_explained_under_the_pointer(self):
        panel = self.frame.help_panel
        self.root.update()
        for key, title in (('load_profile', 'Load profile...'), ('save_profile', 'Save profile...'),
                           ('save', 'Update saved configuration')):
            self.frame.buttons[key].event_generate('<Motion>', x=3, y=3)     # as the mouse does, through the window
            self.root.update()
            self.assertEqual(panel.shown[0], title)

    def test_the_explanation_stays_while_the_pointer_is_over_the_column(self):
        panel = self.frame.help_panel
        panel.point_at(self.frame.entries['INPUTS.images_folder'])
        panel.point_at(self.frame.content)                      # leaving the field starts the return to the page ...
        panel.point_at(panel.text)                              # ... but the pointer went to read the explanation
        end = time.time() + 0.4
        while time.time() < end:
            self.root.update()
            time.sleep(0.02)
        self.assertEqual(panel.shown[0], 'Images folder')

    def test_the_keyboard_focus_shows_the_field_and_the_pointer_wins(self):
        panel = self.frame.help_panel
        panel.focus_on(self.frame.entries['INPUTS.images_folder'])
        self.assertEqual(panel.shown[0], 'Images folder')
        panel.point_at(self.frame.entries['INPUTS.results_folder'])
        self.assertEqual(panel.shown[0], 'Results folder')
        panel.point_at(None)
        panel._forget_hover()
        self.assertEqual(panel.shown[0], 'Images folder')                             # back to the field with the focus
        panel.focus_on(panel.text)                                                    # clicking the column changes nothing
        self.assertEqual(panel.shown[0], 'Images folder')

    def test_a_real_focus_event_is_followed(self):
        self.root.attributes('-alpha', 0.0)
        self.root.deiconify()
        self.root.update()
        entry = self.frame.entries['INPUTS.results_folder']
        entry.focus_force()
        self.root.update()
        self.assertEqual(self.frame.help_panel.shown[0], 'Results folder')

    def test_the_rows_of_the_band_table_and_the_buttons_are_explained(self):
        panel = self.frame.help_panel
        row = self.frame.band_table.rows[0]['widgets']['name']
        self.assertEqual(panel.explanation_of(row)[0], 'Bands of the camera')
        self.assertEqual(panel.explanation_of(self.frame.buttons['regions'])[0], 'Check inputs, Check / draw panel regions')
        self.assertEqual(panel.explanation_of(self.frame.buttons['run'])[0], 'Check inputs, Check environment, Run, Open results folder')
        self.assertEqual(panel.explanation_of(self.frame.preset_box)[0], 'Camera preset')

    def test_the_note_of_a_part_is_not_repeated_under_each_of_its_fields(self):
        panel = self.frame.help_panel
        for key in ('CHECKS.jump_threshold', 'CHECKS.band_ratio_tolerance', 'PANEL.panels_used'):
            panel.point_at(self.frame.entries[key])
            self.assertEqual(panel.shown[2], '', key)
            self.assertEqual(len(panel.rules), 0, key)
        self.frame.show_page('CORRECTION')                                  # the page and the headings carry the notes
        self.assertEqual(panel.shown[2], 'Checks of the DLS data (limits)')
        self.assertEqual(len(panel.rules), 1)

    def test_exiftool_is_looked_for_and_reported(self):
        found = {'path': r'C:\tools\exiftool.exe', 'source': 'the system PATH', 'package': True, 'version': None}
        with mock.patch.object(wf, 'find_exiftool', return_value=(found['path'], found['source'])), \
                mock.patch.object(wf, 'pyexiftool_installed', return_value=True):
            self.frame._check_exiftool()
        title, text = self.frame.exiftool_details()
        self.assertEqual(title, 'ExifTool on this computer')
        self.assertIn('- Program: C:\\tools\\exiftool.exe', text)
        self.assertIn('- Version: checking...', text)
        self.assertEqual(self.frame.exiftool_summary.cget('text'), '')
        self.frame._handle('exiftool', (found['path'], '12.70'))
        self.assertIn('- Version: 12.70', self.frame.exiftool_details()[1])
        self.frame._handle('exiftool', ('C:\\other\\exiftool.exe', '1.0'))               # an old answer is ignored
        self.assertIn('- Version: 12.70', self.frame.exiftool_details()[1])
        self.frame.parent.after_cancel(self.frame.exiftool_after)

    def test_a_missing_exiftool_or_package_is_a_problem_under_the_field(self):
        with mock.patch.object(wf, 'find_exiftool', return_value=(None, 'the system PATH')), \
                mock.patch.object(wf, 'pyexiftool_installed', return_value=False):
            self.frame._check_exiftool()
        text = self.frame.exiftool_summary.cget('text')
        self.assertIn('ExifTool was not found in the system PATH', text)
        self.assertIn('pyexiftool is not installed', text)
        self.assertTrue(self.frame.exiftool_summary.winfo_manager())
        self.assertIn('was not found', self.frame.exiftool_details()[1])
        self.assertIn('pyexiftool: not installed', self.frame.exiftool_details()[1])

    def test_the_column_can_be_resized_and_keeps_the_text_inside(self):
        self.root.attributes('-alpha', 0.0)
        self.root.deiconify()
        self.root.geometry('1400x800')
        self.root.update()
        panes = self.frame.panes
        panel = self.frame.help_panel
        wide = panel.widget.winfo_width()
        total = panes.winfo_width()
        panes.sash_place(0, total - 560, 0)                         # drag the line to the left: a wider column
        self.root.update()
        self.assertGreater(panel.widget.winfo_width(), wide + 100)
        self.assertLessEqual(panel.text.winfo_width(), panel.widget.winfo_width())
        self.root.geometry('1700x800')
        self.root.update()
        self.assertAlmostEqual(panel.widget.winfo_width(), panes.winfo_width() - panes.sash_coord(0)[0] - 6, delta=8)

    def test_the_tiles_are_smaller_than_the_column_and_choose_the_page(self):
        self.root.attributes('-alpha', 0.0)
        self.root.deiconify()
        self.root.geometry('1400x860')
        self.root.update()
        tiles = self.frame.navigation
        self.assertLess(tiles.widget.winfo_width(), 150)
        heights = {tile.winfo_height() for tile in tiles.tiles}
        self.assertEqual(len(heights), 1)
        full = tiles.widget.winfo_height() / tiles.size()
        self.assertLess(heights.pop(), full * 0.75)
        self.assertEqual(tiles.curselection(), (0,))
        tiles.tiles[2].event_generate('<Button-1>')
        self.root.update()
        self.assertEqual(tiles.curselection(), (2,))
        self.assertTrue(self.frame.pages['CORRECTION'].winfo_manager())
        self.assertFalse(self.frame.pages['INPUTS'].winfo_manager())
        tiles.tiles[2].focus_force()
        self.root.update()
        tiles.tiles[2].event_generate('<Down>')
        self.root.update()
        self.assertEqual(tiles.curselection(), (3,))
        self.assertEqual(self.frame.help_panel.shown[0], 'Run and results')


if __name__ == '__main__':
    unittest.main()
