"""
Choose a folder while looking at what it contains: the files of the folder (folders first, with their sizes) and, at the
right, a picture of the image file that is selected, so that the folder of the raw images, or the one of the results, can be
recognized before it is chosen.

    ask_folder(parent, title, initialdir)   the dialog; returns the path of the folder that was chosen, or '' if cancelled
    ask_folders(parent, title, initialdir, folders, chosen, pattern)
                                            the dialog that builds a list of folders, from any location, and chooses the
                                            copy used of each capture that is in more than one of them (with its picture);
                                            returns ([folders], {capture: folder}) or None if cancelled

What does not need a window is tested without a display:
    list_folder(destination)    the contents of a folder, sorted
    preview_picture(path)       a picture of an image file (stretched between its 1 % and 99.5 % percentiles)
    size_text(size)             a size in bytes as text
    identical_copies(copies)    whether the copies of a repeated capture have the same files
    valid_choices(groups, chosen)   the choices that name one of the copies of a repeated capture
"""

# Standard Library Imports
import os
import queue
import threading
from pathlib import Path
import tkinter as tk
from tkinter import ttk

# Local Imports
from .ui_theme import COLORS, hide_title_icon

IMAGE_SUFFIXES = ('.tif', '.tiff', '.png', '.jpg', '.jpeg')
PREVIEW_BOUNDS = (1600, 1200)             # the picture is reduced to this before it is stretched and drawn
NO_PREVIEW = "Select an image to preview it."


# ======================================================================================
# LOGIC WITHOUT USER INTERFACE
# ======================================================================================

def size_text(size):
    """ A size in bytes as text (None, for a folder, is an empty text) """
    if size is None:
        return ''
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if size < 1024 or unit == 'TiB':
            return f"{size:.0f} {unit}" if unit == 'B' else f"{size:.1f} {unit}"
        size /= 1024


def list_folder(destination, base=None):
    """
    The contents of a folder: (the folder, [(name, is_folder, size in bytes or None), ...]) with the folders first and then the
    files, each group by name without regard to case. destination is a path (a relative one is taken from base; the path of a
    file stands for its folder). OSError if the folder cannot be read
    """
    target = Path(str(destination).strip().strip('"')).expanduser()
    if not target.is_absolute() and base is not None:
        target = Path(base) / target
    target = Path(os.path.abspath(target))
    if target.is_file():
        target = target.parent
    rows = []
    with os.scandir(target) as contents:
        for entry in contents:
            try:
                folder = entry.is_dir()
                size = None if folder else entry.stat().st_size
            except OSError:
                folder, size = False, None
            rows.append((entry.name, folder, size))
    rows.sort(key=lambda row: (not row[1], row[0].casefold()))
    return target, rows


def is_image(path):
    return Path(path).suffix.lower() in IMAGE_SUFFIXES


def preview_picture(path, bounds=PREVIEW_BOUNDS):
    """
    A picture of an image file for the eye only (the file is not changed): one band of a raw or reflectance .tif (16 bits,
    floating point, with or without invalid values) is reduced and stretched between its 1 % and 99.5 % percentiles, a
    color picture (.png, .jpg) is shown as it is
    Outputs: (PIL image that fits in bounds, (width, height) of the file, text of the type of its values);
             ValueError if the file is not an image, and what the readers raise if it cannot be read
    """
    import numpy as np
    from PIL import Image
    path = Path(path)
    if path.suffix.lower() in ('.tif', '.tiff'):
        import tifffile
        data = np.squeeze(tifffile.imread(str(path)))
        kind = str(data.dtype)
    else:
        with Image.open(path) as source:
            kind = source.mode
            if source.mode not in ('L', 'I', 'I;16', 'F'):
                width, height = source.size
                picture = source.convert('RGB')
                picture.thumbnail(bounds)
                return picture, (width, height), kind
            data = np.asarray(source)
    if data.ndim == 3:                                  # several channels: the first one
        data = data[..., 0] if data.shape[-1] <= 4 else data[0]
    if data.ndim != 2:
        raise ValueError(f"{path.name} is not an image")
    height, width = data.shape
    step = max(1, int(max(width / bounds[0], height / bounds[1])))
    small = data[::step, ::step].astype(np.float32)
    valid = small[np.isfinite(small)]
    if valid.size == 0:
        raise ValueError("the image has no valid values")
    low, high = np.percentile(valid, (1, 99.5))
    if high <= low:
        high = low + 1
    stretched = np.clip((np.nan_to_num(small, nan=low) - low) / (high - low), 0, 1)
    picture = Image.fromarray((stretched * 255).astype(np.uint8)).convert('RGB')
    picture.thumbnail(bounds)
    return picture, (width, height), kind


