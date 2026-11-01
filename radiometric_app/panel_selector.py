"""
Check, accept and (if needed) draw the reference panel region of each band

The window shows one band at a time, with the bands in a list at the left (so that any number of bands fits, for example
the ten of a dual camera): the image around the panel with the region found from the QR code (yellow, dashed), the region
that is accepted or saved (green) and the region proposed (cyan), and the pixels, saturated pixels and variation inside.
Each band must be accepted: "Accept" takes the region as it is shown, "Edit region" turns on the drawing in the same
window, where the polygon can be moved, extended or drawn, and "Confirm edit" accepts it. The regions that were saved
before are shown as accepted. The vertices are pixels of the undistorted image and are saved in the file used by the
workflow (PANEL.manual_regions_file), with the origin of each region (accepted as found, or edited).

    python -m radiometric_app.panel_selector [--config config/radiometric_inputs.json] [--root folder] [--capture IMG_0005]

Without --config, config/radiometric_inputs.json in the root folder is used if it exists, otherwise the defaults.

What every element does is shown at the bottom of the window when the mouse is over it.

The logic (PolygonModel, ViewTransform, SelectionSession, CaptureSet) has no user interface and is tested without a display.
"""

# Standard Library Imports
import argparse
import math
import sys
from pathlib import Path

# Third-Party Imports

# Local Imports
import radiometric_correction_tools as rct
from .ui_theme import COLORS, FONT, apply_dark_theme, hide_title_icon

DEFAULT_HINT = "Point at a button or at an image to see what it does."

# What each element does, shown while the mouse is over it
HINTS = {
    'bands': "Bands: choose the band to look at. Each band must be accepted. After the name: accepted (green), to accept "
             "(a region is proposed but not accepted) or no region (none was found: draw it with Edit region). What you "
             "changed in a region that you are editing and did not confirm is lost when you change band.",
    'previous': "Previous band (Left arrow): show the previous band, without accepting or changing anything (what you "
                "changed in a region that you are editing and did not confirm is lost).",
    'next': "Next band (Right arrow): show the next band, without accepting or changing anything (what you changed in a "
            "region that you are editing and did not confirm is lost).",
    'accept': "Accept (Enter): use the region outlined for this band (green or cyan; if there is none, the yellow one found "
              "from the QR code) and go to the next band that is not accepted. Press it again on an accepted band to undo "
              "the acceptance.",
    'confirm': "Confirm edit (Enter): accept the polygon as it is drawn for this band and go to the next band that is not "
               "accepted. It needs 3 or more vertices, inside the image and without crossing edges. The file keeps "
               "exactly this polygon.",
    'edit': "Edit region (E): turn on the drawing for this band, to move, add or delete vertices, or to draw the region when "
            "none was found. Click adds a vertex (on an edge it inserts one), drag a vertex to move it, right click removes "
            "it. Nothing is accepted until you confirm.",
    'discard': "Discard edit (Esc): turn the drawing off and go back to the region as it was before, without changing what "
               "is accepted.",
    'reset': "Reset to automatic (R): put the polygon back on the region found from the QR code (yellow dashed line). It is "
             "not accepted until you confirm. Only while editing.",
    'clear': "Clear (C): remove all the vertices to draw the region from scratch with clicks. Backspace removes only the "
             "last vertex. Only while editing.",
    'zoom': "Whole image / Zoom to region: switch between the whole image and a close view around the panel. The wheel "
            "zooms and the middle button drags the image (also the left button, when you are not editing).",
    'canvas': "Image of this band around the panel. Outlines: yellow dashed = region found from the QR code; green = region "
              "accepted or saved before; cyan = region proposed or being edited. The numbers below the image are the "
              "pixels, the saturated pixels and the variation (std / mean) inside the region: lower means a more uniform "
              "panel. The wheel zooms; while editing, clicks draw.",
    'capture': "Capture: choose which panel capture to look at (those before the flight and those after it, if there are "
               "any). All their bands must be accepted before saving. What you accepted is kept for each capture while "
               "you switch.",
    'accept_all': "Accept all automatic regions: for every band of this capture that is not accepted yet, accept the region "
                  "found from the QR code (yellow outline). Bands without one are left for you to draw with Edit region. "
                  "Look at the outlines first: accepting means that this region is the one used for the reference panel.",
    'inner': "Inner area of the panel (a number above 0 and up to 1; Enter or Apply): the fraction of the region found from "
             "the QR code that is measured, without the edge. The yellow regions are made again with it; the ones accepted as "
             "found from the QR code are to accept again, the ones you drew or edited stay. Save and close sets it in the "
             "configuration.",
    'finish': "Save and close: write the accepted regions (and whether each was accepted as found or edited) to the "
              "regions file; the workflow then uses exactly these regions. Available when every band of the panel "
              "captures is accepted.",
    'cancel': "Cancel: close the window and save nothing.",
}

VERTEX_RADIUS_PIXELS = 7      # distance on the screen to pick a vertex
EDGE_DISTANCE_PIXELS = 6      # distance on the screen to insert a vertex on an edge

ORIGIN_TEXT = {'accepted_automatic': 'as found from the QR code', 'edited': 'drawn or edited', None: ''}


# ======================================================================================
# LOGIC WITHOUT USER INTERFACE
# ======================================================================================

