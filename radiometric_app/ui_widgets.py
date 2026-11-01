"""
Widgets shared by the windows of this project

    ScrollablePage   a frame whose content scrolls (mouse wheel and scroll bar) when it is taller than the window
    BandTable        the bands of the camera: use, name, spectral window and panel reflectance, one row per band
    NavTiles         large tiles, one above the other, to choose the page of a window
    HelpPanel        a column that explains the field under the mouse pointer or with the keyboard focus

The conversion between the rows of the table and the configuration (rows_from_model, model_from_rows) and the
validation of the rows (validate_rows) are plain functions, tested without a display.
"""

# Standard Library Imports
import math

# Local Imports
from .ui_theme import COLORS, FONT, FONT_BOLD

# ======================================================================================
# THE ROWS OF THE BAND TABLE (NO USER INTERFACE)
# ======================================================================================
# A row is {'name', 'center', 'width', 'reflectance' (texts), 'process' (bool)}. The bands that are used are written in the
# three dictionaries of the configuration (reflectance, center wavelength and bandwidth by band); the bands that are in
# the files but are not used are the names in the list of ignored bands.


def split_names(text):
    return [name.strip() for name in str(text).split(',') if name.strip()]


def rows_from_model(reflectance, centers, widths, ignored_text=''):
    """ Rows from the raw values of the configuration: {band: text} dictionaries and the text of the ignored bands """
    names = []
    for source in (centers, widths, reflectance):
        names.extend(name for name in source if name not in names)
    rows = [{'name': name, 'center': str(centers.get(name, '')), 'width': str(widths.get(name, '')),
             'reflectance': str(reflectance.get(name, '')), 'process': True} for name in names]
    rows.extend({'name': name, 'center': '', 'width': '', 'reflectance': '', 'process': False}
                for name in split_names(ignored_text) if name not in names)
    return rows


def model_from_rows(rows):
    """ The raw values of the configuration from rows: (reflectance, centers, widths, ignored text) """
    reflectance, centers, widths, ignored = {}, {}, {}, []
    for row in rows:
        name = row['name'].strip()
        if not name:
            continue
        if row['process']:
            reflectance[name], centers[name], widths[name] = row['reflectance'], row['center'], row['width']
        elif name not in ignored:
            ignored.append(name)
    return reflectance, centers, widths, ', '.join(ignored)


def _number(text):
    try:
        value = float(str(text).strip())
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def validate_rows(rows, check_reflectance=True):
    """
    Problems of the rows: [(row number from 0 or None, field, message)]. The window of a processed band must be
    positive; the reflectance is checked only when it is used (check_reflectance)
    """
    problems = []
    names = [row['name'].strip() for row in rows]
    for number, row in enumerate(rows):
        name = names[number]
        if not name:
            problems.append((number, 'name', 'the band needs a name'))
        elif names.count(name) > 1:
            problems.append((number, 'name', f"the name '{name}' is used by more than one band"))
        if not row['process']:
            continue
        for field, label in (('center', 'center wavelength'), ('width', 'bandwidth')):
            value = _number(row[field])
            if value is None or value <= 0:
                problems.append((number, field, f"{name or 'band'}: the {label} must be a number above 0"))
        if check_reflectance:
            value = _number(row['reflectance'])
            if value is None or not 0 < value <= 1:
                problems.append((number, 'reflectance', f"{name or 'band'}: the panel reflectance must be a number in (0, 1]"))
    if rows and not any(row['process'] and row['name'].strip() for row in rows):
        problems.append((None, 'process', 'at least one band must be used'))
    return problems


# ======================================================================================
# WIDGETS
# ======================================================================================