# ======================================================================================
# WINDOW
# ======================================================================================

class ImagePreview(ttk.Frame):
    """ The picture of an image file and what it is; the file is read in another thread, so the list stays quick to use """

    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, background=COLORS['panel'], width=260, height=220, highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)
        self.status = tk.StringVar(value=NO_PREVIEW)
        self.label = ttk.Label(self, textvariable=self.status, wraplength=300, justify='left')
        self.label.pack(fill='x', pady=(7, 0))
        self.photo = self.picture = self.path = None
        self.requests, self.results = queue.Queue(), queue.Queue()
        self.serial = 0
        self.closed = False
        self.canvas.bind('<Configure>', self._resize)
        self.bind('<Destroy>', self._destroyed, add='+')
        threading.Thread(target=self._read, args=(self.requests, self.results), daemon=True).start()
        self.poll = self.after(50, self._poll)

    def show(self, path=None):
        """ Show the picture of a file (None: nothing); a file that is asked for while another is being read replaces it """
        self.serial += 1
        self.path = Path(path) if path else None
        self.picture = self.photo = None
        self.canvas.delete('all')
        self.status.set(f"Reading {self.path.name}..." if self.path else NO_PREVIEW)
        if self.path is not None:
            self.requests.put((self.serial, self.path))

    @staticmethod
    def _read(requests, results):
        while True:
            item = requests.get()
            while not requests.empty():                  # only the last one that was asked for is of interest
                item = requests.get()
            if item is None:
                return
            serial, path = item
            try:
                answer, error = preview_picture(path), None
            except Exception as exc:                     # a file that cannot be read is said, not raised
                answer, error = None, f"{type(exc).__name__}: {exc}"
            results.put((serial, answer, error))

    def _poll(self):
        if self.closed:
            return
        try:
            while True:
                serial, answer, error = self.results.get_nowait()
                if serial != self.serial:
                    continue
                if error:
                    self.status.set(f"{self.path.name}\nNo preview: {error}")
                else:
                    self.picture, (width, height), kind = answer
                    self.status.set(f"{self.path.name}\n{width} × {height} pixels; {kind}. Preview contrast only; "
                                    "source values are unchanged.")
                    self._draw()
        except queue.Empty:
            pass
        self.poll = self.after(50, self._poll)

    def _resize(self, event=None):
        self.label.configure(wraplength=max(100, self.winfo_width() - 4))
        self._draw()

    def _draw(self):
        if self.picture is None:
            return
        from PIL import ImageTk
        picture = self.picture.copy()
        picture.thumbnail((max(1, self.canvas.winfo_width() - 12), max(1, self.canvas.winfo_height() - 12)))
        self.photo = ImageTk.PhotoImage(picture, master=self)
        self.canvas.delete('all')
        self.canvas.create_image(self.canvas.winfo_width() / 2, self.canvas.winfo_height() / 2, image=self.photo,
                                 anchor='center')

    def _destroyed(self, event):
        if event.widget is self:
            self.closed = True
            self.requests.put(None)
            self.after_cancel(self.poll)


