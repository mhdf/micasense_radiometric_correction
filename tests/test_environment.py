"""
Tests for environment.py (the check of the computer) and for __main__.py (the check before the window opens)

Run from the project folder with:  python -m unittest tests.test_environment -v
"""

import contextlib
import importlib.machinery
import io
import json
import platform
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from radiometric_app import __main__ as launch
from radiometric_app import environment as env


def item(name, status='ok', level='required', fix=None, detail='', version='1.0'):
    return {'name': name, 'status': status, 'version': version, 'detail': detail, 'fix': fix, 'level': level}


class PythonAndPackagesTests(unittest.TestCase):

    def test_this_python_is_accepted_and_an_old_one_is_not(self):
        self.assertEqual(env.python_item()['status'], 'ok')
        with mock.patch.object(env.sys, 'version_info', (3, 9, 0, 'final', 0)):
            old = env.python_item()
        self.assertEqual(old['status'], 'old')
        self.assertIn('3.10 or newer', old['detail'])
        self.assertIsNotNone(old['fix'])

    def test_tkinter_is_reported_with_its_version(self):
        found = env.tkinter_item()
        self.assertIn(found['status'], ('ok', 'missing'))
        with mock.patch.dict(sys.modules, {'tkinter': None}):                      # the import fails
            self.assertEqual(env.tkinter_item()['status'], 'missing')
            self.assertIn('Tcl/Tk', env.tkinter_item()['fix'])

    def test_a_missing_package_says_how_to_install_it(self):
        found = env.package_item('no_such_module_xyz', ('no-such-package',), 'no-such-package', 'testing')
        self.assertEqual((found['name'], found['status'], found['version']), ('no-such-package', 'missing', None))
        self.assertEqual(found['fix'], 'pip install no-such-package')

    def test_an_installed_package_has_its_version(self):
        found = env.package_item('numpy', ('numpy',), 'numpy', 'the images')
        self.assertEqual(found['status'], 'ok')
        self.assertRegex(found['version'], r'^\d+\.\d+')
        self.assertEqual(env.package_item('numpy', ('numpy',), 'numpy', 'the images', deep=True)['status'], 'ok')

    def test_the_version_comes_from_the_module_when_the_installation_has_no_record_of_it(self):
        found = env.package_item('json', ('no-record-of-json',), 'json', 'testing', deep=True)       # json.__version__
        self.assertEqual((found['status'], found['version']), ('ok', json.__version__))

    def test_a_package_that_is_installed_but_cannot_be_loaded_is_broken(self):
        with mock.patch.object(env.importlib, 'import_module', side_effect=OSError('the DLL was not found')):
            found = env.package_item('numpy', ('numpy',), 'numpy', 'the images', deep=True)
        self.assertEqual(found['status'], 'broken')
        self.assertIn('OSError: the DLL was not found', found['detail'])
        self.assertIn('force-reinstall numpy', found['fix'])

    def test_pyzbar_that_cannot_load_zbar_says_what_to_install(self):
        def import_module(name):
            if name == 'pyzbar.pyzbar':
                raise ImportError('Unable to find zbar shared library')
            return types.ModuleType(name)
        with mock.patch.object(env.importlib, 'import_module', side_effect=import_module):
            found = env.package_item('pyzbar', ('pyzbar',), 'pyzbar', 'the QR code', deep=True)
        self.assertEqual(found['status'], 'broken')
        self.assertIn('zbar', found['fix'])

    def test_pyzbar_without_the_visual_cpp_runtime_names_the_package_to_install_on_windows(self):
        def import_module(name):
            if name == 'pyzbar.pyzbar':
                raise FileNotFoundError("Could not find module 'site-packages\\pyzbar\\libzbar-64.dll' (or one of its "
                                        "dependencies). Try using the full path with constructor syntax.")
            return types.ModuleType(name)
        with mock.patch.object(env.importlib, 'import_module', side_effect=import_module):
            found = env.package_item('pyzbar', ('pyzbar',), 'pyzbar', 'the QR code', deep=True)
        self.assertEqual(found['status'], 'broken')
        self.assertIn('libzbar-64.dll', found['detail'])
        self.assertIn('winget install Microsoft.VCRedist.2013.x64', found['fix'])
        self.assertIn('vcredist_x64.exe', found['fix'])

    def test_the_exiftool_module_of_another_package_is_recognized(self):
        other = types.ModuleType('exiftool')
        other.__spec__ = importlib.machinery.ModuleSpec('exiftool', None)
        with mock.patch.dict(sys.modules, {'exiftool': other}):
            found = env.package_item('exiftool', ('PyExifTool', 'pyexiftool'), 'PyExifTool', 'the metadata')
        self.assertEqual(found['status'], 'broken')
        self.assertIn('pip uninstall exiftool', found['fix'])

    def test_the_packages_are_the_ones_the_program_imports(self):
        modules = {module for module, _, _, _ in env.PACKAGES}
        self.assertEqual(modules, {'numpy', 'cv2', 'PIL', 'tifffile', 'matplotlib', 'skimage', 'shapely', 'pyzbar', 'exiftool'})
        root = Path(__file__).resolve().parent.parent
        listed = {line.split('>')[0].split('=')[0].strip().lower() for line in (root / 'requirements.txt').read_text(
            encoding='utf-8').splitlines() if line.strip() and not line.startswith('#')}
        self.assertEqual(listed, {install.lower() for _, _, install, _ in env.PACKAGES})
        conda = (root / 'environment.yml').read_text(encoding='utf-8')
        for name in ('numpy', 'opencv', 'pillow', 'tifffile', 'matplotlib', 'scikit-image', 'shapely', 'pyzbar', 'PyExifTool', 'tk'):
            self.assertIn(name, conda)