class PolygonModel:
    """ Vertices [x, y] in image pixels """

    def __init__(self, vertices=None):
        self.vertices = [[float(x), float(y)] for x, y in (vertices or [])]

    def copy(self):
        return PolygonModel(self.vertices)

    def clear(self):
        self.vertices = []

    def move(self, index, point):
        self.vertices[index] = [float(point[0]), float(point[1])]

    def remove(self, index):
        del self.vertices[index]

    def remove_last(self):
        if self.vertices:
            self.vertices.pop()

    def nearest_vertex(self, point, radius):
        """ Index of the closest vertex within radius (image pixels), or None """
        best, best_distance = None, radius
        for index, (x, y) in enumerate(self.vertices):
            distance = math.hypot(x - point[0], y - point[1])
            if distance <= best_distance:
                best, best_distance = index, distance
        return best

    def nearest_edge(self, point, distance):
        """ Index i of the closest edge (vertex i to vertex i + 1, the last one closing the polygon) within distance """
        count = len(self.vertices)
        if count < 2:
            return None
        best, best_distance = None, distance
        for index in range(count if count > 2 else 1):
            (ax, ay), (bx, by) = self.vertices[index], self.vertices[(index + 1) % count]
            length_squared = (bx - ax) ** 2 + (by - ay) ** 2
            t = 0.0 if length_squared == 0 else max(0.0, min(1.0, ((point[0] - ax) * (bx - ax) +
                                                                  (point[1] - ay) * (by - ay)) / length_squared))
            d = math.hypot(point[0] - (ax + t * (bx - ax)), point[1] - (ay + t * (by - ay)))
            if d <= best_distance:
                best, best_distance = index, d
        return best

    def add(self, point, radius):
        """
        Add a vertex: inserted in the edge if the point is within radius (image pixels) of one, otherwise appended
        Returns the index of the new vertex
        """
        edge = self.nearest_edge(point, radius)
        if edge is None:
            self.vertices.append([float(point[0]), float(point[1])])
            return len(self.vertices) - 1
        self.vertices.insert(edge + 1, [float(point[0]), float(point[1])])
        return edge + 1

    def as_integers(self):
        return [[int(round(x)), int(round(y))] for x, y in self.vertices]


class ViewTransform:
    """ Zoom and pan: canvas = scale * image + offset """

    def __init__(self, scale=1.0, offset=(0.0, 0.0)):
        self.scale, self.offset = float(scale), (float(offset[0]), float(offset[1]))
        self.fit_scale = self.scale

    def to_canvas(self, point):
        return (point[0] * self.scale + self.offset[0], point[1] * self.scale + self.offset[1])

    def to_image(self, point):
        return ((point[0] - self.offset[0]) / self.scale, (point[1] - self.offset[1]) / self.scale)

    def fit(self, image_size, canvas_size):
        """ Show the whole image, centered """
        self.scale = self.fit_scale = min(canvas_size[0] / image_size[0], canvas_size[1] / image_size[1])
        self.offset = ((canvas_size[0] - image_size[0] * self.scale) / 2, (canvas_size[1] - image_size[1] * self.scale) / 2)

    def fit_box(self, box, image_size, canvas_size):
        """ Show the part of the image in box (left, top, right, bottom), centered; zooming out can still reach the whole image """
        left, top, right, bottom = box
        self.fit_scale = min(canvas_size[0] / image_size[0], canvas_size[1] / image_size[1])
        self.scale = min(canvas_size[0] / max(right - left, 1), canvas_size[1] / max(bottom - top, 1))
        self.offset = ((canvas_size[0] - (left + right) * self.scale) / 2, (canvas_size[1] - (top + bottom) * self.scale) / 2)

    def zoom_at(self, canvas_point, factor, maximum=60.0):
        """ Zoom keeping the image point under canvas_point where it is """
        anchor = self.to_image(canvas_point)
        self.scale = max(self.fit_scale * 0.5, min(self.fit_scale * maximum, self.scale * factor))
        self.offset = (canvas_point[0] - anchor[0] * self.scale, canvas_point[1] - anchor[1] * self.scale)

    def pan(self, dx, dy):
        self.offset = (self.offset[0] + dx, self.offset[1] + dy)


def tile_crop_box(polygons, image_size, aspect, margin=0.8, minimum=60):
    """
    The part of the image to show in a tile: around the polygons with a margin, with the aspect ratio (width / height)
    of the tile where possible, and always inside the image. Returns (left, top, right, bottom) in pixels
    """
    width, height = image_size
    points = [point for polygon in polygons for point in polygon]
    if not points:
        return (0, 0, width, height)
    xs, ys = zip(*points)
    left, right, top, bottom = min(xs), max(xs), min(ys), max(ys)
    pad = max(minimum, margin * max(right - left, bottom - top))
    left, right, top, bottom = left - pad, right + pad, top - pad, bottom + pad
    box_width, box_height = right - left, bottom - top
    if box_width / box_height < aspect:
        extra = box_height * aspect - box_width
        left, right = left - extra / 2, right + extra / 2
    else:
        extra = box_width / aspect - box_height
        top, bottom = top - extra / 2, bottom + extra / 2
    return (int(max(0, round(left))), int(max(0, round(top))), int(min(width, round(right))), int(min(height, round(bottom))))


class SelectionResult(dict):
    """ {band: polygon} of the accepted regions, with .origins = {band: 'accepted_automatic' or 'edited'} """

    def __init__(self, regions, origins):
        super().__init__(regions)
        self.origins = dict(origins)


