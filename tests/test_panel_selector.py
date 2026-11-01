"""
Tests for panel_selector.py: the logic is tested without a display; the window is tested with a hidden Tk root
(skipped if Tk cannot open one)

Run from the project folder with:  python -m unittest tests.test_panel_selector -v
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import radiometric_correction_tools as rct
from radiometric_app import panel_selector as ps
from tests import require_window_size

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / 'images' / 'raw'
BANDS = ['Blue', 'Green', 'Panchro', 'Red', 'Red edge', 'NIR']


class PolygonModelTests(unittest.TestCase):

    def test_vertices_are_appended_and_inserted_in_edges(self):
        model = ps.PolygonModel([[0, 0], [100, 0], [100, 100]])
        self.assertEqual(model.add([300, 300], 5), 3)                  # far from everything: appended
        self.assertEqual(model.add([50, 2], 5), 1)                     # near the edge 0 -> 1: inserted after vertex 0
        self.assertEqual(model.vertices[1], [50.0, 2.0])
        self.assertEqual(len(model.vertices), 5)

    def test_the_closing_edge_is_an_edge(self):
        model = ps.PolygonModel([[0, 0], [100, 0], [100, 100]])
        self.assertEqual(model.nearest_edge([49, 51], 3), 2)           # edge 2 -> 0 (the diagonal)
        self.assertIsNone(model.nearest_edge([500, 500], 3))

    def test_nearest_vertex_within_a_radius(self):
        model = ps.PolygonModel([[0, 0], [100, 0], [100, 100]])
        self.assertEqual(model.nearest_vertex([98, 3], 5), 1)
        self.assertIsNone(model.nearest_vertex([50, 50], 5))

    def test_move_remove_clear_and_integers(self):
        model = ps.PolygonModel([[0, 0], [10, 0], [10, 10]])
        model.move(2, (11.6, 9.4))
        self.assertEqual(model.as_integers(), [[0, 0], [10, 0], [12, 9]])
        model.remove(0)
        self.assertEqual(len(model.vertices), 2)
        model.remove_last()
        model.clear()
        model.remove_last()                                             # nothing to remove: no error
        self.assertEqual(model.vertices, [])

    def test_copy_is_independent(self):
        model = ps.PolygonModel([[0, 0], [10, 0], [10, 10]])
        other = model.copy()
        other.move(0, (5, 5))
        self.assertEqual(model.vertices[0], [0.0, 0.0])


class ViewTransformTests(unittest.TestCase):

    def test_fit_centers_the_image(self):
        view = ps.ViewTransform()
        view.fit((200, 100), (400, 400))
        self.assertEqual(view.scale, 2.0)
        self.assertEqual(view.to_canvas((0, 0)), (0.0, 100.0))
        self.assertEqual(view.to_canvas((200, 100)), (400.0, 300.0))

    def test_canvas_and_image_coordinates_are_inverse(self):
        view = ps.ViewTransform(2.5, (13, -7))
        for point in ((0, 0), (10.5, 20.25), (-4, 99)):
            back = view.to_image(view.to_canvas(point))
            self.assertAlmostEqual(back[0], point[0])
            self.assertAlmostEqual(back[1], point[1])

    def test_zoom_keeps_the_point_under_the_cursor(self):
        view = ps.ViewTransform()
        view.fit((200, 100), (400, 400))
        before = view.to_image((150, 180))
        view.zoom_at((150, 180), 1.25)
        after = view.to_image((150, 180))
        self.assertAlmostEqual(before[0], after[0])
        self.assertAlmostEqual(before[1], after[1])
        self.assertAlmostEqual(view.scale, 2.5)

    def test_zoom_is_limited(self):
        view = ps.ViewTransform()
        view.fit((200, 100), (400, 400))
        for _ in range(100):
            view.zoom_at((10, 10), 2)
        self.assertEqual(view.scale, 2.0 * 60)
        for _ in range(100):
            view.zoom_at((10, 10), 0.5)
        self.assertEqual(view.scale, 1.0)

    def test_pan_moves_the_offset(self):
        view = ps.ViewTransform(1.0, (0, 0))
        view.pan(5, -3)
        self.assertEqual(view.to_canvas((0, 0)), (5.0, -3.0))


class CloseUpTests(unittest.TestCase):

    def test_without_polygons_the_whole_image_is_shown(self):
        self.assertEqual(ps.tile_crop_box([], (1456, 1088), 340 / 250), (0, 0, 1456, 1088))

    def test_the_box_surrounds_the_polygon_with_the_aspect_of_the_tile_inside_the_image(self):
        polygon = [[700, 500], [800, 500], [800, 600], [700, 600]]
        left, top, right, bottom = ps.tile_crop_box([polygon], (1456, 1088), 340 / 250)
        self.assertTrue(left < 700 and right > 800 and top < 500 and bottom > 600)
        self.assertTrue(0 <= left and right <= 1456 and 0 <= top and bottom <= 1088)
        self.assertAlmostEqual((right - left) / (bottom - top), 340 / 250, delta=0.02)

    def test_a_polygon_near_the_corner_is_kept_inside_the_image(self):
        box = ps.tile_crop_box([[[5, 5], [60, 5], [60, 60]]], (400, 300), 340 / 250)
        self.assertEqual((box[0], box[1]), (0, 0))
        self.assertTrue(box[2] <= 400 and box[3] <= 300)

    def test_the_margin_has_a_minimum_for_small_polygons(self):
        left, top, right, bottom = ps.tile_crop_box([[[500, 500], [502, 500], [502, 502]]], (1456, 1088), 1.0)
        self.assertGreaterEqual(right - left, 120)

    def test_a_box_can_be_shown_in_the_view_and_the_whole_image_stays_reachable(self):
        view = ps.ViewTransform()
        view.fit_box((100, 100, 300, 200), (1000, 1000), (800, 400))
        self.assertEqual(view.scale, 4.0)                                  # the box fills the canvas
        self.assertEqual(view.to_canvas((200, 150)), (400.0, 200.0))       # and is centered
        self.assertEqual(view.fit_scale, 0.4)                              # zooming out can reach the whole image
        view.zoom_at((400, 200), 1 / 20)
        self.assertEqual(view.scale, 0.2)


class PanelImagesMixin:
    """ Images of the panel capture, shared by the tests that need them """

    _images = None
    _target = None

    @classmethod
    def panel_images(cls):
        if PanelImagesMixin._images is None:
            files = rct.read_band_files(rct.find_capture_paths(RAW, 'IMG_0005'), BANDS, os.environ.get('exiftoolpath'))
            PanelImagesMixin._images = {band: rct.Image(*files[band]) for band in BANDS}
        return PanelImagesMixin._images

    @classmethod
    def target_images(cls):
        """ Images without a reference panel: there is no region to find from the QR code """
        if PanelImagesMixin._target is None:
            files = rct.read_band_files(rct.find_capture_paths(RAW, 'IMG_0010'), BANDS, os.environ.get('exiftoolpath'))
            PanelImagesMixin._target = {band: rct.Image(*files[band]) for band in BANDS}
        return PanelImagesMixin._target


class WrappedLinesTests(unittest.TestCase):

    def test_text_is_wrapped_at_words(self):
        self.assertEqual(ps.count_wrapped_lines(len, 'aaa bbb ccc', 20), 1)
        self.assertEqual(ps.count_wrapped_lines(len, 'aaa bbb ccc', 7), 2)          # 'aaa bbb' / 'ccc'
        self.assertEqual(ps.count_wrapped_lines(len, 'aaa bbb ccc', 3), 3)

    def test_a_word_longer_than_the_width_takes_its_own_line_and_new_lines_count(self):
        self.assertEqual(ps.count_wrapped_lines(len, 'a verylongword b', 5), 3)
        self.assertEqual(ps.count_wrapped_lines(len, 'one\ntwo', 50), 2)
        self.assertEqual(ps.count_wrapped_lines(len, '', 10), 1)


class CaptureSetTests(PanelImagesMixin, unittest.TestCase):

    def make(self, names=('IMG_0010', 'IMG_0005', 'IMG_0007'), required=('IMG_0005',), **kwargs):
        self.loaded = []

        def load(name):
            self.loaded.append(name)
            if name == 'BROKEN':
                raise OSError('cannot read')
            return self.panel_images() if name in ('IMG_0005', 'IMG_0007') else self.target_images()
        return ps.CaptureSet(names, load, required, **kwargs)

    def test_the_panel_captures_come_first_and_every_name_is_there_once(self):
        captures = self.make(('IMG_0010', 'IMG_0005', 'IMG_0010'), ('IMG_0007', 'IMG_0005', 'IMG_0007'))
        self.assertEqual(captures.names, ['IMG_0007', 'IMG_0005', 'IMG_0010'])
        self.assertEqual(captures.required, ['IMG_0007', 'IMG_0005'])
        self.assertEqual(captures.current, 'IMG_0007')

    def test_a_required_capture_that_is_not_in_the_list_is_added(self):
        self.assertEqual(self.make(('IMG_0010',), ('IMG_0005',)).names, ['IMG_0005', 'IMG_0010'])

    def test_there_must_be_a_capture(self):
        with self.assertRaises(ValueError):
            ps.CaptureSet([], None)

    def test_images_are_read_when_a_capture_is_opened_and_only_once(self):
        captures = self.make()
        self.assertEqual(self.loaded, [])
        first = captures.session('IMG_0010')
        self.assertIs(captures.session('IMG_0010'), first)
        self.assertEqual(self.loaded, ['IMG_0010'])
        self.assertEqual(captures.summary('IMG_0005'), 'not opened')
        self.assertEqual(captures.summary('IMG_0010'), '0 of 6 accepted')

    def test_images_that_were_read_already_are_used(self):
        captures = self.make(preloaded={'IMG_0005': self.panel_images()})
        captures.session('IMG_0005')
        self.assertEqual(self.loaded, [])

    def test_a_reading_error_is_raised_and_nothing_is_kept(self):
        captures = self.make(('BROKEN', 'IMG_0005'))
        with self.assertRaises(OSError):
            captures.session('BROKEN')
        self.assertNotIn('BROKEN', captures.sessions)

    def test_a_capture_without_a_loader_cannot_be_opened(self):
        with self.assertRaises(KeyError):
            ps.CaptureSet(['a'], None).session('a')

    def test_the_regions_saved_for_each_capture_are_applied_to_that_capture(self):
        polygon = [[700, 200], [850, 200], [850, 320], [700, 320]]
        captures = self.make(saved={'IMG_0005': {'Blue': polygon}}, saved_origins={'IMG_0005': {'Blue': 'edited'}})
        self.assertEqual(captures.session('IMG_0005').state('Blue'), 'accepted')
        self.assertEqual(captures.session('IMG_0007').state('Blue'), 'pending')
        self.assertEqual(captures.summary('IMG_0005'), '1 of 6 accepted')

    def test_the_inner_area_is_changed_in_every_capture_and_in_those_opened_later(self):
        automatic = self.make().session('IMG_0005').automatic_polygon('Blue')
        captures = self.make(saved={'IMG_0005': {'Blue': automatic}, 'IMG_0007': {'Blue': automatic}},
                             saved_origins={'IMG_0005': {'Blue': 'accepted_automatic'},
                                            'IMG_0007': {'Blue': 'accepted_automatic'}})
        first = captures.session('IMG_0005')                                      # IMG_0007 is not opened yet
        self.assertEqual(first.state('Blue'), 'accepted')
        self.assertEqual(captures.set_inner_area(0.5), {'IMG_0005': ['Blue']})
        self.assertEqual(captures.percent_inner_area, 0.5)
        second = captures.session('IMG_0007')
        self.assertEqual(second.percent_inner_area, 0.5)
        self.assertEqual(second.state('Blue'), 'pending')                         # made with the old value: to accept again
        self.assertEqual(captures.set_inner_area(0.5), {})
        with self.assertRaises(ValueError):
            captures.set_inner_area(2)

    def test_the_result_says_which_inner_area_the_tool_was_left_with(self):
        captures = self.make(percent_inner_area=0.6)
        session = captures.session('IMG_0005')
        session.accept_all_automatic()
        self.assertEqual(captures.result().percent_inner_area, 0.6)
        captures.set_inner_area(0.3)
        session.accept_all_automatic()
        results = captures.result()
        self.assertEqual(results.percent_inner_area, 0.3)
        self.assertEqual(list(results), ['IMG_0005'])
        self.assertEqual(results, {'IMG_0005': results['IMG_0005']})              # it is a dictionary

    def test_the_panel_captures_must_be_accepted_before_saving(self):
        captures = self.make(required=('IMG_0005', 'IMG_0007'))
        self.assertEqual(captures.not_ready(), {'IMG_0005': None, 'IMG_0007': None})
        captures.session('IMG_0005').accept_all_automatic()
        self.assertEqual(captures.not_ready(), {'IMG_0007': None})
        self.assertFalse(captures.is_ready())
        captures.session('IMG_0007').accept('Blue')
        self.assertEqual(captures.not_ready(), {'IMG_0007': BANDS[1:]})
        captures.session('IMG_0007').accept_all_automatic()
        self.assertTrue(captures.is_ready())

    def test_only_the_captures_that_are_fully_accepted_are_in_the_result(self):
        captures = self.make(required=('IMG_0005',))
        captures.session('IMG_0005').accept_all_automatic()
        captures.session('IMG_0007').accept('Blue')                    # not complete: left out
        captures.session('IMG_0010')                                   # opened, nothing accepted
        result = captures.result()
        self.assertEqual(list(result), ['IMG_0005'])
        self.assertEqual(list(result['IMG_0005']), BANDS)
        self.assertEqual(set(result['IMG_0005'].origins.values()), {'accepted_automatic'})

    def test_a_single_session_is_a_set_with_one_capture(self):
        session = ps.SelectionSession(self.panel_images())
        captures = ps.CaptureSet.single(session, 'IMG_0005')
        self.assertIs(captures.session('IMG_0005'), session)
        self.assertEqual((captures.names, captures.required, captures.current), (['IMG_0005'], ['IMG_0005'], 'IMG_0005'))


class SelectionSessionTests(PanelImagesMixin, unittest.TestCase):

    def session(self, saved=None, origins=None):
        return ps.SelectionSession(self.panel_images(), saved, saved_origins=origins)

    def test_nothing_is_accepted_at_the_start_and_the_proposal_is_the_automatic_region(self):
        session = self.session()
        self.assertEqual(session.accepted_count(), 0)
        self.assertFalse(session.is_complete())
        polygon = session.proposed_polygon('Blue')
        self.assertEqual(len(polygon), 4)
        self.assertEqual(polygon, session.automatic_polygon('Blue'))
        self.assertIsNot(session.proposed_polygon('Blue'), session.proposed_polygon('Blue'))   # copies
        self.assertEqual({session.state(band) for band in BANDS}, {'pending'})

    def test_saved_regions_count_as_accepted_with_their_origin(self):
        automatic = self.session().automatic_polygon('Blue')
        edited = [[100, 100], [200, 100], [200, 200]]
        session = self.session({'Blue': automatic, 'Green': edited, 'Red': edited},
                               {'Red': 'accepted_automatic'})
        self.assertEqual(session.state('Blue'), 'accepted')
        self.assertEqual(session.origins['Blue'], 'accepted_automatic')       # worked out: it is the automatic region
        self.assertEqual(session.origins['Green'], 'edited')
        self.assertEqual(session.origins['Red'], 'accepted_automatic')        # as written in the file
        self.assertEqual(session.proposed_polygon('Green'), edited)
        self.assertEqual(session.state('NIR'), 'pending')
        self.assertEqual(session.accepted_count(), 3)

    def test_a_saved_region_that_is_not_valid_is_ignored_and_reported(self):
        session = self.session({'Blue': [[1, 1], [2, 2]], 'Green': [[10, 10], [9000, 10], [9000, 50]]})
        self.assertEqual(session.accepted_count(), 0)
        self.assertEqual(len(session.notes), 2)
        self.assertIn("band 'Blue'", session.notes[0])
        self.assertEqual(session.proposed_polygon('Blue'), session.automatic_polygon('Blue'))

    def test_a_band_in_the_file_that_is_not_in_the_images_is_left_out(self):
        session = self.session({'Infrared': [[10, 10], [20, 10], [20, 20]]})
        self.assertEqual(session.accepted_count(), 0)
        self.assertEqual(session.notes, [])

    def test_a_capture_without_a_panel_has_no_region_to_accept(self):
        session = ps.SelectionSession({'Blue': self.target_images()['Blue']})
        self.assertIsNone(session.automatic_polygon('Blue'))
        self.assertEqual(session.state('Blue'), 'no_region')
        with self.assertRaisesRegex(ValueError, 'at least 3'):
            session.accept('Blue')
        self.assertEqual(session.accept_all_automatic(), ['Blue'])
        session.accept('Blue', [[600, 300], [700, 300], [700, 400], [600, 400]])
        self.assertEqual((session.state('Blue'), session.origins['Blue']), ('accepted', 'edited'))

    def test_accepting_validates_rounds_and_works_out_the_origin(self):
        session = self.session()
        with self.assertRaisesRegex(ValueError, "Band 'Blue'.*at least 3"):
            session.accept('Blue', [[1, 1], [5, 5]])
        with self.assertRaisesRegex(ValueError, 'cross'):
            session.accept('Blue', [[10, 10], [60, 50], [60, 10], [10, 50]])
        self.assertEqual(session.state('Blue'), 'pending')
        session.accept('Blue', [[10.4, 10.6], [60, 10], [60, 50]])
        self.assertEqual(session.polygons['Blue'], [[10, 11], [60, 10], [60, 50]])
        self.assertEqual(session.origins['Blue'], 'edited')
        session.accept('Green')                                             # the region that is shown, as it is
        self.assertEqual(session.origins['Green'], 'accepted_automatic')
        self.assertEqual(session.polygons['Green'], session.automatic_polygon('Green'))

    def test_undoing_an_acceptance_goes_back_to_what_was_proposed(self):
        edited = [[100, 100], [200, 100], [200, 200]]
        session = self.session({'Blue': edited})
        session.accept('Blue', [[150, 150], [250, 150], [250, 250]])
        session.unaccept('Blue')
        self.assertEqual(session.state('Blue'), 'pending')
        self.assertEqual(session.proposed_polygon('Blue'), edited)           # the saved region, not accepted any more
        self.assertIsNone(session.origins['Blue'])

    def test_accept_all_takes_the_automatic_region_of_the_bands_that_are_not_accepted(self):
        edited = [[100, 100], [200, 100], [200, 200]]
        session = self.session({'Blue': edited})
        self.assertEqual(session.accept_all_automatic(), [])
        self.assertTrue(session.is_complete())
        self.assertEqual(session.polygons['Blue'], edited)                    # accepted ones are not touched
        self.assertEqual(session.polygons['Green'], session.automatic_polygon('Green'))
        self.assertEqual({session.origins[band] for band in BANDS if band != 'Blue'}, {'accepted_automatic'})

    @staticmethod
    def area(polygon):
        xs, ys = zip(*polygon)
        return abs(sum(xs[i] * ys[(i + 1) % len(xs)] - xs[(i + 1) % len(xs)] * ys[i] for i in range(len(xs)))) / 2

    def test_a_new_inner_area_makes_the_automatic_regions_again_and_puts_back_those_accepted_as_found(self):
        automatic = self.session().automatic_polygon('Blue')
        drawn = [[100, 100], [200, 100], [200, 200], [100, 200]]
        session = self.session({'Blue': automatic, 'Green': drawn, 'Red': automatic}, {'Red': 'accepted_automatic'})
        before = {band: session.automatic_polygon(band) for band in BANDS}
        self.assertEqual(session.set_inner_area(0.4), ['Blue', 'Red'])            # accepted as found from the QR code
        self.assertEqual(session.percent_inner_area, 0.4)
        for band in ('Blue', 'Red'):
            self.assertEqual(session.state(band), 'pending')
            self.assertEqual(session.proposed_polygon(band), session.automatic_polygon(band))      # the new one
            self.assertLess(self.area(session.automatic_polygon(band)), self.area(before[band]))
        self.assertEqual((session.state('Green'), session.proposed_polygon('Green')), ('accepted', drawn))   # drawn: kept
        self.assertEqual(session.origins['Green'], 'edited')
        self.assertEqual(session.accepted_count(), 1)
        session.accept('Blue')
        self.assertEqual(session.origins['Blue'], 'accepted_automatic')           # of the new region

    def test_the_inner_area_is_checked_and_the_same_value_changes_nothing(self):
        session = self.session()
        session.accept('Blue')
        self.assertEqual(session.set_inner_area(0.7), [])
        self.assertEqual(session.state('Blue'), 'accepted')
        for value in (0, -0.1, 1.5, 'abc'):
            with self.assertRaises(ValueError):
                session.set_inner_area(value)
        self.assertEqual(session.percent_inner_area, 0.7)
        self.assertEqual(session.set_inner_area(1), ['Blue'])                       # 1 is allowed: the whole region

    def test_the_result_has_every_accepted_polygon_with_its_origin(self):
        session = self.session()
        session.accept('Blue')
        session.accept('Red', [[100, 100], [200, 100], [200, 200]])
        result = session.result()
        self.assertEqual(list(result), ['Blue', 'Red'])
        self.assertEqual(result.origins, {'Blue': 'accepted_automatic', 'Red': 'edited'})
        self.assertIsInstance(result, dict)

    def test_the_next_band_to_accept_goes_round(self):
        session = self.session()
        self.assertEqual(session.next_not_accepted(), 1)
        session.accept('Green')
        self.assertEqual(session.next_not_accepted(), 2)
        session.go_to(5)
        self.assertEqual(session.next_not_accepted(), 0)                      # round the end
        session.accept_all_automatic()
        self.assertIsNone(session.next_not_accepted())

    def test_stats_of_the_automatic_region_and_of_an_invalid_one(self):
        session = self.session()
        stats = session.stats('Blue', session.automatic_polygon('Blue'))
        self.assertGreater(stats['n_pixels'], 1000)
        self.assertLess(stats['variation_percent'], 5)
        self.assertIsNone(session.stats('Blue', [[1, 1], [2, 2]]))

    def test_navigation_is_clamped(self):
        session = self.session()
        session.go_to(-3)
        self.assertEqual(session.band, 'Blue')
        session.go_to(99)
        self.assertEqual(session.band, 'NIR')

    def test_a_region_accepted_here_gives_the_same_calibration_in_the_workflow(self):
        from radiometric_app import radiometric_workflow as wf
        session = self.session()
        session.accept_all_automatic()
        result = session.result()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'regions.json'
            wf.save_panel_regions(path, 'IMG_0005', result, result.origins)
            saved = wf.load_panel_regions(path)['IMG_0005']
            origins = wf.load_panel_origins(path)['IMG_0005']
        self.assertEqual(origins['Blue'], 'accepted_automatic')
        levels = wf.default_config()['PANEL.reflectance_by_band']
        image = rct.Image(self.panel_images()['Blue'].image_path, self.panel_images()['Blue'].metadata)
        automatic = image.detect_reference_panel(levels, 0.7)[1]
        manual_image = rct.Image(image.image_path, image.metadata)
        manual = manual_image.detect_reference_panel(levels, 0.7, manual_bounds=saved['Blue'],
                                                     region_origin=origins['Blue'])[1]
        self.assertEqual(manual, automatic)
        self.assertEqual(manual_image.panel_audit['region_origin'], 'accepted_automatic')
        self.assertEqual(manual_image.panel_audit['region_source'], 'manual')


class WindowTests(PanelImagesMixin, unittest.TestCase):

    def setUp(self):
        import tkinter as tk
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"no display for Tk: {error}")
        self.root.withdraw()
        self.open_window()

    def open_window(self, saved=None, origins=None, images=None):
        self.session = ps.SelectionSession(images or self.panel_images(), saved, saved_origins=origins)
        self.window = ps.PanelSelectorWindow(self.root, self.session)
        self.root.geometry('1100x760')
        self.root.update()

    def reopen(self, **kwargs):
        self.root.destroy()
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()
        self.open_window(**kwargs)

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def click(self, handler, image_point):
        x, y = self.window.view.to_canvas(image_point)
        handler(SimpleNamespace(x=x, y=y))

    def hover(self, widget):
        widget.event_generate('<Enter>')
        self.root.update()
        text = self.window.hint_label.cget('text')
        widget.event_generate('<Leave>')
        self.root.update()
        return text

    def band_list(self):
        return [self.window.band_list.get(index) for index in range(self.window.band_list.size())]

    # --- the band and its decision ---------------------------------------------------------------------------

    def test_it_opens_on_the_first_band_with_the_bands_in_a_list(self):
        window = self.window
        self.assertIn('Blue (1 of 6)', window.title_label.cget('text'))
        self.assertEqual(self.band_list(), [f"{band}   (to accept)" for band in BANDS])
        self.assertEqual(window.band_list.curselection(), (0,))
        self.assertEqual(window.status_label.cget('text'), '0 of 6 bands accepted')
        self.assertIn('pixels', window.stats_label.cget('text'))
        self.assertIn('Not accepted yet', window.message_label.cget('text'))
        self.assertFalse(window.editing)
        self.assertEqual(window.model.as_integers(), self.session.automatic_polygon('Blue'))
        self.assertEqual(window.buttons['accept'].cget('text'), 'Accept')
        self.assertTrue(window.buttons['finish'].instate(['disabled']))
        self.assertTrue(window.buttons['reset'].instate(['disabled']))              # the tools of the drawing are off
        self.assertTrue(window.buttons['clear'].instate(['disabled']))

    def test_a_band_can_be_chosen_in_the_list(self):
        window = self.window
        window.band_list.selection_clear(0, 'end')
        window.band_list.selection_set(3)
        window._on_band_chosen(None)
        self.assertEqual(self.session.band, 'Red')
        self.assertIn('Red (4 of 6)', window.title_label.cget('text'))

    def test_accepting_marks_the_band_and_goes_to_the_next_one_that_is_not_accepted(self):
        window = self.window
        self.session.accept('Green')
        window.accept_or_confirm()                                         # Blue, as it is: the region of the QR code
        self.assertEqual(self.session.origins['Blue'], 'accepted_automatic')
        self.assertEqual(self.session.band, 'Panchro')                     # Green was accepted already
        self.assertEqual(self.band_list()[0], 'Blue   (accepted)')
        self.assertEqual(window.status_label.cget('text'), '2 of 6 bands accepted')
        self.assertIn('Still to accept: Panchro, Red, Red edge, NIR', window.summary_label.cget('text'))

    def test_pressing_accept_again_takes_the_acceptance_back(self):
        window = self.window
        window.accept_or_confirm()
        window.show_band('Blue')
        self.assertEqual(window.buttons['accept'].cget('text'), 'Accepted (undo)')
        self.assertIn('Accepted (as found from the QR code)', window.message_label.cget('text'))
        window.accept_or_confirm()
        self.assertEqual(self.session.state('Blue'), 'pending')
        self.assertEqual(window.buttons['accept'].cget('text'), 'Accept')
        self.assertEqual(window.status_label.cget('text'), '0 of 6 bands accepted')

    def test_the_enter_key_accepts_but_never_undoes(self):
        window = self.window
        window._key_accept()
        self.assertEqual(self.session.state('Blue'), 'accepted')
        window.show_band('Blue')
        window._key_accept()
        self.assertEqual(self.session.state('Blue'), 'accepted')

    def test_all_the_automatic_regions_can_be_accepted_without_drawing(self):
        window = self.window
        window.accept_all()
        self.assertFalse(window.editing)
        self.assertTrue(self.session.is_complete())
        self.assertFalse(window.buttons['finish'].instate(['disabled']))
        self.assertIn('Every region is accepted', window.summary_label.cget('text'))
        window.finish()
        self.assertFalse(window.cancelled)
        result = self.session.result()
        self.assertEqual(list(result), BANDS)
        self.assertEqual(set(result.origins.values()), {'accepted_automatic'})

    def test_finishing_before_every_band_is_accepted_is_refused(self):
        window = self.window
        window.accept_or_confirm()
        window.finish()
        self.assertFalse(window.finished)
        self.assertIn('Accept every band of the panel captures first', window.message_label.cget('text'))
        self.assertIn('Green', window.message_label.cget('text'))

    def test_the_regions_saved_before_are_shown_as_accepted(self):
        edited = [[700, 200], [850, 200], [850, 320], [700, 320]]
        self.reopen(saved={'Blue': edited}, origins={'Blue': 'edited'})
        window = self.window
        self.assertEqual(self.band_list()[0], 'Blue   (accepted)')
        self.assertEqual(window.buttons['accept'].cget('text'), 'Accepted (undo)')
        self.assertIn('Accepted (drawn or edited)', window.message_label.cget('text'))
        self.assertEqual(window.status_label.cget('text'), '1 of 6 bands accepted')
        self.assertEqual(self.session.proposed_polygon('Blue'), edited)
        self.assertEqual(window.model.as_integers(), edited)

    def test_a_saved_region_that_cannot_be_used_is_reported(self):
        self.reopen(saved={'Blue': [[1, 1], [2, 2]]})
        self.assertIn("The saved region of band 'Blue' is not valid", self.window.summary_label.cget('text'))

    def test_a_band_without_a_region_has_to_be_drawn(self):
        self.reopen(images={'Blue': self.target_images()['Blue'], 'Green': self.target_images()['Green']})
        window = self.window
        self.assertEqual(self.band_list(), ['Blue   (no region)', 'Green   (no region)'])
        self.assertIn('No region was found', window.message_label.cget('text'))
        self.assertTrue(window.buttons['accept'].instate(['disabled']))
        window._key_accept()
        self.assertEqual(self.session.accepted_count(), 0)
        window.accept_all()
        self.assertIn('Blue, Green', window.message_label.cget('text'))
        self.assertFalse(self.session.is_complete())
        window.toggle_edit()                                               # drawing it is possible
        self.assertFalse(window.buttons['accept'].instate(['disabled']))

    def test_cancel_returns_nothing(self):
        self.window.cancel()
        self.assertTrue(self.window.cancelled)

    # --- the drawing, in the same window ---------------------------------------------------------------------

    def test_edit_region_turns_the_drawing_on_and_off_in_the_same_window(self):
        window = self.window
        window.toggle_edit()
        self.assertTrue(window.editing)
        self.assertEqual(window.buttons['accept'].cget('text'), 'Confirm edit')
        self.assertEqual(window.buttons['edit'].cget('text'), 'Discard edit')
        self.assertFalse(window.buttons['reset'].instate(['disabled']))
        self.assertIn('Drawing', window.message_label.cget('text'))
        self.assertEqual(str(window.canvas.cget('cursor')), 'crosshair')
        window.toggle_edit()
        self.assertFalse(window.editing)
        self.assertEqual(window.buttons['edit'].cget('text'), 'Edit region')
        self.assertTrue(window.buttons['reset'].instate(['disabled']))
        self.assertEqual(str(window.canvas.cget('cursor')), 'arrow')

    def test_clicks_only_draw_while_editing(self):
        window = self.window
        before = [list(vertex) for vertex in window.model.vertices]
        self.click(window._on_press, [300, 300])
        self.click(window._on_release, [300, 300])
        self.assertEqual(window.model.vertices, before)

    def test_clicks_add_drag_and_remove_vertices(self):
        window = self.window
        window.edit_band('Blue')
        window.clear()
        for point in ([300, 300], [500, 300], [500, 450]):
            self.click(window._on_press, point)
            self.click(window._on_release, point)
        self.assertEqual(window.model.as_integers(), [[300, 300], [500, 300], [500, 450]])
        self.assertIn('pixels', window.stats_label.cget('text'))
        self.click(window._on_press, [500, 450])           # pick the last vertex and drag it
        self.click(window._on_drag, [520, 470])
        self.click(window._on_release, [520, 470])
        self.assertEqual(window.model.as_integers()[2], [520, 470])
        self.click(window._on_remove, [520, 470])
        self.assertEqual(len(window.model.vertices), 2)
        self.assertIn('at least 3', window.stats_label.cget('text'))

    def test_confirming_an_invalid_region_shows_the_reason_and_stays(self):
        window = self.window
        window.edit_band('Blue')
        window.clear()
        window.accept_or_confirm()
        self.assertTrue(window.editing)
        self.assertEqual(self.session.state('Blue'), 'pending')
        self.assertIn('at least 3 vertices', window.message_label.cget('text'))

    def test_an_edited_region_is_accepted_as_edited_and_the_drawing_is_turned_off(self):
        window = self.window
        window.edit_band('Blue')
        window.model.move(0, (window.model.vertices[0][0] + 12, window.model.vertices[0][1] + 7))
        window.accept_or_confirm()
        self.assertEqual(self.session.origins['Blue'], 'edited')
        self.assertNotEqual(self.session.polygons['Blue'], self.session.automatic_polygon('Blue'))
        self.assertFalse(window.editing)
        self.assertEqual(self.session.band, 'Green')

    def test_confirming_the_last_band_that_is_not_accepted_stays_on_it(self):
        window = self.window
        for band in BANDS[1:]:
            self.session.accept(band)
        window.edit_band('Blue')
        window.accept_or_confirm()
        self.assertTrue(self.session.is_complete())
        self.assertEqual(self.session.band, 'Blue')
        self.assertFalse(window.editing)
        self.assertEqual(window.buttons['accept'].cget('text'), 'Accepted (undo)')
        self.assertFalse(window.buttons['finish'].instate(['disabled']))

    def test_changing_band_or_discarding_loses_what_was_not_confirmed(self):
        window = self.window
        before = self.session.proposed_polygon('Blue')
        window.edit_band('Blue')
        window.clear()
        window.toggle_edit()
        self.assertEqual(window.model.as_integers(), before)
        self.assertEqual(self.session.state('Blue'), 'pending')
        window.edit_band('Blue')
        window.clear()
        window.next()
        self.assertFalse(window.editing)
        window.previous()
        self.assertEqual(window.model.as_integers(), before)

    def test_the_accepted_region_is_kept_to_compare_while_editing(self):
        window = self.window
        self.session.accept('Blue', [[700, 200], [850, 200], [850, 320], [700, 320]])
        window.show_band('Blue')
        window.toggle_edit()
        self.assertEqual(window.model.as_integers(), [[700, 200], [850, 200], [850, 320], [700, 320]])
        window.reset()                                                      # back to the QR code region
        self.assertEqual(window.model.as_integers(), self.session.automatic_polygon('Blue'))
        window._redraw()                                                    # draws the accepted region too, to compare
        self.assertEqual(self.session.polygons['Blue'], [[700, 200], [850, 200], [850, 320], [700, 320]])

    def test_previous_and_next_move_between_bands(self):
        window = self.window
        window.show_band('Green')
        window.next()
        self.assertIn('Panchro (3 of 6)', window.title_label.cget('text'))
        window.previous()
        window.previous()
        self.assertIn('Blue (1 of 6)', window.title_label.cget('text'))
        window.previous()                                                   # clamped
        self.assertIn('Blue (1 of 6)', window.title_label.cget('text'))

    def test_reset_clear_and_undo_work_while_editing_only(self):
        window = self.window
        window.clear()
        self.assertEqual(len(window.model.vertices), 4)                    # not editing: nothing happens
        window.toggle_edit()
        window.clear()
        self.assertEqual(window.model.vertices, [])
        window.reset()
        self.assertEqual(window.model.as_integers(), self.session.automatic_polygon('Blue'))
        window.undo()
        self.assertEqual(len(window.model.vertices), 3)

    # --- the view ----------------------------------------------------------------------------------------------

    def test_the_view_opens_close_to_the_region_and_can_show_the_whole_image(self):
        window = self.window
        self.root.attributes('-alpha', 0.0)
        self.root.deiconify()
        self.root.update()
        window.show_band('Blue')
        self.root.update()
        close_up = window.view.scale
        width, height = window.session.image_size()
        whole = min(window.canvas.winfo_width() / width, window.canvas.winfo_height() / height)
        self.assertGreater(close_up, whole * 1.5)                           # the panel fills a good part of the picture
        window.toggle_zoom()
        self.assertAlmostEqual(window.view.scale, whole, places=3)
        self.assertEqual(window.buttons['zoom'].cget('text'), 'Zoom to region')
        window.toggle_zoom()
        self.assertAlmostEqual(window.view.scale, close_up, places=3)
        for vertex in window.model.vertices:                                # the region is inside the picture
            x, y = window.view.to_canvas(vertex)
            self.assertTrue(0 < x < window.canvas.winfo_width() and 0 < y < window.canvas.winfo_height())

    def test_zoom_with_the_wheel_and_pan(self):
        window = self.window
        before = window.view.scale
        window._on_wheel(SimpleNamespace(x=200, y=200, delta=120))
        self.assertGreater(window.view.scale, before)
        window._on_pan_start(SimpleNamespace(x=10, y=10))
        offset = window.view.offset
        window._on_pan_move(SimpleNamespace(x=30, y=25))
        self.assertEqual(window.view.offset, (offset[0] + 20, offset[1] + 15))

    def test_the_left_button_drags_the_image_when_not_editing(self):
        window = self.window
        window._on_press(SimpleNamespace(x=10, y=10))
        offset = window.view.offset
        window._on_drag(SimpleNamespace(x=40, y=30))
        window._on_release(SimpleNamespace(x=40, y=30))
        self.assertEqual(window.view.offset, (offset[0] + 30, offset[1] + 20))

    def test_a_dual_camera_with_ten_bands_fits_in_the_list(self):
        names = [f"B{number}" for number in range(10)]
        images = {name: self.panel_images()[BANDS[number % 6]] for number, name in enumerate(names)}
        self.reopen(images=images)
        self.assertEqual(self.window.band_list.size(), 10)
        self.window.show_band('B9')
        self.assertIn('B9 (10 of 10)', self.window.title_label.cget('text'))
        self.assertEqual(self.window.band_list.curselection(), (9,))

    # --- keys ------------------------------------------------------------------------------------------------------

    def test_the_window_has_the_inner_area_of_the_panel_and_a_hint_for_it(self):
        window = self.window
        self.assertEqual(window.inner_var.get(), '0.7')
        self.assertIn('Inner area of the panel', self.hover(window.inner_entry))
        self.assertIn('Inner area of the panel', self.hover(window.buttons['inner']))

    def test_applying_a_new_inner_area_makes_the_regions_again_and_puts_back_those_accepted_as_found(self):
        window = self.window
        window.accept_all()
        self.assertTrue(self.session.is_complete())
        drawn = [[100, 100], [200, 100], [200, 200], [100, 200]]
        self.session.accept('Green', drawn)
        old = self.session.automatic_polygon('Blue')
        window.inner_var.set('0.5')
        window.apply_inner_area()
        self.assertEqual(self.window.captures.percent_inner_area, 0.5)
        self.assertNotEqual(self.session.automatic_polygon('Blue'), old)
        self.assertEqual(self.session.state('Blue'), 'pending')
        self.assertEqual(self.session.state('Green'), 'accepted')                  # drawn: kept
        message = window.message_label.cget('text')
        self.assertIn('Inner area 0.5: regions found again.', message)
        self.assertIn('5 accepted as found from the QR code are to accept again', message)     # Green was drawn
        self.assertIn('drawn or edited ones stay', message)
        self.assertLess(len(message), 125)                                                       # the line is not wrapped
        self.assertEqual(window.inner_var.get(), '0.5')
        self.assertFalse(self.window.captures.is_ready())                           # they have to be accepted again

    def test_a_wrong_inner_area_or_one_typed_while_drawing_is_refused_and_the_old_one_stays(self):
        window = self.window
        for text in ('abc', '0', '1.5', ''):
            window.inner_var.set(text)
            window.apply_inner_area()
            self.assertEqual(window.inner_var.get(), '0.7', text)
            self.assertIn('a number above 0 and up to 1', window.message_label.cget('text'))
        window.edit_band('Blue')
        window.inner_var.set('0.5')
        window.apply_inner_area()
        self.assertEqual(window.captures.percent_inner_area, 0.7)
        self.assertIn('Confirm or discard the edit', window.message_label.cget('text'))
        self.assertTrue(window.editing)                                             # what was drawn is not lost
        window.toggle_edit()
        window.inner_var.set('0,5')                                                 # a decimal comma is accepted
        window.apply_inner_area()
        self.assertEqual(window.captures.percent_inner_area, 0.5)

    def test_enter_in_the_box_applies_the_value_and_escape_leaves_the_box_without_closing_the_tool(self):
        from unittest import mock
        window = self.window
        window.inner_var.set('0.4')
        self.assertNotEqual(window.inner_entry.bind('<Return>'), '')
        self.assertEqual(window._inner_return(), 'break')                           # Enter does not accept the band
        self.assertEqual(window.captures.percent_inner_area, 0.4)
        window.inner_var.set('0.9')
        with mock.patch.object(window, '_typing', return_value=True):
            window._key_escape()
        self.assertFalse(window.finished)
        self.assertEqual(window.inner_var.get(), '0.4')                             # what was not applied is dropped

    def test_escape_leaves_the_drawing_and_then_cancels(self):
        window = self.window
        window.edit_band('Blue')
        window._key_escape()
        self.assertFalse(window.editing)
        self.assertFalse(window.finished)
        window._key_escape()
        self.assertTrue(window.cancelled and window.finished)

    def test_the_keys_of_the_drawing_do_nothing_when_not_editing(self):
        window = self.window
        window._key_clear()
        window._key_reset()
        window._key_undo()
        self.assertEqual(len(window.model.vertices), 4)
        window._key_edit()
        self.assertTrue(window.editing)
        window._key_next()
        self.assertEqual(self.session.band, 'Green')
        self.assertFalse(window.editing)

    # --- several captures ----------------------------------------------------------------------------------------

    def open_captures(self, names=('IMG_0005', 'IMG_0007', 'IMG_0010'), required=('IMG_0005',), **kwargs):
        """ A window on a set of captures: IMG_0005 and IMG_0007 show the panel, IMG_0010 does not """
        self.root.destroy()
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()
        self.loaded = []

        def load(name):
            self.loaded.append(name)
            if name == 'BROKEN':
                raise OSError('cannot read')
            return self.panel_images() if name in ('IMG_0005', 'IMG_0007') else self.target_images()
        self.captures = ps.CaptureSet(names, load, required, **kwargs)
        self.window = ps.PanelSelectorWindow(self.root, self.captures)
        self.session = self.window.session
        self.root.geometry('1100x760')
        self.root.update()

    def test_the_capture_box_is_only_shown_when_there_is_more_than_one_capture(self):
        self.assertEqual(self.window.capture_box.winfo_manager(), '')
        self.open_captures()
        self.assertEqual(self.window.capture_box.winfo_manager(), 'pack')
        self.assertEqual(list(self.window.capture_box.cget('values')),
                         ['IMG_0005  (panel, 0 of 6 accepted)', 'IMG_0007  (not opened)', 'IMG_0010  (not opened)'])
        self.assertEqual(self.window.capture_var.get(), 'IMG_0005  (panel, 0 of 6 accepted)')

    def test_another_capture_can_be_chosen(self):
        self.open_captures()
        window = self.window
        self.assertEqual(self.loaded, ['IMG_0005'])                           # the first capture is opened with the window
        window.capture_var.set('IMG_0010  (not opened)')
        window._on_capture_chosen()
        self.assertEqual(self.captures.current, 'IMG_0010')
        self.assertEqual(self.loaded, ['IMG_0005', 'IMG_0010'])
        self.assertIs(window.session, self.captures.session('IMG_0010'))
        self.assertEqual(self.band_list()[0], 'Blue   (no region)')                    # it has no panel
        self.assertIn('IMG_0010: 0 of 6 bands accepted', window.summary_label.cget('text'))
        self.assertEqual(window.capture_var.get(), 'IMG_0010  (0 of 6 accepted)')

    def test_what_is_accepted_is_kept_for_each_capture(self):
        self.open_captures()
        window = self.window
        window.accept_all()
        self.assertTrue(window.switch_capture('IMG_0007'))
        self.assertEqual(window.session.accepted_count(), 0)
        window.accept_or_confirm()
        self.assertTrue(window.switch_capture('IMG_0005'))
        self.assertEqual(window.session.accepted_count(), 6)
        self.assertEqual(window.status_label.cget('text'), '6 of 6 bands accepted')
        self.assertEqual(self.captures.session('IMG_0007').accepted_count(), 1)
        self.assertIn('IMG_0007  (1 of 6 accepted)', window.capture_box.cget('values'))

    def test_the_images_of_each_capture_are_kept_apart(self):
        self.open_captures()
        window = self.window
        window.switch_capture('IMG_0010')
        window.switch_capture('IMG_0005')
        self.assertEqual(self.band_list()[0], 'Blue   (to accept)')
        keys = set(window.display_cache)
        self.assertIn(('IMG_0005', 'Blue'), keys)
        self.assertIn(('IMG_0010', 'Blue'), keys)

    def test_switching_capture_turns_the_drawing_off(self):
        self.open_captures()
        window = self.window
        window.edit_band('Blue')
        self.assertTrue(window.switch_capture('IMG_0007'))
        self.assertFalse(window.editing)

    def test_a_capture_that_cannot_be_read_is_reported_and_the_current_one_stays(self):
        self.open_captures(('IMG_0005', 'BROKEN'))
        window = self.window
        self.assertFalse(window.switch_capture('BROKEN'))
        self.assertEqual(self.captures.current, 'IMG_0005')
        self.assertIn('BROKEN could not be opened: OSError: cannot read', window.message_label.cget('text'))
        self.assertEqual(window.capture_var.get(), 'IMG_0005  (panel, 0 of 6 accepted)')

    def test_a_capture_with_other_bands_is_refused(self):
        self.open_captures()
        window = self.window
        self.captures.sessions['IMG_0007'] = ps.SelectionSession({'Blue': self.panel_images()['Blue']})
        self.assertFalse(window.switch_capture('IMG_0007'))
        self.assertIn('has the bands', window.message_label.cget('text'))

    def test_two_panel_captures_must_both_be_accepted_before_saving(self):
        self.open_captures(required=('IMG_0005', 'IMG_0007'))
        window = self.window
        window.accept_all()
        self.assertTrue(window.buttons['finish'].instate(['disabled']))
        self.assertIn('Panel captures: IMG_0005 (6 of 6 accepted), IMG_0007 (not opened)',
                      window.summary_label.cget('text'))
        window.finish()
        self.assertFalse(window.finished)
        self.assertIn('IMG_0007: not opened', window.message_label.cget('text'))
        window.switch_capture('IMG_0007')
        window.accept_or_confirm()
        window.finish()
        self.assertIn('IMG_0007: still to accept Green', window.message_label.cget('text'))
        window.accept_all()
        self.assertFalse(window.buttons['finish'].instate(['disabled']))
        window.finish()
        self.assertFalse(window.cancelled)
        result = self.captures.result()
        self.assertEqual(list(result), ['IMG_0005', 'IMG_0007'])
        self.assertEqual(list(result['IMG_0007']), BANDS)

    def test_a_capture_that_was_only_looked_at_does_not_stop_saving(self):
        self.open_captures()
        window = self.window
        window.accept_all()
        window.switch_capture('IMG_0010')                               # looked at, nothing accepted
        self.assertFalse(window.buttons['finish'].instate(['disabled']))
        window.finish()
        self.assertEqual(list(self.captures.result()), ['IMG_0005'])

    def test_saved_regions_of_the_other_captures_are_shown_when_they_are_opened(self):
        polygon = [[700, 200], [850, 200], [850, 320], [700, 320]]
        self.open_captures(saved={'IMG_0007': {'Blue': polygon}}, saved_origins={'IMG_0007': {'Blue': 'edited'}})
        window = self.window
        window.switch_capture('IMG_0007')
        self.assertEqual(self.band_list()[:2], ['Blue   (accepted)', 'Green   (to accept)'])
        self.assertIn('Accepted (drawn or edited)', window.message_label.cget('text'))

    def test_pointing_at_the_capture_box_says_what_it_is(self):
        self.open_captures()
        self.root.attributes('-alpha', 0.0)
        self.root.deiconify()
        self.root.update()
        self.assertEqual(self.hover(self.window.capture_box), ps.HINTS['capture'])
        self.assertIn('those before the flight and those after it', ps.HINTS['capture'])

    # --- the explanation area ------------------------------------------------------------------------------------

    def test_the_explanation_area_does_not_change_size_while_pointing_at_elements(self):
        window = self.window
        self.root.attributes('-alpha', 0.0)
        self.root.deiconify()
        self.root.geometry('1100x760')
        self.root.update()
        window.edit_band('Blue')
        self.root.update()
        sizes = {(window.footer.winfo_height(), window.canvas.winfo_height(), window.canvas.winfo_width())}
        for name, button in window.buttons.items():
            self.hover(button)
            self.root.update()
            sizes.add((window.footer.winfo_height(), window.canvas.winfo_height(), window.canvas.winfo_width()))
        self.hover(window.canvas)
        self.hover(window.band_list)
        sizes.add((window.footer.winfo_height(), window.canvas.winfo_height(), window.canvas.winfo_width()))
        self.assertEqual(len(sizes), 1, sizes)

    def test_the_area_has_the_height_of_the_longest_explanation_and_the_whole_width(self):
        window = self.window
        self.root.attributes('-alpha', 0.0)
        self.root.deiconify()
        require_window_size(self, self.root, 1800, 700)
        self.root.geometry('1000x700')
        self.root.update()
        wrap = window.footer.winfo_width() - 24
        self.assertEqual(int(window.hint_label.cget('wraplength')), wrap)
        self.assertEqual(window.footer.winfo_width(), self.root.winfo_width())              # all the width of the window
        self.assertEqual(window.footer.winfo_height(), window.hint_height(wrap))
        self.root.geometry('1060x700')                                                         # narrower: more lines
        self.root.update()
        narrow = window.footer.winfo_height()
        self.root.geometry('1800x700')
        self.root.update()
        self.assertLess(window.footer.winfo_height(), narrow)

    def test_the_longest_explanation_fits_in_the_area(self):
        window = self.window
        self.root.attributes('-alpha', 0.0)
        self.root.deiconify()
        self.root.update()
        longest = max(ps.HINTS.values(), key=len)
        window.hint_label.configure(text=longest)
        self.root.update()
        self.assertLessEqual(window.hint_label.winfo_reqheight(), window.footer.winfo_height())
        self.assertGreaterEqual(window.hint_label.winfo_height(), window.hint_label.winfo_reqheight() - 1)

    # --- what each element does ------------------------------------------------------------------------------

    def test_there_is_no_fixed_explanation_only_the_hint_of_the_element_under_the_mouse(self):
        window = self.window
        self.assertFalse(hasattr(window, 'legend_label'))
        self.assertEqual(window.hint_label.cget('text'), ps.DEFAULT_HINT)
        self.assertNotIn('Mouse:', ps.DEFAULT_HINT)

    def test_every_element_has_a_hint(self):
        window = self.window
        names = set(window.buttons) | {'canvas', 'capture', 'bands', 'confirm', 'discard'}
        self.assertEqual(set(ps.HINTS), names)
        self.assertNotIn('skip', ps.HINTS)                                  # there is no separate automatic option

    def test_pointing_at_the_elements_says_what_they_do(self):
        window = self.window
        self.root.attributes('-alpha', 0.0)             # a canvas gets the events of the mouse only when it is shown
        self.root.deiconify()
        self.root.update()
        for name, button in window.buttons.items():
            self.assertEqual(self.hover(button), ps.HINTS[name], name)
        self.assertEqual(self.hover(window.canvas), ps.HINTS['canvas'])
        self.assertEqual(self.hover(window.band_list), ps.HINTS['bands'])
        self.assertEqual(window.hint_label.cget('text'), ps.DEFAULT_HINT)

    def test_the_hint_of_the_accept_and_edit_buttons_follows_the_mode(self):
        window = self.window
        self.root.attributes('-alpha', 0.0)
        self.root.deiconify()
        self.root.update()
        window.toggle_edit()
        self.assertEqual(self.hover(window.buttons['accept']), ps.HINTS['confirm'])
        self.assertEqual(self.hover(window.buttons['edit']), ps.HINTS['discard'])

    def test_the_hints_carry_the_important_information(self):
        hints = ps.HINTS
        for name, phrases in {'accept': ('Enter', 'next band', 'undo'),
                              'confirm': ('Enter', '3 or more vertices', 'next band'),
                              'edit': ('Click adds a vertex', 'right click', 'nothing is accepted until'),
                              'discard': ('Esc', 'without changing'),
                              'previous': ('Left arrow', 'lost'), 'next': ('Right arrow', 'lost'),
                              'reset': ('QR code', 'not accepted until'),
                              'clear': ('Backspace',),
                              'zoom': ('wheel', 'middle button'),
                              'bands': ('accepted', 'to accept', 'no region', 'lost'),
                              'canvas': ('wheel', 'Cyan', 'yellow dashed', 'green', 'variation'),
                              'accept_all': ('QR code', 'Bands without one', 'used for the reference panel'),
                              'finish': ('every band', 'exactly these regions'),
                              'cancel': ('save nothing',)}.items():
            for phrase in phrases:
                self.assertIn(phrase.lower(), hints[name].lower(), f"{name}: {phrase}")


class CommandLineTests(unittest.TestCase):

    def test_a_configuration_file_that_does_not_exist_returns_2(self):
        self.assertEqual(ps.main(['--config', str(ROOT / 'missing_inputs.json'), '--root', str(ROOT)]), 2)

    def test_invalid_configuration_is_reported_and_returns_1(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'inputs.json'
            config.write_text(json.dumps({'INPUTS.reference_panel_capture': 'IMG_9999'}), encoding='utf-8')
            self.assertEqual(ps.main(['--config', str(config), '--root', str(ROOT)]), 1)


if __name__ == '__main__':
    unittest.main()