class ExifToolTests(unittest.TestCase):

    def test_the_configured_file_comes_first_and_relative_paths_start_at_the_root(self):
        with tempfile.TemporaryDirectory() as folder:
            program = Path(folder) / 'tools' / 'exiftool.exe'
            program.parent.mkdir()
            program.write_text('x')
            self.assertEqual(env.locate_exiftool(str(program)), (str(program), 'INPUTS.exiftool_path'))
            self.assertEqual(env.locate_exiftool('tools/exiftool.exe', folder), (str(Path(folder) / 'tools/exiftool.exe'),
                                                                                'INPUTS.exiftool_path'))
            self.assertEqual(env.locate_exiftool(str(program) + '.no'), (None, 'INPUTS.exiftool_path'))

    def test_then_the_environment_variable_and_then_the_path(self):
        with mock.patch.dict(env.os.environ, {'exiftoolpath': ''}), mock.patch.object(env.shutil, 'which', return_value=None):
            self.assertEqual(env.locate_exiftool(''), (None, 'the system PATH'))
        with mock.patch.dict(env.os.environ, {'exiftoolpath': 'exiftool'}), \
                mock.patch.object(env.shutil, 'which', return_value='C:/x/exiftool'):
            self.assertEqual(env.locate_exiftool(''), ('C:/x/exiftool', 'the environment variable exiftoolpath'))

    def test_the_item_for_the_program(self):
        with mock.patch.object(env, 'locate_exiftool', return_value=(None, 'the system PATH')):
            missing = env.exiftool_item()
        self.assertEqual((missing['status'], missing['level']), ('missing', 'needed'))
        self.assertIn('https://exiftool.org', missing['fix'])
        with mock.patch.object(env, 'locate_exiftool', return_value=('C:/x/exiftool.exe', 'the system PATH')):
            with mock.patch.object(env, 'exiftool_version', return_value='12.70'):
                found = env.exiftool_item()
            self.assertEqual((found['status'], found['version']), ('ok', '12.70'))
            with mock.patch.object(env, 'exiftool_version', return_value=None):
                self.assertEqual(env.exiftool_item()['status'], 'broken')
            with mock.patch.object(env, 'exiftool_version') as asked:
                self.assertEqual(env.exiftool_item(version=False)['status'], 'ok')       # the program is not run
                asked.assert_not_called()

    def test_the_configured_path_is_read_from_the_configuration_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertEqual(env.configured_exiftool_path(root), '')
            (root / env.CONFIG_FOLDER).mkdir()
            (root / env.CONFIG_FOLDER / env.DEFAULT_CONFIG_NAME).write_text(
                json.dumps({'INPUTS.exiftool_path': 'C:/tools/exiftool.exe'}), encoding='utf-8')
            self.assertEqual(env.configured_exiftool_path(root), 'C:/tools/exiftool.exe')
            other = root / 'other.json'
            other.write_text(json.dumps({'INPUTS.exiftool_path': 'D:/e.exe'}), encoding='utf-8')
            self.assertEqual(env.configured_exiftool_path(root, other), 'D:/e.exe')
            other.write_text('{not json', encoding='utf-8')
            self.assertEqual(env.configured_exiftool_path(root, other), '')

    def test_the_configuration_of_earlier_versions_is_read_too(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / env.DEFAULT_CONFIG_NAME).write_text(json.dumps({'INPUTS.exiftool_path': 'E:/x.exe'}), encoding='utf-8')
            self.assertEqual(env.configured_exiftool_path(folder), 'E:/x.exe')