class ScrollablePage:
    """
    A page that scrolls. Put the widgets in .body; place .widget (the outer frame) where the page goes.
    The scroll bar is shown only when the content is taller than the page
    """

    def __init__(self, parent):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.widget = ttk.Frame(parent)
        self.canvas = tk.Canvas(self.widget, highlightthickness=0, borderwidth=0, background=COLORS['background'])
        self.scrollbar = ttk.Scrollbar(self.widget, orient='vertical', command=self.canvas.yview)
        self.body = ttk.Frame(self.canvas, padding=(0, 0, 18, 0))          # room before the scroll bar
        self.window_id = self.canvas.create_window((0, 0), window=self.body, anchor='nw')
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side='left', fill='both', expand=True)
        self.scrollbar_shown = False
        self.body.bind('<Configure>', lambda event: self._update())
        self.canvas.bind('<Configure>', self._on_canvas)
        self.widget.bind('<Enter>', lambda event: self.canvas.bind_all('<MouseWheel>', self._on_wheel))
        self.widget.bind('<Leave>', lambda event: self.canvas.unbind_all('<MouseWheel>'))

    def _on_canvas(self, event):
        self.canvas.itemconfigure(self.window_id, width=event.width)
        self._update()

    def _update(self):
        self.canvas.configure(scrollregion=self.canvas.bbox('all'))
        needed = self.body.winfo_reqheight() > self.canvas.winfo_height() > 1
        if needed and not self.scrollbar_shown:
            self.scrollbar.pack(side='right', fill='y')
            self.scrollbar_shown = True
        elif not needed and self.scrollbar_shown:
            self.scrollbar.pack_forget()
            self.scrollbar_shown = False
            self.canvas.yview_moveto(0)

    def _on_wheel(self, event):
        if self.scrollbar_shown:
            self.canvas.yview_scroll(-1 if event.delta > 0 else 1, 'units')

    def scrolls(self):
        """ True while the content is taller than the page """
        return self.scrollbar_shown


class BandTable:
    """
    The bands of the camera, one row each: Use (a band that is not used is in the files but is left out), name (as
    written by the camera), center wavelength, bandwidth and panel reflectance. on_change is called after every edit
    """

    COLUMNS = (('process', 'Use', 4), ('name', 'Band name', 18), ('center', 'Center (nm)', 10), ('width', 'Bandwidth (nm)', 12),
               ('reflectance', 'Panel reflectance', 14))

    def __init__(self, parent, on_change=None):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.on_change = on_change
        self.reflectance_enabled = True
        self.quiet = False
        self.rows = []                      # {'vars': {...}, 'widgets': {...}}
        self.widget = ttk.Frame(parent)
        self.grid_frame = ttk.Frame(self.widget)
        self.grid_frame.pack(anchor='w')
        for column, (key, title, width) in enumerate(self.COLUMNS):
            ttk.Label(self.grid_frame, text=title, style='Muted.TLabel').grid(row=0, column=column, sticky='w', padx=(0, 8))

    # --- rows ---------------------------------------------------------------------------------------------------

    def set_rows(self, rows):
        """ Replace all the rows (the change is notified once) """
        self.quiet = True
        try:
            for row in self.rows:
                for widget in row['widgets'].values():
                    widget.destroy()
            self.rows = []
            for row in rows:
                self.add_row(row)
        finally:
            self.quiet = False
        self._refresh()
        self._notify()

    def get_rows(self):
        return [{'name': row['vars']['name'].get(), 'center': row['vars']['center'].get(),
                 'width': row['vars']['width'].get(), 'reflectance': row['vars']['reflectance'].get(),
                 'process': bool(row['vars']['process'].get())} for row in self.rows]

    def add_row(self, values=None):
        tk, ttk = self.tk, self.ttk
        values = dict({'name': '', 'center': '', 'width': '', 'reflectance': '', 'process': True}, **(values or {}))
        number = len(self.rows) + 1
        variables = {'process': tk.BooleanVar(value=values['process'])}
        widgets = {}
        for column, (key, title, width) in enumerate(self.COLUMNS):
            if key == 'process':
                widgets[key] = ttk.Checkbutton(self.grid_frame, variable=variables[key])
                widgets[key].grid(row=number, column=column, sticky='w', pady=1)
            else:
                variables[key] = tk.StringVar(value=values[key])
                widgets[key] = ttk.Entry(self.grid_frame, textvariable=variables[key], width=width)
                widgets[key].grid(row=number, column=column, sticky='w', padx=(0, 8), pady=1)
        row = {'vars': variables, 'widgets': widgets}
        widgets['remove'] = ttk.Button(self.grid_frame, text='Remove', width=8, command=lambda row=row: self.remove(row))
        widgets['remove'].grid(row=number, column=len(self.COLUMNS), sticky='w', pady=1)
        for variable in variables.values():
            variable.trace_add('write', lambda *_: self._edited())
        self.rows.append(row)
        if not self.quiet:
            self._refresh()
            self._notify()
        return row

    def remove(self, row):
        if row not in self.rows:
            return
        self.rows.remove(row)
        for widget in row['widgets'].values():
            widget.destroy()
        for number, other in enumerate(self.rows, start=1):          # close the gap
            for widget in other['widgets'].values():
                widget.grid_configure(row=number)
        self._refresh()
        self._notify()

    def remove_at(self, index):
        self.remove(self.rows[index])

    # --- state ------------------------------------------------------------------------------------------------

    def set_reflectance_state(self, enabled):
        """ The panel reflectance is typed only when it is the source of the reflectance """
        self.reflectance_enabled = enabled
        self._refresh()

    def reflectance_entries(self):
        return [row['widgets']['reflectance'] for row in self.rows]

    def problems(self):
        return validate_rows(self.get_rows(), self.reflectance_enabled)

    def _edited(self):
        if self.quiet:
            return
        self._refresh()
        self._notify()

    def _notify(self):
        if self.on_change is not None:
            self.on_change()

    def _refresh(self):
        """ Mark the invalid entries and disable the ones that a band that is not used does not need """
        marked = {(number, field) for number, field, _ in self.problems() if number is not None}
        for number, row in enumerate(self.rows):
            used = bool(row['vars']['process'].get())
            for field in ('name', 'center', 'width', 'reflectance'):
                widget = row['widgets'][field]
                widget.configure(style='Invalid.TEntry' if (number, field) in marked else 'TEntry')
                disabled = (field != 'name' and not used) or (field == 'reflectance' and not self.reflectance_enabled)
                widget.state(['disabled'] if disabled else ['!disabled'])


