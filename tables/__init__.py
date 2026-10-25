"""Tables, found by name, opened in a window.

    from tables import ReadOnlyTable, StagedTable

    ReadOnlyTable(dsn, "fact_table", schema="analytics").open()
    StagedTable(dsn, "model_e", updates="updates").open()

The hierarchy is the point. ``DatabaseTable`` is a dsn, a schema and a table
name, reflected; what a subclass adds is what you are allowed to do with it:

    DatabaseTable      finds the table and works out its columns
      ReadOnlyTable    browse: sort, filter, search, copy
      StagedTable      edit: staging, validation, undo, commit

Both windows are the same grid -- the same header, filter dialog, search,
clipboard, row numbers and lazy window -- so neither is a reimplementation of
the other. The read-only one simply has no staging block and no editable cell.

To add a third, subclass ``DatabaseTable`` and point ``window_class`` at your
window, or override ``window()`` when it needs more than the table to build.

A Qt application has to exist before any window does::

    from PySide6.QtWidgets import QApplication
    from app import apply_theme

    app = QApplication([])
    apply_theme(app)
    table = ReadOnlyTable(dsn, "fact_table")
    table.open()
    app.exec()
"""

from .base import DatabaseTable
from .browse import BrowseWindow, ReadOnlyTable
from .staged import StagedTable

__all__ = [
    "DatabaseTable",
    "ReadOnlyTable",
    "StagedTable",
    "BrowseWindow",
]