class ReportTests(unittest.TestCase):

    def setUp(self):
        self.items = [item('Python'), item('numpy'),
                      item('pyzbar', 'missing', fix='pip install pyzbar', detail='the QR code', version=None),
                      item('shapely', 'missing', fix='pip install shapely', version=None),
                      item('ExifTool', 'missing', 'needed', fix='Install ExifTool (https://exiftool.org).', version=None)]

    def test_the_levels(self):
        self.assertEqual([found['name'] for found in env.problems(self.items)], ['pyzbar', 'shapely', 'ExifTool'])
        self.assertEqual([found['name'] for found in env.problems(self.items, 'required')], ['pyzbar', 'shapely'])
        self.assertEqual([found['name'] for found in env.problems(self.items, 'needed')], ['ExifTool'])
        self.assertEqual(env.problems(self.items[:2]), [])

    def test_one_line_for_each_thing(self):
        lines = env.report_lines(self.items)
        self.assertEqual(len(lines), 5)
        self.assertTrue(lines[0].startswith('ok       Python'))
        self.assertTrue(lines[2].startswith('MISSING  pyzbar'))
        self.assertTrue(all(line == line.rstrip() for line in lines))

    def test_the_missing_packages_are_in_one_install_command(self):
        text = env.install_text(self.items)
        self.assertIn('-m pip install pyzbar shapely', text)
        self.assertIn('requirements.txt', text)
        self.assertIn('environment.yml', text)
        self.assertIn('ExifTool: Install ExifTool', text)
        self.assertNotIn('pyzbar: pip install', text)                          # not repeated one by one
        self.assertEqual(env.install_text(self.items[:2]), '')

    def test_the_pip_command_uses_the_python_with_a_console(self):
        with mock.patch.object(env.sys, 'executable', r'C:\Program Files\Python310\pythonw.exe'):
            text = env.install_text(self.items)
        self.assertIn(r'"C:\Program Files\Python310\python.exe" -m pip install pyzbar shapely', text)

    def test_the_pip_command_can_be_for_another_python(self):
        text = env.install_text(self.items, r'D:\envs\other\pythonw.exe')
        self.assertIn(r'D:\envs\other\python.exe -m pip install pyzbar shapely', text)

    def test_the_message_names_the_problems_and_the_python(self):
        text = env.message_text(self.items)
        self.assertIn('pyzbar: missing (the QR code)', text)
        self.assertIn('ExifTool: missing', text)
        self.assertIn(sys.executable, text)
        self.assertEqual(env.message_text(self.items[:2]), '')


