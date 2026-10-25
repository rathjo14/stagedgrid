"""Browsing a table: everything the editor does to *look* at data, and nothing
that changes it.

Sorting, the filter header and its per-type dialog, quick search, copy, the
row numbers, dark mode and the lazy window are all the grid's own, so this
reuses them rather than growing a second copy that would drift. What it leaves
out is the whole editing half: no staging block, no validation, no undo, no
commit, and no cell is editable.

The window holds a window's worth of rows and asks the database for the rest,
exactly as the editor does -- so the table being large is not a reason to
avoid opening it, and a sort still means the whole table rather than the part
that happens to be loaded.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QToolBar,
)

import grid
from data import RowMatcher, row_order_key
from grid import GridView, StagedTableModel, stylesheet
from sqlview import MergedView, SqlView, Untranslatable, ViewQuery

from .base import DatabaseTable

#: Rows held at once, as in the editor: the database does the ordering,
#: filtering and searching over the whole table, so this only bounds what is
#: fetched to show.
WINDOW_ROWS = 1_000


class BrowseWindow(QMainWindow):
    """A read-only grid over one table."""

    def __init__(
        self,
        table: DatabaseTable,
        parent=None,
        window: Optional[int] = None,
    ) -> None:
        super().__init__(parent)
        self.table = table
        self.window_size = window or WINDOW_ROWS
        self.total_rows = 0
        self._requerying = False
        self._shape = None

        self.setWindowTitle(f"{table.table_name} - read only")
        self.resize(1400, 820)
        self.setStyleSheet(stylesheet())

        self.model = StagedTableModel(table.spec, editable=False)
        self.source = MergedView(
            table.engine, SqlView(table.model, table.spec, table.lookups)
        )
        self.model.set_window_source(self.source)

        self.view = GridView(self.model, self)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerItem)
        self.view.verticalScrollBar().valueChanged.connect(self._window_follow)
        self.setCentralWidget(self.view)

        self._build_toolbar()
        self._build_status_bar()
        self.model.viewChanged.connect(self._reload_view)
        self.view.filterRequested.connect(self.show_filter_dialog)
        self.view.statsChanged.connect(self._stats_label.setText)
        self.model.message.connect(self.flash)
        self.reload()

    # ------------------------------------------------------------- chrome
    def _build_toolbar(self) -> None:
        bar = QToolBar("Main", self)
        bar.setMovable(False)
        bar.setIconSize(QSize(16, 16))
        self.addToolBar(bar)

        self.action_reload = QAction("Reload", self)
        self.action_reload.setShortcut(QKeySequence.StandardKey.Refresh)
        self.action_reload.triggered.connect(self.reload)
        bar.addAction(self.action_reload)
        bar.addSeparator()

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search all columns...  (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(260)
        # Wait for a pause in the typing: every keystroke is a query.
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(
            lambda: self.model.set_quick_search(self.search.text())
        )
        self.search.textChanged.connect(lambda _: self._search_timer.start())
        bar.addWidget(self.search)

        find = QAction("Find", self)
        find.setShortcut(QKeySequence.StandardKey.Find)
        find.triggered.connect(self.search.setFocus)
        self.addAction(find)
        bar.addSeparator()

        self.action_clear_filters = QAction("Clear filters", self)
        self.action_clear_filters.triggered.connect(self.model.clear_filters)
        bar.addAction(self.action_clear_filters)

        self.action_clear_sort = QAction("Clear sort", self)
        self.action_clear_sort.triggered.connect(self.model.clear_sort)
        bar.addAction(self.action_clear_sort)

        self.action_fit = QAction("Fit columns", self)
        self.action_fit.triggered.connect(self.view.resizeColumnsToContents)
        bar.addAction(self.action_fit)
        bar.addSeparator()

        self.action_dark = QAction("Dark mode", self)
        self.action_dark.setCheckable(True)
        self.action_dark.setChecked(grid.C is grid.DARK)
        self.action_dark.toggled.connect(self.toggle_dark)
        bar.addAction(self.action_dark)

    def _build_status_bar(self) -> None:
        self._counts_label = QLabel()
        self._stats_label = QLabel()
        self._message_label = QLabel()
        self._message_label.setContentsMargins(18, 0, 0, 0)
        self.statusBar().addWidget(self._counts_label)
        self.statusBar().addWidget(self._message_label, 1)
        self.statusBar().addPermanentWidget(self._stats_label)
        self.statusBar().setSizeGripEnabled(False)

    def toggle_dark(self, dark: bool) -> None:
        from app import apply_theme

        apply_theme(QApplication.instance(), "dark" if dark else "light")
        self.setStyleSheet(stylesheet())
        for widget in (
            self.view,
            self.view.horizontalHeader(),
            self.view.verticalHeader(),
        ):
            widget.viewport().update()
        self._refresh()

    def flash(self, text: str, error: bool = False) -> None:
        self._message_label.setStyleSheet(
            f"color: {grid.C.error_border.name()}; font-weight: bold;"
            if error
            else f"color: {grid.C.text.name()};"
        )
        self._message_label.setText(text)

    def _refresh(self) -> None:
        self._counts_label.setText(
            f"  {self.total_rows:,} row(s), holding "
            f"{self.model.resident_rows():,}"
        )

    # --------------------------------------------------------------- data
    def reload(self) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.model.load([], self.table.lookups)
            self._refresh_window(0, structural=True)
            self._shape = self._view_shape()
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Load failed", str(exc))
            return
        QApplication.restoreOverrideCursor()
        self.flash(
            f"{self.total_rows:,} row(s) in {self.table.table_name}, "
            f"holding {self.model.resident_rows():,}"
        )

    def _view_shape(self) -> tuple:
        return (
            tuple(self.model.sort_keys),
            tuple(
                (key, flt.describe())
                for key in sorted(self.model.filters.active_keys())
                for flt in [self.model.filters.get(key)]
                if flt is not None
            ),
            self.model.quick_search,
        )

    def _query(self) -> ViewQuery:
        return ViewQuery(
            sort=tuple(self.model.sort_keys),
            filters=self.model.filters,
            search=self.model.quick_search,
        )

    def _refresh_window(self, at: int, structural: bool = False) -> bool:
        """Ask the database for the view, and hold the slice starting at ``at``."""
        if self._requerying:
            return False
        refused = None
        failed = None
        self._requerying = True
        try:
            lookups = self.model.lookups
            self.source.view.lookups = lookups
            self.source.rules(
                RowMatcher(
                    self.table.spec, lookups,
                    self.model.filters, self.model.quick_search,
                ),
                row_order_key(self.table.spec, lookups, self.model.sort_keys),
            )
            # Nothing is ever held: there is no editing, so the database's
            # answer is the whole answer.
            self.source.refresh(self._query(), [])
            self.total_rows = self.source.visible_count()
            rows = self.source.visible_slice(at, self.window_size)
            self.model.load_window(rows, at, structural=structural)
        except Untranslatable as exc:
            refused = exc
        except Exception as exc:
            failed = exc
        finally:
            self._requerying = False

        if failed is not None:
            self.flash(f"Query failed: {failed}", True)
            return False
        if refused is not None:
            if refused.key is not None:
                self.model.clear_filters(refused.key)
            self.flash(f"{refused}, so that filter was not applied", True)
            return False
        self._refresh()
        return True

    def _reload_view(self) -> None:
        shape = self._view_shape()
        reordered = shape != self._shape
        self._shape = shape
        if not self._refresh_window(
            0 if reordered else self.model.window_at, structural=True
        ):
            return
        if reordered:
            self.view.scrollToTop()
        self.flash(f"{self.total_rows:,} row(s) match")

    def _window_follow(self) -> None:
        if self._requerying:
            return
        top = max(0, self.view.verticalScrollBar().value() - self.model.staged_count)
        height = max(1, self.view.verticalHeader().defaultSectionSize())
        onscreen = max(1, self.view.viewport().height() // height)
        low = self.model.window_at
        high = low + self.model.resident_rows()
        if low <= top and top + onscreen <= high:
            return
        self._refresh_window(max(0, top - self.window_size // 4))

    # ------------------------------------------------------------ filters
    def show_filter_dialog(self, section: int) -> None:
        from app import FilterDialog

        col = self.model.column(section)
        if not col.kind.filterable:
            self.flash(f"{col.header} cannot be filtered ({col.kind.value})", True)
            return
        dialog = FilterDialog(self.model, section, self)
        if dialog.exec():
            self.model.set_filter(dialog.result_filter())


class ReadOnlyTable(DatabaseTable):
    """A table you can look at: sort, filter, search, copy. Nothing writes.

        ReadOnlyTable(dsn, "fact_table", schema="analytics").open()

    It needs nothing of the table but that it exists -- no natural key, no
    foreign keys, no enum -- because none of what it offers depends on them.
    """

    window_class = BrowseWindow
