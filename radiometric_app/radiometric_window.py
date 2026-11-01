"""
Window for the radiometric correction workflow

The pages are built from radiometric_workflow.FIELDS (so a new key appears here without changes) and the work is done
by radiometric_workflow.run() in a separate thread, so the window stays responsive. RadiometricFrame builds its
widgets inside a ttk.Frame (its .widget): it can be placed in any window, for example as a page of the photogrammetric
workflow window.

    python -m radiometric_app.radiometric_window [--config config/radiometric_inputs.json] [--root folder]

Only the values that differ from the defaults are saved in the configuration file.

ConfigModel (the values being edited and their validation) has no user interface and is tested without a display.
"""

# Standard Library Imports
import argparse
import json
import os
import platform
import queue
import sys
import threading
import traceback
from pathlib import Path

# Local Imports
from . import camera_presets, environment
import radiometric_correction_tools as rct
from . import radiometric_workflow as wf
from .ui_theme import COLORS, apply_dark_theme, hide_title_icon
from .capture_picker import pick_captures
from .ui_widgets import BandTable, HelpPanel, NavTiles, ScrollablePage, model_from_rows, rows_from_model

DEFAULT_CONFIG_NAME = wf.DEFAULT_CONFIG_NAME

# The keys that the band table edits: the reflectance, center wavelength and bandwidth of each band that is used, and the
# bands that are in the files but are left out
BAND_KEYS = ('PANEL.reflectance_by_band', 'PANEL.band_center_wavelengths', 'PANEL.band_bandwidths',
             'INPUTS.ignored_bands')

# Pages of the window: key prefix, title. The last page ('RUN') is not made of fields
SECTIONS = [('INPUTS', 'Inputs'), ('PANEL', 'Reference panel'), ('CORRECTION', 'DLS'),
            ('RUN', 'Run and results')]

# The keys whose prefix is one of these are shown in the page (the DLS page has the correction and its checks), and
# the keys that are shown in another page than their prefix says
PAGE_TITLES = {'CORRECTION': 'DLS (Downwelling Light Sensor)'}          # where it is not the title in the list of pages
PAGE_PREFIXES = {'CORRECTION': ('CORRECTION', 'CHECKS')}
FIELD_PAGE = {'PANEL.panels_used': 'INPUTS', 'PANEL.panel_orientation': 'INPUTS'}

# The keys that hold the name of a capture, typed or chosen by looking at the captures of the images folder: the text
# of the choice of no capture, for the key that may be empty
CAPTURE_KEYS = {'INPUTS.reference_panel_capture': None,
                'INPUTS.reference_panel_capture_after': 'No panel capture after the flight'}   # kept for old configurations


# What the help column says when the pointer and the keyboard focus are not on a field: title and text of each page.
# The texts use the marks of ui_widgets.HelpPanel: '# ' sub-heading, '- ' list item, any other line is a paragraph
PAGE_HELP = {
    'INPUTS': ("Inputs",
               "Where the images are, which captures show the reference panel and which captures are corrected.\n"
               "# The steps\n"
               "- Choose the images folder and the results folder.\n"
               "- Choose what is written: reflectance with the panel (the usual), reflectance from the DLS only, or "
               "radiance.\n"
               "- Choose the captures that show the reference panel, before and after the flight, and check them.\n"
               "- Choose the captures to correct.\n"
               "- Then check the inputs and the regions of the panel in the Reference panel page."),
    'PANEL': ("Reference panel",
              "The bands of the camera, the known reflectance of the reference panel and how the panel is measured in its "
              "image.\n"
              "# In this page\n"
              "- Bands of the camera: the table of the bands, with the buttons to detect them from a capture or to fill them "
              "from a camera preset.\n"
              "- Reflectance of the panel: where the value of each band comes from.\n"
              "- Panel measurement: how the panel is measured in its image (the panel captures and how they are used are "
              "chosen in the Inputs page).\n"
              "- Reference panel regions: check the inputs, then check, accept or draw the region of the panel in each band."),
    'CORRECTION': ("DLS (Downwelling Light Sensor)", wf.GROUP_NOTES['Correction with the DLS'],
                   'Checks of the DLS data (limits)', wf.GROUP_NOTES['Checks of the DLS data (limits)']),
    'RUN': ("Run and results",
            "Check the inputs, run the correction and follow it in the log.\n"
            "The configuration is saved when the run starts, so the results can be traced to the values used."),
}

# Explanations of the parts of the pages that are not fields of the configuration
BAND_NOTE = ("One row per band of the camera.\n"
             "- Use: a band that is not used is in the files but is left out of the processing, for example a thermal band.\n"
             "- Band name: as written by the camera (XMP:BandName).\n"
             "- Center and bandwidth (nm): the spectral window of the band, used to compute the reflectance of the panel "
             "from its spectral response file.\n"
             "- Panel reflectance: the value provided with the panel for the band. It is typed here only when the source of "
             "the reflectance is 'Values typed in the table of the bands'.\n"
             "The values at the start are those of the panel that goes with the example images: use the ones of your panel.")
BAND_TOOLS_NOTE = ("- Add band: a new row at the end of the table.\n"
                   "- Detect from capture: reads the names and the spectral windows of the bands from the metadata of the "
                   "reference panel capture. The panel reflectances that were typed are kept for the bands that stay.\n"
                   "- Remove (in each row): deletes the row of the band.")
PRESET_NOTE = ("A camera preset fills the table of the bands, and the file name pattern, for a camera model.\n"
               "The presets come from the documentation of the manufacturer, not from images: only the MicaSense RedEdge-P "
               "was tested. Check the result against your own data, for example with Detect from capture.")
REGIONS_NOTE = ("- Check inputs: tells whether the folders, the panel capture and the bands are right, before anything is "
                "opened.\n"
                "- Check / draw panel regions: opens the panel capture band by band, with the region found from the QR code "
                "and the regions saved before. Accept each one there, or turn on the drawing (Edit region) when the region "
                "is wrong or the QR code cannot be read. Every band has to be accepted.\n"
                "The tool also has the inner area of the panel, which decides the region that is found from the QR code: "
                "what it is set to when the tool is closed with Save and close is kept here.\n"
                "The regions are saved in the file above; if it is empty, in panel_regions.json in the results folder, which "
                "is then filled in above.")
PYTHON_NOTE = ("The window runs in the Python shown first and cannot change it. The Python chosen here is the one that is "
               "proposed in the dialog that opens before the window and that the launchers use the next time; it is saved "
               "in config/python_environment.json, not in the configuration or in the profiles.\n"
               "- Check this Python: runs it to see its version, whether it can open a window, and which packages and "
               "ExifTool it has, and what to install if something is missing.\n"
               "- Use it for the next launches: saves it, after trying it (Python 3.10 or newer, with working Tkinter).")
RUN_NOTES = {
    'buttons': ("Check inputs, Check environment, Run, Open results folder",
                "- Check inputs: tells whether everything the correction needs is in place, without running it.\n"
                "- Check environment: lists the Python, Tkinter, the Python packages (with their versions) and ExifTool of "
                "this computer, and what to install when something is missing.\n"
                "- Run: saves the configuration and corrects the captures in the background; the window stays usable.\n"
                "- Open results folder: shows the corrected images and the provenance records."),
    'log': ("Log", "What the correction did: errors, flags raised by the checks of the DLS data, and the files written."),
}
PROFILE_NOTES = {
    'save': ("Update saved configuration", "Replaces the saved configuration, the file shown at the left (the one that the "
                                           "window reads when it opens), with what is in the window, so that the window "
                                           "opens with these values the next time. Only the values that differ from the "
                                           "defaults are written. It is not tied to a project or to a results folder: the "
                                           "settings of each run are in its results folder (radiometric_result.json), and a "
                                           "profile keeps a setup under a name. It is done by itself when you run the "
                                           "correction, so the button is for keeping changes without running."),
    'save_profile': ("Save profile...", "A profile is a named copy of all the values, for a camera or a flight, kept "
                     "apart from the working configuration. This button writes every value in the window in a json file "
                     "that you name (the config/profiles folder is proposed); the working configuration does not change.\n"
                     "Running does not make a profile, but every run writes the complete configuration that it used in "
                     "radiometric_result.json (and in the provenance record of each capture), next to the results: Load "
                     "profile... reads those too."),
    'load_profile': ("Load profile...", "Replaces the values in the window with those of a json file; they become the "
                     "working configuration when you save or run.\n"
                     "# Files that can be loaded\n"
                     "- A profile, written with Save profile... (in config/profiles).\n"
                     "- radiometric_result.json of a past run, in its results folder: the settings that the run used, its "
                     "folders included (the results folder then is that of the run).\n"
                     "- <capture>_provenance.json, in the same folder: the same settings, kept with each corrected "
                     "capture.\n"
                     "- The saved configuration (config/radiometric_inputs.json).\n"
                     "# Not a profile\n"
                     "- panel_regions.json holds the panel regions drawn in each band: choose it in 'Panel regions drawn by "
                     "hand' (Reference panel page).\n"
                     "A file with keys that this version of the program does not know is not loaded, and the keys are "
                     "named."),
}

# What each output uses: the reflectance from the DLS alone only compares the panel with the DLS (the panel captures and
# their measurement stay, the choice of the panels does not), and the radiance uses neither the panel nor the DLS
PANEL_CHOICE_KEYS = ('PANEL.panels_used', 'PANEL.panel_orientation')
PANEL_KEYS = ('INPUTS.reference_panel_capture', 'PANEL.reflectance_source', 'PANEL.spectral_file',
              'PANEL.percent_inner_area', 'PANEL.saturation_threshold', 'PANEL.manual_regions_file') + PANEL_CHOICE_KEYS
DLS_LIMIT_KEYS = ('CHECKS.band_ratio_tolerance', 'CHECKS.ratio_range_min', 'CHECKS.ratio_range_max',
                  'CHECKS.max_sun_angle_discrepancy_deg', 'CHECKS.jump_threshold', 'CHECKS.panel_drift_tolerance',
                  'CHECKS.panel_illumination_tolerance')
PANEL_CHECK_NOTE = ("Measures all the panel captures listed, without correcting anything, and shows them in a table: the "
                    "moment of each one (before or after the flight, from its time), its heading against the sun, and how "
                    "far it is from the other captures of its moment (the largest band). A capture that departs more than "
                    "the spread limit is flagged. The limit is set at the top of the table, and it is saved with the "
                    "configuration and used by the run (CHECKS.panel_capture_spread_tolerance).\n"
                    "This limit is for captures of the same moment, a few seconds apart, so it is small (2 %). 'Light of "
                    "the panel: largest difference' (15 %) is another check: it compares the light of each image with "
                    "that of the nearest panel capture.\n"
                    "Leave out the selected removes them from the panel captures and adds them to the captures to leave "
                    "out.")

# The fields that show live information in the help column, and the information
DETAILS = {'INPUTS.image_captures': 'captures_details', 'INPUTS.file_pattern': 'pattern_details',
           'PANEL.spectral_file': 'spectral_details', 'INPUTS.exiftool_path': 'exiftool_details'}


def field_help(field):
    """ What the help column says about a field """
    return field['help'].rstrip('\n')


def page_of(key):
    """ The page where a key is shown """
    if key in FIELD_PAGE:
        return FIELD_PAGE[key]
    prefix = key.split('.')[0]
    for page, _ in SECTIONS:
        if prefix in PAGE_PREFIXES.get(page, (page,)):
            return page
    return prefix


# ======================================================================================
# VALUES BEING EDITED (NO USER INTERFACE)
# ======================================================================================