class NavTiles:
    """
    Tiles in a column to choose a page (the pages of the photogrammetric workflow window look like this), as tall as two
    thirds of what the column would give them. The selected tile is highlighted; click, Enter/Space (focused tile) and Up/Down choose one. on_select(index) is called
    after the choice; get, size, select and curselection follow the interface of a Listbox
    """

    def __init__(self, parent, titles, on_select=None, width=132):
        import tkinter as tk
        self.tk = tk
        self.titles = list(titles)
        self.on_select = on_select
        self.selected = None
        self.widget = tk.Frame(parent, background=COLORS['background'], width=width)
        self.widget.pack_propagate(False)
        self.widget.grid_propagate(False)
        self.widget.grid_columnconfigure(0, weight=1)
        self.tiles = []
        for index, title in enumerate(self.titles):
            self.widget.grid_rowconfigure(index, minsize=100)
            tile = tk.Label(self.widget, text=title, anchor='w', justify='left', wraplength=width - 30, padx=14,
                            font=('Segoe UI', 11, 'bold'),
                            background=COLORS['tile'], foreground=COLORS['foreground'], takefocus=True,
                            highlightthickness=1, highlightbackground=COLORS['border'], highlightcolor=COLORS['tile_border'],
                            cursor='hand2')
            tile.grid(row=index, column=0, sticky='nsew', pady=(0, 6))
            tile.bind('<Button-1>', lambda event, index=index: self._choose(index))
            tile.bind('<Return>', lambda event, index=index: self._choose(index))
            tile.bind('<space>', lambda event, index=index: self._choose(index))
            tile.bind('<Up>', lambda event, index=index: self._choose(max(index - 1, 0), focus=True))
            tile.bind('<Down>', lambda event, index=index: self._choose(min(index + 1, len(self.titles) - 1), focus=True))
            tile.bind('<Enter>', lambda event, index=index: self._paint(index, hover=True))
            tile.bind('<Leave>', lambda event, index=index: self._paint(index))
            self.tiles.append(tile)
        self.widget.bind('<Configure>', self._fit)

    def _fit(self, event):
        """ The tiles are two thirds as tall as they would be if they filled the column (with a floor and a ceiling) """
        height = int(min(max(event.height / max(len(self.titles), 1) * 2 / 3, 70), 170))
        for index in range(len(self.titles)):
            self.widget.grid_rowconfigure(index, minsize=height)

    def _paint(self, index, hover=False):
        if index == self.selected:
            background, border = COLORS['tile_selected'], COLORS['tile_border']
        else:
            background, border = (COLORS['tile_hover'] if hover else COLORS['tile']), COLORS['border']
        self.tiles[index].configure(background=background, highlightbackground=border)

    def _choose(self, index, focus=False):
        self.select(index)
        if focus:
            self.tiles[index].focus_set()
        if self.on_select is not None:
            self.on_select(index)

    def select(self, index):
        previous, self.selected = self.selected, index
        for number in {previous, index} - {None}:
            self._paint(number)

    def curselection(self):
        return () if self.selected is None else (self.selected,)

    def size(self):
        return len(self.titles)

    def get(self, index):
        return self.titles[index]