def ask_name(parent, title, prompt):
    """ A small window that asks for a name; the name, or '' if cancelled """
    window = tk.Toplevel(parent)
    window.withdraw()
    window.transient(parent)
    window.title(title)
    window.configure(background=COLORS['background'])
    body = ttk.Frame(window, padding=14)
    body.pack(fill='both', expand=True)
    ttk.Label(body, text=prompt).pack(anchor='w', pady=(0, 6))
    name = tk.StringVar(window)
    entry = ttk.Entry(body, textvariable=name, width=40)
    entry.pack(fill='x')
    answer = []
    buttons = ttk.Frame(body)
    buttons.pack(fill='x', pady=(12, 0))
    ttk.Button(buttons, text='Cancel', command=window.destroy).pack(side='right')
    ttk.Button(buttons, text='OK', command=lambda: (answer.append(name.get().strip()), window.destroy())).pack(
        side='right', padx=(0, 6))
    window.bind('<Return>', lambda event: (answer.append(name.get().strip()), window.destroy()))
    window.bind('<Escape>', lambda event: window.destroy())
    hide_title_icon(window)
    window.deiconify()
    window.wait_visibility()
    window.grab_set()
    entry.focus_set()
    parent.wait_window(window)
    return answer[0] if answer else ''


class FolderBrowser(tk.Toplevel):
    """ The window; use ask_folder() to run it """

    def __init__(self, parent, title, initialdir):
        super().__init__(parent)
        self.withdraw()
        self.title(title)
        self.transient(parent)
        self.parent = parent
        self.result = ''
        self.current = Path(initialdir)
        self.entries = {}
        self.configure(background=COLORS['background'])
        self.minsize(720, 420)
        width, height = min(940, self.winfo_screenwidth() - 60), min(620, self.winfo_screenheight() - 100)
        self.geometry(f"{width}x{height}")
        self.protocol('WM_DELETE_WINDOW', self.cancel)
        self.bind('<Escape>', lambda event: self.cancel())

        body = ttk.Frame(self, padding=14)
        body.pack(fill='both', expand=True)
        navigation = ttk.Frame(body)
        navigation.pack(fill='x', pady=(0, 10))
        self.up_button = ttk.Button(navigation, text='Up', command=self.up)
        self.up_button.pack(side='left')
        ttk.Button(navigation, text='Home', command=lambda: self.navigate(Path.home())).pack(side='left', padx=(6, 0))
        ttk.Button(navigation, text='Refresh', command=lambda: self.navigate(self.current)).pack(side='left', padx=6)
        ttk.Button(navigation, text='New Folder...', command=self.new_folder).pack(side='left')

        address = ttk.Frame(body)
        address.pack(fill='x', pady=(0, 10))
        self.location = tk.StringVar(self, str(self.current))
        self.address = ttk.Entry(address, textvariable=self.location)
        self.address.pack(side='left', fill='x', expand=True)
        self.address.bind('<Return>', lambda event: self.navigate(self.location.get()))
        ttk.Button(address, text='Go', command=lambda: self.navigate(self.location.get())).pack(side='left', padx=(6, 0))

        self.panes = tk.PanedWindow(body, orient='horizontal', sashwidth=6, sashrelief='flat', borderwidth=0,
                                    background=COLORS['border'], opaqueresize=True)
        self.panes.pack(fill='both', expand=True)
        listing = ttk.Frame(self.panes)
        self.panes.add(listing, minsize=300, stretch='always')
        listing.rowconfigure(0, weight=1)
        listing.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(listing, columns=('kind', 'size'), selectmode='browse')
        self.tree.heading('#0', text='Name', anchor='w')
        self.tree.heading('kind', text='Type', anchor='w')
        self.tree.heading('size', text='Size', anchor='e')
        self.tree.column('#0', width=260, minwidth=140, stretch=True)
        self.tree.column('kind', width=65, minwidth=55, stretch=False)
        self.tree.column('size', width=85, minwidth=65, anchor='e', stretch=False)
        self.tree.grid(row=0, column=0, sticky='nsew')
        vertical = ttk.Scrollbar(listing, orient='vertical', command=self.tree.yview)
        vertical.grid(row=0, column=1, sticky='ns')
        self.tree.configure(yscrollcommand=vertical.set)
        self.tree.bind('<Double-1>', self.open_selected)
        self.tree.bind('<Return>', self.open_selected)
        self.tree.bind('<BackSpace>', lambda event: self.up())
        self.tree.bind('<<TreeviewSelect>>', self._selection_changed)
        self.preview = ImagePreview(self.panes)
        self.panes.add(self.preview, minsize=240, stretch='always')

        self.summary = tk.StringVar(self)
        self.selection = tk.StringVar(self)
        summary_label = ttk.Label(body, textvariable=self.summary, style='Muted.TLabel')
        selection_label = ttk.Label(body, textvariable=self.selection, wraplength=width - 45)
        self.actions = actions = ttk.Frame(body)
        self.accept_button = ttk.Button(actions, text='Select Folder', command=self.accept)
        self.accept_button.pack(side='right')
        ttk.Button(actions, text='Cancel', command=self.cancel).pack(side='right', padx=(0, 6))
        for widget, options in ((actions, {'fill': 'x'}), (selection_label, {'fill': 'x', 'pady': (6, 10)}),
                                (summary_label, {'anchor': 'w', 'pady': (7, 0)})):
            widget.pack(side='bottom', before=self.panes, **options)       # before the list: they are never cut off
        body.bind('<Configure>', lambda event: selection_label.configure(wraplength=max(100, event.width - 4)), add='+')
        hide_title_icon(self)
        self.navigate(initialdir)

    def navigate(self, destination):
        """ Show the contents of a folder; False (with the reason shown) if it cannot be read """
        from tkinter import messagebox
        try:
            target, rows = list_folder(destination, self.current)
        except (OSError, ValueError) as error:
            messagebox.showerror('Cannot open folder', str(error), parent=self)
            self.location.set(str(self.current))
            return False
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)
        self.entries.clear()
        self.current = target
        self.location.set(str(target))
        folders = 0
        for index, (name, folder, size) in enumerate(rows):
            self.entries[str(index)] = (target / name, folder)
            self.tree.insert('', 'end', iid=str(index), text=name,
                             values=('Folder' if folder else 'File', size_text(size)))
            folders += folder
        self.summary.set(f"{folders} folders, {len(rows) - folders} files. Double-click a folder to open it.")
        self.up_button.state(['disabled'] if target.parent == target else ['!disabled'])
        self._selection_changed()
        return True

    def selected_folder(self):
        """ The folder that is selected in the list, or else the one that is open """
        selected = self.tree.selection()
        if selected and selected[0] in self.entries:
            path, folder = self.entries[selected[0]]
            if folder:
                return path
        return self.current

    def _selection_changed(self, event=None):
        self.selection.set(f"Folder to use: {self.selected_folder()}")
        selected = self.tree.selection()
        path, folder = self.entries.get(selected[0], (None, True)) if selected else (None, True)
        path = path if not folder and is_image(path) else None
        if path != self.preview.path:
            self.preview.show(path)

    def open_selected(self, event=None):
        selected = self.tree.selection()
        if selected and selected[0] in self.entries:
            path, folder = self.entries[selected[0]]
            if folder:
                self.navigate(path)
        return 'break'

    def up(self):
        self.navigate(self.current.parent)

    def new_folder(self):
        """ Make a folder in the one that is open and open it """
        from tkinter import messagebox
        name = ask_name(self, 'New Folder', 'Folder name:')
        if not name:
            return
        if name in ('.', '..') or Path(name).anchor or any(character in name for character in ('/', '\\')):
            messagebox.showerror('Invalid folder name', 'Enter a single folder name.', parent=self)
            return
        try:
            target = self.current / name
            target.mkdir()
        except (OSError, ValueError) as error:
            messagebox.showerror('Cannot create folder', str(error), parent=self)
            return
        self.navigate(target)

    def accept(self):
        from tkinter import messagebox
        if self.location.get().strip().strip('"') != str(self.current):      # a path that was typed and not opened yet
            if not self.navigate(self.location.get()):
                return
        target = self.selected_folder()
        if not target.is_dir():
            messagebox.showerror('Folder unavailable', 'Select an existing folder.', parent=self)
            return
        self.result = str(target)
        self.destroy()

    def cancel(self):
        self.result = ''
        self.destroy()