class MessageTests(unittest.TestCase):

    def test_a_question_returns_the_answer_of_the_box(self):
        with mock.patch.object(env, '_tk_message', return_value=True) as shown:
            self.assertTrue(env.show_message('t', 'text', question=True))
        shown.assert_called_once_with('t', 'text', True)

    def test_without_tkinter_windows_uses_its_own_box_and_without_both_the_text_goes_to_the_error_output(self):
        with mock.patch.object(env, '_tk_message', side_effect=RuntimeError), \
                mock.patch.object(env.os, 'name', 'nt'), \
                mock.patch.object(env, '_windows_message', return_value=True) as windows:
            self.assertTrue(env.show_message('t', 'text', question=True))
        windows.assert_called_once()
        errors = io.StringIO()
        with mock.patch.object(env, '_tk_message', side_effect=RuntimeError), \
                mock.patch.object(env, '_windows_message', side_effect=RuntimeError), \
                contextlib.redirect_stderr(errors):
            self.assertFalse(env.show_message('title', 'the text', question=True))
        self.assertEqual(errors.getvalue(), 'title\nthe text\n')


class PythonEnvironmentFileTests(unittest.TestCase):

    def test_the_python_is_saved_in_the_config_folder_and_read_back(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(env.read_python_environment(folder), {})
            path = env.save_python_environment(folder, r'C:\envs\py310\python.exe')
            self.assertEqual(path, Path(folder) / 'config' / 'python_environment.json')
            self.assertEqual(env.read_python_environment(folder), {'python': r'C:\envs\py310\python.exe'})
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'python': r'C:\envs\py310\python.exe'})

    def test_the_file_written_by_powershell_has_a_byte_order_mark_and_is_read(self):
        with tempfile.TemporaryDirectory() as folder:
            path = env.python_environment_path(folder)
            path.parent.mkdir()
            path.write_bytes(b'\xef\xbb\xbf' + json.dumps({'python': 'D:/p/python.exe'}).encode('utf-8'))
            self.assertEqual(env.read_python_environment(folder), {'python': 'D:/p/python.exe'})

    def test_a_file_that_cannot_be_used_is_no_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = env.python_environment_path(folder)
            path.parent.mkdir()
            path.write_text('{not json', encoding='utf-8')
            self.assertEqual(env.read_python_environment(folder), {})
            path.write_text('["a list"]', encoding='utf-8')
            self.assertEqual(env.read_python_environment(folder), {})

    def test_the_launchers_know_the_file(self):
        launchers = Path(__file__).resolve().parent.parent / 'launchers'
        self.assertIn('config\\python_environment.json', (launchers / 'select_python.ps1').read_text(encoding='utf-8'))
        self.assertIn('select_python.ps1', (launchers / '_python.cmd').read_text(encoding='utf-8'))
        for name in ('open_radiometric_ui_debug.cmd', 'open_panel_selector.cmd', 'check_environment.cmd'):
            text = (launchers / name).read_bytes()
            self.assertIn(b'_python.cmd', text, name)
            self.assertIn(b'\r\n', text, name)                                 # cmd files keep their Windows line endings
        main = (launchers.parent / 'open_radiometric_ui.cmd').read_bytes()           # the main one is in the project folder
        self.assertIn(b'launchers\\_python.cmd', main)
        self.assertIn(b'cd /d "%~dp0"', main)
        self.assertNotIn(b'%~dp0..', main)
        self.assertIn(b'\r\n', main)
        self.assertFalse((launchers / 'open_radiometric_ui.cmd').exists())