class HelpPanel:
    """
    A column with the explanation of a field. What it shows, in this order of preference: the field under the mouse
    pointer, the field that has the keyboard focus, and the explanation of the page (set_default). A widget is described
    with describe(widget, title, text, extra_title, extra_text, details): everything inside it (also what is added later,
    like the rows of a table) shows that explanation; details is an optional function that returns the (title, text) of
    live information (what a file pattern matches now, for example), shown above the explanation; refresh() shows it again after a change. The
    pointer and the focus are followed from the toplevel window, so nothing has to be bound to each widget; follow(toplevel)
    starts it. While the pointer is over the column itself the explanation stays, so that it can be read and scrolled.

    The texts are written with a few marks, one paragraph per line: a line that starts with '# ' is a sub-heading, one that
    starts with '- ' is an item of a list, any other line is a paragraph (the lines after the first are indented). The text
    wraps to the width of the column, which the user can change
    """

    SECTIONS = ('body', 'extra', 'detail')

    def __init__(self, parent, width=340):
        import tkinter as tk
        self.tk = tk
        self.width = width
        self.widget = tk.Frame(parent, background=COLORS['panel'], width=width)
        self.text = tk.Text(self.widget, wrap='word', borderwidth=0, highlightthickness=0, padx=20, pady=18,
                            background=COLORS['panel'], foreground=COLORS['foreground'], font=FONT, cursor='arrow',
                            takefocus=0, state='disabled', insertwidth=0, selectbackground=COLORS['accent'],
                            selectforeground=COLORS['foreground'], spacing1=0, spacing2=5, spacing3=0)
        self.text.pack(fill='both', expand=True)
        self.text.tag_configure('title', font=('Segoe UI', 12, 'bold'), spacing3=12)
        for section in self.SECTIONS:
            color = COLORS['muted'] if section == 'extra' else COLORS['foreground']
            self.text.tag_configure(f'{section}_p', lmargin1=0, lmargin2=18, spacing2=5, spacing3=12, foreground=color)
            self.text.tag_configure(f'{section}_b', lmargin1=14, lmargin2=30, tabs=(30,), spacing2=5, spacing3=7,
                                    foreground=color)
            self.text.tag_configure(f'{section}_h', font=FONT_BOLD, spacing1=6, spacing3=6, foreground=COLORS['foreground'])
        self.text.tag_configure('section', font=FONT_BOLD, spacing3=8)
        self.text.tag_configure('rule', spacing1=10, spacing3=12)
        self.text.bind('<Configure>', lambda event: self._fit_rules())
        self.text.bind('<Enter>', lambda event: self.text.bind_all('<MouseWheel>', self._on_wheel))
        self.text.bind('<Leave>', lambda event: self.text.unbind_all('<MouseWheel>'))
        self.rules = []
        self.described = {}              # str(widget) -> (title, text, extra title, extra text, details function)
        self.default = ('', '', '', '', None)
        self.hover = None
        self.focus = None
        self.shown = self.default
        self.written = None
        self.revert_job = None
        self.root = None

    # --- what is shown --------------------------------------------------------------------------------------

    def describe(self, widget, title, text, extra_title='', extra_text='', details=None):
        self.described[str(widget)] = (title, text, extra_title, extra_text, details)

    def set_default(self, title, text, extra_title='', extra_text='', details=None):
        """ The explanation of the page; the pointer and the focus are forgotten (a new page was opened) """
        self.default = (title, text, extra_title, extra_text, details)
        self.hover = self.focus = None
        self._refresh()

    def refresh(self):
        """ Show the explanation again, with the live information as it is now """
        self._refresh()

    def _refresh(self):
        self.shown = self.hover or self.focus or self.default
        title, text, extra_title, extra_text, details = self.shown
        live = details() if details else None
        content = (title, text, extra_title, extra_text, live)
        if content != self.written:
            self.written = content
            self._write(*content)

    def _write(self, title, text, extra_title, extra_text, live):
        t = self.text
        t.configure(state='normal')
        t.delete('1.0', 'end')
        for rule in self.rules:
            rule.destroy()
        self.rules = []
        if title:
            t.insert('end', title + '\n', 'title')
        if live and live[0]:                                   # what is true now comes first, then the explanation
            t.insert('end', live[0] + '\n', 'section')
            self._markup(live[1], 'detail')
            self._rule()
        self._markup(text, 'body')
        if extra_title:
            self._rule()
            t.insert('end', extra_title + '\n', 'section')
            self._markup(extra_text, 'extra')
        t.configure(state='disabled')
        t.yview_moveto(0)

    def _markup(self, text, section):
        """ Write the paragraphs, sub-headings and list items of a text """
        for line in str(text).split('\n'):
            line = line.strip()
            if not line:
                continue
            if line.startswith('# '):
                self.text.insert('end', line[2:] + '\n', f'{section}_h')
            elif line.startswith('- '):
                self.text.insert('end', '•\t' + line[2:] + '\n', f'{section}_b')
            else:
                self.text.insert('end', line + '\n', f'{section}_p')

    def _rule(self):
        """ A thin line between the parts of the explanation """
        from tkinter import Frame
        rule = Frame(self.text, height=1, background=COLORS['border'], width=max(self.text.winfo_width() - 44, 40))
        self.rules.append(rule)
        self.text.window_create('end', window=rule)
        self.text.insert('end', '\n', 'rule')

    def _fit_rules(self):
        for rule in self.rules:
            rule.configure(width=max(self.text.winfo_width() - 44, 40))

    def _on_wheel(self, event):
        self.text.yview_scroll(-1 if event.delta > 0 else 1, 'units')

    def plain_text(self):
        """ What is written now, without the marks of the formatting """
        return self.text.get('1.0', 'end').strip()

    def explanation_of(self, widget):
        """ The explanation of the widget or of the nearest widget around it that has one, or None """
        while widget is not None:
            found = self.described.get(str(widget))
            if found:
                return found
            widget = widget.master
        return None

    # --- the pointer and the focus --------------------------------------------------------------------------

    def _in_panel(self, widget):
        return widget is not None and str(widget).startswith(str(self.widget))

    def point_at(self, widget):
        """ The mouse pointer is over widget (None: over nothing). Leaving a field goes back to the page a moment later """
        if self._in_panel(widget):
            if self.revert_job is not None:                       # the pointer went to read the explanation
                self.widget.after_cancel(self.revert_job)
                self.revert_job = None
            return
        found = self.explanation_of(widget)
        if self.revert_job is not None:
            self.widget.after_cancel(self.revert_job)
            self.revert_job = None
        if found:
            if found is not self.hover:
                self.hover = found
                self._refresh()
        elif self.hover is not None:
            self.revert_job = self.widget.after(120, self._forget_hover)

    def _forget_hover(self):
        self.revert_job = None
        self.hover = None
        self._refresh()

    def focus_on(self, widget):
        """ The keyboard focus moved to widget """
        if self._in_panel(widget):
            return
        self.focus = self.explanation_of(widget)
        self._refresh()

    def follow(self, toplevel, area=None):
        """
        Follow the pointer and the focus in a window (the events of the widgets inside it reach it), over the widgets of
        area (default: the widget that holds the column)
        """
        self.root = toplevel
        area = str(area if area is not None else self.widget.master)

        def inside(widget):
            return widget is not None and str(widget).startswith(area)

        def on_motion(event):
            try:
                if self.widget.winfo_exists() and inside(event.widget):
                    self.point_at(event.widget)
            except self.tk.TclError:
                pass

        def on_focus(event):
            try:
                if self.widget.winfo_exists() and inside(event.widget):
                    self.focus_on(event.widget)
            except self.tk.TclError:
                pass

        toplevel.bind('<Motion>', on_motion, add='+')
        toplevel.bind('<FocusIn>', on_focus, add='+')