class SelectionSession:
    """
    The decisions of the user, band by band
    Inputs: {band: rct.Image} of the panel capture, the regions saved before {band: polygon} (they count as accepted, with
            the origins {band: origin} written with them) and the parameters used to find the automatic region
    Each band has to be accepted: with the region found from the QR code as it is, or with a polygon that was edited.
    The origin of an accepted region is 'accepted_automatic' if it is the region found from the QR code, else 'edited'
    """

    def __init__(self, images, saved=None, percent_inner_area=0.7, sat_threshold=65000, saved_origins=None):
        self.images = dict(images)
        self.bands = list(self.images)
        self.saved = {band: polygon for band, polygon in (saved or {}).items() if band in self.images}
        self.percent_inner_area = percent_inner_area
        self.sat_threshold = sat_threshold
        self.index = 0
        self.decisions = {band: None for band in self.bands}          # None or 'accepted'
        self.polygons = {band: None for band in self.bands}           # the accepted polygons
        self.origins = {band: None for band in self.bands}
        self.notes = []                                               # what could not be used from the saved regions
        self._automatic = {}
        for band, polygon in list(self.saved.items()):
            try:
                self.accept(band, polygon, origin=(saved_origins or {}).get(band))
            except ValueError as error:
                self.notes.append(f"The saved region of band '{band}' is not valid and was ignored: {error}")
                del self.saved[band]

    @property
    def band(self):
        return self.bands[self.index]

    def set_inner_area(self, value):
        """
        Change the inner area of the panel, which decides the region found from the QR code (a number above 0 and up to 1;
        ValueError if it is not). The regions that were accepted as found from the QR code were made with the old value,
        so they are to accept again (the one that is proposed is the new one); the regions that were drawn or edited stay.
        Returns the bands that are to accept again
        """
        value = float(value)
        if not 0 < value <= 1:
            raise ValueError("The inner area of the panel must be above 0 and up to 1")
        if value == self.percent_inner_area:
            return []
        self.percent_inner_area = value
        self._automatic = {}
        again = [band for band in self.bands if self.origins[band] == 'accepted_automatic']
        for band in again:
            self.unaccept(band)
            self.saved.pop(band, None)
        return again

    def image_size(self, band=None):
        image = self.images[band or self.band]
        return (image.image.shape[1], image.image.shape[0])

    def automatic_polygon(self, band):
        """ Region found from the QR code in the undistorted image, or None """
        if band not in self._automatic:
            located = self.images[band].locate_reference_panel(self.percent_inner_area, self.sat_threshold)
            polygon = None
            if located is not None and located[2] == 'undistorted':
                polygon = [[int(x), int(y)] for x, y in located[0]]
            self._automatic[band] = polygon
        return self._automatic[band]

    def proposed_polygon(self, band):
        """ The region shown for a band: the accepted one, else the saved one, else the one found from the QR code """
        if self.polygons[band] is not None:
            return [list(vertex) for vertex in self.polygons[band]]
        if self.saved.get(band):
            return [list(vertex) for vertex in self.saved[band]]
        return [list(vertex) for vertex in (self.automatic_polygon(band) or [])]

    starting_polygon = proposed_polygon

    def state(self, band):
        """ 'accepted', 'pending' (there is a region to accept) or 'no_region' (it has to be drawn) """
        if self.decisions[band] == 'accepted':
            return 'accepted'
        return 'pending' if self.proposed_polygon(band) else 'no_region'

    def stats(self, band, polygon):
        """ Pixels, saturated pixels and variation (%) inside a polygon, or None while it is not a valid region """
        try:
            rct.validate_polygon(polygon, self.image_size(band), band)
        except ValueError:
            return None
        return self.images[band].panel_region_stats(polygon, self.sat_threshold)

    def accept(self, band, polygon=None, origin=None):
        """
        Accept a polygon for a band (default: the one that is shown); ValueError with the reason if it is not a valid region
        The origin is worked out when it is not given: 'accepted_automatic' if it is the region found from the QR code
        """
        if polygon is None:
            polygon = self.proposed_polygon(band)
        rct.validate_polygon(polygon, self.image_size(band), f"Band '{band}'")
        polygon = [[int(round(x)), int(round(y))] for x, y in polygon]
        if origin is None:
            origin = 'accepted_automatic' if polygon == self.automatic_polygon(band) else 'edited'
        self.polygons[band] = polygon
        self.decisions[band] = 'accepted'
        self.origins[band] = origin

    confirm = accept

    def unaccept(self, band):
        self.polygons[band] = None
        self.decisions[band] = None
        self.origins[band] = None

    def accept_all_automatic(self):
        """ Accept the region found from the QR code for every band that is not accepted; returns the bands without one """
        left = []
        for band in self.bands:
            if self.decisions[band] == 'accepted':
                continue
            automatic = self.automatic_polygon(band)
            if automatic:
                self.accept(band, automatic, origin='accepted_automatic')
            else:
                left.append(band)
        return left

    def go_to(self, index):
        self.index = max(0, min(len(self.bands) - 1, index))

    def accepted_count(self):
        return sum(decision == 'accepted' for decision in self.decisions.values())

    def is_complete(self):
        return all(decision == 'accepted' for decision in self.decisions.values())

    def next_not_accepted(self, after=None):
        """ Index of the next band (after the current one, going round) that is not accepted, or None """
        start = self.index if after is None else after
        for step in range(1, len(self.bands) + 1):
            index = (start + step) % len(self.bands)
            if self.decisions[self.bands[index]] != 'accepted':
                return index
        return None

    def result(self):
        """ The accepted regions {band: polygon}, with .origins """
        accepted = [band for band in self.bands if self.decisions[band] == 'accepted']
        return SelectionResult({band: self.polygons[band] for band in accepted},
                               {band: self.origins[band] for band in accepted if self.origins[band]})


class SelectionResults(dict):
    """ {capture: SelectionResult}, with .percent_inner_area: the inner area of the panel that the tool was left with """

    percent_inner_area = None


class CaptureSet:
    """
    The captures that can be looked at, and the session of each one (opened when it is chosen)
    Inputs: the names of the captures, a function that reads the images of a capture ({band: rct.Image}), the captures that
            must be fully accepted before the regions are saved (the panel captures), the regions and origins saved before
            for every capture ({capture: {band: ...}}) and images that were read already ({capture: {band: rct.Image}})
    """

    def __init__(self, names, load, required=(), saved=None, saved_origins=None, percent_inner_area=0.7,
                 sat_threshold=65000, preloaded=None, current=None):
        self.required = list(dict.fromkeys(required))
        self.names = self.required + [name for name in dict.fromkeys(names) if name not in self.required]
        if not self.names:
            raise ValueError("There is no capture to look at")
        self.load = load
        self.saved, self.saved_origins = saved or {}, saved_origins or {}
        self.initial_inner_area = self.percent_inner_area = percent_inner_area
        self.sat_threshold = sat_threshold
        self.sessions = {}
        for name, images in (preloaded or {}).items():
            self.sessions[name] = self._make(name, images)
        self.current = current if current in self.names else self.names[0]

    @classmethod
    def single(cls, session, name='capture'):
        """ A set with one capture whose session exists already """
        captures = cls([name], None, required=[name])
        captures.sessions[name] = session
        return captures

    def _make(self, name, images):
        session = SelectionSession(images, self.saved.get(name), self.initial_inner_area, self.sat_threshold,
                                   self.saved_origins.get(name))
        if self.percent_inner_area != self.initial_inner_area:           # the value was changed before it was opened
            session.set_inner_area(self.percent_inner_area)
        return session

    def set_inner_area(self, value):
        """
        Change the inner area of the panel in every capture (see SelectionSession.set_inner_area; the captures that are
        opened later use it too). Returns {capture: bands that are to accept again} for the captures where there are some
        """
        value = float(value)
        if not 0 < value <= 1:
            raise ValueError("The inner area of the panel must be above 0 and up to 1")
        self.percent_inner_area = value
        again = {name: session.set_inner_area(value) for name, session in self.sessions.items()}
        return {name: bands for name, bands in again.items() if bands}

    def session(self, name):
        """ The session of a capture; its images are read the first time (any error of the reading is raised) """
        if name not in self.sessions:
            if self.load is None:
                raise KeyError(name)
            self.sessions[name] = self._make(name, self.load(name))
        return self.sessions[name]

    def summary(self, name):
        if name not in self.sessions:
            return 'not opened'
        session = self.sessions[name]
        return f"{session.accepted_count()} of {len(session.bands)} accepted"

    def not_ready(self):
        """ The captures that must be accepted and are not: {capture: bands still to accept, or None if not opened} """
        missing = {}
        for name in self.required:
            session = self.sessions.get(name)
            if session is None:
                missing[name] = None
            elif not session.is_complete():
                missing[name] = [band for band in session.bands if session.decisions[band] != 'accepted']
        return missing

    def is_ready(self):
        return not self.not_ready()

    def result(self):
        """ {capture: SelectionResult} of the captures whose bands are all accepted (a SelectionResults) """
        results = SelectionResults({name: session.result() for name, session in self.sessions.items()
                                    if session.is_complete()})
        results.percent_inner_area = self.percent_inner_area
        return results