class AnotherPythonTests(unittest.TestCase):
    """ Running, choosing and starting another Python """

    def test_pythonw_is_the_python_next_to_it_and_the_same_file_is_the_same_python(self):
        self.assertEqual(env.normalized_python(r'C:\envs\a\pythonw.exe'), Path(r'C:\envs\a\python.exe'))
        self.assertEqual(env.normalized_python('"C:/envs/a/python.exe"'), Path('C:/envs/a/python.exe'))
        self.assertTrue(env.same_python(sys.executable, sys.executable))
        self.assertTrue(env.same_python(Path(sys.executable).with_name('pythonw.exe'), sys.executable))
        self.assertFalse(env.same_python(sys.executable, Path(sys.executable).parent / 'other' / 'python.exe'))

    def test_another_python_starts_with_its_own_folders_first_and_without_the_variables_of_this_one(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'Library' / 'bin').mkdir(parents=True)
            with mock.patch.dict(env.os.environ, {'PYTHONHOME': 'x', 'PYTHONPATH': 'y', 'PATH': 'z'}):
                conda = env.process_environment(Path(folder) / 'python.exe')
                plain = env.process_environment(Path(folder).parent / 'python.exe')
        self.assertEqual(conda['PATH'].split(env.os.pathsep)[:3], [str(Path(folder) / 'Library' / 'bin'), folder, 'z'])
        self.assertEqual(plain['PATH'].split(env.os.pathsep)[:2], [str(Path(folder).parent), 'z'])
        for variables in (conda, plain):
            self.assertNotIn('PYTHONHOME', variables)
            self.assertNotIn('PYTHONPATH', variables)

    def test_this_python_is_run_to_see_its_version_and_whether_it_can_open_a_window(self):
        report = env.inspect_python(sys.executable)
        self.assertEqual(report['version'], platform.python_version())
        self.assertTrue(report['python_compatible'])
        self.assertIn('ok', report['tkinter'])
        self.assertTrue(env.same_python(report['executable'], sys.executable))

    def test_a_python_that_does_not_exist_or_does_not_answer_is_an_error(self):
        with self.assertRaises(FileNotFoundError):
            env.inspect_python(Path('C:/no/such/python.exe'))
        with tempfile.TemporaryDirectory() as folder:
            fake = Path(folder) / 'python.exe'
            fake.write_text('x')
            failed = mock.Mock(returncode=1, stdout='', stderr='cannot start')
            with mock.patch.object(env.subprocess, 'run', return_value=failed):
                with self.assertRaisesRegex(RuntimeError, 'cannot start'):
                    env.inspect_python(fake)
            garbage = mock.Mock(returncode=0, stdout='hello\nnot json', stderr='')
            with mock.patch.object(env.subprocess, 'run', return_value=garbage):
                with self.assertRaisesRegex(RuntimeError, 'Could not read'):
                    env.inspect_python(fake)
            with mock.patch.object(env.subprocess, 'run', side_effect=env.subprocess.TimeoutExpired('python', 25)):
                with self.assertRaisesRegex(RuntimeError, 'could not be run'):
                    env.inspect_python(fake)

    def report(self, compatible=True, tk=True):
        return {'executable': r'C:\envs\a\python.exe', 'version': '3.10.1' if compatible else '3.8.0',
                'python_compatible': compatible, 'tkinter': {'ok': tk, 'detail': 'Tk 8.6' if tk else 'no display'}}

    def test_a_python_is_saved_only_after_it_was_found_able_to_open_a_window(self):
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(env, 'inspect_python', return_value=self.report()):
                report = env.remember_python(folder, 'whatever')
            self.assertEqual(env.read_python_environment(folder), {'python': r'C:\envs\a\python.exe'})
            self.assertEqual(report['version'], '3.10.1')
            self.assertEqual(sorted(path.name for path in (Path(folder) / 'config').iterdir()), ['python_environment.json'])
            with mock.patch.object(env, 'inspect_python', return_value=self.report(compatible=False)):
                with self.assertRaisesRegex(ValueError, '3.10 or newer; this is Python 3.8.0'):
                    env.remember_python(folder, 'old')
            with mock.patch.object(env, 'inspect_python', return_value=self.report(tk=False)):
                with self.assertRaisesRegex(ValueError, 'working Tkinter. no display'):
                    env.remember_python(folder, 'no tk')
            with mock.patch.object(env, 'inspect_python', side_effect=RuntimeError('does not run')):
                with self.assertRaises(RuntimeError):
                    env.remember_python(folder, 'broken')
            self.assertEqual(env.read_python_environment(folder), {'python': r'C:\envs\a\python.exe'})   # still the one that works

    def test_the_check_of_another_python_is_the_json_of_its_own_check(self):
        items = [item('Python'), item('shapely', 'missing')]
        done = mock.Mock(returncode=1, stdout=json.dumps(items), stderr='')                 # 1: something is missing
        with tempfile.TemporaryDirectory() as folder:
            fake = Path(folder) / 'python.exe'
            fake.write_text('x')
            with mock.patch.object(env.subprocess, 'run', return_value=done) as run:
                self.assertEqual(env.check_python_environment(fake, 'project'), items)
            command = run.call_args[0][0]
            self.assertEqual(command[1:], ['-m', 'radiometric_app', 'check', '--json', '--root', 'project', '--deep'])
            self.assertEqual(Path(run.call_args[1]['cwd']), Path(env.__file__).resolve().parent.parent)
            with mock.patch.object(env.subprocess, 'run', return_value=mock.Mock(returncode=1, stdout='', stderr='No module')):
                with self.assertRaisesRegex(RuntimeError, 'No module'):
                    env.check_python_environment(fake, 'project', deep=False)

    def test_the_program_is_started_again_with_the_chosen_python_without_asking_again(self):
        with tempfile.TemporaryDirectory() as folder:
            python = Path(folder) / 'python.exe'
            python.write_text('x')
            (Path(folder) / 'pythonw.exe').write_text('x')
            with mock.patch.object(env.subprocess, 'Popen') as popen:
                env.relaunch(python, 'window', ['--root', '.'])
        command = popen.call_args[0][0]
        self.assertEqual(command[1:], ['-m', 'radiometric_app', 'window', '--root', '.'])
        if env.os.name == 'nt':
            self.assertTrue(command[0].endswith('pythonw.exe'))                           # without a console
        self.assertEqual(popen.call_args[1]['env'][env.CONFIRMED_VARIABLE], '1')
        self.assertEqual(popen.call_args[1]['cwd'], env.os.getcwd())