def ask_folder(parent, title, initialdir):
    """ Open the window above another one and wait; the folder that was chosen as text, or '' if it was cancelled """
    dialog = FolderBrowser(parent, title, initialdir)
    dialog.deiconify()
    dialog.wait_visibility()
    dialog.grab_set()
    dialog.tree.focus_set()
    parent.wait_window(dialog)
    return dialog.result


# ======================================================================================
# SEVERAL FOLDERS AND THEIR REPEATED CAPTURES
# ======================================================================================

def identical_copies(copies):
    """
    Whether each copy of a repeated capture has the same files as every other copy: the same names and the same bytes
    copies: {folder: [paths]}, as rct.repeated_captures gives them. Outputs: {folder: True / False}
    """
    import filecmp
    files = {folder: {Path(path).name: path for path in paths} for folder, paths in copies.items()}
    return {folder: all(set(own) == set(other) and
                        all(filecmp.cmp(own[name], other[name], shallow=False) for name in own)
                        for folder_other, other in files.items() if folder_other != folder)
            for folder, own in files.items()}


def distinct_paths(folders):
    """ Each folder written from where the folders part: [.../survey/raw, .../calibration/raw] -> ['…\\survey\\raw', ...] """
    folders = [Path(os.path.abspath(folder)) for folder in folders]
    try:
        common = Path(os.path.commonpath(folders)) if len(folders) > 1 else None
    except ValueError:                                          # on different drives
        common = None
    if common is None or any(folder == common for folder in folders):
        return [str(folder) for folder in folders]
    return [os.path.join('…', os.path.relpath(folder, common)) for folder in folders]