def count_wrapped_lines(measure, text, width):
    """ Lines that a text takes when it is wrapped at words in a width; measure(text) gives the width of a text """
    total = 0
    for paragraph in text.split('\n'):
        line = ''
        total += 1
        for word in paragraph.split(' '):
            candidate = f"{line} {word}" if line else word
            if line and measure(candidate) > width:
                total += 1
                line = word
            else:
                line = candidate
    return total


# ======================================================================================
# WINDOW
# ======================================================================================

class PanelSelectorWindow:
    """
    One window for the bands of a panel capture: the list of the bands, the image of the selected band with its region and
    the decisions. A band is accepted as it is shown, or edited in the same window (Edit region) and then confirmed.
    Use select_panel_regions() to run it
    """

    def __init__(self, root, source, title="Reference panel regions", start=None):
        import tkinter as tk
        from tkinter import ttk
        from PIL import Image as PILImage, ImageTk
        self.tk, self.ttk, self.PILImage, self.ImageTk = tk, ttk, PILImage, ImageTk
        self.root = root
        self.captures = CaptureSet.single(source) if isinstance(source, SelectionSession) else source
        self.session = self.captures.session(self.captures.current)
        self.band_names = list(self.session.bands)
        self.capture_labels = {}
        self.cancelled = True
        self.finished = False
        self.editing = False
        self.whole_image = False
        self.model = PolygonModel()
        self.view = ViewTransform()
        self.display = None
        self.photo = None
        self.drag_vertex = None
        self.pan_start = None
        self.fitted = None
        self.display_cache = {}
        self.buttons = {}
        self.root.title(title)
        self.root.geometry("1180x860")
        self.root.minsize(1060, 600)
        apply_dark_theme(self.root)
        self._build()
        hide_title_icon(self.root)
        self.show_band(self.session.index)

    # --- construction ---------------------------------------------------------------------------------------

    def _build(self):
        tk, ttk = self.tk, self.ttk
        top = ttk.Frame(self.root, padding=(12, 8))
        top.pack(fill='x')
        self.title_label = ttk.Label(top, style='Title.TLabel')
        self.title_label.pack(side='left')
        self.status_label = ttk.Label(top, style='Muted.TLabel')
        self.status_label.pack(side='right')
        self.capture_var = tk.StringVar()
        self.capture_box = ttk.Combobox(top, textvariable=self.capture_var, state='readonly', width=44)
        if len(self.captures.names) > 1:
            self.capture_box.pack(side='right', padx=(6, 12))
            ttk.Label(top, text='Capture', style='Muted.TLabel').pack(side='right')
        self.capture_box.bind('<<ComboboxSelected>>', lambda event: self._on_capture_chosen())
        self._hover(self.capture_box, 'capture')

        # The explanation takes the whole width and has the height of the longest explanation, so that pointing at an
        # element never changes the size of what is above it (which made the image blink)
        footer = ttk.Frame(self.root, padding=(12, 0, 12, 6))
        footer.pack(side='bottom', fill='x')
        footer.pack_propagate(False)
        self.footer = footer
        self.hint_label = ttk.Label(footer, text=DEFAULT_HINT, foreground=COLORS['foreground'], justify='left')
        self.hint_label.pack(anchor='w', fill='x')
        footer.configure(height=self.hint_height(1156))
        footer.bind('<Configure>', self._on_footer_configure)

        body = ttk.Frame(self.root)
        body.pack(fill='both', expand=True)
        side = ttk.Frame(body, padding=(12, 0, 0, 0))
        side.pack(side='left', fill='y')
        ttk.Label(side, text='Bands', style='Heading.TLabel').pack(anchor='w', pady=(0, 4))
        holder = ttk.Frame(side)
        holder.pack(fill='y', expand=True)
        self.band_list = tk.Listbox(holder, exportselection=False, activestyle='none', width=26, font=FONT,
                                    background=COLORS['panel'], foreground=COLORS['foreground'], borderwidth=0,
                                    highlightthickness=0, selectbackground=COLORS['accent'],
                                    selectforeground=COLORS['foreground'])
        scroll = ttk.Scrollbar(holder, command=self.band_list.yview)
        self.band_list.configure(yscrollcommand=scroll.set)
        self.band_list.pack(side='left', fill='y', expand=True)
        scroll.pack(side='right', fill='y')
        self.band_list.bind('<<ListboxSelect>>', self._on_band_chosen)
        self._hover(self.band_list, 'bands')

        main = ttk.Frame(body)
        main.pack(side='left', fill='both', expand=True)
        row_end = ttk.Frame(main, padding=(12, 2, 12, 8))
        row_end.pack(side='bottom', fill='x')
        row_band = ttk.Frame(main, padding=(12, 4, 12, 2))
        row_band.pack(side='bottom', fill='x')
        info = ttk.Frame(main, padding=(12, 6))
        info.pack(side='bottom', fill='x')
        self.stats_label = ttk.Label(info)
        self.stats_label.pack(anchor='w')
        self.message_label = ttk.Label(info, style='Muted.TLabel')
        self.message_label.pack(anchor='w')
        self.summary_label = ttk.Label(info, style='Muted.TLabel')
        self.summary_label.pack(anchor='w')

        self.canvas = tk.Canvas(main, background=COLORS['panel'], highlightthickness=0)
        self.canvas.pack(fill='both', expand=True, padx=12)
        self.canvas.bind('<Configure>', self._on_configure)
        self.canvas.bind('<Button-1>', self._on_press)
        self.canvas.bind('<B1-Motion>', self._on_drag)
        self.canvas.bind('<ButtonRelease-1>', self._on_release)
        self.canvas.bind('<Button-3>', self._on_remove)
        self.canvas.bind('<Button-2>', self._on_pan_start)
        self.canvas.bind('<B2-Motion>', self._on_pan_move)
        self.canvas.bind('<MouseWheel>', self._on_wheel)
        self._hover(self.canvas, 'canvas')

        # the band: moving between bands and deciding on this one; the tools of the drawing; the whole image
        for name, text, command in (('previous', 'Previous', self.previous), ('next', 'Next', self.next)):
            self.buttons[name] = ttk.Button(row_band, text=text, command=command)
            self.buttons[name].pack(side='left', padx=(0, 6))
        ttk.Separator(row_band, orient='vertical').pack(side='left', fill='y', padx=(6, 12), pady=2)
        self.buttons['accept'] = ttk.Button(row_band, text='Accept', command=self.accept_or_confirm)
        self.buttons['accept'].pack(side='left', padx=(0, 6))
        self.buttons['edit'] = ttk.Button(row_band, text='Edit region', command=self.toggle_edit)
        self.buttons['edit'].pack(side='left', padx=(0, 6))
        ttk.Separator(row_band, orient='vertical').pack(side='left', fill='y', padx=(6, 12), pady=2)
        for name, text, command in (('reset', 'Reset to automatic', self.reset), ('clear', 'Clear', self.clear)):
            self.buttons[name] = ttk.Button(row_band, text=text, command=command)
            self.buttons[name].pack(side='left', padx=(0, 6))
        # the capture: all the automatic regions, saving and leaving
        for name, text, command, side in (('cancel', 'Cancel', self.cancel, 'right'),
                                          ('finish', 'Save and close', self.finish, 'right'),
                                          ('accept_all', 'Accept all automatic regions', self.accept_all, 'left'),
                                          ('zoom', 'Whole image', self.toggle_zoom, 'left')):
            self.buttons[name] = ttk.Button(row_end, text=text, command=command)
            self.buttons[name].pack(side=side, padx=(0, 6) if side == 'left' else (6, 0))
        inner = ttk.Frame(row_end)                      # the inner area of the panel decides the region found from the QR code
        inner.pack(side='left', padx=(12, 0))
        ttk.Label(inner, text='Inner area of the panel', style='Muted.TLabel').pack(side='left', padx=(0, 6))
        self.inner_var = tk.StringVar(value=f"{self.captures.percent_inner_area:g}")
        self.inner_entry = ttk.Entry(inner, textvariable=self.inner_var, width=6)
        self.inner_entry.pack(side='left')
        self.buttons['inner'] = ttk.Button(inner, text='Apply', command=self.apply_inner_area)
        self.buttons['inner'].pack(side='left', padx=(6, 0))
        self.inner_entry.bind('<Return>', self._inner_return)
        for widget in (inner, self.inner_entry, self.buttons['inner']):
            self._hover(widget, 'inner')
        self._hover(self.buttons['previous'], 'previous')
        self._hover(self.buttons['next'], 'next')
        self._hover(self.buttons['accept'], lambda: 'confirm' if self.editing else 'accept')
        self._hover(self.buttons['edit'], lambda: 'discard' if self.editing else 'edit')
        for name in ('reset', 'clear', 'zoom', 'cancel', 'finish', 'accept_all'):
            self._hover(self.buttons[name], name)
        for sequence, command in (('<Return>', self._key_accept), ('<e>', self._key_edit), ('<r>', self._key_reset),
                                  ('<c>', self._key_clear), ('<BackSpace>', self._key_undo),
                                  ('<Left>', self._key_previous), ('<Right>', self._key_next),
                                  ('<Escape>', self._key_escape)):
            self.root.bind(sequence, lambda event, command=command: command())
        self.root.protocol('WM_DELETE_WINDOW', self.cancel)

    def hint_height(self, wrap):
        """ Height in pixels of the footer: the lines of the longest explanation when it is wrapped at wrap pixels """
        from tkinter import font as tkfont
        font = tkfont.Font(root=self.root, font=FONT)
        lines = max(count_wrapped_lines(font.measure, text, wrap) for text in [DEFAULT_HINT, *HINTS.values()])
        return lines * font.metrics('linespace') + 12

    def _on_footer_configure(self, event):
        wrap = max(event.width - 24, 200)
        self.hint_label.configure(wraplength=wrap)
        height = self.hint_height(wrap)
        if int(self.footer.cget('height')) != height:
            self.footer.configure(height=height)

    def _hover(self, widget, name):
        """ Show what an element does while the mouse is over it (name: the key of the hint, or a function that gives it) """
        widget.bind('<Enter>', lambda event: self.hint_label.configure(text=HINTS[name() if callable(name) else name]), add='+')
        widget.bind('<Leave>', lambda event: self.hint_label.configure(text=DEFAULT_HINT), add='+')

    # --- the bands and the captures -------------------------------------------------------------------------

    def _status_text(self):
        return f"{self.session.accepted_count()} of {len(self.session.bands)} bands accepted"

    def display_image(self, band):
        key = (self.captures.current, band)
        if key not in self.display_cache:
            self.display_cache[key] = self.session.images[band].display_image()
        return self.display_cache[key]

    def _capture_label(self, name):
        kind = 'panel, ' if name in self.captures.required else ''
        return f"{name}  ({kind}{self.captures.summary(name)})"

    def _refresh_capture_box(self):
        self.capture_labels = {self._capture_label(name): name for name in self.captures.names}
        self.capture_box.configure(values=list(self.capture_labels))
        self.capture_var.set(self._capture_label(self.captures.current))

    def _on_capture_chosen(self):
        name = self.capture_labels.get(self.capture_var.get())
        if name is not None:
            self.switch_capture(name)

    def switch_capture(self, name):
        """ Look at another capture (its images are read the first time). Returns False if it could not be opened """
        if name == self.captures.current:
            return True
        self.message(f"Reading {name}...", error=False)
        self.root.update_idletasks()
        try:
            session = self.captures.session(name)
            if session.bands != self.band_names:
                raise ValueError(f"{name} has the bands {session.bands}, not {self.band_names}")
        except Exception as error:
            self._refresh_capture_box()
            self.message(f"{name} could not be opened: {type(error).__name__}: {error}", error=True)
            return False
        self.captures.current = name
        self.session = session
        self.show_band(session.index)
        return True

    def _band_line(self, band):
        state = self.session.state(band)
        label = {'accepted': 'accepted', 'pending': 'to accept', 'no_region': 'no region'}[state]
        return f"{band}   ({label})", {'accepted': COLORS['ok'], 'pending': COLORS['foreground'],
                                       'no_region': COLORS['error']}[state]

    def _refresh_band_list(self):
        self.band_list.delete(0, 'end')
        for index, band in enumerate(self.band_names):
            text, color = self._band_line(band)
            self.band_list.insert('end', text)
            self.band_list.itemconfigure(index, foreground=color)
        self.band_list.selection_clear(0, 'end')
        self.band_list.selection_set(self.session.index)
        self.band_list.see(self.session.index)

    def _on_band_chosen(self, event):
        selection = self.band_list.curselection()
        if selection and selection[0] != self.session.index:
            self.show_band(selection[0])

    def show_band(self, band):
        """ Show a band (its name or number): its image, its region and what has been decided; drawing is turned off """
        index = band if isinstance(band, int) else self.band_names.index(band)
        self.session.go_to(index)
        self.editing = False
        self.display = self.PILImage.fromarray(self.display_image(self.session.band))
        self.model = PolygonModel(self.session.proposed_polygon(self.session.band))
        self.fitted = None
        self.whole_image = False
        self.refresh()

    def refresh(self):
        """ Everything that depends on the decisions: the list, the texts, the buttons and the drawing """
        session, band = self.session, self.session.band
        self.title_label.configure(text=f"Reference panel regions: {band} ({session.index + 1} of {len(self.band_names)})")
        self.status_label.configure(text=self._status_text())
        self._refresh_capture_box()
        self._refresh_band_list()
        state = session.state(band)
        if self.editing:
            text = "Drawing: click adds a vertex, drag moves one, right click removes it. Confirm accepts this polygon."
        elif state == 'accepted':
            origin = ORIGIN_TEXT.get(session.origins[band], '')
            text = "Accepted" + (f" ({origin})" if origin else '') + ". Edit region changes it; Accepted (undo) takes the acceptance back."
        elif state == 'pending':
            text = "Not accepted yet: Accept uses the region as shown; Edit region lets you change it first."
        else:
            text = "No region was found from the QR code: Edit region, then click the corners of the panel (3 or more clicks)."
        self.message(text, error=False)
        pending = [name for name in session.bands if session.decisions[name] != 'accepted']
        summary = (f"{self.captures.current}: {session.accepted_count()} of {len(session.bands)} bands accepted. " +
                   ("Every region is accepted." if not pending else f"Still to accept: {', '.join(pending)}."))
        missing = self.captures.not_ready()
        if len(self.captures.required) > 1 or (missing and list(missing) != [self.captures.current]):
            summary += (" Panel captures: " + ', '.join(f"{name} ({self.captures.summary(name)})"
                                                        for name in self.captures.required) + '.')
        if self.captures.is_ready():
            summary += " Save and close writes the accepted captures."
        if session.notes:
            summary += ' ' + ' '.join(session.notes)
        self.summary_label.configure(text=summary)
        self._update_buttons(state)
        self._update_stats()
        self._fit_if_possible()
        self._redraw()

    def _update_buttons(self, state):
        buttons = self.buttons
        editing = self.editing
        buttons['accept'].configure(text='Confirm edit' if editing else ('Accepted (undo)' if state == 'accepted' else 'Accept'))
        buttons['edit'].configure(text='Discard edit' if editing else 'Edit region')
        buttons['zoom'].configure(text='Zoom to region' if self.whole_image else 'Whole image')
        buttons['accept'].state(['disabled'] if (state == 'no_region' and not editing) else ['!disabled'])
        for name in ('reset', 'clear'):
            buttons[name].state(['!disabled'] if editing else ['disabled'])
        buttons['finish'].state(['!disabled'] if self.captures.is_ready() else ['disabled'])
        self.canvas.configure(cursor='crosshair' if editing else 'arrow')

    # --- the image ---------------------------------------------------------------------------------------------

    def _fit_if_possible(self):
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        key = (self.captures.current, self.session.band, self.whole_image)
        if width > 20 and height > 20 and self.fitted != key:
            size = self.session.image_size()
            polygons = [polygon for polygon in (self.session.proposed_polygon(self.session.band),
                                                self.session.automatic_polygon(self.session.band)) if polygon]
            if self.whole_image or not polygons:
                self.view.fit(size, (width, height))
            else:
                self.view.fit_box(tile_crop_box(polygons, size, width / height, margin=1.6, minimum=120), size, (width, height))
            self.fitted = key

    def toggle_zoom(self):
        self.whole_image = not self.whole_image
        self.fitted = None
        self._fit_if_possible()
        self._update_buttons(self.session.state(self.session.band))
        self._redraw()

    def _outline(self, polygon, color, **options):
        points = [coordinate for vertex in polygon for coordinate in self.view.to_canvas(vertex)]
        if len(polygon) >= 3:
            self.canvas.create_polygon(points, outline=color, fill='', **options)
        elif len(polygon) == 2:
            self.canvas.create_line(points, fill=color, **options)

    def _redraw(self):
        canvas, view = self.canvas, self.view
        width, height = max(self.canvas.winfo_width(), 1), max(self.canvas.winfo_height(), 1)
        canvas.delete('all')
        if self.display is not None:
            scale = view.scale
            data = (1 / scale, 0, -view.offset[0] / scale, 0, 1 / scale, -view.offset[1] / scale)
            shown = self.display.transform((width, height), self.PILImage.AFFINE, data, resample=self.PILImage.BILINEAR)
            self.photo = self.ImageTk.PhotoImage(shown)
            canvas.create_image(0, 0, image=self.photo, anchor='nw')
        session, band = self.session, self.session.band
        accepted = session.polygons[band] if session.decisions[band] == 'accepted' else None
        reference = accepted or session.saved.get(band)
        if self.editing:
            if reference:
                self._outline(reference, COLORS['ok'], width=2)               # accepted or saved before, to compare
            self._outline(self.model.vertices, COLORS['manual'], width=2)
        else:
            self._outline(self.model.vertices, COLORS['ok'] if accepted else COLORS['manual'], width=2)
        # the region found from the QR code is drawn last, so that it can be seen over an identical drawn one
        automatic = session.automatic_polygon(band)
        if automatic:
            self._outline(automatic, COLORS['automatic'], dash=(6, 4), width=2)
        if self.editing:
            for x, y in (view.to_canvas(vertex) for vertex in self.model.vertices):
                canvas.create_rectangle(x - 4, y - 4, x + 4, y + 4, outline=COLORS['manual'], fill=COLORS['background'])

    def _update_stats(self):
        vertices = self.model.vertices
        stats = self.session.stats(self.session.band, vertices) if len(vertices) >= 3 else None
        if stats is None:
            self.stats_label.configure(text="Region: no region yet" if not vertices else
                                       "Region: draw at least 3 vertices without crossing edges")
        else:
            self.stats_label.configure(
                text=f"Region: {stats['n_pixels']} pixels, {stats['n_saturated']} saturated, "
                     f"variation {stats['variation_percent']:.2f} % (lower is more uniform)")

    def message(self, text, error):
        self.message_label.configure(text=text, style='Error.TLabel' if error else 'Muted.TLabel')

    # --- the mouse -----------------------------------------------------------------------------------------------

    def _image_point(self, event):
        return self.view.to_image((event.x, event.y))

    def _on_configure(self, event):
        self._fit_if_possible()
        self._redraw()

    def _on_press(self, event):
        self.canvas.focus_set()
        if not self.editing:                                  # looking at it: the left button drags the image
            self.pan_start = (event.x, event.y)
            return
        point = self._image_point(event)
        radius = VERTEX_RADIUS_PIXELS / self.view.scale
        index = self.model.nearest_vertex(point, radius)
        if index is None:
            index = self.model.add(point, EDGE_DISTANCE_PIXELS / self.view.scale)
        self.drag_vertex = index
        self._redraw()

    def _on_drag(self, event):
        if not self.editing:
            self._on_pan_move(event)
        elif self.drag_vertex is not None:
            self.model.move(self.drag_vertex, self._image_point(event))
            self._redraw()

    def _on_release(self, event):
        self.drag_vertex = None
        self.pan_start = None
        if self.editing:
            self._update_stats()
            self._redraw()

    def _on_remove(self, event):
        if not self.editing:
            return
        index = self.model.nearest_vertex(self._image_point(event), VERTEX_RADIUS_PIXELS / self.view.scale)
        if index is not None:
            self.model.remove(index)
            self._update_stats()
            self._redraw()

    def _on_pan_start(self, event):
        self.pan_start = (event.x, event.y)

    def _on_pan_move(self, event):
        if self.pan_start is not None:
            self.view.pan(event.x - self.pan_start[0], event.y - self.pan_start[1])
            self.pan_start = (event.x, event.y)
            self._redraw()

    def _on_wheel(self, event):
        self.view.zoom_at((event.x, event.y), 1.25 if event.delta > 0 else 1 / 1.25)
        self._redraw()

    # --- the decisions -------------------------------------------------------------------------------------------

    def accept_or_confirm(self):
        """ Accept the region as it is shown (or, while editing, the polygon that was drawn) and go to the next band """
        session, band = self.session, self.session.band
        if self.editing:
            polygon = self.model.vertices
        elif session.decisions[band] == 'accepted':
            session.unaccept(band)                                 # pressed again: take the acceptance back
            self.refresh()
            return
        else:
            polygon = None
        try:
            session.accept(band, polygon)
        except ValueError as error:
            self.message(str(error), error=True)
            return
        following = session.next_not_accepted()
        if following is None:
            self.show_band(session.index)
        else:
            self.show_band(following)

    confirm = accept = accept_or_confirm

    def toggle_edit(self):
        """ Turn the drawing on, or off (discarding what was drawn and not confirmed) """
        self.editing = not self.editing
        self.model = PolygonModel(self.session.proposed_polygon(self.session.band))
        self.refresh()

    def edit_band(self, band):
        """ Show a band with the drawing turned on """
        self.show_band(band)
        self.toggle_edit()

    def accept_all(self):
        left = self.session.accept_all_automatic()
        self.show_band(self.session.index)
        if left:
            self.message(f"No region was found from the QR code for: {', '.join(left)}. Draw them with Edit region.", error=True)

    def _inner_return(self, event=None):
        """ Enter in the box of the inner area: apply it, and do not accept the band as Enter does elsewhere """
        self.apply_inner_area()
        return 'break'

    def apply_inner_area(self):
        """
        Use the inner area that is typed: the region found from the QR code is made again in every band and the regions
        that were accepted as found from it are to accept again (the ones that were drawn or edited stay)
        """
        text = self.inner_var.get().strip().replace(',', '.')
        try:
            value = float(text)
        except ValueError:
            value = None
        if value is None or not 0 < value <= 1:
            self.inner_var.set(f"{self.captures.percent_inner_area:g}")
            self.message("The inner area of the panel is a number above 0 and up to 1, for example 0.7.", error=True)
            return
        if value == self.captures.percent_inner_area:
            return
        if self.editing:
            self.inner_var.set(f"{self.captures.percent_inner_area:g}")
            self.message("Confirm or discard the edit before changing the inner area of the panel.", error=True)
            return
        self.message("Finding the regions again...", error=False)
        self.root.update_idletasks()
        again = self.captures.set_inner_area(value)
        self.inner_var.set(f"{value:g}")
        self.show_band(self.session.index)
        text = f"Inner area {value:g}: regions found again."              # short: the line is not wrapped
        count = sum(len(bands) for bands in again.values())
        if count:
            text += f" {count} accepted as found from the QR code are to accept again; drawn or edited ones stay."
        self.message(text, error=False)

    def previous(self):
        self.show_band(self.session.index - 1)

    def next(self):
        self.show_band(self.session.index + 1)

    def reset(self):
        if not self.editing:
            return
        self.model = PolygonModel(self.session.automatic_polygon(self.session.band) or [])
        self._update_stats()
        self._redraw()

    def clear(self):
        if not self.editing:
            return
        self.model.clear()
        self._update_stats()
        self._redraw()

    def undo(self):
        if not self.editing:
            return
        self.model.remove_last()
        self._update_stats()
        self._redraw()

    # --- keys ------------------------------------------------------------------------------------------------------

    def _typing(self):
        """ The keys that draw are left alone while a box that takes text has the focus """
        return self.root.focus_get() is not None and self.root.focus_get().winfo_class() in ('TCombobox', 'TEntry', 'Entry')

    def _key_accept(self):
        if not self._typing() and (self.editing or self.session.state(self.session.band) == 'pending'):
            self.accept_or_confirm()                       # Enter never takes an acceptance back

    def _key_edit(self):
        if not self._typing():
            self.toggle_edit()

    def _key_reset(self):
        if not self._typing():
            self.reset()

    def _key_clear(self):
        if not self._typing():
            self.clear()

    def _key_undo(self):
        if not self._typing():
            self.undo()

    def _key_previous(self):
        if not self._typing():
            self.previous()

    def _key_next(self):
        if not self._typing():
            self.next()

    def _key_escape(self):
        if self._typing():                                  # in a box that takes text: leave it, with its value as it was
            self.inner_var.set(f"{self.captures.percent_inner_area:g}")
            self.canvas.focus_set()
        elif self.editing:
            self.toggle_edit()
        else:
            self.cancel()

    # --- end -----------------------------------------------------------------------------------------------------

    def finish(self):
        missing = self.captures.not_ready()
        if missing:
            parts = [f"{name}: " + ('not opened' if bands is None else f"still to accept {', '.join(bands)}")
                     for name, bands in missing.items()]
            self.message("Accept every band of the panel captures first. " + '; '.join(parts), error=True)
            return
        self.cancelled = False
        self.finished = True
        self.root.destroy()

    def cancel(self):
        self.cancelled = True
        self.finished = True
        self.root.destroy()