class CommandLineTests(unittest.TestCase):

    def run_main(self, items, *arguments):
        output = io.StringIO()
        with mock.patch.object(env, 'check_environment', return_value=items) as check, contextlib.redirect_stdout(output):
            code = env.main(list(arguments))
        return code, output.getvalue(), check

    def test_everything_in_place_returns_0(self):
        code, text, _ = self.run_main([item('Python'), item('numpy')])
        self.assertEqual(code, 0)
        self.assertIn('Everything the program needs is in place.', text)
        self.assertEqual(self.run_main([item('Python')], '--quiet')[:2], (0, ''))

    def test_a_problem_returns_1_and_says_what_to_install(self):
        code, text, _ = self.run_main([item('Python'), item('shapely', 'missing', fix='pip install shapely')], '--quiet')
        self.assertEqual(code, 1)
        self.assertIn('MISSING  shapely', text)
        self.assertIn('pip install shapely', text)

    def test_deep_and_json(self):
        code, text, check = self.run_main([item('Python')], '--deep', '--json', '--root', 'somewhere')
        self.assertEqual(json.loads(text)[0]['name'], 'Python')
        self.assertTrue(check.call_args.args[2])                              # deep
        self.assertEqual(check.call_args.args[1], 'somewhere')

    def test_this_computer_passes_when_exiftool_is_there(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = env.main(['--root', '.'])
        self.assertIn(code, (0, 1))
        self.assertIn('Python', output.getvalue())
        self.assertIn('Tkinter', output.getvalue())


class LaunchTests(unittest.TestCase):
    """ python -m radiometric_app: the check, the message and what is opened """

    def setUp(self):
        patcher = mock.patch.object(launch, '_confirm_python', return_value=None)           # the dialog is tested below
        self.confirm = patcher.start()
        self.addCleanup(patcher.stop)

    def run_launch(self, argv, items, answer=False):
        window = mock.Mock(main=mock.Mock(return_value=0))
        with mock.patch.object(env, 'check_environment', return_value=items), \
                mock.patch.object(env, 'show_message', return_value=answer) as shown, \
                mock.patch.object(launch.importlib, 'import_module', return_value=window) as opened:
            code = launch.main(argv)
        return code, shown, opened, window

    def test_what_is_missing_is_shown_and_nothing_is_opened(self):
        code, shown, opened, _ = self.run_launch(['window', '--root', '.'], [item('Python'), item('shapely', 'missing')])
        self.assertEqual(code, 1)
        shown.assert_called_once()
        self.assertIn('shapely', shown.call_args.args[1])
        self.assertFalse(shown.call_args.kwargs.get('question'))
        opened.assert_not_called()

    def test_without_exiftool_the_user_decides(self):
        items = [item('Python'), item('ExifTool', 'missing', 'needed', fix='Install ExifTool')]
        code, shown, opened, _ = self.run_launch([], items, answer=False)
        self.assertEqual(code, 1)
        self.assertTrue(shown.call_args.kwargs['question'])
        opened.assert_not_called()
        code, _, opened, window = self.run_launch(['--root', 'x'], items, answer=True)
        self.assertEqual(code, 0)
        opened.assert_called_once_with('radiometric_app.radiometric_window')
        window.main.assert_called_once_with(['--root', 'x'])

    def test_when_all_is_in_place_the_window_opens_without_a_message(self):
        code, shown, opened, window = self.run_launch(['--config', 'c.json'], [item('Python')])
        self.assertEqual(code, 0)
        shown.assert_not_called()
        opened.assert_called_once_with('radiometric_app.radiometric_window')
        window.main.assert_called_once_with(['--config', 'c.json'])

    def test_the_panel_tool_has_its_own_command(self):
        _, _, opened, window = self.run_launch(['panels', '--root', '.', '--capture', 'IMG_0005'], [item('Python')])
        opened.assert_called_once_with('radiometric_app.panel_selector')
        window.main.assert_called_once_with(['--root', '.', '--capture', 'IMG_0005'])

    def test_check_is_the_command_line_check(self):
        with mock.patch.object(env, 'main', return_value=1) as check:
            self.assertEqual(launch.main(['check', '--deep']), 1)
        check.assert_called_once_with(['--deep'])

    def test_a_package_that_cannot_be_loaded_is_explained_in_the_message(self):
        broken = [item('Python'), item('opencv-python', 'broken', detail='OSError: the DLL was not found')]
        with mock.patch.object(env, 'check_environment', side_effect=[[item('Python')], broken]), \
                mock.patch.object(env, 'show_message') as shown, \
                mock.patch.object(launch.importlib, 'import_module', side_effect=ImportError('DLL load failed')):
            self.assertEqual(launch.main([]), 1)
        self.assertIn('opencv-python: broken (OSError: the DLL was not found)', shown.call_args.args[1])

    def test_the_exiftool_path_of_the_configuration_is_used_and_the_program_is_not_run(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'config').mkdir()
            (Path(folder) / 'config' / 'radiometric_inputs.json').write_text(
                json.dumps({'INPUTS.exiftool_path': 'C:/tools/exiftool.exe'}), encoding='utf-8')
            with mock.patch.object(env, 'check_environment', return_value=[item('Python')]) as check, \
                    mock.patch.object(launch.importlib, 'import_module', return_value=mock.Mock(main=lambda argv: 0)):
                launch.main(['--root', folder])
        self.assertEqual(check.call_args.args[0], 'C:/tools/exiftool.exe')
        self.assertIs(check.call_args.kwargs['exiftool_version_too'], False)


class ConfirmPythonTests(unittest.TestCase):
    """ The dialog that asks for the Python each time the window starts """

    def ask(self, chosen, saved=None, argv=('--root', 'x')):
        from radiometric_app import python_selection
        with tempfile.TemporaryDirectory() as folder:
            if saved:
                env.save_python_environment(folder, saved)
            with mock.patch('tkinter.Tk') as root, \
                    mock.patch.object(python_selection, 'choose_python_environment', return_value=chosen) as dialog, \
                    mock.patch.object(env, 'relaunch') as relaunch:
                outcome = launch._confirm_python(folder, 'window', list(argv))
        return outcome, dialog, relaunch, root

    def test_cancelling_the_dialog_closes_the_program(self):
        outcome, dialog, relaunch, root = self.ask(None)
        self.assertEqual(outcome, 0)
        relaunch.assert_not_called()
        root.return_value.destroy.assert_called_once()

    def test_the_python_that_is_running_goes_on_in_the_same_process(self):
        outcome, _, relaunch, _ = self.ask(Path(sys.executable))
        self.assertIsNone(outcome)
        relaunch.assert_not_called()

    def test_another_python_starts_the_window_again_and_this_one_stops(self):
        other = Path(r'C:\envs\other\python.exe')
        outcome, _, relaunch, _ = self.ask(other)
        self.assertEqual(outcome, 0)
        relaunch.assert_called_once_with(other, 'window', ['--root', 'x'])

    def test_the_dialog_proposes_the_saved_python_if_it_is_there_and_else_the_running_one(self):
        _, dialog, _, _ = self.ask(None, saved=sys.executable)
        self.assertEqual(dialog.call_args[0][1], sys.executable)
        _, dialog, _, _ = self.ask(None, saved=r'C:\gone\python.exe')
        self.assertEqual(dialog.call_args[0][1], sys.executable)
        _, dialog, _, _ = self.ask(None)
        self.assertEqual(dialog.call_args[0][1], sys.executable)

    def test_without_tkinter_the_dialog_is_skipped(self):
        with mock.patch.dict(sys.modules, {'tkinter': None}):
            self.assertIsNone(launch._confirm_python('.', 'window', []))

    def test_the_window_asks_each_time_unless_it_was_asked_not_to_or_was_started_by_the_dialog(self):
        window = mock.Mock(main=mock.Mock(return_value=0))
        items = [item('Python')]

        def run(argv, environment=None):
            with mock.patch.object(env, 'check_environment', return_value=items), \
                    mock.patch.object(launch, '_confirm_python', return_value=None) as confirm, \
                    mock.patch.object(launch.importlib, 'import_module', return_value=window), \
                    mock.patch.dict(env.os.environ, environment or {}):
                launch.main(argv)
                remaining = env.os.environ.get(env.CONFIRMED_VARIABLE)
            return confirm, remaining

        confirm, _ = run(['window', '--root', '.'])
        confirm.assert_called_once_with('.', 'window', ['--root', '.'])
        confirm, _ = run([])                                                       # window is the default command
        confirm.assert_called_once()
        confirm, _ = run(['window', '--skip-python-dialog', '--root', '.'])
        confirm.assert_not_called()
        window.main.assert_called_with(['--root', '.'])                            # the option is not the window's
        confirm, remaining = run(['window'], {env.CONFIRMED_VARIABLE: '1'})
        confirm.assert_not_called()
        self.assertIsNone(remaining)                                               # it is not passed on to other programs
        confirm, _ = run(['panels', '--root', '.'])                                # only the window asks
        confirm.assert_not_called()

    def test_the_window_that_was_cancelled_or_started_again_opens_nothing_here(self):
        with mock.patch.object(launch, '_confirm_python', return_value=0), \
                mock.patch.object(env, 'check_environment') as check, \
                mock.patch.object(launch.importlib, 'import_module') as opened:
            self.assertEqual(launch.main(['window']), 0)
        check.assert_not_called()
        opened.assert_not_called()


if __name__ == '__main__':
    unittest.main()