def valid_choices(groups, chosen):
    """ {capture: folder} of the choices that name one of the copies of a repeated capture (the others are dropped) """
    valid = {}
    for name, copies in groups.items():
        target = chosen.get(name)
        folder = next((folder for folder in copies
                       if target is not None and os.path.normcase(os.path.abspath(folder)) ==
                       os.path.normcase(os.path.abspath(target))), None)
        if folder is not None:
            valid[name] = folder
    return valid


class RepeatedCapturesDialog(tk.Toplevel):
    """ Choose the folder whose copy is used, for each capture that is in more than one folder; no file is changed """

    def __init__(self, parent, groups, chosen=None):
        super().__init__(parent)
        self.withdraw()
        self.title('Choose the copy of each repeated capture')
        self.transient(parent)
        self.groups = groups
        self.names = list(groups)
        self.choices = valid_choices(groups, chosen or {})
        self.copies = []
        self.result = None
        self.configure(background=COLORS['background'])
        self.minsize(760, 520)
        self.geometry(f"{min(1180, self.winfo_screenwidth() - 60)}x{min(720, self.winfo_screenheight() - 100)}")
        self.protocol('WM_DELETE_WINDOW', self.cancel)
        self.bind('<Escape>', lambda event: self.cancel())

        body = ttk.Frame(self, padding=14)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='Choose which folder supplies each repeated capture.', style='Heading.TLabel').pack(anchor='w')
        guidance = ttk.Label(body, text="The capture is used with all the files of the copy that is chosen; the other "
                             "copies are left out of the processing. No file is moved or deleted. A repeated name does not "
                             "mean the same images: the Contents column says whether the files of the copies are "
                             "identical.", wraplength=1080)
        guidance.pack(fill='x', pady=(6, 10))
        self.status = tk.StringVar(self)
        status = ttk.Label(body, textvariable=self.status, style='Muted.TLabel', wraplength=1080)
        status.pack(side='bottom', fill='x', pady=(6, 0))
        actions = ttk.Frame(body)
        actions.pack(side='bottom', fill='x', pady=(8, 0))
        ttk.Button(actions, text='Use this copy', command=self.keep).pack(side='left')
        ttk.Button(actions, text='Use this folder for the remaining captures', command=self.keep_remaining).pack(
            side='left', padx=(6, 0))
        ttk.Button(actions, text='Use choices', command=self.accept).pack(side='right')
        ttk.Button(actions, text='Cancel', command=self.cancel).pack(side='right', padx=6)

        panes = tk.PanedWindow(body, orient='horizontal', sashwidth=6, sashrelief='flat', borderwidth=0,
                               background=COLORS['border'], opaqueresize=True)
        panes.pack(fill='both', expand=True)
        left = ttk.Frame(panes)
        panes.add(left, minsize=220, width=290, stretch='never')
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.captures = ttk.Treeview(left, columns=('capture', 'choice'), show='headings', selectmode='browse')
        for key, title, width in (('capture', 'Capture', 115), ('choice', 'Copy used', 150)):
            self.captures.heading(key, text=title, anchor='w')
            self.captures.column(key, width=width, minwidth=70, stretch=True)
        self.captures.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(left, orient='vertical', command=self.captures.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        self.captures.configure(yscrollcommand=scroll.set)
        right = ttk.Frame(panes)
        panes.add(right, minsize=400, stretch='always')
        right.columnconfigure(0, weight=1)
        right.rowconfigure(3, weight=1)
        ttk.Label(right, text='Copies in the folders').grid(row=0, column=0, sticky='w')
        self.sources = ttk.Treeview(right, columns=('folder', 'files', 'contents'), show='headings', selectmode='browse',
                                    height=4)
        for key, title, width in (('folder', 'Folder (the part that differs)', 300), ('files', 'Files', 50),
                                  ('contents', 'Contents', 210)):
            self.sources.heading(key, text=title, anchor='w')
            self.sources.column(key, width=width, minwidth=45, stretch=key == 'folder')
        self.sources.grid(row=1, column=0, sticky='nsew', pady=(6, 0))
        self.copy_path = tk.StringVar(self)
        path_label = ttk.Label(right, textvariable=self.copy_path, style='Muted.TLabel', wraplength=600)
        path_label.grid(row=2, column=0, sticky='ew', pady=(4, 0))
        right.bind('<Configure>', lambda event: path_label.configure(wraplength=max(200, event.width - 4)), add='+')
        self.preview = ImagePreview(right)
        self.preview.grid(row=3, column=0, sticky='nsew', pady=(8, 0))
        for index, name in enumerate(self.names):
            self.captures.insert('', 'end', iid=str(index), values=(name, self._choice_text(name)))
        self.captures.bind('<<TreeviewSelect>>', self.select_capture)
        self.sources.bind('<<TreeviewSelect>>', self.preview_copy)
        body.bind('<Configure>', lambda event: (guidance.configure(wraplength=max(300, event.width - 4)),
                                                status.configure(wraplength=max(300, event.width - 4))), add='+')
        hide_title_icon(self)
        if self.names:
            self.captures.selection_set('0')
            self.select_capture()

    def _choice_text(self, name):
        folder = self.choices.get(name)
        return Path(folder).name if folder is not None else 'Choose a copy'

    def current_name(self):
        selected = self.captures.selection()
        return self.names[int(selected[0])] if selected else None

    def selected_copy(self):
        selected = self.sources.selection()
        return self.copies[int(selected[0])] if selected else None

    def select_capture(self, event=None):
        name = self.current_name()
        if name is None:
            return
        copies = self.groups[name]
        self.copies = list(copies)
        try:
            same = identical_copies(copies)
        except OSError:
            same = {}
        self.sources.delete(*self.sources.get_children())
        shown = distinct_paths(self.copies)
        for index, folder in enumerate(self.copies):
            contents = ('' if folder not in same else
                        'Identical files' if same[folder] else 'Different files')
            self.sources.insert('', 'end', iid=str(index), values=(shown[index], len(copies[folder]), contents))
        chosen = self.choices.get(name)
        self.sources.selection_set(str(self.copies.index(chosen) if chosen in self.copies else 0))
        self.preview_copy()
        self.update_status()

    def preview_copy(self, event=None):
        name, folder = self.current_name(), self.selected_copy()
        self.copy_path.set(f"Selected copy: {folder}" if folder is not None else '')
        if name is not None and folder is not None:
            self.preview.show(sorted(self.groups[name][folder], key=lambda path: Path(path).name)[0])

    def keep(self):
        """ The selected copy is the one used; the next capture without a choice is shown """
        name, folder = self.current_name(), self.selected_copy()
        if name is None or folder is None:
            return
        self.choices[name] = folder
        self.captures.set(str(self.names.index(name)), 'choice', self._choice_text(name))
        waiting = next((index for index, item in enumerate(self.names) if item not in self.choices), None)
        if waiting is not None:
            self.captures.selection_set(str(waiting))
            self.captures.see(str(waiting))
            self.select_capture()
        self.update_status()

    def keep_remaining(self):
        """ The folder of the selected copy supplies every capture without a choice that has a copy there """
        folder = self.selected_copy()
        if folder is None:
            return
        for index, name in enumerate(self.names):
            if name not in self.choices and folder in self.groups[name]:
                self.choices[name] = folder
                self.captures.set(str(index), 'choice', self._choice_text(name))
        self.update_status()

    def update_status(self):
        self.status.set(f"{len(self.choices)} of {len(self.groups)} repeated captures have a copy chosen. Select a "
                        "capture to see its copies; Use choices keeps them.")

    def accept(self):
        from tkinter import messagebox
        waiting = [name for name in self.names if name not in self.choices]
        if waiting:
            messagebox.showerror('Choose a copy for every repeated capture',
                                 f"No copy chosen for: {', '.join(waiting)}", parent=self)
            return
        self.result = {name: str(folder) for name, folder in self.choices.items()}
        self.destroy()

    def cancel(self):
        self.result = None
        self.destroy()


def ask_repeated_captures(parent, groups, chosen=None):
    """ Open the dialog above another window and wait; {capture: folder} of the choices, or None if it was cancelled """
    previous = parent.grab_current()
    try:
        dialog = RepeatedCapturesDialog(parent, groups, chosen)
        dialog.deiconify()
        dialog.wait_visibility()
        dialog.grab_set()
        parent.wait_window(dialog)
        return dialog.result
    finally:
        if previous is not None and previous.winfo_exists():
            previous.grab_set()


class FoldersBrowser(FolderBrowser):
    """ The folder browser that builds a list of folders, from any location, and chooses the copy of repeated captures """

    def __init__(self, parent, title, initialdir, folders=(), chosen=None, pattern=None):
        self.folders = []
        super().__init__(parent, title, initialdir)
        self.chosen = dict(chosen or {})
        self.pattern = pattern
        self.result = None
        self.geometry(f"{min(1100, self.winfo_screenwidth() - 60)}x{min(740, self.winfo_screenheight() - 100)}")
        self.tree.configure(selectmode='extended')
        self.accept_button.configure(text='Use selected folders')
        ttk.Button(self.actions, text='Add selected / current folders', command=self.add_folders).pack(side='left')
        ttk.Button(self.actions, text='Remove', command=self.remove_folders).pack(side='left', padx=(6, 0))
        ttk.Button(self.actions, text='Clear list', command=lambda: self.set_folders([])).pack(side='left', padx=(6, 0))
        ttk.Button(self.actions, text='Review repeated captures...', command=lambda: self.review(force=True)).pack(
            side='left', padx=(6, 0))
        basket = ttk.Frame(self.actions.master)
        basket.pack(side='bottom', before=self.panes, fill='x', pady=(8, 4))
        basket.columnconfigure(0, weight=1)
        self.folder_list = ttk.Treeview(basket, columns=('path',), show='headings', selectmode='extended', height=3)
        self.folder_list.column('path', width=700, minwidth=150, stretch=True)
        self.folder_list.grid(row=0, column=0, sticky='nsew')
        vertical = ttk.Scrollbar(basket, orient='vertical', command=self.folder_list.yview)
        vertical.grid(row=0, column=1, sticky='ns')
        self.folder_list.configure(yscrollcommand=vertical.set)
        self.folder_list.bind('<Delete>', lambda event: self.remove_folders())
        self.set_folders(folders)

    def set_folders(self, folders):
        """ The list of folders (each once, in the order given) """
        from radiometric_correction_tools import folder_list
        self.folders = [Path(os.path.abspath(folder)) for folder in folder_list(list(folders))]
        self.folder_list.delete(*self.folder_list.get_children())
        for index, folder in enumerate(self.folders):
            self.folder_list.insert('', 'end', iid=str(index), values=(str(folder),))
        self.folder_list.heading('path', text=f"Folders to use ({len(self.folders)})", anchor='w')
        self.accept_button.state(['!disabled'] if self.folders else ['disabled'])

    def _selection_changed(self, event=None):
        super()._selection_changed(event)
        self.selection.set("Ctrl/Shift selects several folders. Add them to the list, then open another location to add "
                           "more. Selecting an image shows its picture.")

    def add_folders(self):
        """ The folders selected in the listing or, when none is, the one that is open """
        if self.location.get().strip().strip('"') != str(self.current):
            if not self.navigate(self.location.get()):
                return
        selected = [self.entries[key][0] for key in self.tree.selection() if key in self.entries and self.entries[key][1]]
        self.set_folders([*self.folders, *(selected or [self.current])])

    def remove_folders(self):
        selected = {int(key) for key in self.folder_list.selection()}
        self.set_folders([folder for index, folder in enumerate(self.folders) if index not in selected])

    def review(self, force=False):
        """
        Choose the copy of the repeated captures of the folders of the list: always with force, else only when a repeated
        capture has no choice. False if it was cancelled or the folders cannot be read
        """
        from tkinter import messagebox
        from radiometric_correction_tools import ImageFolders, repeated_captures
        try:
            groups = repeated_captures(ImageFolders(self.folders), self.pattern)
        except (OSError, ValueError) as error:
            messagebox.showerror('Review repeated captures', str(error), parent=self)
            return False
        choices = valid_choices(groups, self.chosen)
        if not groups:
            self.chosen = {}
            if force:
                messagebox.showinfo('Review repeated captures', 'No capture is in more than one of the folders.',
                                    parent=self)
            return True
        if force or len(choices) < len(groups):
            answer = ask_repeated_captures(self, groups, choices)
            if answer is None:
                return False
            choices = answer
        self.chosen = {name: str(folder) for name, folder in choices.items()}
        self.folder_list.heading('path', text=f"Folders to use ({len(self.folders)}); {len(groups)} repeated "
                                 "captures, copy chosen", anchor='w')
        return True

    def accept(self):
        from tkinter import messagebox
        if not self.folders:
            return
        missing = [folder for folder in self.folders if not folder.is_dir()]
        if missing:
            messagebox.showerror('Folder unavailable', 'These folders do not exist:\n' +
                                 '\n'.join(str(folder) for folder in missing), parent=self)
            return
        if not self.review():
            return
        self.result = ([str(folder) for folder in self.folders], self.chosen)
        self.destroy()

    def cancel(self):
        self.result = None
        self.destroy()


def ask_folders(parent, title, initialdir, folders=(), chosen=None, pattern=None):
    """
    Open the browser of several folders above another window and wait
    Outputs: ([folders], {capture: folder of the copy used} of the repeated captures), or None if it was cancelled
    """
    dialog = FoldersBrowser(parent, title, initialdir, folders, chosen, pattern)
    dialog.deiconify()
    dialog.wait_visibility()
    dialog.grab_set()
    dialog.tree.focus_set()
    parent.wait_window(dialog)
    return dialog.result