def select_panel_regions(captures, load, required=None, saved=None, saved_origins=None, percent_inner_area=0.7,
                         sat_threshold=65000, parent=None, preloaded=None, current=None, start=None):
    """
    Open the window and wait for the user
    Inputs: the names of the captures that can be looked at, a function that reads the images of a capture, the panel
            captures that must be fully accepted (default: the first one), the regions and origins saved before for every
            capture (they are shown as accepted), the images that were read already and, when another window is open,
            that window (the window is then modal and opens above it); start is not used any more (kept so that callers work)
    Outputs: {capture: SelectionResult} for every capture whose bands are all accepted (the panel captures at least), or
             None if the user cancelled. It is a SelectionResults: its percent_inner_area is the inner area of the panel
             that the tool was left with (the one given, unless it was changed in the window)
    """
    import tkinter as tk
    required = list(captures[:1]) if required is None else list(required)
    capture_set = CaptureSet(captures, load, required, saved, saved_origins, percent_inner_area, sat_threshold,
                             preloaded, current)
    if parent is None:
        window_root = tk.Tk()
        window = PanelSelectorWindow(window_root, capture_set, start=start)
        window_root.mainloop()
    else:
        window_root = tk.Toplevel(parent)
        window_root.transient(parent)
        window = PanelSelectorWindow(window_root, capture_set, start=start)
        window_root.grab_set()
        parent.wait_window(window_root)
    return None if window.cancelled else capture_set.result()


