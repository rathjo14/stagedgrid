"""The window: filter dialog, main window, entry point.

There is no pending-changes panel. To get at the staged work, go through
data.py (see its docstring) -- in short, ``window.model.changeset()`` for the
pending changes and ``repo.load_rows()`` for the data.

A sink is just a callable that takes ``ChangeSet.to_payload()`` and returns a
one-line description for the status bar.  ``file_sink()`` is the default (it
writes ``changesets/changeset-<stamp>.json``); plugging in the real service is::

    MainWindow(repo, sink=my_api.submit_changes)

Run it from the command line::

    python app.py --dsn ... --limit 2000 --theme dark

Colours live in grid.py; read them as ``grid.C`` so a theme switch is seen.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Optional, Sequence


def _use_venv_if_needed() -> None:
    """Re-run under ./.venv when the current interpreter lacks the deps.

    So that ``python app.py`` works whether or not the venv is activated.
    Delete this (and the call below) if you manage environments yourself.
    """
    import importlib.util

    if importlib.util.find_spec("PySide6") is not None:
        return
    here = Path(__file__).resolve().parent
    for candidate in (here / ".venv/Scripts/python.exe", here / ".venv/bin/python"):
        if candidate.exists() and candidate != Path(sys.executable).resolve():
            import subprocess

            raise SystemExit(
                subprocess.call([str(candidate), str(Path(__file__).resolve()), *sys.argv[1:]])
            )
    raise SystemExit(
        "PySide6 is not installed for this interpreter and no .venv was found "
        "next to app.py.\nTry:  python -m venv .venv && "
        ".venv/Scripts/python -m pip install -r requirements.txt"
    )


_use_venv_if_needed()

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import (
    QAction,
    QKeySequence,
    QUndoStack,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTextBrowser,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from schema import ColumnSpec, ModelE, describe, dsn, make_engine
from data import ColumnFilter, Condition, Join, Op, Repository, ops_for
import grid
from grid import GridView, StagedTableModel, set_theme, stylesheet

import qdarktheme    # the window chrome
import darkdetect     # which way "auto" goes (Qt only knows from 6.5)

BLANKS_LABEL = "(Blanks)"   # how an empty value reads in the filter checklist

SHORTCUTS = [
    ("Ctrl+C / Ctrl+Shift+C", "Copy selection / copy with headers"),
    ("Ctrl+X / Ctrl+V", "Cut / paste (tab separated, Excel compatible)"),
    ("Delete", "Clear the selected cells -- or delete whole selected rows"),
    ("Ctrl+D / Ctrl+R", "Fill down / fill right"),
    ("Ctrl+Plus", "Add a staged row"),
    ("Ctrl+Minus", "Delete the selected rows"),
    ("Ctrl+Backspace", "Revert pending edits on the selected rows"),
    ("Ctrl+Z / Ctrl+Y", "Undo / redo"),
    ("F2 / Shift+F2", "Edit cell / open the large editor"),
    ("Alt+Down", "Open the drop-down in a key cell"),
    ("Ctrl+Shift+L", "Filter the current column"),
    ("Ctrl+F", "Jump to the search box"),
    ("Click an id", "Select the whole row; drag or Ctrl+click for several"),
    ("Right-click an id", "Delete, revert or restore those rows"),
    ("Click a header", "Sort; Ctrl+click adds a second sort level"),
    ("Click the red arrow", "Restore the rows deleted at that position"),
]


def file_sink(directory: str | Path = "changesets") -> Callable[[dict[str, Any]], str]:
    """Return a sink that writes each payload to ``<directory>/changeset-<stamp>.json``."""

    def submit(payload: dict[str, Any]) -> str:
        folder = Path(directory)
        folder.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = folder / f"changeset-{stamp}.json"
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return f"changeset written to {path}"

    return submit


class FilterDialog(QDialog):
    """Excel-style filter for one column: value checklist plus two conditions."""

    def __init__(
        self,
        column: ColumnSpec,
        values: Sequence[tuple[str, int]],
        current: Optional[ColumnFilter] = None,
        sort_state: Optional[bool] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.column = column
        self._values = list(values)
        self.result_filter: Optional[ColumnFilter] = None
        self.sort_request: Optional[bool] = None  # True = descending
        self.clear_sort = False

        self.setWindowTitle(f"Filter - {column.header}")
        self.setMinimumWidth(380)

        root = QVBoxLayout(self)
        root.addWidget(self._build_header())
        root.addWidget(self._build_sort_row(sort_state))
        root.addWidget(self._build_conditions(current))
        root.addWidget(self._build_value_list(current), 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        clear = buttons.addButton("Clear filter", QDialogButtonBox.ButtonRole.ResetRole)
        clear.clicked.connect(self._on_clear)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ------------------------------------------------------------- widgets
    @staticmethod
    def _row(*widgets: QWidget) -> QWidget:
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        for widget in widgets:
            layout.addWidget(widget, 1 if isinstance(widget, QLineEdit) else 0)
        return box

    def _build_header(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(
            QLabel(f"<b>{self.column.header}</b>  <code>{self.column.key}</code>")
        )
        if self.column.doc:
            doc = QLabel(self.column.doc)
            doc.setWordWrap(True)
            doc.setStyleSheet("color: #555;")
            layout.addWidget(doc)
        return box

    def _build_sort_row(self, sort_state: Optional[bool]) -> QWidget:
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        numeric = self.column.kind.numeric
        asc = QPushButton("Sort 1 → 9" if numeric else "Sort A → Z")
        desc = QPushButton("Sort 9 → 1" if numeric else "Sort Z → A")
        asc.clicked.connect(lambda: self._sort(False))
        desc.clicked.connect(lambda: self._sort(True))
        layout.addWidget(asc)
        layout.addWidget(desc)
        if sort_state is not None:
            drop = QPushButton("Remove sort")
            drop.clicked.connect(self._drop_sort)
            layout.addWidget(drop)
        layout.addStretch(1)
        return box

    def _build_conditions(self, current: Optional[ColumnFilter]) -> QWidget:
        group = QGroupBox("Condition")
        form = QFormLayout(group)
        ops = ops_for(self.column)

        self.op1 = QComboBox()
        self.op2 = QComboBox()
        for combo in (self.op1, self.op2):
            combo.addItem("(none)", None)
            for op in ops:
                combo.addItem(op.value, op)
            combo.currentIndexChanged.connect(self._sync_condition_inputs)

        self.val1 = QLineEdit()
        self.val1b = QLineEdit()
        self.val1b.setPlaceholderText("and")
        self.val2 = QLineEdit()
        self.val2b = QLineEdit()
        self.val2b.setPlaceholderText("and")

        self.join = QComboBox()
        self.join.addItem("And", Join.AND)
        self.join.addItem("Or", Join.OR)

        form.addRow(self._row(self.op1, self.val1, self.val1b))
        form.addRow(self._row(self.join, self.op2, self.val2, self.val2b))

        if current is not None and current.conditions:
            self._load_condition(0, current.conditions[0])
            if len(current.conditions) > 1:
                self._load_condition(1, current.conditions[1])
            at = self.join.findData(current.join)
            if at >= 0:
                self.join.setCurrentIndex(at)
        self._sync_condition_inputs()
        return group

    def _load_condition(self, slot: int, cond: Condition) -> None:
        combo = self.op1 if slot == 0 else self.op2
        at = combo.findData(cond.op)
        if at >= 0:
            combo.setCurrentIndex(at)
        (self.val1 if slot == 0 else self.val2).setText(cond.operand)
        (self.val1b if slot == 0 else self.val2b).setText(cond.operand2)

    def _slots(self) -> tuple[tuple[QComboBox, QLineEdit, QLineEdit], ...]:
        return ((self.op1, self.val1, self.val1b), (self.op2, self.val2, self.val2b))

    def _sync_condition_inputs(self) -> None:
        for combo, first, second in self._slots():
            op: Optional[Op] = combo.currentData()
            arity = 0 if op is None else op.arity
            first.setEnabled(arity >= 1)
            second.setVisible(arity == 2)
            if arity == 0:
                first.clear()
            if arity != 2:
                second.clear()
        enabled = self.op1.currentData() is not None
        self.join.setEnabled(enabled)
        self.op2.setEnabled(enabled)

    def _build_value_list(self, current: Optional[ColumnFilter]) -> QWidget:
        group = QGroupBox("Values")
        layout = QVBoxLayout(group)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search values...")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_search)
        layout.addWidget(self.search)

        self.select_all = QCheckBox("(Select all)")
        self.select_all.setTristate(True)
        self.select_all.clicked.connect(self._toggle_all)
        layout.addWidget(self.select_all)

        self.list = QListWidget()
        self.list.setUniformItemSizes(True)
        self.list.setAlternatingRowColors(True)
        allowed = current.allowed if current is not None else None
        for text, count in self._values:
            item = QListWidgetItem(f"{text or BLANKS_LABEL}   ({count})")
            item.setData(Qt.ItemDataRole.UserRole, text)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked = allowed is None or text in allowed
            item.setCheckState(
                Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            )
            self.list.addItem(item)
        self.list.itemChanged.connect(lambda *_: self._refresh_select_all())
        layout.addWidget(self.list, 1)

        hint = QLabel(f"{len(self._values)} distinct value(s)")
        hint.setStyleSheet("color: #777;")
        layout.addWidget(hint)
        self._refresh_select_all()
        return group

    def sizeHint(self) -> QSize:
        return QSize(420, 560)

    # -------------------------------------------------------------- actions
    def _apply_search(self, text: str) -> None:
        needle = text.casefold()
        for i in range(self.list.count()):
            item = self.list.item(i)
            label = str(item.data(Qt.ItemDataRole.UserRole) or BLANKS_LABEL)
            item.setHidden(bool(needle) and needle not in label.casefold())

    def _visible_items(self) -> list[QListWidgetItem]:
        return [
            self.list.item(i)
            for i in range(self.list.count())
            if not self.list.item(i).isHidden()
        ]

    def _toggle_all(self) -> None:
        target = (
            Qt.CheckState.Checked
            if self.select_all.checkState() != Qt.CheckState.Unchecked
            else Qt.CheckState.Unchecked
        )
        self.list.blockSignals(True)
        for item in self._visible_items():
            item.setCheckState(target)
        self.list.blockSignals(False)
        self._refresh_select_all()

    def _refresh_select_all(self) -> None:
        items = self._visible_items()
        checked = sum(1 for i in items if i.checkState() == Qt.CheckState.Checked)
        self.select_all.blockSignals(True)
        if not items or checked == 0:
            self.select_all.setCheckState(Qt.CheckState.Unchecked)
        elif checked == len(items):
            self.select_all.setCheckState(Qt.CheckState.Checked)
        else:
            self.select_all.setCheckState(Qt.CheckState.PartiallyChecked)
        self.select_all.blockSignals(False)

    def _sort(self, descending: bool) -> None:
        self.sort_request = descending
        self._on_accept()

    def _drop_sort(self) -> None:
        self.clear_sort = True
        self._on_accept()

    def _on_clear(self) -> None:
        self.result_filter = ColumnFilter(key=self.column.key)
        self.accept()

    def _collect_conditions(self) -> Optional[tuple[Condition, ...]]:
        out: list[Condition] = []
        for combo, first, second in self._slots():
            op: Optional[Op] = combo.currentData()
            if op is None:
                continue
            operand, operand2 = first.text().strip(), second.text().strip()
            if op.arity >= 1 and not operand:
                QMessageBox.warning(self, "Filter", f"'{op.value}' needs a value.")
                return None
            if op.arity == 2 and not operand2:
                QMessageBox.warning(self, "Filter", f"'{op.value}' needs two values.")
                return None
            if self.column.kind.numeric and op.arity:
                for text in filter(None, (operand, operand2)):
                    try:
                        float(text.replace(",", ""))
                    except ValueError:
                        QMessageBox.warning(self, "Filter", f"{text!r} is not a number.")
                        return None
            out.append(Condition(op=op, operand=operand, operand2=operand2))
        return tuple(out)

    def _on_accept(self) -> None:
        conditions = self._collect_conditions()
        if conditions is None:
            return
        checked = {
            str(self.list.item(i).data(Qt.ItemDataRole.UserRole))
            for i in range(self.list.count())
            if self.list.item(i).checkState() == Qt.CheckState.Checked
        }
        all_values = {text for text, _ in self._values}
        allowed = None if checked == all_values else frozenset(checked)
        if allowed is not None and not allowed and not conditions:
            QMessageBox.warning(
                self, "Filter", "That would hide every row. Select at least one value."
            )
            return
        self.result_filter = ColumnFilter(
            key=self.column.key,
            allowed=allowed,
            conditions=conditions,
            join=self.join.currentData() or Join.AND,
        )
        self.accept()


class MainWindow(QMainWindow):
    """Toolbar, menus, the grid and the status bar.

    The pending changes are not shown in a panel: read them from
    ``window.model.changeset()`` (see the module docstring).
    """

    def __init__(
        self,
        repository: Repository,
        sink: Optional[Callable[[dict[str, Any]], str]] = None,
        limit: Optional[int] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.sink = sink or file_sink()
        self.limit = limit
        self._first_load = True

        self.setWindowTitle(f"stagedgrid - {repository.spec.table_name}")
        self.resize(1480, 840)
        self.setStyleSheet(stylesheet())

        self.undo_stack = QUndoStack(self)
        self.model = StagedTableModel(repository.spec, undo_stack=self.undo_stack)
        self.view = GridView(self.model, self)
        self.setCentralWidget(self.view)

        self._build_actions()
        self._build_toolbar()
        self._build_menus()
        self._build_status_bar()

        self.view.filterRequested.connect(self.show_filter_dialog)
        self.view.statsChanged.connect(self._stats_label.setText)
        self.model.stateChanged.connect(self._refresh)
        self.model.message.connect(self.flash)

        self.reload()

    # ------------------------------------------------------------- chrome
    def _build_actions(self) -> None:
        self.action_reload = QAction("Reload", self)
        self.action_reload.setShortcut(QKeySequence.StandardKey.Refresh)
        self.action_reload.triggered.connect(self.reload)

        self.action_add_row = QAction("Add row", self)
        self.action_add_row.setShortcut("Ctrl++")
        self.action_add_row.triggered.connect(lambda: self.model.add_staged_rows(1))

        self.action_delete = QAction("Delete rows", self)
        self.action_delete.setShortcut("Ctrl+-")
        self.action_delete.triggered.connect(self.view.delete_selected_rows)

        self.action_revert = QAction("Revert rows", self)
        self.action_revert.setShortcut("Ctrl+Backspace")
        self.action_revert.triggered.connect(self.view.revert_selected_rows)

        self.action_undo = self.undo_stack.createUndoAction(self, "Undo")
        self.action_undo.setShortcut(QKeySequence.StandardKey.Undo)
        self.action_redo = self.undo_stack.createRedoAction(self, "Redo")
        self.action_redo.setShortcut(QKeySequence.StandardKey.Redo)

        self.action_clear_filters = QAction("Clear filters", self)
        self.action_clear_filters.triggered.connect(self.model.clear_filters)

        self.action_clear_sort = QAction("Clear sort", self)
        self.action_clear_sort.triggered.connect(self.model.clear_sort)

        self.action_dark = QAction("Dark mode", self)
        self.action_dark.setCheckable(True)
        self.action_dark.setChecked(grid.C is grid.DARK)
        self.action_dark.toggled.connect(self.toggle_dark)

        self.action_show_deleted = QAction("Show deleted rows", self)
        self.action_show_deleted.setCheckable(True)
        self.action_show_deleted.toggled.connect(self.model.set_show_deleted)

        self.action_commit = QAction("Commit", self)
        self.action_commit.setShortcut("Ctrl+S")
        self.action_commit.triggered.connect(self.commit)

        self.action_export = QAction("Export changeset...", self)
        self.action_export.triggered.connect(self.export_changeset)

        self.action_fit = QAction("Fit columns", self)
        self.action_fit.triggered.connect(self.view.resizeColumnsToContents)

        self.action_focus_search = QAction("Find", self)
        self.action_focus_search.setShortcut(QKeySequence.StandardKey.Find)
        self.action_focus_search.triggered.connect(lambda: self.search.setFocus())

        self.action_shortcuts = QAction("Keyboard shortcuts", self)
        self.action_shortcuts.setShortcut("F1")
        self.action_shortcuts.triggered.connect(self.show_shortcuts)

        self.action_quit = QAction("Quit", self)
        self.action_quit.setShortcut(QKeySequence.StandardKey.Quit)
        self.action_quit.triggered.connect(self.close)
        self.addAction(self.action_focus_search)

    def _build_toolbar(self) -> None:
        bar = QToolBar("Main")
        bar.setMovable(False)
        self.addToolBar(bar)
        bar.addAction(self.action_reload)
        bar.addSeparator()
        bar.addAction(self.action_add_row)
        bar.addAction(self.action_delete)
        bar.addAction(self.action_revert)
        bar.addSeparator()
        bar.addAction(self.action_undo)
        bar.addAction(self.action_redo)
        bar.addSeparator()

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search all columns...   (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(260)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(180)
        self._search_timer.timeout.connect(
            lambda: self.model.set_quick_search(self.search.text())
        )
        self.search.textChanged.connect(lambda _: self._search_timer.start())
        bar.addWidget(self.search)
        bar.addAction(self.action_clear_filters)
        bar.addAction(self.action_clear_sort)

        self.deleted_toggle = QCheckBox("Show deleted")
        self.deleted_toggle.toggled.connect(self.action_show_deleted.setChecked)
        self.action_show_deleted.toggled.connect(self.deleted_toggle.setChecked)
        bar.addWidget(self.deleted_toggle)
        bar.addSeparator()
        bar.addAction(self.action_commit)

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction(self.action_reload)
        file_menu.addAction(self.action_export)
        file_menu.addSeparator()
        file_menu.addAction(self.action_quit)

        edit_menu = self.menuBar().addMenu("&Edit")
        edit_menu.addAction(self.action_undo)
        edit_menu.addAction(self.action_redo)
        edit_menu.addSeparator()
        edit_menu.addAction(self.action_add_row)
        edit_menu.addAction(self.action_delete)
        edit_menu.addAction(self.action_revert)
        edit_menu.addSeparator()
        edit_menu.addAction("Copy  (Ctrl+C)", self.view.copy_selection)
        edit_menu.addAction("Paste  (Ctrl+V)", self.view.paste)
        edit_menu.addAction("Fill down  (Ctrl+D)", lambda: self.view.fill(True))

        data_menu = self.menuBar().addMenu("&Data")
        data_menu.addAction(self.action_clear_filters)
        data_menu.addAction(self.action_clear_sort)
        data_menu.addAction(self.action_show_deleted)
        data_menu.addAction(self.action_fit)
        data_menu.addAction(self.action_dark)
        data_menu.addSeparator()
        data_menu.addAction(self.action_commit)

        help_menu = self.menuBar().addMenu("&Help")
        help_menu.addAction(self.action_shortcuts)
        help_menu.addAction("Colour legend", self.show_legend)

    def createPopupMenu(self):
        """No right-click menu for hiding the toolbar -- it is permanent."""
        return None

    def _build_status_bar(self) -> None:
        self._counts_label = QLabel()
        self._stats_label = QLabel()
        self._message_label = QLabel()
        self._message_label.setContentsMargins(18, 0, 0, 0)
        self.statusBar().addWidget(self._counts_label)
        self.statusBar().addWidget(self._message_label, 1)
        self.statusBar().addPermanentWidget(self._stats_label)
        self.statusBar().setSizeGripEnabled(False)

    # --------------------------------------------------------------- data
    def reload(self) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            lookups = self.repository.load_lookups()
            rows = self.repository.load_rows(limit=self.limit)
            self.model.load(rows, lookups)
            self.model.validator.set_existing_keys(self.repository.natural_keys())
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(
                self, "Load failed", f"{exc}{demo_schema_hint(self.repository, exc)}"
            )
            return
        QApplication.restoreOverrideCursor()
        note = f"Loaded {len(rows)} row(s) from {self.repository.spec.table_name}"
        if self._first_load:
            self._first_load = False
            note += self._filter_to_latest_version()
        self.flash(note)

    def _version_column(self) -> Optional[ColumnSpec]:
        """The natural-key foreign key that points at a version dimension."""
        for col in self.model.spec.key_columns:
            if col.fk is not None and col.fk.label_attr in ("version", "revision"):
                return col
        return None

    def _filter_to_latest_version(self) -> str:
        """Open on the newest version only; clearing the filter shows them all."""
        col = self._version_column()
        if col is None:
            return ""
        choices = self.model.lookups.choices(col)  # naturally sorted, newest last
        if not choices:
            return ""
        latest = choices[-1]
        self.model.set_filter(ColumnFilter(key=col.key, allowed=frozenset({latest})))
        return f"   |   filtered to {col.header} {latest} (newest of {len(choices)})"

    def _refresh(self) -> None:
        counts = self.model.dirty_counts()
        self._counts_label.setText(
            f"  {self.model.total_db_rows()} row(s), showing "
            f"{self.model.visible_db_rows()}   |   "
            f"+{counts['inserts']}  ~{counts['updates']}  -{counts['deletes']}"
            + (f"   |   {counts['errors']} problem(s)" if counts["errors"] else "")
        )

    def toggle_dark(self, dark: bool) -> None:
        """Repaint everything in the other palette."""
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

    # ------------------------------------------------------------ filters
    def show_filter_dialog(self, section: int) -> None:
        col = self.model.column(section)
        if not col.kind.filterable:
            self.flash(
                f"{col.header} is a json column - filtering is not supported", True
            )
            return
        sort_state = next((d for k, d in self.model.sort_keys if k == col.key), None)
        dialog = FilterDialog(
            col,
            self.model.distinct_values(section),
            self.model.filters.get(col.key),
            sort_state,
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if dialog.clear_sort:
            self.model.set_sort([(k, d) for k, d in self.model.sort_keys if k != col.key])
        if dialog.sort_request is not None:
            self.model.set_sort([(col.key, dialog.sort_request)])
        if dialog.result_filter is not None:
            flt = dialog.result_filter
            self.model.set_filter(flt)
            self.flash(
                f"{col.header}: {flt.describe()}"
                if flt.is_active
                else f"Filter cleared on {col.header}"
            )

    # ----------------------------------------------------------- changeset
    def commit(self) -> None:
        report = self.model.validation_report()
        self.model.revalidate()
        self._refresh()
        if not report.ok:
            QMessageBox.warning(
                self,
                "Commit",
                "Fix the highlighted problems first:\n\n"
                + "\n".join(f"• {m}" for m in report.messages[:10]),
            )
            return
        changeset = self.model.changeset()
        if changeset.is_empty:
            self.flash("Nothing to commit")
            return

        if changeset.new_values:
            # These rows point at dimension values that only exist here, so the
            # foreign keys would fail. Creating them is the API's job, not ours.
            names = ", ".join(
                f"{v['table']}.{v['column']} = {v['label']!r}"
                for v in changeset.new_values
            )
            QMessageBox.information(
                self,
                "Commit",
                "These rows use values that are not in the database yet:\n\n"
                f"{names}\n\n"
                "stagedgrid does not create dimension values. Send the "
                "changeset to your API -- its payload lists them under "
                '"new_dimension_values" so it can create them first.\n\n'
                "File > Export changeset... writes it to a file.",
            )
            return

        # Replay the whole changeset inside a transaction and roll it back, so
        # the database gets the last word before we ask for confirmation.
        dry = self.repository.apply(changeset, dry_run=True)
        if not dry.ok:
            QMessageBox.critical(
                self, "Commit", f"The database rejected the changes:\n\n{dry.error}"
            )
            return

        answer = QMessageBox.question(
            self,
            "Commit",
            f"Apply to {self.repository.spec.table_name}?\n\n{changeset.summary()}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        result = self.repository.apply(changeset)
        if not result.ok:
            QMessageBox.critical(self, "Commit failed", result.error)
            return
        try:
            note = self.sink(changeset.to_payload()) or "changeset sent"
        except Exception as exc:  # the sink is the owner's code
            note = f"sink failed: {exc}"
        self.reload()
        self.flash(f"{result.summary()}. {note}")

    def export_changeset(self) -> None:
        changeset = self.model.changeset()
        if changeset.is_empty:
            self.flash("Nothing to export")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export changeset", "changeset.json", "JSON (*.json)"
        )
        if not path:
            return
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(changeset.to_payload(), handle, indent=2)
        self.flash(f"Exported to {path}")

    # ---------------------------------------------------------------- help
    def show_shortcuts(self) -> None:
        rows = "".join(
            f"<tr><td style='padding-right:18px'><code>{key}</code></td>"
            f"<td>{what}</td></tr>"
            for key, what in SHORTCUTS
        )
        self._info_dialog("Keyboard shortcuts", f"<table>{rows}</table>")

    def show_legend(self) -> None:
        def swatch(colour, text):
            return (
                f"<tr><td style='background:{colour.name()};width:28px'>&nbsp;</td>"
                f"<td style='padding-left:10px'>{text}</td></tr>"
            )

        body = (
            "<table cellspacing='4'>"
            + swatch(grid.C.staged_row, "staged insert (natural key is editable here)")
            + swatch(grid.C.staged_blank, "the always-blank row: type or paste to add more")
            + swatch(grid.C.updated_row, "row with pending edits")
            + swatch(grid.C.updated_cell, "the edited cell itself")
            + swatch(grid.C.key_readonly, "natural key on an existing row (not editable)")
            + swatch(grid.C.error_cell, "validation error - hover for the reason")
            + swatch(grid.C.warning_cell, "warning")
            + "</table>"
            "<p>A deleted row leaves the grid and a red triangle marks where it "
            "was, in the gutter on the left. Click the triangle to bring it "
            "back, or turn on <i>Show deleted</i>.</p>"
        )
        self._info_dialog("Colour legend", body)

    def _info_dialog(self, title: str, html: str) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(520, 440)
        layout = QVBoxLayout(dialog)
        browser = QTextBrowser()
        browser.setHtml(html)
        layout.addWidget(browser)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    # --------------------------------------------------------------- close
    def closeEvent(self, event) -> None:
        counts = self.model.dirty_counts()
        pending = counts["inserts"] + counts["updates"] + counts["deletes"]
        if pending:
            answer = QMessageBox.question(
                self,
                "Discard changes?",
                f"{pending} pending change(s) have not been committed.\n"
                "Close anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        event.accept()


def apply_theme(app: QApplication, theme: str = "auto") -> bool:
    """Theme the chrome with pyqtdarktheme; return whether we ended up dark.

    pyqtdarktheme styles the window, menus, toolbar, dialogs and scrollbars.
    The grid paints its own cells on top from the palette in grid.py, which is
    what ``set_theme`` picks -- see the colour section of the README.
    """
    qdarktheme.setup_theme(theme)
    dark = theme == "dark" or (theme == "auto" and bool(darkdetect.isDark()))
    set_theme(dark)
    return dark


def demo_schema_hint(repository, exc: Exception) -> str:
    """Explain the confusing case: our demo tables, somebody else's database.

    Without this you get "relation \"model_a\" does not exist" naming a table
    you have never heard of, because no model was configured and the bundled
    demo schema was used by default.
    """
    if repository.model is not ModelE:
        return ""
    return (
        "\n\nNote: no model was configured, so this is the bundled DEMO schema "
        f"({ModelE.__tablename__}), which does not exist in that database.\n\n"
        "Point it at your own models with\n"
        "    export STAGEDGRID_MODEL=package.module:ClassName\n"
        "or --model, and check it with --describe."
    )


def load_model(ref: str) -> type:
    """Import a mapped class named as ``package.module:ClassName``.

    Your models do not have to live here -- anything importable works, so put
    your repo on PYTHONPATH or pip install it, and point at it.
    """
    module_name, _, attr = ref.partition(":")
    if not module_name or not attr:
        raise SystemExit(
            f"--model wants 'package.module:ClassName', got {ref!r}"
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise SystemExit(f"--model {ref}: {exc}") from None
    try:
        return getattr(module, attr)
    except AttributeError:
        raise SystemExit(f"--model {ref}: {module_name} has no {attr!r}") from None


def parse_labels(text: str) -> dict[str, str]:
    """``"region=code,version=tag"`` -> which column of a dimension to show."""
    mapping = {}
    for pair in text.split(","):
        pair = pair.strip()
        if not pair:
            continue
        table, sep, column = pair.partition("=")
        if not sep:
            raise SystemExit(f"--labels wants 'table=column', got {pair!r}")
        mapping[table.strip()] = column.strip()
    return mapping


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Staged editor for a fact table.",
        epilog="Point it at your own schema with --model and --dsn, or set "
               "STAGEDGRID_MODEL / STAGEDGRID_DSN. With neither, it opens the "
               "bundled demo schema.",
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help="SQLAlchemy URL (or $STAGEDGRID_DSN)",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("STAGEDGRID_MODEL"),
        help="the mapped class to edit, as package.module:ClassName "
             "(or $STAGEDGRID_MODEL). Defaults to the demo schema.",
    )
    parser.add_argument(
        "--labels",
        default=os.environ.get("STAGEDGRID_LABELS"),
        help="which column of a dimension table to show instead of its id, "
             "as table=column,table=column. Only needed when the guess is "
             "wrong -- see --describe.",
    )
    parser.add_argument(
        "--describe",
        action="store_true",
        help="print what the grid worked out about the table, and exit",
    )
    parser.add_argument("--limit", type=int, default=None, help="max rows to load")
    parser.add_argument(
        "--changesets",
        default="changesets",
        help="directory the committed payloads are written to",
    )
    parser.add_argument(
        "--theme",
        choices=("auto", "light", "dark"),
        default="auto",
        help="auto follows the desktop (default)",
    )
    args = parser.parse_args(argv)

    model = load_model(args.model) if args.model else ModelE
    labels = parse_labels(args.labels) if args.labels else None
    repository = Repository(
        engine=make_engine(args.dsn), model=model, label_columns=labels
    )

    # Say which schema and database before touching either, so a failure
    # below is readable rather than mysterious. Note that --model's default
    # comes from the environment, so it never shows up in sys.argv: this line
    # is what tells you whether it arrived.
    print(
        f"stagedgrid: {args.model or 'the bundled demo schema'} "
        f"({model.__tablename__}) at {args.dsn or dsn()}",
        file=sys.stderr,
    )
    if not args.model:
        print(
            "            STAGEDGRID_MODEL is not set in this process and no "
            "--model was given,\n            so the bundled demo models are "
            "being used. Run --describe to see what that means.",
            file=sys.stderr,
        )

    if args.describe:
        print("environment seen by this process:")
        for name in ("STAGEDGRID_MODEL", "STAGEDGRID_DSN", "STAGEDGRID_LABELS"):
            value = os.environ.get(name)
            print(f"  {name:<18} {value!r}" if value else f"  {name:<18} (not set)")
        print(f"  python             {sys.executable}")
        print()
        print(f"dsn: {args.dsn or dsn()}")
        print(describe(repository.spec))
        return 0

    app = QApplication(sys.argv[:1])
    app.setApplicationName("stagedgrid")
    apply_theme(app, args.theme)

    window = MainWindow(
        repository, sink=file_sink(args.changesets), limit=args.limit
    )
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
