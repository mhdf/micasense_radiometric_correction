"""
Tests for folder_browser.py: what lists a folder and reads a picture is tested without a display; the window with a Tk root
(skipped if Tk cannot open one)

Run from the project folder with:  python -m unittest tests.test_folder_browser -v
"""

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import tifffile

from radiometric_app import folder_browser as fb

ROOT = Path(__file__).resolve().parent.parent


def make_tree(folder):
    """ a_folder/ B_folder/ (with an image), an image, a text and another file """
    folder = Path(folder)
    (folder / 'a_folder').mkdir()
    (folder / 'B_folder').mkdir()
    (folder / 'B_folder' / 'inside.tif').write_bytes(b'x')
    gradient = (100 + np.arange(40 * 60, dtype=np.uint16).reshape(40, 60) // 20)
    tifffile.imwrite(folder / 'IMG_0001_1.tif', gradient)
    (folder / 'notes.txt').write_text('hello', encoding='utf-8')
    (folder / 'Zeta.dat').write_bytes(b'0' * 2048)
    return folder


class LogicTests(unittest.TestCase):

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.tree = make_tree(self.folder.name)

    def tearDown(self):
        self.folder.cleanup()

    def test_a_size_is_written_in_the_unit_that_fits(self):
        self.assertEqual(fb.size_text(None), '')
        self.assertEqual(fb.size_text(0), '0 B')
        self.assertEqual(fb.size_text(1023), '1023 B')
        self.assertEqual(fb.size_text(2048), '2.0 KiB')
        self.assertEqual(fb.size_text(3 * 1024 * 1024), '3.0 MiB')
        self.assertEqual(fb.size_text(5 * 1024 ** 4), '5.0 TiB')

    def test_a_folder_is_listed_with_the_folders_first_then_the_files_without_regard_to_case(self):
        target, rows = fb.list_folder(self.tree)
        self.assertEqual(target, Path(str(self.tree)).absolute())
        self.assertEqual([name for name, _, _ in rows],
                         ['a_folder', 'B_folder', 'IMG_0001_1.tif', 'notes.txt', 'Zeta.dat'])
        self.assertEqual([folder for _, folder, _ in rows], [True, True, False, False, False])
        sizes = {name: size for name, _, size in rows}
        self.assertIsNone(sizes['a_folder'])
        self.assertEqual(sizes['Zeta.dat'], 2048)
        self.assertEqual(sizes['notes.txt'], 5)

    def test_the_path_of_a_file_stands_for_its_folder_and_a_relative_path_starts_at_the_base(self):
        target, _ = fb.list_folder(self.tree / 'notes.txt')
        self.assertEqual(target, Path(str(self.tree)).absolute())
        target, rows = fb.list_folder('B_folder', base=self.tree)
        self.assertEqual(target, Path(str(self.tree / 'B_folder')).absolute())
        self.assertEqual([name for name, _, _ in rows], ['inside.tif'])
        target, _ = fb.list_folder(f'  "{self.tree / "a_folder"}"  ')                   # quotes and spaces are ignored
        self.assertEqual(target.name, 'a_folder')

    def test_a_folder_that_cannot_be_read_is_an_error(self):
        with self.assertRaises(OSError):
            fb.list_folder(self.tree / 'missing')

    def test_the_images_are_known_by_their_suffix(self):
        for name in ('a.tif', 'a.TIFF', 'b.png', 'c.JPG', 'd.jpeg'):
            self.assertTrue(fb.is_image(name), name)
        for name in ('a.txt', 'a.tif.bak', 'folder', 'a.csv'):
            self.assertFalse(fb.is_image(name), name)

    def test_the_picture_of_a_raw_image_is_stretched_for_the_eye_and_the_file_is_not_changed(self):
        path = self.tree / 'IMG_0001_1.tif'
        before = path.read_bytes()
        picture, (width, height), kind = fb.preview_picture(path)
        self.assertEqual((width, height, kind), (60, 40, 'uint16'))
        self.assertEqual(picture.mode, 'RGB')
        values = np.asarray(picture)[..., 0]
        self.assertEqual((values.min(), values.max()), (0, 255))                       # dark values use the whole range
        self.assertEqual(path.read_bytes(), before)

    def test_a_floating_point_image_with_invalid_values_has_a_picture(self):
        data = np.linspace(0, 0.6, 50 * 80, dtype=np.float64).reshape(50, 80)
        data[:5, :5] = np.nan
        path = self.tree / 'reflectance.tif'
        tifffile.imwrite(path, data)
        picture, (width, height), kind = fb.preview_picture(path)
        self.assertEqual((width, height, kind), (80, 50, 'float64'))
        self.assertEqual(picture.size, (80, 50))
        path = self.tree / 'nothing.tif'
        tifffile.imwrite(path, np.full((10, 10), np.nan))
        with self.assertRaisesRegex(ValueError, 'no valid values'):
            fb.preview_picture(path)

    def test_a_big_image_is_reduced_to_the_bounds(self):
        path = self.tree / 'big.tif'
        tifffile.imwrite(path, np.random.default_rng(1).integers(0, 60000, (1200, 1600), dtype=np.uint16))
        picture, (width, height), _ = fb.preview_picture(path, bounds=(400, 300))
        self.assertEqual((width, height), (1600, 1200))                              # what the file is
        self.assertLessEqual(picture.size[0], 400)
        self.assertLessEqual(picture.size[1], 300)

    def test_a_color_picture_is_shown_as_it_is_and_what_is_not_an_image_is_an_error(self):
        from PIL import Image
        path = self.tree / 'color.png'
        Image.new('RGB', (30, 20), (200, 10, 10)).save(path)
        picture, size, kind = fb.preview_picture(path)
        self.assertEqual((size, kind, picture.getpixel((0, 0))), ((30, 20), 'RGB', (200, 10, 10)))
        path = self.tree / 'four_dimensions.tif'
        tifffile.imwrite(path, np.zeros((3, 4, 5, 6), dtype=np.uint8), photometric='minisblack')
        with self.assertRaisesRegex(ValueError, 'is not an image'):
            fb.preview_picture(path)
        with self.assertRaises(Exception):
            fb.preview_picture(self.tree / 'notes.txt')


def make_copies(folder):
    """ flight/ and panels/, both with the capture IMG_0005 (the same files), and IMG_0007 in both with different files """
    flight, panels = Path(folder) / 'flight', Path(folder) / 'panels'
    for target in (flight, panels):
        target.mkdir()
        for index in (1, 2):
            tifffile.imwrite(target / f'IMG_0005_{index}.tif', np.full((8, 8), index, dtype=np.uint16))
    tifffile.imwrite(flight / 'IMG_0007_1.tif', np.zeros((8, 8), dtype=np.uint16))
    tifffile.imwrite(panels / 'IMG_0007_1.tif', np.ones((8, 8), dtype=np.uint16))
    tifffile.imwrite(flight / 'IMG_0010_1.tif', np.zeros((8, 8), dtype=np.uint16))
    return flight, panels


class RepeatedCaptureTests(unittest.TestCase):

    def test_the_copies_are_compared_by_their_bytes_and_only_choices_of_a_copy_are_kept(self):
        import radiometric_correction_tools as rct
        with tempfile.TemporaryDirectory() as folder:
            flight, panels = make_copies(folder)
            groups = rct.repeated_captures([flight, panels])
            self.assertEqual(list(groups), ['IMG_0005', 'IMG_0007'])
            self.assertEqual(fb.identical_copies(groups['IMG_0005']), {flight: True, panels: True})
            self.assertEqual(fb.identical_copies(groups['IMG_0007']), {flight: False, panels: False})
            choices = fb.valid_choices(groups, {'IMG_0005': str(panels), 'IMG_0007': folder, 'IMG_0010': str(flight)})
            self.assertEqual(choices, {'IMG_0005': panels})
        self.assertEqual(fb.distinct_paths([r'C:\data\01\survey\raw', r'C:\data\01\calibration\raw']),
                         ['…\\survey\\raw', '…\\calibration\\raw'])
        self.assertEqual(fb.distinct_paths([r'C:\data', r'C:\data\raw']), [r'C:\data', r'C:\data\raw'])  # one holds the other
        self.assertEqual(fb.distinct_paths([r'C:\a', r'D:\a']), [r'C:\a', r'D:\a'])


class WindowTests(unittest.TestCase):

    def setUp(self):
        import tkinter as tk
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"no display for Tk: {error}")
        from radiometric_app.ui_theme import apply_dark_theme
        apply_dark_theme(self.root)
        self.root.geometry('300x100+30+30')
        self.folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)    # the picture may still be being read
        self.tree = make_tree(self.folder.name)
        self.browser = fb.FolderBrowser(self.root, 'Images folder', self.tree)
        self.root.update()

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass
        self.folder.cleanup()

    def pump(self, condition, timeout=20):
        end = time.time() + timeout
        while not condition() and time.time() < end:
            self.root.update()
            time.sleep(0.05)
        self.assertTrue(condition(), 'timed out')

    def names(self):
        return [self.browser.tree.item(item)['text'] for item in self.browser.tree.get_children()]

    def select(self, name):
        key = next(key for key, (path, _) in self.browser.entries.items() if path.name == name)
        self.browser.tree.selection_set(key)
        self.root.update()

    def test_it_opens_on_the_folder_and_lists_it(self):
        browser = self.browser
        self.assertEqual(browser.title(), 'Images folder')
        self.assertEqual(browser.location.get(), str(Path(str(self.tree)).absolute()))
        self.assertEqual(self.names(), ['a_folder', 'B_folder', 'IMG_0001_1.tif', 'notes.txt', 'Zeta.dat'])
        self.assertEqual(browser.tree.item(browser.tree.get_children()[0])['values'][0], 'Folder')
        self.assertEqual(browser.tree.item(browser.tree.get_children()[-1])['values'], ['File', '2.0 KiB'])
        self.assertEqual(browser.summary.get(), '2 folders, 3 files. Double-click a folder to open it.')
        self.assertEqual(browser.selection.get(), f"Folder to use: {browser.current}")
        self.assertEqual(browser.preview.status.get(), fb.NO_PREVIEW)

    def test_a_folder_is_opened_and_left_with_up(self):
        browser = self.browser
        self.select('B_folder')
        self.assertEqual(browser.selection.get(), f"Folder to use: {self.tree / 'B_folder'}")        # the one that is selected
        browser.open_selected()
        self.assertEqual(self.names(), ['inside.tif'])
        self.assertEqual(browser.current, Path(str(self.tree / 'B_folder')).absolute())
        browser.up()
        self.assertEqual(browser.current, Path(str(self.tree)).absolute())
        browser.navigate(Path(self.tree.anchor))
        self.assertIn('disabled', browser.up_button.state())                               # nothing above the root

    def test_the_image_that_is_selected_has_a_picture_and_what_is_not_an_image_does_not(self):
        browser = self.browser
        self.select('IMG_0001_1.tif')
        self.pump(lambda: 'pixels' in browser.preview.status.get())
        self.assertIn('IMG_0001_1.tif\n60 × 40 pixels; uint16. Preview contrast only; source values are unchanged.',
                      browser.preview.status.get())
        self.assertIsNotNone(browser.preview.picture)
        self.assertEqual(browser.preview.path.name, 'IMG_0001_1.tif')
        self.select('notes.txt')
        self.assertEqual(browser.preview.status.get(), fb.NO_PREVIEW)
        self.assertIsNone(browser.preview.path)
        self.select('IMG_0001_1.tif')
        self.select('a_folder')                                                            # a folder neither
        self.assertEqual(browser.preview.status.get(), fb.NO_PREVIEW)

    def test_an_image_that_cannot_be_read_says_so(self):
        (self.tree / 'broken.tif').write_bytes(b'not a tiff at all')
        self.browser.navigate(self.tree)
        self.select('broken.tif')
        self.pump(lambda: 'No preview' in self.browser.preview.status.get())
        self.assertTrue(self.browser.preview.status.get().startswith('broken.tif\nNo preview: '))

    def test_the_folder_that_is_selected_or_else_the_one_that_is_open_is_chosen(self):
        browser = self.browser
        browser.accept()
        self.assertEqual(browser.result, str(Path(str(self.tree)).absolute()))
        self.browser = fb.FolderBrowser(self.root, 'Images folder', self.tree)
        self.select('a_folder')
        self.browser.accept()
        self.assertEqual(self.browser.result, str(Path(str(self.tree / 'a_folder')).absolute()))
        self.browser = fb.FolderBrowser(self.root, 'Images folder', self.tree)
        self.select('notes.txt')                                                           # a file is not a folder
        self.browser.accept()
        self.assertEqual(self.browser.result, str(Path(str(self.tree)).absolute()))

    def test_cancelling_chooses_nothing(self):
        self.browser.cancel()
        self.assertEqual(self.browser.result, '')
        self.assertFalse(self.browser.winfo_exists())

    def test_a_path_that_was_typed_is_opened_and_one_that_does_not_exist_is_refused(self):
        browser = self.browser
        with mock.patch('tkinter.messagebox.showerror') as error:
            browser.location.set(str(self.tree / 'missing'))
            self.assertFalse(browser.navigate(browser.location.get()))
        self.assertIn('Cannot open folder', error.call_args[0][0])
        self.assertEqual(browser.location.get(), str(Path(str(self.tree)).absolute()))     # the one that is open comes back
        browser.location.set(str(self.tree / 'a_folder'))                                  # typed, not opened with Go
        browser.accept()
        self.assertEqual(browser.result, str(Path(str(self.tree / 'a_folder')).absolute()))

    def test_a_new_folder_is_made_in_the_open_one_and_opened(self):
        browser = self.browser
        with mock.patch.object(fb, 'ask_name', return_value='fresh'):
            browser.new_folder()
        self.assertTrue((self.tree / 'fresh').is_dir())
        self.assertEqual(browser.current.name, 'fresh')
        browser.up()
        self.assertIn('fresh', self.names())
        with mock.patch.object(fb, 'ask_name', return_value=''):                          # cancelled: nothing
            browser.new_folder()
        for bad in ('..', 'a/b', 'a\\b', 'C:\\x'):
            with mock.patch.object(fb, 'ask_name', return_value=bad), mock.patch('tkinter.messagebox.showerror') as error:
                browser.new_folder()
            self.assertEqual(error.call_args[0][0], 'Invalid folder name', bad)
        with mock.patch.object(fb, 'ask_name', return_value='fresh'), mock.patch('tkinter.messagebox.showerror') as error:
            browser.new_folder()                                                           # it exists already
        self.assertEqual(error.call_args[0][0], 'Cannot create folder')

    def test_the_window_is_run_above_another_one_and_gives_the_folder(self):
        made = []
        real = fb.FolderBrowser

        def make(*arguments):
            made.append(real(*arguments))
            return made[0]
        self.root.update()
        with mock.patch.object(fb, 'FolderBrowser', side_effect=make) as constructor, \
                mock.patch.object(self.root, 'wait_window', side_effect=lambda window: (self.select_in(made[0], 'B_folder'),
                                                                                       made[0].accept())):
            chosen = fb.ask_folder(self.root, 'Results folder', self.tree)
        constructor.assert_called_once_with(self.root, 'Results folder', self.tree)
        self.assertEqual(chosen, str(Path(str(self.tree / 'B_folder')).absolute()))

    def test_a_list_of_folders_is_built_and_the_repeated_captures_are_chosen_before_it_is_used(self):
        flight, panels = make_copies(self.tree / 'a_folder')
        browser = fb.FoldersBrowser(self.root, 'Images folder', flight.parent, [flight], {}, None)
        self.root.update()
        self.assertEqual(browser.folders, [flight])
        for name in ('flight', 'panels'):                                     # both selected: added once each
            key = next(key for key, (path, _) in browser.entries.items() if path.name == name)
            browser.tree.selection_add(key)
        browser.add_folders()
        self.assertEqual(browser.folders, [flight, panels])
        self.assertEqual(browser.folder_list.heading('path')['text'], 'Folders to use (2)')
        with mock.patch.object(fb, 'ask_repeated_captures', return_value=None) as ask:    # cancelled: not used yet
            browser.accept()
        self.assertEqual(list(ask.call_args[0][1]), ['IMG_0005', 'IMG_0007'])
        self.assertTrue(browser.winfo_exists())
        chosen = {'IMG_0005': str(panels), 'IMG_0007': str(flight)}
        with mock.patch.object(fb, 'ask_repeated_captures', return_value=chosen):
            browser.accept()
        self.assertEqual(browser.result, ([str(flight), str(panels)], chosen))

    def test_the_folders_are_used_without_asking_when_every_repeated_capture_has_its_copy(self):
        flight, panels = make_copies(self.tree / 'a_folder')
        chosen = {'IMG_0005': str(panels), 'IMG_0007': str(flight), 'IMG_0099': str(flight)}    # IMG_0099 is not repeated
        browser = fb.FoldersBrowser(self.root, 'Images folder', flight, [flight, panels], chosen, None)
        browser.folder_list.selection_set('1')
        browser.remove_folders()
        self.assertEqual(browser.folders, [flight])
        with mock.patch.object(fb, 'ask_repeated_captures') as ask:
            browser.accept()
        ask.assert_not_called()
        self.assertEqual(browser.result, ([str(flight)], {}))                 # nothing is repeated in one folder
        browser = fb.FoldersBrowser(self.root, 'Images folder', flight, [flight, panels], chosen, None)
        with mock.patch.object(fb, 'ask_repeated_captures') as ask:
            browser.accept()
        ask.assert_not_called()
        self.assertEqual(browser.result[1], {'IMG_0005': str(panels), 'IMG_0007': str(flight)})

    def test_the_dialog_of_the_repeated_captures_shows_the_copies_and_keeps_the_choices(self):
        import radiometric_correction_tools as rct
        flight, panels = make_copies(self.tree / 'a_folder')
        dialog = fb.RepeatedCapturesDialog(self.root, rct.repeated_captures([flight, panels]))
        self.root.update()
        rows = [dialog.sources.item(item)['values'] for item in dialog.sources.get_children()]
        self.assertEqual(rows, [['…\\flight', 2, 'Identical files'],
                                ['…\\panels', 2, 'Identical files']])
        self.assertEqual(dialog.copy_path.get(), f"Selected copy: {flight}")
        self.pump(lambda: dialog.preview.path is not None and dialog.preview.path.name == 'IMG_0005_1.tif')
        with mock.patch('tkinter.messagebox.showerror') as error:              # a capture without a choice
            dialog.accept()
        self.assertIn('IMG_0005', error.call_args[0][1])
        dialog.sources.selection_set('1')
        dialog.keep()                                                          # panels for IMG_0005; IMG_0007 is shown
        self.assertEqual(dialog.current_name(), 'IMG_0007')
        self.assertEqual(dialog.sources.item('0')['values'][2], 'Different files')
        dialog.keep_remaining()                                                # the first copy (flight) for the rest
        self.assertIn('2 of 2', dialog.status.get())
        dialog.accept()
        self.assertEqual(dialog.result, {'IMG_0005': str(panels), 'IMG_0007': str(flight)})

    def select_in(self, browser, name):
        key = next(key for key, (path, _) in browser.entries.items() if path.name == name)
        browser.tree.selection_set(key)

    def test_the_name_prompt_returns_what_was_typed(self):
        def widgets(parent):
            for child in parent.winfo_children():
                yield child
                yield from widgets(child)

        def answer(window, button):
            for widget in widgets(window):
                if widget.winfo_class() == 'TEntry':
                    widget.insert(0, '  my folder ')
            next(widget for widget in widgets(window) if widget.winfo_class() == 'TButton' and
                 widget.cget('text') == button).invoke()
        with mock.patch.object(self.root, 'wait_window', side_effect=lambda window: answer(window, 'OK')):
            self.assertEqual(fb.ask_name(self.root, 'New Folder', 'Folder name:'), 'my folder')
        with mock.patch.object(self.root, 'wait_window', side_effect=lambda window: answer(window, 'Cancel')):
            self.assertEqual(fb.ask_name(self.root, 'New Folder', 'Folder name:'), '')


if __name__ == '__main__':
    unittest.main()