def main(argv=None):
    from . import radiometric_workflow as wf
    parser = argparse.ArgumentParser(description="Check, accept and draw the reference panel region of each band")
    parser.add_argument('--config', help="configuration json file of the workflow (default: config/radiometric_inputs.json "
                                         "in --root if it exists, otherwise the defaults)")
    parser.add_argument('--root', default='.', help="folder from which the relative paths are resolved")
    parser.add_argument('--capture', help="capture to show first; it is added to the panel captures if it is not one "
                                          "(default: INPUTS.reference_panel_capture)")
    arguments = parser.parse_args(argv)

    config_file = Path(arguments.config) if arguments.config else wf.default_config_path(arguments.root)
    if arguments.config and not config_file.is_file():
        print(f"ERROR: configuration file not found: {config_file}", file=sys.stderr)
        return 2
    config = wf.load_config(config_file) if config_file.is_file() else wf.default_config()
    problems = wf.validate_config(config, arguments.root, regions_file_may_be_missing=True)    # this tool creates it
    if problems:
        for problem in problems:
            print(f"ERROR: {problem}", file=sys.stderr)
        return 1
    root = Path(arguments.root)
    required = list(dict.fromkeys(name for group in wf.panel_capture_groups(config) for name in group))
    if arguments.capture and arguments.capture not in required:
        required.insert(0, arguments.capture)
    first = arguments.capture or required[0]
    regions_path = wf.resolve_path(root, config['PANEL.manual_regions_file'] or
                                   Path(config['INPUTS.results_folder']) / 'panel_regions.json')
    images_folder = wf.images_source(config, root)
    names = rct.list_capture_names(images_folder, config['INPUTS.file_pattern'])
    preloaded = {}
    for name in required:
        print(f"Reading {name} ...", flush=True)
        preloaded[name] = wf.read_capture_images(config, root, name)
    saved = wf.load_panel_regions(regions_path) if regions_path.is_file() else {}
    origins = wf.load_panel_origins(regions_path) if regions_path.is_file() else {}
    selected = select_panel_regions(names, lambda name: wf.read_capture_images(config, root, name), required, saved,
                                    origins, config['PANEL.percent_inner_area'], config['PANEL.saturation_threshold'],
                                    preloaded=preloaded, current=first)
    if selected is None:
        print("Cancelled: nothing was saved.")
        return 1
    regions_path.parent.mkdir(parents=True, exist_ok=True)
    for name, result in selected.items():
        wf.save_panel_regions(regions_path, name, result, result.origins)
    print(f"PANEL_REGIONS_JSON={regions_path.resolve()}")
    if selected.percent_inner_area != config['PANEL.percent_inner_area']:
        print(f"The regions found from the QR code were made with an inner area of {selected.percent_inner_area:g}, not the "
              f"{config['PANEL.percent_inner_area']:g} of the configuration: set PANEL.percent_inner_area to it to get the "
              "same ones where a band has no accepted region.")
    if config['PANEL.manual_regions_file'] == '':
        print("Set PANEL.manual_regions_file to this file in the configuration to use it.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
