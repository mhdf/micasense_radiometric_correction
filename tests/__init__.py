"""
What the tests share

- Every Tk root that a test makes is kept until the tests end. The tests make hundreds of windows in one process, and the
  threads that read images in the background can be the ones that free a root that is no longer used: with Python 3.12,
  Tcl then stops the process ("Tcl_AsyncDelete: async handler deleted by the wrong thread"). The program has one window
  for its whole life, so this only concerns the tests.
- require_window_size: the tests of the layout need windows larger than some screens (GitHub's computers have one small
  screen), and are skipped there instead of failing.
"""

# Standard Library Imports
import tkinter

_ROOTS = []
_tk_init = tkinter.Tk.__init__


def _kept_tk_init(self, *arguments, **options):
    _tk_init(self, *arguments, **options)
    _ROOTS.append(self)


tkinter.Tk.__init__ = _kept_tk_init


def require_window_size(test, root, width, height):
    """ Give the window this size, or skip the test when the screen cannot hold it """
    root.geometry(f"{width}x{height}")
    root.update()
    if root.winfo_width() < width or root.winfo_height() < height:
        test.skipTest(f"the screen cannot hold a window of {width} x {height} (got {root.winfo_width()} x "
                      f"{root.winfo_height()})")