class ConfigModel:
    """
    The text typed in each field and its validation. Values are kept as typed ('raw') and converted when asked,
    so that a half-typed number never loses what the user wrote
    """

    def __init__(self, saved=None):
        self.fields = {field['key']: field for field in wf.FIELDS}
        merged = wf.merge_config(saved or {})        # unknown keys raise wf.ConfigError
        self.raw = {key: self.to_raw(self.fields[key], merged[key]) for key in self.fields}

    @staticmethod
    def to_raw(field, value):
        if field['kind'] == 'band_values':
            return {band: str(number) for band, number in value.items()}
        if field['kind'] == 'bool':
            return bool(value)
        if field['kind'] == 'folders':
            return rct.FOLDER_SEPARATOR.join(rct.folder_list(value))
        if field['kind'] == 'capture_folders':
            return json.dumps(value) if value else ''
        return str(value)

    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_text(encoding='utf-8')))

    def get(self, key):
        return self.raw[key]

    def set(self, key, raw):
        if key not in self.fields:
            raise KeyError(key)
        self.raw[key] = raw

    def set_band(self, key, band, text):
        self.raw[key][band] = text

    @staticmethod
    def parse(field, raw):
        """ Returns (value, None) or (None, message) """
        kind = field['kind']
        try:
            if kind == 'bool':
                return bool(raw), None
            if kind in ('folder', 'file', 'text', 'choice'):
                return str(raw).strip(), None
            if kind == 'folders':                        # one folder is saved as a path, several as a list
                folders = rct.folder_list(raw)
                return (folders[0] if len(folders) == 1 else folders or ''), None
            if kind == 'capture_folders':
                try:
                    value = json.loads(raw) if str(raw).strip() else {}
                except ValueError:
                    value = None
                if not isinstance(value, dict) or not all(isinstance(item, str) for item in value.values()):
                    return None, 'must be a dictionary {capture name: folder}'
                return value, None
            if kind == 'float':
                return float(raw), None
            if kind == 'int':
                number = float(raw)
                if number != int(number):
                    raise ValueError
                return int(number), None
            if kind == 'band_values':
                values, problems = {}, []
                for band, text in raw.items():
                    try:
                        values[band] = float(text)
                    except ValueError:
                        problems.append(band)
                if problems:
                    return None, f"not a number for {', '.join(problems)}"
                return values, None
        except (TypeError, ValueError):
            return None, 'must be an integer' if kind == 'int' else 'must be a number'
        return None, f"unknown kind {kind}"

    def config(self):
        """ Returns (configuration with the values that could be read, {key: message} of those that could not) """
        config, errors = wf.default_config(), {}
        for key, field in self.fields.items():
            value, message = self.parse(field, self.raw[key])
            if message:
                errors[key] = message
            else:
                config[key] = value
        # The typed reflectances are not used when the spectral file is the source: what cannot be read there is not an
        # error, and a placeholder (0.5) takes its place in the configuration
        if config['PANEL.reflectance_source'] == 'spectral_file' and 'PANEL.reflectance_by_band' in errors:
            del errors['PANEL.reflectance_by_band']
            config['PANEL.reflectance_by_band'] = {band: self._usable(text)
                                                    for band, text in self.raw['PANEL.reflectance_by_band'].items()}
        return config, errors

    @staticmethod
    def _usable(text):
        try:
            value = float(text)
        except ValueError:
            return 0.5
        return value if 0 < value <= 1 else 0.5

    def overrides(self):
        """ Only the valid values that differ from the defaults """
        config, errors = self.config()
        defaults = wf.default_config()
        return {key: value for key, value in config.items() if key not in errors and value != defaults[key]}

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_text(json.dumps(self.overrides(), indent=2), encoding='utf-8')
        os.replace(temporary, path)
        return path


# ======================================================================================
# WINDOW
# ======================================================================================

