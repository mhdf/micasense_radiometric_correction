# Development notes

[Back to the README](../README.md)

<div align="justify">

## Project layout

```
radiometric_correction_tools.py   the library (it keeps its name and its place: other programs import it)
radiometric_app/                  the program: configuration and runner, UI, panel regions tool, capture picker, ...
    presets/                      the camera presets that come with the program
tests/                            the tests
docs/                             the guide pages, the figure of the tools and the screenshots of the UI
_config.yml                       the website of the documentation (GitHub Pages: the README and docs/, see below)
launchers/                        launchers for Windows (double-click), the choice of the Python and the check of the computer
config/                           your configuration, profiles, presets and chosen Python (not kept in git)
requirements.txt, environment.yml the Python packages (pip) and the conda environment (see docs/installation.md)
open_radiometric_window.cmd       double-click to open the UI (the other launchers are in launchers/)
images/raw/                       the example images: IMG_0003, IMG_0005 and IMG_0009 show the reference panel before the flight, IMG_0422 and IMG_0423 after it, IMG_0010 is the capture to correct
reference_panel_spectral_response/ the spectral response (reflectance by wavelength) of the panel of the example
results/                          what the program writes (not kept in git)
```

The commands in the guide are run from the project folder (the one with `radiometric_correction_tools.py`), for example `python -m radiometric_app.radiometric_window`. `radiometric_app` can be copied into another program together with `radiometric_correction_tools.py` (the modules of the folder import each other with relative imports).

## The website of the documentation

GitHub Pages publishes the README (the home page) and the pages of `docs/` as a website, built by GitHub from the branch `main` (Settings > Pages > Deploy from a branch > `main`, `/ (root)`) every time it changes. `_config.yml` chooses the theme (Just the Docs), leaves out the program, the tests and the images, and gives each page its title and its place in the side bar: a new page of `docs/` needs a line there, in the order of the table of the README (a test checks it). The Markdown files have no front matter, so they read the same on GitHub; the links between them are converted to the pages of the website.

## How the UI is built

- The pages (Inputs, Reference panel, DLS) are built from `radiometric_workflow.FIELDS`, so a new key appears in the UI without changes to it. The explanations of the help column are the `label` and `help` of each entry of `FIELDS` and `GROUP_NOTES`, written with a few marks (a line that starts with `# ` is a sub-heading, one that starts with `- ` is an item of a list, any other line is a paragraph). Labels and descriptions may change in future versions, the keys will not.
- `radiometric_window.RadiometricFrame` builds its widgets in a `ttk.Frame` (`.widget`), so it can be placed in any window, for example as a page of another program's UI. `radiometric_app/ui_theme.py` holds the colors and styles of the dark theme.
- `ConfigModel` (the text typed in each field, its conversion and the file) has no window and is tested without a display.

## Tests

```
python -m unittest discover -s tests -t . -v
```

The tests use the example images and need ExifTool (set the environment variable `exiftoolpath` if it is not in the PATH). The tests of the UI need a display and are skipped without one. `tests/test_readme.py` checks the documentation: the contents list of the README has every section in order, every link of the README and of the pages of `docs` reaches an existing file and heading, and the images exist.

</div>