class RadiometricFrame:
    """
    The pages of the workflow. It is created with a parent widget and packed by the caller:
        frame = RadiometricFrame(parent, config_path, root); frame.widget.pack(fill='both', expand=True)
    """

    def __init__(self, parent, config_path=None, root='.'):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.parent = parent
        self.root_folder = Path(root).resolve()
        self.config_path = Path(config_path) if config_path else wf.default_config_path(self.root_folder)
        self.load_message = None
        try:
            self.model = ConfigModel.load(self.config_path) if self.config_path.is_file() else ConfigModel()
        except (ValueError, OSError) as error:
            self.model = ConfigModel()
            self.load_message = f"{self.config_path} was not used: {error}"
        merged = self._merge_old_panel_field()
        self.variables = {}
        self.entries = {}
        self.pages = {}
        self.groups = {}                  # last heading written in each page
        self.source_widgets = {'values': [], 'spectral_file': []}
        self.band_table = None
        self.buttons = {}
        self.field_widgets = {}                      # the widgets of each field, to enable and disable them
        self.orientation_names = {}                  # the text shown in the list of the orientations -> value saved
        self.orientation_after = None
        self.output_forced = None                    # the DLS correction set by the user before an output forced it
        self.forcing_output = False
        self.syncing = False
        self.loading_values = False                  # a profile is being loaded: its values are not corrected
        self.results_before = None                   # the results folder before the last change (see _follow_results_folder)
        self.detecting = False
        self.presets, self.preset_problems = camera_presets.load_presets(camera_presets.preset_folders(self.root_folder))
        self.summary_after = None
        self.events = queue.Queue()
        self.running = False
        self.last_result = None
        self.widget = ttk.Frame(parent)
        self._after_id = None
        self.widget.bind('<Destroy>', lambda event: self.close() if event.widget is self.widget else None)
        self._build()
        self.wrapped_labels = []
        self._collect_wrapped(self.content)
        self.content.bind('<Configure>', self._rewrap)
        self._table_from_model()
        self._show_page(SECTIONS[0][0])
        self._apply_source_state()
        self._update_spectral_summary()
        self.preview_after = None
        self._update_pattern_preview()
        self.exiftool_after = self.exiftool_state = None
        self._check_exiftool()
        self._apply_output_state()
        self._schedule_orientations(10)
        if self.load_message:
            self.log(self.load_message, 'error')
        if merged:
            self.log(merged)
        self._after_id = self.parent.after(100, self._poll)

    def close(self):
        """ Stop looking for events of the worker threads (done when the frame is destroyed) """
        if getattr(self, 'exiftool_after', None) is not None:
            try:
                self.parent.after_cancel(self.exiftool_after)
            except self.tk.TclError:
                pass
            self.exiftool_after = None
        if getattr(self, 'preview_after', None) is not None:
            try:
                self.parent.after_cancel(self.preview_after)
            except self.tk.TclError:
                pass
            self.preview_after = None
        if getattr(self, 'orientation_after', None) is not None:
            try:
                self.parent.after_cancel(self.orientation_after)
            except self.tk.TclError:
                pass
            self.orientation_after = None
        if self.summary_after is not None:
            try:
                self.parent.after_cancel(self.summary_after)
            except self.tk.TclError:
                pass
            self.summary_after = None
        if self._after_id is not None:
            try:
                self.parent.after_cancel(self._after_id)
            except self.tk.TclError:
                pass
            self._after_id = None

    # --- construction ---------------------------------------------------------------------------------------

    def _build(self):
        tk, ttk = self.tk, self.ttk
        bottom = ttk.Frame(self.widget, padding=(12, 6, 12, 10))
        bottom.pack(side='bottom', fill='x')
        save_button = ttk.Button(bottom, text='Update saved configuration', command=self.save)
        save_button.pack(side='right')
        self.buttons['save'] = save_button
        save_profile = ttk.Button(bottom, text='Save profile...', command=self.save_profile_as)
        save_profile.pack(side='right', padx=(0, 6))
        load_profile = ttk.Button(bottom, text='Load profile...', command=self.load_profile_from)
        load_profile.pack(side='right', padx=(0, 6))
        self.buttons['save_profile'], self.buttons['load_profile'] = save_profile, load_profile
        self.status = ttk.Label(bottom, style='Muted.TLabel', text=f"Configuration file: {self.config_path}")
        self.status.pack(side='left')                       # packed last: a long path gives way to the buttons

        body = ttk.Frame(self.widget)
        body.pack(fill='both', expand=True)
        self.navigation = NavTiles(body, [title for _, title in SECTIONS], self._on_navigate)
        self.navigation.widget.pack(side='left', fill='y', padx=(12, 0), pady=(12, 6))
        self.panes = tk.PanedWindow(body, orient='horizontal', sashwidth=6, sashrelief='flat', borderwidth=0,
                                    background=COLORS['border'], opaqueresize=True)
        self.panes.pack(side='left', fill='both', expand=True, padx=(8, 12), pady=(12, 6))
        self.content = ttk.Frame(self.panes, padding=(12, 10, 10, 0))
        self.help_panel = HelpPanel(self.panes)
        self.panes.add(self.content, minsize=520, stretch='always')                # the line between the two can be dragged
        self.panes.add(self.help_panel.widget, width=380, minsize=220, stretch='never')

        self.scroll_pages = {}
        for prefix, title in SECTIONS:
            if prefix == 'RUN':
                page = ttk.Frame(self.content)
                self.pages[prefix] = page
            else:
                scrolling = ScrollablePage(self.content)           # the pages of fields scroll when they are long
                self.scroll_pages[prefix] = scrolling
                self.pages[prefix] = scrolling.widget
                page = scrolling.body
            ttk.Label(page, text=PAGE_TITLES.get(prefix, title), style='Title.TLabel').pack(anchor='w', pady=(0, 10))
            if prefix == 'RUN':
                self._build_run_page(page)
            else:
                if prefix == 'PANEL':
                    self._build_band_section(page)
                for field in wf.FIELDS:
                    if page_of(field['key']) == prefix:
                        self._build_field(page, field)
                if prefix == 'INPUTS':
                    self._build_python_section(page)
        for key in BAND_KEYS:                                     # these keys live in the band table
            self.variables[key] = self.band_table
        self.entries['PANEL.panels_used'].bind('<<ComboboxSelected>>', lambda event: self._panels_selected())
        chosen = self.variables['PANEL.panels_used'].get()
        self.panels_used_ok = chosen if self._panels_used_allowed(chosen) else 'first'
        self.results_before = self.model.get('INPUTS.results_folder')
        self.help_panel.describe(save_button, *PROFILE_NOTES['save'])
        self.help_panel.describe(save_profile, *PROFILE_NOTES['save_profile'])
        self.help_panel.describe(load_profile, *PROFILE_NOTES['load_profile'])
        self.help_panel.follow(self.parent.winfo_toplevel(), self.widget)       # the pages and the buttons at the bottom

    def _build_band_section(self, page):
        tk, ttk = self.tk, self.ttk
        heading = ttk.Label(page, text='Bands of the camera', style='Heading.TLabel')
        heading.pack(anchor='w', pady=(6, 2))
        ttk.Separator(page).pack(fill='x', pady=(0, 8))
        self.band_table = BandTable(page, self._table_changed)
        self.band_table.widget.pack(anchor='w')
        self.help_panel.describe(heading, 'Bands of the camera', BAND_NOTE)
        self.help_panel.describe(self.band_table.widget, 'Bands of the camera', BAND_NOTE)
        tools = ttk.Frame(page)
        self.help_panel.describe(tools, 'Add band, Detect from capture', BAND_TOOLS_NOTE)
        tools.pack(fill='x', pady=(8, 0))
        self.band_buttons = {}
        self.band_buttons['add'] = ttk.Button(tools, text='Add band', command=lambda: self.band_table.add_row())
        self.band_buttons['detect'] = ttk.Button(tools, text='Detect from capture', command=self.detect_bands)
        self.band_buttons['add'].pack(side='left')
        self.band_buttons['detect'].pack(side='left', padx=(6, 0))
        preset_line = ttk.Frame(page)                         # its own line: the descriptions of the presets are long
        preset_line.pack(fill='x', pady=(8, 0))
        self.help_panel.describe(preset_line, 'Camera preset', PRESET_NOTE)
        ttk.Label(preset_line, text='Camera preset', style='Muted.TLabel').pack(side='left', padx=(0, 6))
        self.preset_labels = {preset['label']: key for key, preset in sorted(self.presets.items(),
                                                                              key=lambda item: item[1]['label'])}
        self.preset_var = tk.StringVar()
        self.band_buttons['preset'] = ttk.Button(preset_line, text='Apply preset', command=self.apply_selected_preset)
        self.band_buttons['preset'].pack(side='right', padx=(6, 0))
        self.preset_box = ttk.Combobox(preset_line, textvariable=self.preset_var, values=list(self.preset_labels),
                                       state='readonly')
        self.preset_box.pack(side='left', fill='x', expand=True)
        self.band_info = ttk.Label(page, text='', style='Muted.TLabel', wraplength=900, justify='left')
        self.band_info.pack(anchor='w', pady=(6, 0))
        if self.preset_problems:
            self.band_info.configure(text='Camera presets not used: ' + '; '.join(self.preset_problems), style='Error.TLabel')

    def _build_hidden(self, field):
        """ A key that is edited in a dialog: its value lives in variables, without widgets on the page """
        tk = self.tk
        key = field['key']
        if field['kind'] == 'band_values':
            self.variables[key] = {}
            for band, text in self.model.get(key).items():
                variable = tk.StringVar(value=text)
                variable.trace_add('write', lambda *_, key=key, band=band, variable=variable:
                                   self._band_changed(key, band, variable.get()))
                self.variables[key][band] = variable
        else:
            variable = tk.StringVar(value=self.model.get(key))
            variable.trace_add('write', lambda *_, key=key, variable=variable: self._changed(key, variable.get()))
            self.variables[key] = variable

    def _build_field(self, page, field):
        tk, ttk = self.tk, self.ttk
        key, kind = field['key'], field['kind']
        if key in BAND_KEYS:
            return                                           # edited in the band table
        group = field.get('group')
        if group and group != self.groups.get(page):
            self.groups[page] = group
            heading = ttk.Label(page, text=group, style='Heading.TLabel')
            heading.pack(anchor='w', pady=(10, 2))
            ttk.Separator(page).pack(fill='x', pady=(0, 8))
            if wf.GROUP_NOTES.get(group):
                self.help_panel.describe(heading, group, wf.GROUP_NOTES[group])
        if field.get('dialog'):
            self._build_hidden(field)
            return
        row = ttk.Frame(page)
        row.pack(fill='x', pady=(0, 9))
        details = getattr(self, DETAILS[key]) if key in DETAILS else None
        self.help_panel.describe(row, field['label'], field_help(field), '', '', details)
        widgets = self.field_widgets.setdefault(key, [])
        if key == 'PANEL.panel_orientation':
            self._build_orientation_field(row, field, widgets)
            self._build_panel_check(page)
            return
        if kind == 'bool':
            variable = tk.BooleanVar(value=self.model.get(key))
            check = ttk.Checkbutton(row, text=field['label'], variable=variable)
            check.pack(anchor='w')
            widgets.append(check)
            variable.trace_add('write', lambda *_, key=key, variable=variable: self._changed(key, variable.get()))
            self.variables[key] = variable
        else:
            ttk.Label(row, text=field['label'], style='Heading.TLabel').pack(anchor='w')
            if kind == 'band_values':
                grid = ttk.Frame(row)
                grid.pack(anchor='w', pady=(3, 0))
                self.variables[key], self.entries[key] = {}, {}
                for column, (band, text) in enumerate(self.model.get(key).items()):
                    ttk.Label(grid, text=band, style='Muted.TLabel').grid(row=0, column=column, padx=(0, 8), sticky='w')
                    variable = tk.StringVar(value=text)
                    entry = ttk.Entry(grid, textvariable=variable, width=9)
                    entry.grid(row=1, column=column, padx=(0, 8))
                    variable.trace_add('write', lambda *_, key=key, band=band, variable=variable:
                                       self._band_changed(key, band, variable.get()))
                    self.variables[key][band], self.entries[key][band] = variable, entry
                    widgets.append(entry)
                    if key == 'PANEL.reflectance_by_band':
                        self.source_widgets['values'].append(entry)
            else:
                line = ttk.Frame(row)
                line.pack(fill='x', pady=(3, 0))
                variable = tk.StringVar(value=self.model.get(key))
                if kind == 'choice' and field.get('labels'):      # a list of descriptions that stands for the stored names
                    labels = field['labels']
                    shown = tk.StringVar(value=labels[variable.get()] if variable.get() in labels else variable.get())
                    entry = ttk.Combobox(line, textvariable=shown, values=[labels[name] for name in field['choices']],
                                         state='readonly', width=max(38, max(map(len, labels.values())) + 2))
                    names = {label: name for name, label in labels.items()}
                    shown.trace_add('write', lambda *_, shown=shown, variable=variable, names=names:
                                    variable.set(names.get(shown.get(), shown.get())))
                    variable.trace_add('write', lambda *_, shown=shown, variable=variable, labels=labels:
                                       shown.set(labels.get(variable.get(), variable.get())))
                elif kind == 'choice':
                    entry = ttk.Combobox(line, textvariable=variable, values=field['choices'], state='readonly',
                                         width=18)
                else:
                    entry = ttk.Entry(line, textvariable=variable, width=12 if kind in ('float', 'int') else 56)
                entry.pack(side='left', fill='x', expand=kind in ('folder', 'folders', 'file', 'text'))
                widgets.append(entry)
                if kind in ('folder', 'folders', 'file'):
                    command = self.browse_spectrum if key == 'PANEL.spectral_file' else (
                        lambda key=key, kind=kind: self._browse(key, kind))
                    browse = ttk.Button(line, text='Browse...', command=command)
                    browse.pack(side='left', padx=(6, 0))
                    widgets.append(browse)
                    if key == 'PANEL.spectral_file':
                        importer = ttk.Button(line, text='Preview and format...', command=self.import_spectrum)
                        importer.pack(side='left', padx=(6, 0))
                        widgets.append(importer)
                        self.source_widgets['spectral_file'].extend([entry, browse, importer])
                if key in CAPTURE_KEYS:
                    select = ttk.Button(line, text='Select...', command=lambda key=key: self.choose_panel_capture(key))
                    select.pack(side='left', padx=(6, 0))
                    widgets.append(select)
                if key in ('INPUTS.image_captures', 'INPUTS.excluded_captures'):
                    ttk.Button(line, text='Select...', command=lambda key=key: self.choose_captures(key)).pack(
                        side='left', padx=(6, 0))
                if key == 'INPUTS.file_pattern':
                    ttk.Button(line, text='Suggest from files', command=self.suggest_pattern).pack(side='left', padx=(6, 0))
                variable.trace_add('write', lambda *_, key=key, variable=variable: self._changed(key, variable.get()))
                self.variables[key], self.entries[key] = variable, entry
        if key == 'INPUTS.images_folder':                           # only what is wrong is written on the page
            self.images_summary = ttk.Label(row, text='', style='Error.TLabel', wraplength=760, justify='left')
        if key == 'INPUTS.image_captures':
            self.captures_summary = ttk.Label(row, text='', style='Error.TLabel', wraplength=760, justify='left')
        if key == 'INPUTS.file_pattern':
            self.pattern_preview = ttk.Label(row, text='', style='Error.TLabel', wraplength=760, justify='left')
        if key == 'PANEL.spectral_file':
            self.spectral_summary = ttk.Label(row, text='', style='Error.TLabel', wraplength=760, justify='left')
        if key == 'INPUTS.exiftool_path':
            self.exiftool_summary = ttk.Label(row, text='', style='Error.TLabel', wraplength=760, justify='left')
        if key == 'PANEL.manual_regions_file':
            self._build_regions_tool(page)

    def _build_python_section(self, page):
        """ The Python that is running this window, and the one to use the next time: choose it, check it, save it """
        tk, ttk = self.tk, self.ttk
        heading = ttk.Label(page, text='Python environment', style='Heading.TLabel')
        heading.pack(anchor='w', pady=(10, 2))
        ttk.Separator(page).pack(fill='x', pady=(0, 8))
        holder = ttk.Frame(page)
        holder.pack(fill='x', pady=(0, 9))
        for widget in (heading, holder):
            self.help_panel.describe(widget, 'Python environment', PYTHON_NOTE)
        ttk.Label(holder, text='Python running this window', style='Heading.TLabel').pack(anchor='w')
        ttk.Label(holder, text=f"Python {platform.python_version()}: {sys.executable}", style='Muted.TLabel').pack(
            anchor='w', pady=(2, 8))
        ttk.Label(holder, text='Python for the next launches', style='Heading.TLabel').pack(anchor='w')
        line = ttk.Frame(holder)
        line.pack(fill='x', pady=(3, 0))
        self.python_var = tk.StringVar(value=environment.read_python_environment(self.root_folder).get('python')
                                       or sys.executable)
        ttk.Entry(line, textvariable=self.python_var).pack(side='left', fill='x', expand=True)
        ttk.Button(line, text='Browse...', command=self.browse_python).pack(side='left', padx=(6, 0))
        buttons = ttk.Frame(holder)
        buttons.pack(anchor='w', pady=(6, 0))
        self.buttons['python_check'] = ttk.Button(buttons, text='Check this Python', command=lambda: self._python_task('check'))
        self.buttons['python_use'] = ttk.Button(buttons, text='Use it for the next launches',
                                                command=lambda: self._python_task('use'))
        self.buttons['python_check'].pack(side='left')
        self.buttons['python_use'].pack(side='left', padx=(6, 0))
        self.python_status = ttk.Label(holder, text='', style='Muted.TLabel', wraplength=760, justify='left')
        self.python_status.pack(anchor='w', pady=(6, 0))

    def browse_python(self):
        from tkinter import filedialog
        current = Path(self.python_var.get().strip().strip('"'))
        chosen = filedialog.askopenfilename(
            title="Select Python executable (python.exe)", initialdir=current.parent if current.parent.is_dir() else None,
            filetypes=(("Python executable", "python.exe pythonw.exe python python3"), ("All files", "*.*")))
        if chosen:
            self.python_var.set(chosen)

    def _python_task(self, action):
        """ Check the Python of the field ('check') or try it and save it for the next launches ('use'), in another thread """
        path, root = self.python_var.get().strip().strip('"'), self.root_folder
        self.python_status.configure(text='Running this Python...', style='Muted.TLabel')
        for name in ('python_check', 'python_use'):
            self.buttons[name].state(['disabled'])

        def work():
            try:
                if action == 'use':
                    payload = environment.remember_python(root, path)
                else:
                    report = environment.inspect_python(path)
                    items = (environment.check_python_environment(path, root)
                             if report['python_compatible'] and report['tkinter']['ok'] else None)
                    payload = {'report': report, 'items': items}
            except Exception as error:                      # what is wrong is shown, not raised in the thread
                payload = error
            self.events.put(('python', (action, payload)))
        threading.Thread(target=work, daemon=True).start()

    def _show_python(self, action, payload):
        for name in ('python_check', 'python_use'):
            self.buttons[name].state(['!disabled'])
        if isinstance(payload, Exception):
            self.python_status.configure(text=str(payload), style='Error.TLabel')
            return
        if action == 'use':
            self.python_status.configure(
                text=f"Saved: the next launches use {payload['executable']} (Python {payload['version']}). "
                     "This window keeps the Python that it is running.", style='Ok.TLabel')
            return
        report, items = payload['report'], payload['items']
        lines = [f"Python {report['version']}: {report['executable']}",
                 "Tkinter: " + ('works' if report['tkinter']['ok'] else 'does not work - ' + report['tkinter']['detail'])]
        if not report['python_compatible']:
            lines.append("The program needs Python 3.10 or newer.")
        found = environment.problems(items) if items is not None else []
        if items is not None and not found:
            lines.append("All the packages and ExifTool are in place.")
        elif found:
            lines += [f"{item['name']}: {item['status']}" + (f" ({item['detail']})" if item['detail'] else '') for item in found]
            lines.append(environment.install_text(items, report['executable']))
        good = items is not None and not found
        self.python_status.configure(text='\n'.join(lines), style='Ok.TLabel' if good else 'Error.TLabel')

    def _build_regions_tool(self, page):
        """ The buttons to check the inputs and to open the tool that checks, accepts and draws the panel regions """
        ttk = self.ttk
        holder = ttk.Frame(page)
        holder.pack(fill='x', pady=(0, 9))
        self.help_panel.describe(holder, 'Check inputs, Check / draw panel regions', REGIONS_NOTE)
        line = ttk.Frame(holder)
        line.pack(anchor='w')
        self.buttons['check_here'] = ttk.Button(line, text='Check inputs', command=self.check_inputs_here)
        self.buttons['check_here'].pack(side='left')
        self.buttons['regions'] = ttk.Button(line, text='Check / draw panel regions...', command=self.draw_regions)
        self.buttons['regions'].pack(side='left', padx=(6, 0))
        self.field_widgets.setdefault('PANEL.manual_regions_file', []).append(self.buttons['regions'])
        self.inputs_status = ttk.Label(holder, text='', style='Muted.TLabel', wraplength=760, justify='left')
        self.inputs_status.pack(anchor='w', pady=(6, 0))

    def _build_run_page(self, page):
        tk, ttk = self.tk, self.ttk
        buttons = ttk.Frame(page)
        buttons.pack(fill='x')
        self.help_panel.describe(buttons, *RUN_NOTES['buttons'])
        for name, text, command in (('check', 'Check inputs', self.check_inputs),
                                    ('environment', 'Check environment', self.check_environment),
                                    ('run', 'Run', self.start_run),
                                    ('open', 'Open results folder', self.open_results)):
            self.buttons[name] = ttk.Button(buttons, text=text, command=command)
            self.buttons[name].pack(side='left', padx=(0, 6))
        self.progress = ttk.Progressbar(page, mode='determinate', maximum=1)
        self.progress.pack(fill='x', pady=(10, 4))
        self.summary = ttk.Label(page, text='', style='Muted.TLabel', wraplength=900, justify='left')
        self.summary.pack(anchor='w', pady=(0, 6))
        frame = ttk.Frame(page)
        frame.pack(fill='both', expand=True, pady=(0, 6))
        self.help_panel.describe(frame, *RUN_NOTES['log'])
        self.log_text = tk.Text(frame, height=14, wrap='word', state='disabled', borderwidth=0, highlightthickness=0,
                                background=COLORS['panel'], foreground=COLORS['foreground'], font=('Consolas', 10),
                                insertbackground=COLORS['foreground'])
        scroll = ttk.Scrollbar(frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.log_text.pack(side='left', fill='both', expand=True)
        for tag, color in (('error', COLORS['error']), ('ok', COLORS['ok']), ('muted', COLORS['muted']),
                           ('flag', COLORS['automatic'])):
            self.log_text.tag_configure(tag, foreground=color)

    # --- pages and values -----------------------------------------------------------------------------------

    def _collect_wrapped(self, widget):
        """ The labels that wrap their text (explanations, notes, messages): they follow the width of the page """
        for child in widget.winfo_children():
            if child.winfo_class() == 'TLabel' and int(float(child.cget('wraplength') or 0)) > 0:
                self.wrapped_labels.append(child)
            self._collect_wrapped(child)

    def _rewrap(self, event=None):
        width = max(self.content.winfo_width() - 100, 300)           # the margins, the scroll bar and the indentation
        for label in self.wrapped_labels:
            label.configure(wraplength=width)

    def _on_navigate(self, index):
        self._show_page(SECTIONS[index][0])

    def _show_page(self, prefix):
        for page in self.pages.values():
            page.pack_forget()
        self.pages[prefix].pack(fill='both', expand=True)
        self.navigation.select([name for name, _ in SECTIONS].index(prefix))
        self.help_panel.set_default(*PAGE_HELP[prefix])

    def show_page(self, prefix):
        self._show_page(prefix)

    def _mark(self, key):
        if key not in self.entries:
            return
        field = self.model.fields[key]
        _, message = self.model.parse(field, self.model.get(key))
        if field['kind'] == 'band_values':
            for band, entry in self.entries[key].items():
                _, bad = self.model.parse({'kind': 'float'}, self.model.get(key)[band])
                entry.configure(style='Invalid.TEntry' if bad else 'TEntry')
        elif key in self.entries:
            widget = self.entries[key]
            base = widget.winfo_class()                       # TEntry, or TCombobox (which has its arrow)
            widget.configure(style=('Invalid.' if message else '') + base)

    def _changed(self, key, raw):
        self.model.set(key, raw)
        self._mark(key)
        self._after_change(key)

    def _band_changed(self, key, band, text):
        self.model.set_band(key, band, text)
        self._mark(key)
        self._after_change(key)

    def _table_changed(self):
        """ The user edited the band table: write its rows in the model """
        if self.syncing:
            return
        reflectance, centers, widths, ignored = model_from_rows(self.band_table.get_rows())
        self.model.raw['PANEL.reflectance_by_band'] = reflectance
        self.model.raw['PANEL.band_center_wavelengths'] = centers
        self.model.raw['PANEL.band_bandwidths'] = widths
        self.model.raw['INPUTS.ignored_bands'] = ignored
        problems = [message for _, _, message in self.band_table.problems()]
        if problems:
            self.band_info.configure(text='; '.join(problems), style='Error.TLabel')
        elif self.band_info.cget('style') == 'Error.TLabel':
            self.band_info.configure(text='', style='Muted.TLabel')
        self._after_change('PANEL.band_center_wavelengths')

    def _table_from_model(self):
        """ Show in the band table what the model has (after loading a profile, for example) """
        self.syncing = True
        try:
            self.band_table.set_rows(rows_from_model(
                self.model.get('PANEL.reflectance_by_band'), self.model.get('PANEL.band_center_wavelengths'),
                self.model.get('PANEL.band_bandwidths'), self.model.get('INPUTS.ignored_bands')))
        finally:
            self.syncing = False

    # --- the captures of the images folder ---------------------------------------------------------------------

    def capture_names(self, with_empty=False):
        """ Names of the captures that the file name pattern finds in the images folder ([] if it cannot be read) """
        try:
            names = rct.list_capture_names(self._images_source(), self.model.get('INPUTS.file_pattern'))
        except (OSError, ValueError):
            names = []
        return ([''] if with_empty else []) + names

    def _picker_arguments(self):
        """ What the window of captures needs: the names, the files of each capture and the bands that can be shown """
        config, _ = self.model.config()
        folder = wf.images_source(config, self.root_folder)
        pattern = config['INPUTS.file_pattern']
        names = self.capture_names()
        bands = [row['name'].strip() for row in self.band_table.get_rows() if row['process'] and row['name'].strip()]
        exiftool = wf.exiftool_executable(config, self.root_folder)
        ignored = wf.capture_names(config['INPUTS.ignored_bands'])
        positions = {}

        def paths_of(name):
            return rct.find_capture_paths(folder, name, pattern)

        def resolve_band(band):
            """ The position of a band among the files of a capture, from the metadata of the first capture """
            if not positions:
                paths = paths_of(names[0])
                for found, (path, _) in rct.read_band_files(paths, None, exiftool, ignored).items():
                    positions[found] = paths.index(path)
            return positions[band]

        return {'paths_of': paths_of, 'bands': bands, 'resolve_band': resolve_band}

    def choose_panel_capture(self, key):
        """
        Choose the captures of the panel of one moment (one, or several taken one after the other) by looking at the
        captures of the images folder (all of them, before and after the flight, in one field)
        """
        names = self.capture_names()
        if not names:
            self.log('There are no captures in the images folder (check the folder and the file name pattern).', 'error')
            return
        chosen = pick_captures(self.parent.winfo_toplevel(), names, selected=wf.capture_names(self.model.get(key)),
                               multiple=True, title='Select the panel captures (before and after the flight)',
                               message='Click every capture of the panel, before and after the flight',
                               **self._picker_arguments())
        if chosen is not None:
            self.set_value(key, ', '.join(chosen))

    def choose_captures(self, key='INPUTS.image_captures'):
        """
        Choose the captures to correct, or the captures to leave out, by looking at those of the folder (the panel captures
        are not offered; nor are the captures left out when choosing those to correct)
        """
        hidden = set(self.panel_capture_names())
        if key == 'INPUTS.image_captures':
            hidden |= set(wf.capture_names(self.model.get('INPUTS.excluded_captures')))
        names = [name for name in self.capture_names() if name not in hidden]
        if not names:
            self.log('There are no other captures in the images folder (check the folder and the file name pattern).', 'error')
            return
        title, message = (('Select the captures to correct', 'Click the captures to correct')
                          if key == 'INPUTS.image_captures' else
                          ('Select the captures to leave out', 'Click the captures that must not be corrected'))
        chosen = pick_captures(self.parent.winfo_toplevel(), names, selected=wf.capture_names(self.model.get(key)),
                               multiple=True, title=title, message=message, **self._picker_arguments())
        if chosen is not None:
            self.set_value(key, ', '.join(chosen))

    def panel_capture_names(self):
        """ The names of the panel captures typed in the Inputs page """
        return [name for key in CAPTURE_KEYS for name in wf.capture_names(self.model.get(key))]

    def suggest_pattern(self):
        try:
            pattern = rct.suggest_file_pattern(path.name for _, path in self._images_source().files())
        except OSError as error:
            self.log(f"The images folder cannot be read: {error}", 'error')
            return
        if pattern is None:
            self.log('No pattern could be suggested from the file names (expected names such as IMG_0005_1.tif).', 'error')
            return
        self.set_value('INPUTS.file_pattern', pattern)
        self.log(f"File name pattern suggested from the files of the images folder: {pattern}")

    @staticmethod
    def _problem(label, text):
        """ Show a problem under a field (the label is only on the page while there is one) """
        label.configure(text=text, style='Error.TLabel')
        if text and not label.winfo_manager():
            label.pack(anchor='w', pady=(3, 0))
        elif not text and label.winfo_manager():
            label.pack_forget()

    @staticmethod
    def _names_list(names, limit=12):
        shown = [f"- {name}" for name in names[:limit]]
        if len(names) > limit:
            shown.append(f"- and {len(names) - limit} more")
        return '\n'.join(shown)

    def _update_images_summary(self):
        """ What is wrong with the images folders: one that does not exist, a repeated capture used without a choice """
        if not hasattr(self, 'images_summary'):
            return
        config, _ = self.model.config()
        source = wf.images_source(config, self.root_folder)
        problems = [f"Folder not found: {folder}." for folder in source.folders if not folder.is_dir()]
        if not problems:
            try:
                names = rct.list_capture_names(source, config['INPUTS.file_pattern'])
                before, after = wf.panel_capture_groups(config)
                waiting = rct.unresolved_captures(source, config['INPUTS.file_pattern'],
                                                  before + after + wf.captures_to_correct(config, names))
            except (OSError, ValueError):
                waiting = []
            if waiting:
                problems.append(f"In more than one folder, without the copy to use chosen (Browse..., Review repeated "
                                f"captures): {', '.join(waiting)}.")
        self._problem(self.images_summary, ' '.join(problems))

    def _update_captures_summary(self):
        """ The captures that will be corrected (the typed ones, or all but the panel captures when the field is empty) """
        if not hasattr(self, 'captures_summary'):
            return
        typed = wf.capture_names(self.model.get('INPUTS.image_captures'))
        left_out = wf.capture_names(self.model.get('INPUTS.excluded_captures'))
        available = self.capture_names()
        problem = ''
        if typed:
            missing = [name for name in typed + left_out if name not in available]
            both = [name for name in typed if name in left_out]
            self._captures_info = (f"{len(typed)} capture(s) will be corrected:", typed)
            if missing:
                problem = f"Not found in the images folder: {', '.join(missing)}."
            if both:
                problem += f" Both selected and left out: {', '.join(both)}."
        else:
            others = [name for name in available if name not in set(self.panel_capture_names()) | set(left_out)]
            missing = [name for name in left_out if name not in available]
            self._captures_info = (f"All the other captures of the folder will be corrected ({len(others)}):", others)
            if missing:
                problem = f"Captures to leave out not found in the images folder: {', '.join(missing)}."
            if not others:
                problem = 'There are no captures to correct in the images folder.'
        problem = problem.strip()
        self._problem(self.captures_summary, problem)
        self.help_panel.refresh()

    def _check_exiftool(self):
        """ Look for ExifTool and pyexiftool now; the version is asked a moment later, in another thread """
        if self.exiftool_after is not None:
            self.parent.after_cancel(self.exiftool_after)
            self.exiftool_after = None
        config, _ = self.model.config()
        path, source = wf.find_exiftool(config, self.root_folder)
        package = wf.pyexiftool_installed()
        self.exiftool_state = {'path': path, 'source': source, 'package': package, 'version': None}
        problems = []
        if path is None:
            problems.append(f"ExifTool was not found in {source}: install it (see the explanation), or choose it here.")
        if not package:
            problems.append('The Python package pyexiftool is not installed (pip install pyexiftool).')
        self._problem(self.exiftool_summary, ' '.join(problems))
        self.help_panel.refresh()
        if path is not None:
            self.exiftool_after = self.parent.after(800, lambda path=path: threading.Thread(
                target=lambda: self.events.put(('exiftool', (path, wf.exiftool_version(path)))), daemon=True).start())

    def exiftool_details(self):
        """ (title, text) for the help column: the ExifTool that was found """
        state = getattr(self, 'exiftool_state', None)
        if not state:
            return None
        if state['path'] is None:
            lines = [f"ExifTool was not found in {state['source']}."]
        else:
            version = state['version'] or ('checking...' if state['version'] is None else 'it cannot be run')
            lines = [f"- Program: {state['path']}", f"- Found through: {state['source']}", f"- Version: {version}"]
        lines.append('- Python package pyexiftool: ' + ('installed' if state['package'] else 'not installed'))
        return 'ExifTool on this computer', '\n'.join(lines)

    def captures_details(self):
        """ (title, text) for the help column: the captures that will be corrected """
        info = getattr(self, '_captures_info', None)
        if not info or not info[1]:
            return None
        return 'Captures to correct', info[0] + '\n' + self._names_list(info[1])

    def pattern_details(self):
        """ (title, text) for the help column: how the file name pattern reads the images folder """
        return getattr(self, '_pattern_info', None)

    def spectral_details(self):
        """ (title, text) for the help column: the reflectance that the spectral file gives in each band """
        return getattr(self, '_spectral_info', None)

    def _update_pattern_preview(self):
        """ Read the images folder with the file name pattern """
        self.preview_after = None
        self._update_images_summary()
        self._update_captures_summary()
        if not hasattr(self, 'pattern_preview'):
            return
        pattern = self.model.get('INPUTS.file_pattern')
        folder = self._images_source()
        self._pattern_info = None
        try:
            template = rct.pattern_template(pattern)
            info = rct.preview_file_pattern(folder, pattern)
        except ValueError as error:
            self._problem(self.pattern_preview, str(error))
            self.help_panel.refresh()
            return
        except OSError:
            self._problem(self.pattern_preview, 'The images folder cannot be read.')
            self.help_panel.refresh()
            return
        sizes = ', '.join(f"{count} with {files} files" for files, count in info['files_per_capture'].items())
        lines = [f"The pattern reads names written as {template}.",
                 f"- {info['matched']} of {info['files']} files match, in {info['captures']} captures"
                 + (f" ({sizes})" if info['captures'] else '') + '.']
        if info['examples']:
            lines.append('# Examples')
            lines.extend(f"- {file} -> capture {capture}" + ('' if index is None else f", file {index}")
                         for file, capture, index in info['examples'])
        if info['unmatched']:
            lines.append('# Not matched, for example')
            lines.extend(f"- {file}" for file in info['unmatched'])
        self._pattern_info = ('In the images folder', '\n'.join(lines))
        self._problem(self.pattern_preview, '' if info['matched'] else 'No file of the images folder matches the pattern.')
        self.help_panel.refresh()

    def _panels_used_allowed(self, name):
        """ The choices of the panel by the light read by the DLS need the DLS correction to be on """
        use = self.variables.get('CORRECTION.use_dls')
        return name not in rct.PANELS_BY_ILLUMINATION or use is None or bool(use.get())

    def _panels_selected(self):
        """
        A choice made in the list of the panel selection approach. Those by the light use the DLS data, so choosing one turns
        the DLS correction on (the status line says so)
        """
        chosen = self.variables['PANEL.panels_used']
        self.panels_used_ok = chosen.get()
        if self._panels_used_allowed(chosen.get()):
            return
        self.variables['CORRECTION.use_dls'].set(True)
        label = self.model.fields['PANEL.panels_used']['labels'][chosen.get()]
        self.status.configure(text=f"'{label}' uses the DLS data: 'Correct illumination changes with the DLS' (DLS page) was "
                                   "turned on.", style='Muted.TLabel')

    def _dls_correction_changed(self):
        """ Turning the DLS correction off while a choice that needs it is made goes back to 'first', and says so """
        chosen = self.variables.get('PANEL.panels_used')
        if self.loading_values or self.forcing_output or chosen is None or self._panels_used_allowed(chosen.get()):
            return                                   # a profile keeps its values: the check of the inputs reports them
        previous = self.model.fields['PANEL.panels_used']['labels'][chosen.get()]
        chosen.set('first')
        self.status.configure(text=f"The panel selection approach was '{previous}' and is now 'Before the flight only': it needs the "
                                   "DLS correction, which is off.", style='Muted.TLabel')

    def _follow_results_folder(self):
        """
        The file of the panel regions that is in the results folder (panel_regions.json, the place that the tool proposes)
        goes with the folder when it is changed; a file that is somewhere else, or has another name, stays as it is
        """
        new = self.model.get('INPUTS.results_folder')
        old, self.results_before = self.results_before, new
        regions = self.model.get('PANEL.manual_regions_file')
        if self.loading_values or not (old and new and regions):
            return

        def default(folder):
            return wf.resolve_path(self.root_folder, folder) / 'panel_regions.json'
        if default(old) == wf.resolve_path(self.root_folder, regions):
            self.set_value('PANEL.manual_regions_file', str(Path(new) / 'panel_regions.json'))

    def _after_change(self, key):
        if key == 'INPUTS.results_folder':
            self._follow_results_folder()
        if key == 'CORRECTION.use_dls':
            self._dls_correction_changed()
        if key == 'PANEL.panels_used' and self._panels_used_allowed(self.model.get(key)):
            self.panels_used_ok = self.model.get(key)
        if key in ('INPUTS.images_folder', 'INPUTS.repeated_capture_folders', 'INPUTS.file_pattern',
                   'INPUTS.image_captures', 'INPUTS.excluded_captures', 'INPUTS.reference_panel_capture',
                   'INPUTS.reference_panel_capture_after'):
            if getattr(self, 'preview_after', None) is not None:
                self.parent.after_cancel(self.preview_after)
            self.preview_after = self.parent.after(400, self._update_pattern_preview)
        if key == 'INPUTS.exiftool_path':
            self._check_exiftool()
        if key in ('INPUTS.images_folder', 'INPUTS.repeated_capture_folders', 'INPUTS.file_pattern',
                   'INPUTS.reference_panel_capture', 'INPUTS.exiftool_path', 'CHECKS.heading_offset_deg'):
            self._schedule_orientations()
        if key == 'INPUTS.output_product':
            self._apply_output_state()
        if key == 'PANEL.reflectance_source':
            self._apply_source_state()
        if key.startswith('PANEL.spectral_') or key in ('PANEL.band_center_wavelengths', 'PANEL.band_bandwidths',
                                                       'PANEL.reflectance_source'):
            if self.summary_after is not None:
                self.parent.after_cancel(self.summary_after)
            self.summary_after = self.parent.after(400, self._update_spectral_summary)

    # --- the panel captures: the old second field, the orientations, the check, the fields of each output ----------

    def _merge_old_panel_field(self):
        """
        The panel captures after the flight of an earlier configuration (INPUTS.reference_panel_capture_after) join the
        panel captures, where the moments are found from the times. Outputs: the text for the log, or None
        """
        after = wf.capture_names(self.model.get('INPUTS.reference_panel_capture_after'))
        if not after:
            return None
        captures = wf.capture_names(self.model.get('INPUTS.reference_panel_capture'))
        merged = captures + [name for name in after if name not in captures]
        if 'INPUTS.reference_panel_capture' in self.variables:
            self.set_value('INPUTS.reference_panel_capture', ', '.join(merged), replace=True)
            self.set_value('INPUTS.reference_panel_capture_after', '', replace=True)
        else:
            self.model.set('INPUTS.reference_panel_capture', ', '.join(merged))
            self.model.set('INPUTS.reference_panel_capture_after', '')
        return (f"The panel captures after the flight of this configuration ({', '.join(after)}) are now in Reference panel "
                f"captures: their moments are found from their times.")

    def _build_orientation_field(self, row, field, widgets):
        """ The orientation: a list of the orientations found among the panel captures (see _show_orientations) """
        tk, ttk = self.tk, self.ttk
        key = field['key']
        ttk.Label(row, text=field['label'], style='Heading.TLabel').pack(anchor='w')
        line = ttk.Frame(row)
        line.pack(fill='x', pady=(3, 0))
        variable = tk.StringVar(value=self.model.get(key))
        shown = tk.StringVar()
        entry = ttk.Combobox(line, textvariable=shown, state='readonly', width=70)
        entry.pack(side='left')
        self.orientation_info = ttk.Label(row, text='', style='Muted.TLabel', wraplength=760, justify='left')
        self.orientation_info.pack(anchor='w', pady=(3, 0))
        shown.trace_add('write', lambda *_: variable.set(self.orientation_names.get(shown.get(), variable.get())))
        variable.trace_add('write', lambda *_: (self._changed(key, variable.get()), self._show_orientation_value()))
        self.variables[key], self.entries[key], self.orientation_shown = variable, entry, shown
        widgets.append(entry)
        self._show_orientations(None, None)

    def _build_panel_check(self, page):
        """ The button that measures the panel captures and shows them in a table """
        ttk = self.ttk
        holder = ttk.Frame(page)
        holder.pack(fill='x', pady=(0, 9))
        self.help_panel.describe(holder, 'Check panel captures', PANEL_CHECK_NOTE)
        self.buttons['panel_check'] = ttk.Button(holder, text='Check panel captures...', command=self.check_panel_captures)
        self.buttons['panel_check'].pack(anchor='w')
        self.field_widgets.setdefault('INPUTS.reference_panel_capture', []).append(self.buttons['panel_check'])

    @staticmethod
    def _orientation_value_text(value):
        return 'All the panel captures' if str(value).strip().lower() == 'all' else f"Heading {value} deg"

    def _show_orientations(self, groups, problem):
        """
        Fill the list of the orientations: all the panel captures, and one entry for each orientation found (groups: see
        wf.orientation_groups; None while they are not known). With one orientation only, the list is not needed
        """
        self.orientation_names = {}
        value = self.model.get('PANEL.panel_orientation')
        if groups and len(groups) > 1:
            counts = '; '.join(f"heading {group['heading']}: {len(group['captures'])}" for group in groups)
            self.orientation_names[f"All the panel captures ({counts})"] = 'all'
        else:
            self.orientation_names['All the panel captures'] = 'all'
        for group in groups or []:
            self.orientation_names[f"Only {wf.orientation_label(group)}"] = str(group['heading'])
        if value not in self.orientation_names.values():
            self.orientation_names[f"{self._orientation_value_text(value)} (not among these panel captures)"] = value
        self.entries['PANEL.panel_orientation'].configure(values=list(self.orientation_names))
        self._show_orientation_value()
        if problem:
            text = f"The orientations of the panel captures are not known: {problem}"
        elif groups is None:
            text = ''
        elif len(groups) == 1:
            text = f"All the panel captures have one orientation: {wf.orientation_label(groups[0])}."
        else:
            text = f"{len(groups)} orientations among the panel captures."
        self.orientation_info.configure(text=text)
        self.orientation_single = groups is not None and len(groups) <= 1 and value == 'all'
        self._apply_output_state()

    def _show_orientation_value(self):
        value = self.model.get('PANEL.panel_orientation')
        for text, name in self.orientation_names.items():
            if name == value:
                if self.orientation_shown.get() != text:
                    self.orientation_shown.set(text)
                return

    def _schedule_orientations(self, delay=800):
        """ Read the headings of the panel captures a moment after their list changes, in another thread """
        if getattr(self, 'orientation_after', None) is not None:
            self.parent.after_cancel(self.orientation_after)
        self.orientation_after = self.parent.after(delay, self._start_orientations)

    def _start_orientations(self):
        self.orientation_after = None
        config, problems = self.model.config()
        if problems:
            return
        threading.Thread(target=self._orientations_worker, args=(config,), daemon=True).start()

    def _orientations_worker(self, config):
        """ The heading against the sun of each panel capture, from the metadata of its first file """
        try:
            folder = wf.images_source(config, self.root_folder)
            exiftool = wf.exiftool_executable(config, self.root_folder)
            headings = {}
            for name in wf.panel_capture_groups(config)[0]:
                paths = rct.find_capture_paths(folder, name, config['INPUTS.file_pattern'])
                metadata = rct._read_metadata(paths[:1], exiftool)[0]
                headings[name] = rct.heading_to_sun_deg(metadata, config['CHECKS.heading_offset_deg'])
            self.events.put(('orientations', (wf.orientation_groups(headings), None)))
        except Exception as error:
            self.events.put(('orientations', (None, f"{type(error).__name__}: {error}")))

    def check_panel_captures(self):
        """ Measure the panel captures without correcting anything, and show them in a table (in another thread) """
        config, problems = self.model.config()
        if not problems:
            problems = wf.validate_config(config, self.root_folder, regions_file_may_be_missing=True)
        if problems:
            for problem in problems:
                self.log(problem, 'error')
            self.status.configure(text='The panel captures cannot be checked: see the problems in the log.',
                                  style='Error.TLabel')
            return
        self.buttons['panel_check'].state(['disabled'])
        self.status.configure(text='Measuring the panel captures...', style='Muted.TLabel')

        def report(event):
            self.events.put(('panel_check_progress', event['message']))

        def work():
            try:
                self.events.put(('panel_check', wf.check_panel_captures(config, self.root_folder, report)))
            except Exception as error:
                self.events.put(('panel_check_failed', f"{type(error).__name__}: {error}"))
        threading.Thread(target=work, daemon=True).start()

    def _show_panel_check(self, rows, flags):
        """ The table of the panel captures, with the button that leaves out the selected ones """
        tk, ttk = self.tk, self.ttk
        self.status.configure(text=f"{len(rows)} panel capture(s) checked.", style='Muted.TLabel')
        window = tk.Toplevel(self.parent.winfo_toplevel())
        window.title('Panel captures')
        window.transient(self.parent.winfo_toplevel())
        window.configure(background=COLORS['background'])
        frame = ttk.Frame(window, padding=12)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text="Each capture against the others of its moment (before or after the flight), on what is "
                              "averaged, in the band where they differ most.", style='Muted.TLabel').pack(anchor='w')
        limit_line = ttk.Frame(frame)
        limit_line.pack(anchor='w', pady=(6, 8))
        ttk.Label(limit_line, text='Flag a capture that differs from the others of its moment by more than').pack(
            side='left')
        limit_text = tk.StringVar(window, f"{100 * self.model.config()[0]['CHECKS.panel_capture_spread_tolerance']:g}")
        ttk.Entry(limit_line, textvariable=limit_text, width=6).pack(side='left', padx=6)
        ttk.Label(limit_line, text='%  (saved with the configuration and used by the run)', style='Muted.TLabel').pack(side='left')
        columns = ('time', 'moment', 'heading', 'orientation', 'departure', 'flag')
        tree = ttk.Treeview(frame, columns=columns, selectmode='extended', height=min(max(len(rows), 4), 16))
        for column, title, width in (('#0', 'Capture', 110), ('time', 'Time', 80), ('moment', 'Moment', 70),
                                     ('heading', 'Heading', 70), ('orientation', 'Orientation', 170),
                                     ('departure', 'Largest departure (band)', 170), ('flag', 'Flag', 300)):
            tree.heading(column, text=title, anchor='w')
            tree.column(column, width=width, anchor='w', stretch=column == 'flag')
        tree.tag_configure('flagged', foreground=COLORS['automatic'])                 # the colour of the flags of the log
        for row in rows:
            departure = ('' if row['band'] is None else f"{100 * row['departure']:+.1f} % ({row['band']})"
                         if row['departure'] is not None else f"pair: {100 * row['spread']:.1f} % ({row['band']})")
            heading = '' if row['heading'] is None else f"{row['heading']:.0f} deg"
            tree.insert('', 'end', iid=row['capture'], text=row['capture'], tags=('flagged',) if row['flag'] else (),
                        values=(str(row['time'])[11:], row['moment'], heading, row['orientation'], departure, row['flag']))
        tree.pack(fill='both', expand=True)

        def reflag(*_):
            """ The flags under the limit typed (the captures are not read again); it becomes the limit of the run """
            try:
                limit = float(limit_text.get()) / 100
            except ValueError:
                return
            if limit <= 0:
                return
            for row in rows:
                if tree.exists(row['capture']):
                    flag = wf.panel_capture_flag(row, limit)
                    tree.set(row['capture'], 'flag', flag)
                    tree.item(row['capture'], tags=('flagged',) if flag else ())
            self.set_value('CHECKS.panel_capture_spread_tolerance', f"{limit:g}")
        limit_text.trace_add('write', reflag)
        self.panel_check_limit = limit_text
        alone = [f"{row['capture']} is the only panel capture {row['moment']} the flight: there is no other capture of "
                 "that moment to compare it with." for row in rows if row['alone']]
        other = [flag for flag in flags if not flag.startswith('Panel capture ')]
        if alone or other:
            notes = ttk.Label(frame, text='\n'.join(alone + other), style='Muted.TLabel', wraplength=900, justify='left')
            notes.pack(anchor='w', fill='x', pady=(6, 0))
            # a note takes one line while the window is wide enough for it
            frame.bind('<Configure>', lambda event: notes.configure(wraplength=max(300, event.width - 28)), add='+')
        line = ttk.Frame(frame)
        line.pack(fill='x', pady=(8, 0))

        def leave_out():
            chosen = list(tree.selection())
            if chosen:
                self.leave_out_panel_captures(chosen)
                for name in chosen:
                    tree.delete(name)
        ttk.Button(line, text='Close', command=window.destroy).pack(side='right')
        ttk.Button(line, text='Leave out the selected', command=leave_out).pack(side='right', padx=(0, 6))
        self.panel_check_window = window

    def leave_out_panel_captures(self, names):
        """ Remove captures from the panel captures, and add them to the captures to leave out (so they are not corrected) """
        captures = [name for name in wf.capture_names(self.model.get('INPUTS.reference_panel_capture')) if name not in names]
        excluded = wf.capture_names(self.model.get('INPUTS.excluded_captures'))
        self.set_value('INPUTS.reference_panel_capture', ', '.join(captures), replace=True)
        self.set_value('INPUTS.excluded_captures', ', '.join(excluded + [name for name in names if name not in excluded]),
                       replace=True)
        self.log(f"Left out of the panel captures: {', '.join(names)} (also added to the captures to leave out).")

    def _set_field_state(self, key, enabled):
        for widget in self.field_widgets.get(key, []):
            widget.state(['!disabled'] if enabled else ['disabled'])

    def _apply_output_state(self):
        """
        Only the fields that the chosen output uses can be edited: the reflectance from the DLS alone does not choose among
        the panels and always uses the DLS (its check box is on); the radiance uses neither the panel nor the DLS (its check
        box is off). The DLS correction chosen before is given back when the output is the panel reflectance again
        """
        if not self.field_widgets:
            return
        output = self.model.get('INPUTS.output_product')
        uses_panel, chooses_panels = output != 'radiance', output == 'panel_reflectance'
        for key in PANEL_KEYS:
            self._set_field_state(key, uses_panel and (chooses_panels or key not in PANEL_CHOICE_KEYS))
        if getattr(self, 'orientation_single', False):
            self._set_field_state('PANEL.panel_orientation', False)       # one orientation: nothing to choose
        for key in DLS_LIMIT_KEYS:
            self._set_field_state(key, output != 'radiance')
        self._set_field_state('CORRECTION.use_dls', chooses_panels)
        use = self.variables.get('CORRECTION.use_dls')
        if use is not None and not self.loading_values:
            self.forcing_output = True
            try:
                if chooses_panels and self.output_forced is not None:
                    use.set(self.output_forced)
                    self.output_forced = None
                elif not chooses_panels:
                    if self.output_forced is None:
                        self.output_forced = bool(use.get())
                    use.set(output == 'dls_reflectance')
            finally:
                self.forcing_output = False
        if uses_panel:
            self._apply_source_state()
        elif self.band_table is not None:
            self.band_table.set_reflectance_state(False)


    def _apply_source_state(self):
        """ Only the controls of the chosen source of the panel reflectance can be edited """
        source = self.model.get('PANEL.reflectance_source')
        usable = self.model.get('INPUTS.output_product') != 'radiance'
        for name, widgets in self.source_widgets.items():
            for widget in widgets:
                widget.state(['!disabled'] if name == source and usable else ['disabled'])
        if self.band_table is not None:
            self.band_table.set_reflectance_state(source == 'values' and
                                                 self.model.get('INPUTS.output_product') != 'radiance')

    def _update_spectral_summary(self):
        self.summary_after = None
        if not hasattr(self, 'spectral_summary'):
            return
        self._spectral_info = None
        config, errors = self.model.config()
        if not config['PANEL.spectral_file'].strip():
            self._problem(self.spectral_summary, '')
        elif errors:
            self._problem(self.spectral_summary, 'Fix the values marked in red to read the file.')
        else:
            try:
                config['PANEL.reflectance_source'] = 'spectral_file'
                levels, info = wf.panel_reflectance_levels(config, self.root_folder)
            except (ValueError, OSError) as error:
                self._problem(self.spectral_summary, str(error))
            else:
                self._problem(self.spectral_summary, '')
                lines = ['The file gives these reflectances:'] + [f"- {band}: {level:.4f}" for band, level in levels.items()]
                partial = [f"{band} {100 * values['coverage']:.0f} %" for band, values in info['bands'].items()
                           if values['coverage'] < 1]
                if partial:
                    lines.append('Part of the window of the band that the file covers: ' + ', '.join(partial) + '.')
                self._spectral_info = ('Read from the spectral file', '\n'.join(lines))
        self.help_panel.refresh()

    def browse_spectrum(self):
        """
        Choose the spectral response file and open the import window on it, with its format detected; the choice (the
        path and the format) is applied only when it is accepted there, so cancelling leaves everything as it was
        """
        from tkinter import filedialog
        current = self.model.get('PANEL.spectral_file')
        start = wf.resolve_path(self.root_folder, current) if current else self.root_folder
        chosen = filedialog.askopenfilename(initialdir=start if start.is_dir() else start.parent,
                                            filetypes=(('CSV files', '*.csv'), ('All files', '*.*')))
        if not chosen:
            return
        known = wf.resolve_path(self.root_folder, current) if current else None
        same = known is not None and known.is_file() and Path(chosen).resolve() == known.resolve()
        self.import_spectrum(chosen, detect=not same)         # the file that is already the source keeps its format

    def import_spectrum(self, path=None, detect=False):
        """
        Open the dialog to preview the spectral response file and its format, and apply what the user accepts
        path: the file to show (default: the one of the configuration); detect: guess its format when the dialog opens
        """
        from .panel_spectrum_dialog import import_spectrum
        config, errors = self.model.config()
        try:
            fmt = wf.spectral_format(config)
        except wf.ConfigError:
            fmt = None
        windows = {band: (center, config['PANEL.band_bandwidths'].get(band, 1.0))
                   for band, center in config['PANEL.band_center_wavelengths'].items()}
        path = path or config['PANEL.spectral_file']
        result = import_spectrum(self.parent.winfo_toplevel(), str(wf.resolve_path(self.root_folder, path)) if path else '',
                                 fmt, windows, config['PANEL.spectral_min_coverage'], detect=detect)
        if result is not None:
            self.apply_import(result)

    def stored_path(self, path):
        """ A path inside the project folder is kept relative to it """
        path = Path(path)
        try:
            return path.resolve().relative_to(self.root_folder).as_posix()
        except ValueError:
            return str(path)

    def apply_import(self, result):
        """ Put what the import dialog returned in the fields (see panel_spectrum_dialog) """
        if result['action'] == 'copy_values':
            self.set_value('PANEL.reflectance_source', 'values')
            current = self.model.get('PANEL.reflectance_by_band')
            self.set_value('PANEL.reflectance_by_band', {band: f"{level:.4f}" for band, level in result['levels'].items()
                                                         if band in current})
            self.log('The values of the spectral file were copied to the typed list of reflectances.')
            return
        fmt = result['format']
        self.set_value('PANEL.spectral_file', self.stored_path(result['file']))
        for key, name in (('PANEL.spectral_delimiter', 'delimiter'), ('PANEL.spectral_delimiter_char', 'delimiter_char'),
                          ('PANEL.spectral_skip_rows', 'skip_rows'), ('PANEL.spectral_skip_footer', 'skip_footer'),
                          ('PANEL.spectral_comment_prefix', 'comment_prefix'),
                          ('PANEL.spectral_exclude_rows', 'exclude_rows'),
                          ('PANEL.spectral_wavelength_min', 'wavelength_min'),
                          ('PANEL.spectral_wavelength_max', 'wavelength_max'),
                          ('PANEL.spectral_reflectance_scale', 'reflectance_scale'),
                          ('PANEL.spectral_wavelength_column', 'wavelength_column'),
                          ('PANEL.spectral_reflectance_column', 'reflectance_column'),
                          ('PANEL.spectral_wavelength_unit', 'wavelength_unit'),
                          ('PANEL.spectral_reflectance_unit', 'reflectance_unit'),
                          ('PANEL.spectral_decimal', 'decimal')):
            self.set_value(key, '' if fmt[name] is None else str(fmt[name]))
        self.set_value('PANEL.band_center_wavelengths', {band: f"{value:g}" for band, value in result['centers'].items()})
        self.set_value('PANEL.band_bandwidths', {band: f"{value:g}" for band, value in result['bandwidths'].items()})
        self.set_value('PANEL.spectral_min_coverage', f"{result['min_coverage']:g}")
        self.set_value('PANEL.reflectance_source', 'spectral_file')
        self.log(f"The panel reflectance will be computed from {result['file']}.")

    def _browse(self, key, kind):
        """
        Choose a folder or a file for a field. A folder is chosen in a window that lists its files and shows a picture of
        the image that is selected (folder_browser), to recognize the folder of the raw images or the one of the results
        """
        if kind == 'folders':
            self._browse_folders(key)
            return
        current = self.model.get(key)
        start = wf.resolve_path(self.root_folder, current) if current else self.root_folder
        initial = start if start.is_dir() else start.parent
        if kind == 'folder':
            from .folder_browser import ask_folder
            while not initial.is_dir() and initial != initial.parent:          # a results folder that does not exist yet
                initial = initial.parent
            chosen = ask_folder(self.parent.winfo_toplevel(), self.model.fields[key]['label'], initial)
        elif key == 'PANEL.manual_regions_file':
            from tkinter import filedialog
            # the file may not exist yet: the dialog that names a file is used, so that any folder can be chosen and a new name
            # typed; an existing file is chosen without asking whether to replace it (it is not written here)
            results = wf.resolve_path(self.root_folder, self.model.get('INPUTS.results_folder'))
            folder = initial if current else results
            while not folder.is_dir() and folder != folder.parent:
                folder = folder.parent
            chosen = filedialog.asksaveasfilename(
                title='Panel regions file', initialdir=folder, initialfile=Path(current).name if current else 'panel_regions.json',
                defaultextension='.json', filetypes=(('JSON files', '*.json'), ('All files', '*.*')), confirmoverwrite=False)
        else:
            from tkinter import filedialog
            chosen = filedialog.askopenfilename(initialdir=initial)
        if chosen:
            self.variables[key].set(chosen)

    def _browse_folders(self, key):
        """
        Choose the images folders: a list built in the folder browser, from any location, with the copy used of each
        capture that is in more than one of them (INPUTS.repeated_capture_folders)
        """
        from .folder_browser import ask_folders
        config, _ = self.model.config()
        folders = [wf.resolve_path(self.root_folder, folder) for folder in rct.folder_list(config[key])]
        initial = next((folder for folder in folders if folder.is_dir()), Path(self.root_folder))
        chosen = {name: str(wf.resolve_path(self.root_folder, folder))
                  for name, folder in config['INPUTS.repeated_capture_folders'].items()}
        answer = ask_folders(self.parent.winfo_toplevel(), self.model.fields[key]['label'], initial, folders, chosen,
                             config['INPUTS.file_pattern'])
        if answer is not None:
            folders, chosen = answer
            self.variables['INPUTS.repeated_capture_folders'].set(json.dumps(chosen) if chosen else '')
            self.variables[key].set(rct.FOLDER_SEPARATOR.join(folders))

    def _images_source(self):
        """ The images folder(s) of the fields, with the copy chosen of each repeated capture (rct.ImageFolders) """
        config, _ = self.model.config()
        return wf.images_source(config, self.root_folder)

    def set_value(self, key, value, replace=False):
        """
        Change a field as if it was typed (used by the program and the tests). The keys of the band table take
        {band: text} dictionaries (the bands of the dictionary are added or changed; with replace=True the dictionary
        becomes the whole list of bands) and, for INPUTS.ignored_bands, text
        """
        if key in BAND_KEYS:
            if key == 'INPUTS.ignored_bands':
                self.model.raw[key] = str(value)
            else:
                merged = {} if replace else dict(self.model.raw[key])
                merged.update({str(band): str(text) for band, text in value.items()})
                self.model.raw[key] = merged
            self._table_from_model()
            self._after_change(key)
            return
        variable = self.variables[key]
        variable.set(value if isinstance(value, bool) else str(value))

    # --- bands: presets and detection ----------------------------------------------------------------------------

    def typed_reflectance(self):
        """ {band: text} of the reflectance now in the table (kept when the bands are replaced) """
        return {row['name'].strip(): row['reflectance'] for row in self.band_table.get_rows()
                if row['name'].strip() and row['reflectance'].strip()}

    def apply_preset(self, key):
        """ Fill the band table (and the file pattern) with a camera preset; the typed reflectances of bands with the
        same name are kept """
        preset = self.presets[key]
        self.band_table.set_rows(camera_presets.band_rows(preset, self.typed_reflectance()))
        if preset.get('file_pattern'):
            self.set_value('INPUTS.file_pattern', preset['file_pattern'])
        text = f"Preset: {preset['label']}."
        if preset.get('notes'):
            text += ' ' + preset['notes']
        self.band_info.configure(text=text, style='Muted.TLabel')
        self.log(f"Camera preset applied: {preset['label']}")

    def apply_selected_preset(self):
        key = self.preset_labels.get(self.preset_var.get())
        if key:
            self.apply_preset(key)

    def detect_bands(self):
        """ Read the bands from the metadata of the files of the panel capture (in a thread) """
        if self.detecting or self.running:
            return
        config, errors = self.model.config()
        relevant = [key for key in errors if key.startswith('INPUTS.')]
        if relevant:
            self.band_info.configure(text='Fix the inputs first: ' + ', '.join(relevant), style='Error.TLabel')
            return
        self.detecting = True
        self.band_buttons['detect'].state(['disabled'])
        self.band_info.configure(text='Reading the bands from the images...', style='Muted.TLabel')
        threading.Thread(target=self._detect_worker, args=(config,), daemon=True).start()

    def _detect_worker(self, config):
        try:
            self.events.put(('bands', (wf.detect_bands(config, self.root_folder),
                                       (wf.panel_capture_groups(config)[0] or [''])[0])))
        except Exception as error:
            self.events.put(('bands_failed', f"{type(error).__name__}: {error}"))

    def apply_detected_bands(self, detected, capture):
        typed = self.typed_reflectance()
        rows = [{'name': band['name'], 'center': f"{band['center_nm']:g}", 'width': f"{band['bandwidth_nm']:g}",
                 'reflectance': '' if band['thermal'] else typed.get(band['name'], ''), 'process': not band['thermal']}
                for band in detected['bands']]
        self.band_table.set_rows(rows)
        left_out = [band['name'] for band in detected['bands'] if band['thermal']]
        text = f"Read {len(rows)} bands from {capture}: {detected['camera'] or 'camera not named'}"
        if detected['serial']:
            text += f" (serial {detected['serial']})"
        text += '.'
        if left_out:
            text += f" Not used because they look thermal: {', '.join(left_out)}."
        missing = [row['name'] for row in rows if row['process'] and not row['reflectance']]
        if missing and self.model.get('PANEL.reflectance_source') == 'values':
            text += f" Type the panel reflectance of: {', '.join(missing)}."
        self.band_info.configure(text=text, style='Muted.TLabel')
        self.log(text)

    def save(self):
        try:
            self.model.save(self.config_path)
        except OSError as error:
            self.status.configure(text=f"Could not save: {error}", style='Error.TLabel')
            return False
        _, errors = self.model.config()
        note = f" ({len(errors)} invalid value(s) were not saved)" if errors else ''
        self.status.configure(text=f"Saved configuration updated: {self.config_path}{note}. The window opens with these "
                                   "values the next time.", style='Muted.TLabel')
        return True

    # --- profiles ---------------------------------------------------------------------------------------------------

    @property
    def profiles_folder(self):
        return self.root_folder / wf.CONFIG_FOLDER / 'profiles'

    def save_profile(self, path):
        """ Save every value (not only the changes) in a json file, so that the profile does not depend on the defaults """
        config, errors = self.model.config()
        if errors:
            self.log('The profile was not saved: ' + '; '.join(f"{key} {message}" for key, message in errors.items()), 'error')
            return False
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_text(json.dumps(config, indent=2), encoding='utf-8')
        os.replace(temporary, path)
        self.status.configure(text=f"Profile saved to {path}", style='Muted.TLabel')
        return True

    def load_profile(self, path):
        """
        Replace the values of all the fields with those of a profile (a json file with any of the keys), or with the settings
        that a run used (its radiometric_result.json or the provenance record of a capture)
        """
        try:
            data = json.loads(Path(path).read_text(encoding='utf-8'))
            values = wf.merge_config(wf.configuration_from_json(data))
        except (ValueError, OSError) as error:
            self.log(f"The profile {path} was not loaded: {error}", 'error')
            self.status.configure(text=f"The profile was not loaded: {error}", style='Error.TLabel')
            return False
        self.loading_values = True
        try:
            for key, field in self.model.fields.items():
                self.set_value(key, ConfigModel.to_raw(field, values[key]), replace=True)
        finally:
            self.loading_values = False
        merged = self._merge_old_panel_field()
        if merged:
            self.log(merged)
        self._apply_output_state()
        if wf.configuration_from_json(data) is not data:                   # what a run wrote: its folders come with it
            self.status.configure(text=f"Settings of the run loaded from {path}. The results folder is the one of that run: "
                                       f"{values['INPUTS.results_folder']}", style='Muted.TLabel')
        else:
            self.status.configure(text=f"Profile loaded from {path}", style='Muted.TLabel')
        return True

    def save_profile_as(self):
        from tkinter import filedialog
        self.profiles_folder.mkdir(exist_ok=True)
        path = filedialog.asksaveasfilename(initialdir=self.profiles_folder, defaultextension='.json',
                                            initialfile='radiometric_profile.json',
                                            filetypes=(('JSON files', '*.json'),))
        if path:
            self.save_profile(path)

    def load_profile_from(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(initialdir=self.profiles_folder if self.profiles_folder.is_dir()
                                          else self.root_folder, filetypes=(('JSON files', '*.json'),))
        if path:
            self.load_profile(path)

    # --- log ------------------------------------------------------------------------------------------------

    def log(self, text, tag='muted'):
        self.log_text.configure(state='normal')
        self.log_text.insert('end', text + '\n', tag)
        self.log_text.configure(state='disabled')
        self.log_text.see('end')

    def clear_log(self):
        self.log_text.configure(state='normal')
        self.log_text.delete('1.0', 'end')
        self.log_text.configure(state='disabled')

    def log_lines(self):
        return self.log_text.get('1.0', 'end').splitlines()

    # --- checks and actions ---------------------------------------------------------------------------------

    def problems(self, for_the_tool=False):
        """
        All the problems of the current inputs: unreadable values first, then the checks of the workflow
        for_the_tool: the file of the panel regions may not exist yet (the tool that draws the regions creates it)
        """
        config, errors = self.model.config()
        table = [message for _, _, message in self.band_table.problems()]
        if table:
            return config, ['Bands of the camera: ' + message for message in table]
        if errors:
            return config, [f"{self.model.fields[key]['label']} ({key}): {message}" for key, message in errors.items()]
        return config, wf.validate_config(config, self.root_folder, regions_file_may_be_missing=for_the_tool)

    def check_inputs_here(self):
        """ Check the inputs and show the result next to the tool, without leaving the page. Returns (config, problems) """
        config, problems = self.problems(for_the_tool=True)
        if problems:
            self.inputs_status.configure(text='\n'.join(problems), style='Error.TLabel')
        else:
            text = 'The inputs are valid: the folders, the panel capture, the bands and the other values can be used.'
            regions = config['PANEL.manual_regions_file']
            if regions and not wf.resolve_path(self.root_folder, regions).is_file():
                text += ' The file of the regions does not exist yet: Save and close in the tool creates it.'
            self.inputs_status.configure(text=text, style='Ok.TLabel')
        return config, problems

    def check_inputs(self):
        self.show_page('RUN')
        self.clear_log()
        config, problems = self.problems()
        for problem in problems:
            self.log(problem, 'error')
        if not problems:
            self.log('The inputs are valid.', 'ok')
        self.summary.configure(text='')
        return config, problems

    def check_environment(self):
        """ List the Python, Tkinter, the packages and ExifTool of this computer in the log (in another thread: ExifTool is run) """
        self.show_page('RUN')
        self.clear_log()
        self.log('Checking the Python, the packages and ExifTool of this computer...')
        self.summary.configure(text='')
        configured = self.model.config()[0]['INPUTS.exiftool_path']
        self.buttons['environment'].state(['disabled'])
        threading.Thread(target=lambda: self.events.put(
            ('environment', environment.check_environment(configured, self.root_folder, deep=True))), daemon=True).start()

    def _show_environment(self, items):
        self.buttons['environment'].state(['!disabled'])
        for item, line in zip(items, environment.report_lines(items)):
            self.log(line, 'ok' if item['status'] == 'ok' else 'error')
        missing = environment.problems(items)
        if missing:
            self.log(environment.install_text(items), 'muted')
            self.summary.configure(text=f"{len(missing)} thing(s) to install or fix (see the log).", style='Error.TLabel')
        else:
            self.log('Everything the program needs is in place.', 'ok')
            self.summary.configure(text='The computer is ready.', style='Ok.TLabel')

    def _set_busy(self, busy):
        self.running = busy
        for name in ('check', 'check_here', 'regions', 'run'):
            self.buttons[name].configure(state='disabled' if busy else 'normal')

    def start_run(self):
        if self.running:
            return
        config, problems = self.check_inputs()
        if problems:
            return
        self.save()
        self._set_busy(True)
        self.summary.configure(text='Running...', style='Muted.TLabel')
        self.progress.configure(value=0)
        threading.Thread(target=self._run_worker, args=(config,), daemon=True).start()

    def _run_worker(self, config):
        try:
            result = wf.run(config, self.root_folder, lambda event: self.events.put(('progress', event)))
            self.events.put(('done', result))
        except Exception:
            self.events.put(('crash', traceback.format_exc()))

    def draw_regions(self):
        if self.running:
            return
        config, problems = self.check_inputs_here()                 # the inputs are checked first, in this page
        if problems:
            return
        self._set_busy(True)
        self.inputs_status.configure(text='Reading the reference panel images...', style='Muted.TLabel')
        self.log('Reading the reference panel images...')
        threading.Thread(target=self._regions_worker, args=(config,), daemon=True).start()

    def regions_path(self, config):
        return wf.resolve_path(self.root_folder, config['PANEL.manual_regions_file'] or
                               Path(config['INPUTS.results_folder']) / 'panel_regions.json')

    def _regions_worker(self, config):
        try:
            required = list(dict.fromkeys(name for group in wf.panel_capture_groups(config) for name in group))
            preloaded = {name: wf.read_capture_images(config, self.root_folder, name) for name in required}
            names = list(required)                                   # the regions are only needed for the panel captures
            path = self.regions_path(config)
            saved = wf.load_panel_regions(path) if path.is_file() else {}
            origins = wf.load_panel_origins(path) if path.is_file() else {}
            self.events.put(('images', (config, names, required, preloaded, saved, origins, path)))
        except Exception as error:
            self.events.put(('regions_failed', f"{type(error).__name__}: {error}"))

    def _open_selector(self, payload):
        from .panel_selector import select_panel_regions
        config, names, required, preloaded, saved, origins, path = payload
        try:
            selected = select_panel_regions(
                names, lambda name: wf.read_capture_images(config, self.root_folder, name), required, saved, origins,
                config['PANEL.percent_inner_area'], config['PANEL.saturation_threshold'],
                parent=self.parent.winfo_toplevel(), preloaded=preloaded)
            if selected is None:
                self.log('Cancelled: no region was saved.')
                self.inputs_status.configure(text='Cancelled: no region was saved.', style='Muted.TLabel')
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                parts = []
                for capture, result in selected.items():
                    found = getattr(result, 'origins', {})
                    wf.save_panel_regions(path, capture, result, found)
                    automatic = sum(origin == 'accepted_automatic' for origin in found.values())
                    parts.append(f"{capture}: {len(result)} region(s), {automatic} as found from the QR code, "
                                 f"{len(result) - automatic} drawn or edited")
                text = f"Saved the accepted regions in {path} ({'; '.join(parts)})."
                self.log(text, 'ok')
                self.inputs_status.configure(text=text, style='Ok.TLabel')
                area = getattr(selected, 'percent_inner_area', None)
                if area is not None and area != config['PANEL.percent_inner_area']:
                    self.set_value('PANEL.percent_inner_area', f"{area:g}")
                    self.log(f"PANEL.percent_inner_area was set to {area:g}, the inner area of the panel of the tool.")
                    self.save()
                if not config['PANEL.manual_regions_file']:
                    self.set_value('PANEL.manual_regions_file', str(path))
                    self.log('PANEL.manual_regions_file was set to this file.')
                    self.save()
        finally:
            self._set_busy(False)

    def open_results(self):
        config, errors = self.model.config()
        folder = wf.resolve_path(self.root_folder, config['INPUTS.results_folder'])
        if folder.is_dir() and hasattr(os, 'startfile'):
            os.startfile(folder)
        elif not folder.is_dir():
            self.log(f"The results folder does not exist yet: {folder}", 'error')

    # --- events from the worker threads ---------------------------------------------------------------------

    def _poll(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        if self._after_id is not None:      # not closed
            try:
                self._after_id = self.parent.after(100, self._poll)
            except self.tk.TclError:    # the window was closed
                self._after_id = None

    def _handle(self, kind, payload):
        if kind == 'progress':
            total = payload['total'] or 1
            self.progress.configure(value=payload['current'] / total)
            self.log(f"[{payload['stage']}] {payload['message']}")
        elif kind == 'done':
            self._show_result(payload)
            self._set_busy(False)
        elif kind == 'crash':
            self.log(payload, 'error')
            self.summary.configure(text='The workflow stopped unexpectedly.', style='Error.TLabel')
            self._set_busy(False)
        elif kind == 'exiftool':
            path, version = payload
            state = self.exiftool_state
            if state and state['path'] == path:
                state['version'] = version or ''
                if not version:
                    self._problem(self.exiftool_summary, f"{path} was found but it cannot be run.")
                self.help_panel.refresh()
        elif kind == 'environment':
            self._show_environment(payload)
        elif kind == 'python':
            self._show_python(*payload)
        elif kind == 'images':
            self._open_selector(payload)
        elif kind == 'bands':
            self.detecting = False
            self.band_buttons['detect'].state(['!disabled'])
            self.apply_detected_bands(*payload)
        elif kind == 'bands_failed':
            self.detecting = False
            self.band_buttons['detect'].state(['!disabled'])
            self.band_info.configure(text=payload, style='Error.TLabel')
            self.log(payload, 'error')
        elif kind == 'regions_failed':
            self.log(payload, 'error')
            self._set_busy(False)
        elif kind == 'orientations':
            self._show_orientations(*payload)
        elif kind == 'panel_check_progress':
            self.status.configure(text=f"Checking the panel captures: {payload[0].lower() + payload[1:]}...",
                                  style='Muted.TLabel')
        elif kind == 'panel_check':
            self.buttons['panel_check'].state(['!disabled'])
            self._show_panel_check(*payload)
        elif kind == 'panel_check_failed':
            self.buttons['panel_check'].state(['!disabled'])
            self.log(f"The panel captures were not checked: {payload}", 'error')
            self.status.configure(text=f"The panel captures were not checked: {payload}", style='Error.TLabel')

    def _show_result(self, result):
        self.last_result = result
        for error in result['errors']:
            self.log(f"ERROR: {error}", 'error')
        for flag in result['flags']:
            self.log(f"FLAG: {flag}", 'flag')
        for line in wf.summary_lines(result):
            self.log(line)
        done = len(result['outputs'])
        text = f"Status: {result['status']}. {done} image(s) written"
        if 'result_file' in result:
            text += f"; details in {result['result_file']}"
        self.summary.configure(text=text, style='Ok.TLabel' if result['status'] == 'completed' else 'Error.TLabel')
        self.progress.configure(value=1 if done else 0)


def main(argv=None):
    import tkinter as tk
    parser = argparse.ArgumentParser(description="Window for the radiometric correction workflow")
    parser.add_argument('--config', help=f"configuration json file (default: {wf.CONFIG_FOLDER}/{DEFAULT_CONFIG_NAME} in --root)")
    parser.add_argument('--root', default='.', help="folder from which the relative paths are resolved")
    arguments = parser.parse_args(argv)
    window = tk.Tk()
    window.title("Radiometric Correction for MicaSense Multispectral Cameras")
    window.geometry("1400x820")
    window.minsize(1150, 620)
    apply_dark_theme(window)
    frame = RadiometricFrame(window, arguments.config, arguments.root)
    frame.widget.pack(fill='both', expand=True)
    hide_title_icon(window)
    window.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
