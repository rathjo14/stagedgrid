"""The grid: colours, the table model, undo commands, painting and the view.

Staged insert rows sit pinned above the stored rows, with an always-blank row
at the end of the block; sorting and filtering only ever touch stored rows, so
work in progress can never be hidden.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

from PySide6.QtCore import (
    QItemSelectionModel,
    QAbstractTableModel,
    QEvent,
    QLocale,
    QModelIndex,
    QPoint,
    QPointF,
    QRect,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QDoubleValidator,
    QFont,
    QGuiApplication,
    QPainter,
    QPen,
    QPolygonF,
    QRegularExpressionValidator,
    QUndoCommand,
    QUndoStack,
)
from PySide6.QtWidgets import (
    QInputDialog,
    QStyle,
    QAbstractItemView,
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from data import (
    ChangeSet,
    ColumnFilter,
    FilterSet,
    Row,
    RowMatcher,
    RowState,
    Validator,
    existing_key_map,
)
from schema import (
    ColumnKind,
    ColumnSpec,
    Lookups,
    TableSpec,
    Unresolved,
    display,
    display_json_pretty,
    is_blank,
    parse,
    sort_key,
)

# -- colours -----------------------------------------------------------------


@dataclass(frozen=True)
class Palette:
    """Every colour the grid paints. Swap the whole thing for dark mode."""

    base: QColor            # table background
    text: QColor
    grid_line: QColor
    selection: QColor       # includes alpha
    updated_row: QColor     # faint: row has pending edits
    updated_cell: QColor    # stronger: this cell is one of them
    staged_row: QColor      # staged insert that passes validation
    staged_blank: QColor    # the always-present empty row
    staged_invalid: QColor  # staged insert that does not pass yet
    staged_invalid_cell: QColor  # ...and the cell that is actually wrong
    staged_invalid_edge: QColor
    staged_mark: QColor     # the "+" on the empty row
    attention: QColor       # the "!!!" on a row that needs looking at
    key_readonly: QColor    # natural-key cell on an existing row
    key_readonly_text: QColor
    error_cell: QColor
    error_border: QColor
    warning_cell: QColor
    warning_border: QColor
    deleted_mark: QColor    # the gutter triangle
    deleted_row: QColor     # a row marked for deletion, when shown
    deleted_text: QColor
    staging_separator: QColor
    header_bg: QColor
    header_filtered: QColor
    header_sorted: QColor
    header_text: QColor
    gutter_text: QColor
    filter_icon: QColor     # funnel when a filter is live
    filter_icon_idle: QColor
    focus_border: QColor    # outline of the active cell
    sort_icon: QColor
    null_text: QColor
    stripe_updated: QColor  # the gutter stripe on an edited row
    commit_ready: QColor    # Commit, with something valid to push
    commit_ready_text: QColor
    commit_blocked: QColor  # Commit, with something still to fix
    commit_blocked_text: QColor


def _palette(**values) -> Palette:
    """Build a palette from "#rrggbb" strings, or (r, g, b, a) tuples.

    Use a tuple whenever you want alpha: Qt reads an 8-digit hex string as
    #AARRGGBB, not #RRGGBBAA, which is an easy way to get a colour you did
    not ask for.
    """
    return Palette(
        **{
            k: (QColor(*v) if isinstance(v, tuple) else QColor(v))
            for k, v in values.items()
        }
    )


LIGHT = _palette(
    # The stored rows are grey and the staging block above them is a lighter
    # grey, so the two halves read apart at a glance without any colour.
    # Colour then means something: green accepted, yellow needs attention,
    # blue edited, red deleted.
    base="#ECECEC", text="#1E1E1E", grid_line="#D4D4D4",
    # Excel tints the selection rather than colouring it, so the cell's own
    # state colour still reads through. Neutral grey, not an accent.
    selection=(0, 0, 0, 34), focus_border="#3C3C3C",
    updated_row="#DCE9FA", updated_cell="#B6D3F7",
    staged_row="#DCF2E1", staged_blank="#F7F7F7",
    staged_invalid="#FBEFC0", staged_invalid_cell="#F6DE7A",
    staged_invalid_edge="#B8860B",
    staged_mark="#1F7A35", attention="#8A6100",
    key_readonly="#DCDCDC", key_readonly_text="#5E5E5E",
    error_cell="#FBE3E5", error_border="#D1383D",
    warning_cell="#FDF3E2", warning_border="#C98416",
    deleted_mark="#D1383D", deleted_row="#F6C9CB", deleted_text="#7A3034",
    staging_separator="#7FB383",
    header_bg="#F4F4F4", header_filtered="#E3EEFB", header_sorted="#ECECEC",
    header_text="#2B2B2B", gutter_text="#6E6E6E",
    filter_icon="#3A6EA5", filter_icon_idle="#8A8A8A", sort_icon="#555555",
    null_text="#B0B0B0", stripe_updated="#4A8BD4",
    commit_ready="#1F7A35", commit_ready_text="#FFFFFF",
    commit_blocked="#E8B621", commit_blocked_text="#3A2D05",
)

DARK = _palette(
    base="#2B2B2B", text="#E4E4E4", grid_line="#3C3C3C",
    selection=(255, 255, 255, 38), focus_border="#C9C9C9",
    updated_row="#23344A", updated_cell="#31567F",
    staged_row="#20402B", staged_blank="#3A3A3A",
    staged_invalid="#4A3E15", staged_invalid_cell="#6E5A14",
    staged_invalid_edge="#D9A520",
    staged_mark="#5FBF7A", attention="#E0B344",
    key_readonly="#232323", key_readonly_text="#9B9B9B",
    error_cell="#3C1E22", error_border="#E06C75",
    warning_cell="#3A2D18", warning_border="#D6A14A",
    deleted_mark="#E06C75", deleted_row="#4A2126", deleted_text="#D79AA0",
    staging_separator="#4E8C5A",
    header_bg="#252526", header_filtered="#25364B", header_sorted="#2F2F30",
    header_text="#D4D4D4", gutter_text="#9A9A9A",
    filter_icon="#6FA8DC", filter_icon_idle="#7A7A7A", sort_icon="#AAAAAA",
    null_text="#6A6A6A", stripe_updated="#4A8BD4",
    commit_ready="#2E9E4B", commit_ready_text="#06240F",
    commit_blocked="#C79400", commit_blocked_text="#2A1F00",
)

#: The palette in force. Swap it with :func:`set_theme`, and read it from other
#: modules as ``grid.C`` -- ``from grid import C`` snapshots the light palette
#: at import time and will not follow a theme change.
C = LIGHT


def set_theme(dark: bool) -> None:
    """Pick the palette. Call before building the window."""
    global C
    C = DARK if dark else LIGHT


def stylesheet() -> str:
    """Qt stylesheet for the current palette.

    Apply this to the *window*, not to the QTableView itself: a stylesheet set
    directly on the view makes Qt own item painting and drop the per-cell
    background brushes, which is where every row/cell state colour lives.
    """
    return """
/* pyqtdarktheme paints selected cells with a blue overlay
   (QAbstractItemView::item:selected). Neutralise it here so the cell keeps
   its own state colour and GridDelegate's grey wash is what shows. */
QTableView::item:selected { background: transparent; }
QTableView {
    background: %(base)s;
    color: %(text)s;
    gridline-color: %(grid)s;
    /* GridDelegate paints the selection wash itself, so that a selected
       cell keeps its own colour. Stop Qt filling it with the accent. */
    selection-background-color: transparent;
    selection-color: %(text)s;
    outline: 0;
}
QHeaderView::section {
    background: %(header)s;
    color: %(header_text)s;
    border: 0px;
    border-right: 1px solid %(grid)s;
    border-bottom: 1px solid %(grid)s;
    padding: 2px 6px;
}
QToolBar { border: 0px; padding: 2px; spacing: 2px; }
QStatusBar::item { border: 0px; }
""" % {
        "base": C.base.name(), "text": C.text.name(), "grid": C.grid_line.name(),
        "header": C.header_bg.name(),
        "header_text": C.header_text.name(),
    }


# -- metrics -----------------------------------------------------------------
ROW_HEIGHT = 22
HEADER_HEIGHT = 30
GUTTER_WIDTH = 64
MARK_ZONE = 14    # clickable width of the deletion marker inside the gutter
GLYPH_WIDTH = 18  # clickable width of the filter funnel inside a header

# -- item data roles ---------------------------------------------------------
ROLE_CELL_STATE = Qt.ItemDataRole.UserRole + 2
ROLE_RAW = Qt.ItemDataRole.UserRole + 4
ROLE_IS_STAGED = Qt.ItemDataRole.UserRole + 5

#: itemData marker for the "add a value" entry in a foreign-key drop-down.
NEW_VALUE = "__new_dimension_value__"

CELL_OK = ""
CELL_ERROR = "error"
CELL_WARNING = "warning"
CELL_UPDATED = "updated"

INT_RE = r"^-?\d*$"


def _is_pending(row: Row) -> bool:
    """Does this stored row differ from what the database holds?"""
    return bool(row.changed_keys()) or row.state is not RowState.CLEAN


class StagedTableModel(QAbstractTableModel):
    """Staged inserts pinned above the rows loaded from the database."""

    stateChanged = Signal()     # anything that can change the pending changeset
    message = Signal(str, bool)  # (text, is_error) -- shown in the status bar

    viewChanged = Signal()
    """The sort, filters or search changed.

    In read-only mode the window listens to this and re-asks the database,
    so ordering and filtering cover the whole table rather than whatever
    happens to be loaded.
    """

    def __init__(
        self,
        spec: TableSpec,
        lookups: Optional[Lookups] = None,
        undo_stack: Optional[QUndoStack] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.spec = spec
        #: The version is one value for everything being worked on, so it is
        #: shown once beside the data instead of as a column where every cell
        #: says the same thing. New rows inherit it; see `set_version`.
        self.version_col = spec.version_column
        #: The columns the grid shows: everything but the primary key, which is
        #: the row index in the gutter, and the version.
        hidden = {spec.pk.key} | (
            {self.version_col.key} if self.version_col is not None else set()
        )
        self.columns = tuple(c for c in spec.columns if c.key not in hidden)
        #: First and last grid column of the natural key, for outlining it.
        key_at = [i for i, c in enumerate(self.columns) if c.in_key]
        self.key_span = (key_at[0], key_at[-1]) if key_at else (-1, -1)
        self.lookups = lookups or Lookups()
        self.undo_stack = undo_stack or QUndoStack(self)
        self.filters = FilterSet()
        self.validator = Validator(spec, self.lookups)

        self._db_rows: list[Row] = []
        # No version yet -- `set_version` fills it in and tops this one up.
        self._staged: list[Row] = [Row.staged(spec)]
        self._view: list[int] = []
        self._marks: dict[int, list[int]] = {}
        self._trailing_marks: list[int] = []
        self._sort_keys: list[tuple[str, bool]] = []  # (column key, descending)
        self._quick_search = ""
        self._show_deleted = False
        self._by_uid: dict[int, Row] = {}
        #: Set in merged mode: a sqlview.MergedView that does the ordering,
        #: filtering and searching in the database and says where the rows we
        #: hold belong. None means every stored row is here in memory.
        self._window = None
        self._window_at = 0        # visible row the resident slice starts at
        self._version_id = None    # what new rows get for the version column
        #: Stored rows this session has touched, by uid. In merged mode these
        #: are kept whatever the window is showing -- they are the rows whose
        #: values the database disagrees with, and the only ones anything
        #: points at. Empty when every row is in memory anyway.
        self._held: dict[int, Row] = {}

    # ------------------------------------------------------------------ data
    def load(self, rows: Sequence[Row], lookups: Optional[Lookups] = None) -> None:
        self.beginResetModel()
        if lookups is not None:
            self.lookups = lookups
            self.validator.lookups = lookups
        self._db_rows = list(rows)
        # A fresh start, so nothing is pending: without this, reloading (to
        # open a branch, say) leaves the previous session's held rows behind
        # and every one of their edits is counted twice.
        self._held.clear()
        self._staged = [self.blank_staged()]
        self.validator.set_existing_keys(existing_key_map(self.spec, self._db_rows))
        self._reindex()
        self._rebuild_order()
        self.endResetModel()
        self.undo_stack.clear()
        self.stateChanged.emit()

    def set_version(self, version_id) -> None:
        """The version every row here belongs to.

        Staged rows are given it outright: the column is not on screen to type
        it into, and a row in another version would not belong in this view.
        """
        self._version_id = version_id
        for row in self._staged:
            self._apply_version(row)

    def _apply_version(self, row: Row) -> Row:
        if self.version_col is not None and self._version_id is not None:
            row.values[self.version_col.key] = self._version_id
        return row

    def blank_staged(self, values: Optional[dict] = None) -> Row:
        """A staging row, carrying the version it belongs to."""
        row = self._apply_version(Row.staged(self.spec))
        if values:
            row.values.update(values)
        return row

    def _reindex(self) -> None:
        self._by_uid = {r.uid: r for r in self._db_rows}
        if self._window is not None:
            # Rows this session has edited stay addressable after scrolling
            # away from them, because undo and the changeset still point at
            # them. Clean rows are the ones nothing points at, which is exactly
            # why they are the ones safe to drop and refetch.
            self._by_uid.update(self._held)
        self._by_uid.update({r.uid: r for r in self._staged})

    # ------------------------------------------------------------- windowing
    def set_window_source(self, source) -> None:
        """Hand ordering, filtering and searching to the database.

        ``source`` is a :class:`sqlview.MergedView`. Once set, the model stops
        computing the view itself: ``rowCount`` becomes the count the database
        reports, and the rows it holds are whatever window was last fetched.
        Left unset -- which is how the tests use it -- the model sorts and
        filters the rows it was given, and that is what the SQL translation is
        checked against.
        """
        self._window = source
        self._window_at = 0

    def load_window(
        self, rows: Sequence[Row], at: int, structural: bool = False
    ) -> None:
        """Show the slice of the view starting at visible row ``at``.

        Unlike :meth:`load` this keeps the staging block, the undo stack and
        the pending changes: it is a scroll, not a reload.

        ``structural`` says whether the shape of the view changed as well as
        its contents -- a new sort or filter did, a scroll did not. A scroll
        must not reset the model, because a reset drops the selection and the
        scroll position, and dropping the scroll position mid-scroll makes the
        grid impossible to drive.
        """
        was_lo = self._window_at
        was_hi = was_lo + len(self._view)
        if structural:
            self.beginResetModel()
        self._db_rows = list(rows)
        self._window_at = max(0, at)
        self._reindex()
        self._rebuild_order()
        if structural:
            self.endResetModel()
            return
        lo = min(was_lo, self._window_at)
        hi = max(was_hi, self._window_at + len(self._view))
        if hi > lo:
            top = len(self._staged) + lo
            bottom = len(self._staged) + hi - 1
            self.dataChanged.emit(
                self.index(top, 0), self.index(bottom, self.columnCount() - 1)
            )
            self.headerDataChanged.emit(Qt.Orientation.Vertical, top, bottom)

    def seed_held(self, rows: Sequence[Row]) -> None:
        """Put rows into the held set before anything edits them.

        Replaying a stored branch names rows by primary key the current window
        may not contain, so they are fetched and handed over first.
        """
        if self._window is None:
            return
        for row in rows:
            self._held.setdefault(row.uid, row)
        self._reindex()

    def held_rows(self) -> list[Row]:
        """The stored rows the database disagrees with, for the window source."""
        return list(self._held.values())

    def _note_held(self) -> None:
        """Keep the held set in step with what is actually pending.

        A row joins it the moment it is edited or marked deleted and leaves it
        when it is reverted, so it holds exactly the rows whose values differ
        from the table's -- no more, because every one of them costs a count
        query when the view is rebuilt.
        """
        if self._window is None:
            return
        for row in self._db_rows:
            if _is_pending(row):
                self._held[row.uid] = row
        for uid, row in list(self._held.items()):
            if not _is_pending(row):
                # Reverted: the database agrees with us again, so let it go.
                del self._held[uid]

    def _stored_rows(self) -> list[Row]:
        """Every stored row this session has an opinion about.

        In memory that is all of them. In merged mode it is the held ones --
        the same thing for every purpose that asks, because a row nobody has
        touched has no pending change, nothing to validate and nothing to
        commit.
        """
        if self._window is not None:
            return self.held_rows()
        return self._db_rows

    # -------------------------------------------------------------- geometry
    @property
    def staged_count(self) -> int:
        return len(self._staged)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        if self._window is not None:
            return len(self._staged) + self._window.visible_count()
        return len(self._staged) + len(self._view)

    def column_index(self, key: str) -> int:
        """Grid column holding ``key`` (the pk is the gutter, not a column)."""
        return [c.key for c in self.columns].index(key)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self.columns)

    def column(self, index: int) -> ColumnSpec:
        return self.columns[index]

    def is_staged_row(self, visual_row: int) -> bool:
        return 0 <= visual_row < len(self._staged)

    def row_at(self, visual_row: int) -> Optional[Row]:
        if visual_row < 0:
            return None
        if visual_row < len(self._staged):
            return self._staged[visual_row]
        offset = visual_row - len(self._staged) - self._window_at
        if 0 <= offset < len(self._view):
            return self._db_rows[self._view[offset]]
        return None        # outside the resident window, in merged mode

    def visual_row_of(self, uid: int) -> int:
        for i, row in enumerate(self._staged):
            if row.uid == uid:
                return i
        for offset, db_index in enumerate(self._view):
            if self._db_rows[db_index].uid == uid:
                return len(self._staged) + self._window_at + offset
        return -1

    # ----------------------------------------------------------------- roles
    def headerData(self, section: int, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal:
            col = self.columns[section]
            if role == Qt.ItemDataRole.DisplayRole:
                return col.header
            if role == Qt.ItemDataRole.ToolTipRole:
                bits = [f"<b>{col.header}</b> &nbsp; <code>{col.key}</code>"]
                if col.doc:
                    bits.append(col.doc)
                kind = col.kind.value
                if col.fk is not None:
                    kind = f"{col.fk.target_table}.{col.fk.label_attr}"
                bits.append(
                    f"<i>{kind}{'' if col.nullable else ', required'}"
                    f"{', part of the unique key' if col.in_key else ''}</i>"
                )
                flt = self.filters.get(col.key)
                if flt is not None:
                    bits.append(f"<b>Filter:</b> {flt.describe()}")
                return "<br>".join(bits)
            return None

        if role == Qt.ItemDataRole.DisplayRole:
            if section < len(self._staged):
                row = self._staged[section]
                if row.is_empty(self.spec):
                    return "+"          # type or paste here
                # A row that passes needs no shouting: green already says so.
                return "!!!" if row.errors else ""
            # Stored rows are numbered by their place in the view, not by their
            # primary key: the key is only known for the rows currently
            # fetched, so it left the column blank everywhere else. The number
            # restarts whenever the view changes, which is what a row number
            # means in a spreadsheet. The key is in the tooltip.
            return str(section - len(self._staged) + 1)
        if role == Qt.ItemDataRole.ToolTipRole:
            row = self.row_at(section)
            if row is None or row.is_staged:
                return None
            pk = row.pk(self.spec)
            return None if pk is None else f"{self.spec.pk.header} {pk}"
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        row = self.row_at(index.row())
        if row is None or row.state is RowState.DELETED:
            return base
        col = self.columns[index.column()]
        editable = (
            col.editable_on_staged if row.is_staged else col.editable_on_existing
        )
        if editable:
            base |= Qt.ItemFlag.ItemIsEditable
        return base

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self.row_at(index.row())
        if row is None:
            return None
        col = self.columns[index.column()]
        value = row.values.get(col.key)

        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return display(col, value, self.lookups)

        if role == ROLE_RAW:
            return value
        if role == ROLE_IS_STAGED:
            return row.is_staged
        if role == ROLE_CELL_STATE:
            return self._cell_state(row, col)

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col.kind.numeric:
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if col.kind in (ColumnKind.ENUM, ColumnKind.BOOL):
                return int(Qt.AlignmentFlag.AlignCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        if role == Qt.ItemDataRole.BackgroundRole:
            return self._background(row, col)

        if role == Qt.ItemDataRole.ForegroundRole:
            if row.state is RowState.DELETED:
                return QBrush(C.deleted_text)
            if col.in_key and not row.is_staged:
                return QBrush(C.key_readonly_text)
            if is_blank(value) and not col.nullable and not row.is_staged:
                return QBrush(C.null_text)
            return None

        if role == Qt.ItemDataRole.FontRole:
            font = QFont()
            if row.state is RowState.DELETED:
                font.setStrikeOut(True)
                return font
            if col.fk is not None and Lookups.is_pending(value):
                font.setItalic(True)  # a value invented in this session
                font.setBold(col.in_key and row.is_staged)
                return font
            if col.in_key and row.is_staged:
                font.setBold(True)
                return font
            if not row.is_staged and row.is_cell_changed(col.key):
                font.setBold(True)
                return font
            return None

        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(row, col, value)
        return None

    def _cell_state(self, row: Row, col: ColumnSpec) -> str:
        if col.key in row.errors:
            return CELL_ERROR
        if col.key in row.warnings:
            return CELL_WARNING
        if not row.is_staged and row.is_cell_changed(col.key):
            return CELL_UPDATED
        return CELL_OK

    def _background(self, row: Row, col: ColumnSpec) -> Optional[QBrush]:
        state = self._cell_state(row, col)
        if row.is_staged:
            # One colour for the whole row, so a row that needs attention is
            # a block rather than a scatter of boxes.
            if row.is_empty(self.spec):
                return QBrush(C.staged_blank)
            if row.errors:
                # A duplicate is the key as a whole, so the key stays one
                # block. Anything else belongs to a cell, and that cell says
                # so: same yellow, further on.
                own = row.errors.get(col.key)
                if own is not None and own != row.key_conflict:
                    return QBrush(C.staged_invalid_cell)
                return QBrush(C.staged_invalid)
            if row.warnings:
                return QBrush(C.warning_cell)
            return QBrush(C.staged_row)
        if state == CELL_ERROR:
            return QBrush(C.error_cell)
        if state == CELL_WARNING:
            return QBrush(C.warning_cell)
        if row.state is RowState.DELETED:
            return QBrush(C.deleted_row)
        if state == CELL_UPDATED:
            return QBrush(C.updated_cell)
        if row.state is RowState.UPDATED:
            if col.in_key:
                return QBrush(_blend(C.updated_row, C.key_readonly))
            return QBrush(C.updated_row)
        if col.in_key:
            return QBrush(C.key_readonly)
        return QBrush(C.base)

    def _tooltip(self, row: Row, col: ColumnSpec, value: Any) -> str:
        bits: list[str] = []
        if col.key in row.errors:
            bits.append(f"<b style='color:#B00020'>{row.errors[col.key]}</b>")
        if col.key in row.warnings:
            bits.append(f"<b style='color:#8A5700'>{row.warnings[col.key]}</b>")
        if col.fk is not None and Lookups.is_pending(value):
            bits.append(
                f"<b>new {col.header}</b> - added in this session and not in the "
                "database.<br>Your API has to create it before these rows can "
                "be applied."
            )
        if col.kind is ColumnKind.JSON and value is not None:
            bits.append(f"<pre>{display_json_pretty(value)}</pre>")
        if not row.is_staged and row.is_cell_changed(col.key):
            old = display(col, (row.original or {}).get(col.key), self.lookups)
            new = display(col, value, self.lookups)
            bits.append(
                f"changed: <s>{old or '(blank)'}</s> &rarr; <b>{new or '(blank)'}</b>"
            )
        if row.state is RowState.DELETED:
            bits.append("<i>row is marked for deletion</i>")
        if col.in_key and not row.is_staged:
            bits.append("<i>part of the unique key -- not editable on an existing row</i>")
        if col.doc:
            bits.append(col.doc)
        if isinstance(value, Unresolved):
            bits.append(f"typed value: {value.text!r}")
        return "<br>".join(bits)

    # --------------------------------------------------------------- editing
    def value_for(self, col: ColumnSpec, text: str) -> tuple[Any, Optional[str]]:
        """Cell text -> a value, inventing a dimension value where needed.

        Typing a label a dimension does not have is how a new one gets added,
        exactly as the "+ New ..." entry does it: having to find that entry to
        say what the cell already says is a nuisance, and a key column often
        needs a value nothing references yet.

        What comes back is a placeholder id. It shows in italics, it appears in
        every drop-down for that column from then on, and it travels in the
        payload under ``new_dimension_values`` for the receiving API to create
        before the rows that reference it.

        Enums are not invented -- their members are a Python class -- so an
        unknown one stays the error it was.

        Returns the value, and the label if one was invented.
        """
        value = parse(col, text, self.lookups)
        if col.fk is None or not isinstance(value, Unresolved):
            return value, None
        label = value.text.strip()
        if not label:
            return value, None
        return self.lookups.add_value(col, label), label

    def announce_new_values(self, labels: Sequence[str], header: str = "") -> None:
        """Say that a value was invented, because nothing else would."""
        labels = [x for x in labels if x]
        if not labels:
            return
        if len(labels) == 1:
            what = f"{header + ' ' if header else ''}'{labels[0]}'"
            self.message.emit(
                f"{what} added for this session - it is not in the database, "
                "your API has to create it",
                False,
            )
            return
        self.message.emit(
            f"{len(labels)} new value(s) added for this session "
            f"({', '.join(sorted(set(labels))[:3])}"
            f"{', ...' if len(set(labels)) > 3 else ''}) - "
            "not in the database, your API has to create them",
            False,
        )

    def setData(self, index: QModelIndex, value: Any, role=Qt.ItemDataRole.EditRole) -> bool:
        if role != Qt.ItemDataRole.EditRole or not index.isValid():
            return False
        row = self.row_at(index.row())
        col = self.columns[index.column()]
        if row is None:
            return False
        try:
            parsed, invented = self.value_for(
                col, "" if value is None else str(value)
            )
        except ValueError as exc:
            self.message.emit(f"{col.header}: {exc}", True)
            return False
        if _equal(parsed, row.values.get(col.key)):
            return False
        self.undo_stack.push(
            CellEditCommand(self, [(row.uid, col.key, parsed)], f"edit {col.header}")
        )
        self.announce_new_values([invented], col.header)
        return True

    def apply_values(self, changes: Iterable[tuple[int, str, Any]]) -> None:
        """Write (uid, column key, value) triples without touching the stack."""
        touched: set[int] = set()
        for uid, key, value in changes:
            row = self._by_uid.get(uid)
            if row is None:
                continue
            row.remember_original()   # copy-on-write: first edit snapshots it
            row.values[key] = value
            touched.add(uid)
        for uid in touched:
            row = self._by_uid[uid]
            row.search_text = None          # its text changed
            row.recompute_state()
        self._note_held()
        self._normalise_staging()
        self.revalidate()
        for uid in touched:
            visual = self.visual_row_of(uid)
            if visual >= 0:
                self.dataChanged.emit(
                    self.index(visual, 0),
                    self.index(visual, self.columnCount() - 1),
                )
        self.headerDataChanged.emit(
            Qt.Orientation.Vertical, 0, max(self.rowCount() - 1, 0)
        )
        self.stateChanged.emit()

    def restore_staging(self, rows: Sequence[Row]) -> None:
        """Replace the whole staging block (used by structural undo)."""
        self.beginResetModel()
        self._staged = [_copy_row(r) for r in rows]
        # Same invariant as _normalise_staging: exactly one trailing blank.
        # Without the pruning, clearing a staged row's last value left a
        # second placeholder behind and the row looked undeletable.
        spec = self.spec
        while (
            len(self._staged) >= 2
            and self._staged[-1].is_empty(spec)
            and self._staged[-2].is_empty(spec)
        ):
            self._staged.pop()
        if not self._staged or not self._staged[-1].is_empty(spec):
            self._staged.append(self.blank_staged())
        self._reindex()
        self.revalidate(emit=False)
        self.endResetModel()
        self.stateChanged.emit()

    def staging_snapshot(self) -> list[Row]:
        return [_copy_row(r) for r in self._staged]

    def set_row_states(self, states: dict[int, RowState]) -> None:
        """Used by the delete/restore commands."""
        self.beginResetModel()
        for uid, state in states.items():
            row = self._by_uid.get(uid)
            if row is None:
                continue
            if state is RowState.CLEAN:
                # "un-delete": fall back to UPDATED when edits are pending.
                row.state = RowState.UPDATED if row.changed_keys() else RowState.CLEAN
            else:
                row.state = state
        self._note_held()
        self._rebuild_order()
        self.revalidate(emit=False)
        self.endResetModel()
        self.stateChanged.emit()
        # A deleted row leaves the view, so there is one row fewer to show.
        # In windowed mode the count came from the database before the
        # deletion, so without this the grid keeps a phantom row at the end.
        self.viewChanged.emit()

    def _normalise_staging(self) -> None:
        """Keep exactly one trailing blank staging row."""
        spec = self.spec
        while (
            len(self._staged) >= 2
            and self._staged[-1].is_empty(spec)
            and self._staged[-2].is_empty(spec)
        ):
            self.beginRemoveRows(QModelIndex(), len(self._staged) - 1, len(self._staged) - 1)
            dropped = self._staged.pop()
            self._by_uid.pop(dropped.uid, None)
            self.endRemoveRows()
        if not self._staged or not self._staged[-1].is_empty(spec):
            at = len(self._staged)
            self.beginInsertRows(QModelIndex(), at, at)
            blank = self.blank_staged()
            self._staged.append(blank)
            self._by_uid[blank.uid] = blank
            self.endInsertRows()

    # ------------------------------------------------------------- structure
    def add_staged_rows(self, count: int = 1, at: Optional[int] = None) -> None:
        before = self.staging_snapshot()
        after = [_copy_row(r) for r in before]
        position = len(after) - 1 if at is None else max(0, min(at, len(after)))
        for _ in range(count):
            after.insert(position, self.blank_staged())
        self.undo_stack.push(
            StagingCommand(self, before, after, f"add {count} staged row(s)")
        )

    def rows_for(self, visual_rows: Iterable[int], for_edit: bool = False) -> list[Row]:
        """The rows at these visual positions, fetching any not resident.

        A selection reaches wherever the user dragged it, and in merged mode
        that is routinely past the window. Acting on only the part that happens
        to be in memory would quietly do less than was asked -- so the rest is
        fetched, in one query per run of adjacent rows.

        ``for_edit`` puts what comes back into the held set, because a row
        about to be deleted has to survive the next window move.
        """
        wanted = sorted(set(visual_rows))
        out: list[Row] = []
        missing: list[int] = []
        for visual in wanted:
            row = self.row_at(visual)
            if row is None:
                missing.append(visual)
            else:
                out.append(row)
        if not missing or self._window is None:
            return out

        runs: list[tuple[int, int]] = []
        start = previous = missing[0]
        for visual in missing[1:]:
            if visual == previous + 1:
                previous = visual
                continue
            runs.append((start, previous))
            start = previous = visual
        runs.append((start, previous))

        fetched: list[Row] = []
        for low, high in runs:
            at = low - len(self._staged)
            fetched.extend(self._window.visible_slice(at, high - low + 1))
        if for_edit:
            self.seed_held(fetched)
        out.extend(fetched)
        return out

    def delete_rows(self, visual_rows: Iterable[int]) -> None:
        """Mark existing rows deleted; drop staged rows outright."""
        rows = self.rows_for(visual_rows, for_edit=True)
        rows = [r for r in rows if r is not None]
        staged = [r for r in rows if r.is_staged and not r.is_empty(self.spec)]
        existing = [r for r in rows if not r.is_staged and r.state is not RowState.DELETED]
        if not staged and not existing:
            blanks = [r for r in rows if r.is_staged]
            self.message.emit(
                "That is the empty row for new entries - nothing to delete"
                if blanks
                else "Those rows are already deleted",
                True,
            )
            return

        self.undo_stack.beginMacro("delete rows")
        if staged:
            before = self.staging_snapshot()
            drop = {r.uid for r in staged}
            after = [_copy_row(r) for r in before if r.uid not in drop]
            self.undo_stack.push(StagingCommand(self, before, after, "remove staged rows"))
        if existing:
            self.undo_stack.push(
                DeleteCommand(self, [r.uid for r in existing], RowState.DELETED)
            )
        self.undo_stack.endMacro()

    def revert_rows(self, visual_rows: Iterable[int]) -> None:
        """Undo pending edits on existing rows (and un-delete them)."""
        changes: list[tuple[int, str, Any]] = []
        undelete: list[int] = []
        for row in self.rows_for(visual_rows, for_edit=True):
            if row is None or row.is_staged:
                continue
            if row.state is RowState.DELETED:
                undelete.append(row.uid)
            for key in row.changed_keys():
                changes.append((row.uid, key, (row.original or {}).get(key)))
        if not changes and not undelete:
            return
        self.undo_stack.beginMacro("revert rows")
        if changes:
            self.undo_stack.push(CellEditCommand(self, changes, "revert cells"))
        if undelete:
            self.undo_stack.push(DeleteCommand(self, undelete, RowState.CLEAN))
        self.undo_stack.endMacro()

    def restore_deleted_above(self, visual_row: int) -> None:
        indices = self.deleted_above(visual_row)
        if not indices:
            return
        uids = [self._db_rows[i].uid for i in indices]
        self.undo_stack.push(DeleteCommand(self, uids, RowState.CLEAN))

    # ------------------------------------------------------- sort and filter
    def sort(self, column: int, order=Qt.SortOrder.AscendingOrder) -> None:
        key = self.columns[column].key
        self.set_sort([(key, order == Qt.SortOrder.DescendingOrder)])

    def add_sort(self, column: int, order=Qt.SortOrder.AscendingOrder) -> None:
        """Ctrl+click on a header: add a secondary/tertiary sort level."""
        key = self.columns[column].key
        keys = [(k, d) for k, d in self._sort_keys if k != key]
        keys.append((key, order == Qt.SortOrder.DescendingOrder))
        self.set_sort(keys)

    def set_sort(self, keys: Sequence[tuple[str, bool]]) -> None:
        self.beginResetModel()
        self._sort_keys = list(keys)
        self._rebuild_order()
        self.endResetModel()
        self.viewChanged.emit()

    @property
    def sort_keys(self) -> list[tuple[str, bool]]:
        return list(self._sort_keys)

    def clear_sort(self) -> None:
        self.set_sort([])

    def set_filter(self, flt: ColumnFilter) -> None:
        self.beginResetModel()
        self.filters.set(flt)
        self._rebuild_order()
        self.endResetModel()
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, self.columnCount() - 1)
        self.stateChanged.emit()
        self.viewChanged.emit()

    def clear_filters(self, key: Optional[str] = None) -> None:
        self.beginResetModel()
        self.filters.clear(key)
        self._rebuild_order()
        self.endResetModel()
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, self.columnCount() - 1)
        self.stateChanged.emit()
        self.viewChanged.emit()

    def set_quick_search(self, text: str) -> None:
        """Toolbar search box: keep rows matching the text in any column."""
        text = (text or "").strip()
        if text == self._quick_search:
            return
        self.beginResetModel()
        self._quick_search = text
        self._rebuild_order()
        self.endResetModel()
        self.stateChanged.emit()
        self.viewChanged.emit()

    @property
    def quick_search(self) -> str:
        return self._quick_search

    @property
    def showing_deleted(self) -> bool:
        return self._show_deleted

    @property
    def window_at(self) -> int:
        """Visible row the resident slice starts at (0 unless windowed)."""
        return self._window_at

    def resident_rows(self) -> int:
        """How many stored rows are actually in memory."""
        return len(self._db_rows)

    def set_show_deleted(self, show: bool) -> None:
        if show == self._show_deleted:
            return
        self.beginResetModel()
        self._show_deleted = show
        self._rebuild_order()
        self.endResetModel()
        # It changes which rows the view consists of, so in merged mode the
        # window has to be asked for again -- the same as a sort or a filter.
        self.viewChanged.emit()

    def _rebuild_order(self) -> None:
        if self._window is not None:
            # The database already ordered, filtered and searched this slice.
            # All that is left is the thing SQL cannot express: collapsing rows
            # deleted in this session into a mark above the row below them.
            self._view, self._marks, self._trailing_marks = self._collapse_deleted(
                range(len(self._db_rows))
            )
            return
        # The same matcher merged mode judges held rows with, so the two
        # modes cannot drift on what a filter or a search means.
        keep = RowMatcher(self.spec, self.lookups, self.filters, self._quick_search)
        order = [i for i, row in enumerate(self._db_rows) if keep(row)]

        for key, descending in reversed(self._sort_keys):
            col = self.spec.get(key)
            if col is None:
                continue
            blanks = [i for i in order if is_blank(self._db_rows[i].values.get(key))]
            filled = [i for i in order if not is_blank(self._db_rows[i].values.get(key))]
            filled.sort(
                key=lambda i: sort_key(col, self._db_rows[i].values.get(key), self.lookups),
                reverse=descending,
            )
            order = filled + blanks

        self._view, self._marks, self._trailing_marks = self._collapse_deleted(order)

    def _collapse_deleted(self, order) -> tuple:
        """Deleted rows leave the view and become a mark on the row below."""
        if self._show_deleted:
            return list(order), {}, []
        view: list[int] = []
        marks: dict[int, list[int]] = {}
        pending: list[int] = []
        for i in order:
            if self._db_rows[i].state is RowState.DELETED:
                pending.append(i)
                continue
            if pending:
                marks[len(view)] = pending
                pending = []
            view.append(i)
        return view, marks, pending

    def deleted_above(self, visual_row: int) -> list[int]:
        """Database-row indices deleted immediately above this visual row."""
        offset = visual_row - len(self._staged) - self._window_at
        if offset < 0:
            return []
        if offset >= len(self._view):
            # Past the resident slice there is only something to say when the
            # slice runs to the end of the view.
            at_end = (
                self._window is None
                or self._window_at + len(self._view) >= self._window.visible_count()
            )
            return list(self._trailing_marks) if at_end else []
        return list(self._marks.get(offset, ()))

    def deleted_rows_above(self, visual_row: int) -> list[Row]:
        return [self._db_rows[i] for i in self.deleted_above(visual_row)]

    def has_trailing_deleted(self) -> bool:
        return bool(self._trailing_marks)

    def distinct_values(self, column: int) -> list[tuple[str, int]]:
        """Distinct display values for the filter dialog.

        Computed with the *other* columns' filters applied, which is what Excel
        does, and ignoring rows deleted in this session.
        """
        col = self.columns[column]
        if self._window is not None:
            # Only a window is here, so the counts have to come from the
            # database -- a checklist of what happens to be on screen would be
            # worse than useless.
            counts = self._window.distinct(
                col, self.filters, self.lookups, self.held_rows()
            )
            return sorted(counts.items(), key=lambda kv: _distinct_key(col, kv[0]))

        preds = []
        for key in self.filters.active_keys():
            if key == col.key:
                continue
            other = self.spec.get(key)
            flt = self.filters.get(key)
            if other is None or flt is None:
                continue
            preds.append((other.key, flt.predicate(other, self.lookups)))

        counts = {}
        for row in self._db_rows:
            if row.state is RowState.DELETED:
                continue
            if not all(pred(row.values.get(key)) for key, pred in preds):
                continue
            text = display(col, row.values.get(col.key), self.lookups)
            counts[text] = counts.get(text, 0) + 1
        return sorted(counts.items(), key=lambda kv: _distinct_key(col, kv[0]))

    # ------------------------------------------------------------ validation
    def revalidate(self, emit: bool = True) -> None:
        self.validator.validate([*self._staged, *self._stored_rows()])
        if emit and self.rowCount():
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(self.rowCount() - 1, self.columnCount() - 1),
            )

    def validation_report(self):
        return self.validator.validate([*self._staged, *self._stored_rows()])

    # ------------------------------------------------------------- changeset
    def replay(self, payload: Optional[dict]) -> dict[str, int]:
        """Put a stored branch's changes back into the grid.

        The inverse of :meth:`changeset`: inserts come back as staged rows,
        updates re-mark the cells they touched, deletes re-mark their rows.
        Anything naming a row that is no longer there is skipped and counted,
        because a branch can outlive the rows it refers to.
        """
        counts = {"inserts": 0, "updates": 0, "deletes": 0, "missing": 0,
                  "unreadable": 0}
        if not isinstance(payload, dict):
            return counts
        operations = payload.get("operations") or []
        if not isinstance(operations, (list, tuple)) or not operations:
            return counts

        by_pk = {r.values.get(self.spec.pk.key): r for r in self._stored_rows()}
        by_pk.update({r.values.get(self.spec.pk.key): r for r in self._db_rows})
        staged: list[Row] = []
        for op in operations:
            if not isinstance(op, dict):
                # A branch written by something else, or an older format.
                counts["unreadable"] += 1
                continue
            kind = op.get("op")
            if kind not in ("insert", "update", "delete"):
                counts["unreadable"] += 1
                continue
            if kind == "insert":
                values = {**(op.get("key") or {}), **(op.get("values") or {})}
                staged.append(self.blank_staged(values))
                counts["inserts"] += 1
                continue
            row = by_pk.get(op.get("pk"))
            if row is None:
                counts["missing"] += 1
                continue
            if kind == "delete":
                row.state = RowState.DELETED
                counts["deletes"] += 1
            elif kind == "update":
                row.remember_original()
                for change in op.get("changes") or []:
                    if not isinstance(change, dict) or "column" not in change:
                        counts["unreadable"] += 1
                        continue
                    row.values[change["column"]] = change.get("new")
                row.search_text = None
                row.recompute_state()
                counts["updates"] += 1

        if staged:
            self.beginResetModel()
            self._staged = staged + self._staged
            self._reindex()
            self.endResetModel()
        self._rebuild_order()
        self.revalidate(emit=False)
        self.layoutChanged.emit()
        self.stateChanged.emit()
        return counts

    def changeset(self) -> ChangeSet:
        return ChangeSet.from_rows(
            self.spec, [*self._staged, *self._stored_rows()], self.lookups
        )

    def dirty_counts(self) -> dict[str, int]:
        counts = {"inserts": 0, "updates": 0, "deletes": 0, "errors": 0}
        for row in self._staged:
            if row.state is RowState.NEW and not row.is_empty(self.spec):
                counts["inserts"] += 1
            if row.errors:
                counts["errors"] += 1
        for row in self._stored_rows():
            if row.state is RowState.UPDATED:
                counts["updates"] += 1
            elif row.state is RowState.DELETED:
                counts["deletes"] += 1
            if row.errors:
                counts["errors"] += 1
        return counts

    def row_by_uid(self, uid: int) -> Optional[Row]:
        return self._by_uid.get(uid)

    def visible_db_rows(self) -> int:
        """Stored rows the resident window is showing."""
        return len(self._view)


# -- undo commands -----------------------------------------------------------
# Rows are addressed by uid, never by visual position, so undo keeps working
# after a sort, a filter change or a deletion.


class CellEditCommand(QUndoCommand):
    """Set one or many cells: typing, fill-down, clear and paste."""

    def __init__(self, model, changes: Sequence[tuple[int, str, Any]], text: str) -> None:
        super().__init__(text)
        self._model = model
        self._new = list(changes)
        self._old: list[tuple[int, str, Any]] = []
        for uid, key, _ in changes:
            row = model.row_by_uid(uid)
            if row is not None:
                self._old.append((uid, key, row.values.get(key)))

    def redo(self) -> None:
        self._model.apply_values(self._new)

    def undo(self) -> None:
        self._model.apply_values(self._old)


class StagingCommand(QUndoCommand):
    """Structural change to the staging block (add / remove / paste)."""

    def __init__(self, model, before: Sequence[Row], after: Sequence[Row], text: str) -> None:
        super().__init__(text)
        self._model = model
        self._before = list(before)
        self._after = list(after)

    def redo(self) -> None:
        self._model.restore_staging(self._after)

    def undo(self) -> None:
        self._model.restore_staging(self._before)


class DeleteCommand(QUndoCommand):
    """Flip existing rows between CLEAN/UPDATED and DELETED."""

    def __init__(self, model, uids: Sequence[int], target: RowState) -> None:
        verb = "delete" if target is RowState.DELETED else "restore"
        super().__init__(f"{verb} {len(uids)} row(s)")
        self._model = model
        self._target = target
        self._uids = list(uids)
        self._previous: dict[int, RowState] = {}
        for uid in self._uids:
            row = model.row_by_uid(uid)
            if row is not None:
                self._previous[uid] = row.state

    def redo(self) -> None:
        self._model.set_row_states({uid: self._target for uid in self._uids})

    def undo(self) -> None:
        self._model.set_row_states(dict(self._previous))


class GridDelegate(QStyledItemDelegate):
    """Per-type cell editors, plus the error border and the staging rule."""

    def __init__(self, model, parent=None) -> None:
        super().__init__(parent)
        self._model = model

    # --------------------------------------------------------------- editors
    def createEditor(
        self, parent: QWidget, option: QStyleOptionViewItem, index: QModelIndex
    ) -> QWidget:
        col = self._model.column(index.column())
        staged = bool(index.data(ROLE_IS_STAGED))

        if col.kind is ColumnKind.ENUM or (col.fk is not None and staged):
            combo = QComboBox(parent)
            combo.setFrame(False)
            combo.setEditable(True)
            combo.lineEdit().setPlaceholderText(f"pick a {col.header}")
            self._fill_combo(combo, col)
            if col.fk is not None:
                # Only foreign keys can grow: an enum's values are fixed by the
                # database type.
                combo.activated.connect(
                    lambda at, c=combo, s=col: self._offer_new_value(at, c, s)
                )
            return combo

        editor = QLineEdit(parent)
        editor.setFrame(False)
        if col.kind in (ColumnKind.INT, ColumnKind.PK):
            editor.setValidator(QRegularExpressionValidator(INT_RE, editor))
            editor.setAlignment(Qt.AlignmentFlag.AlignRight)
        elif col.kind is ColumnKind.FLOAT:
            validator = QDoubleValidator(editor)
            validator.setNotation(QDoubleValidator.Notation.ScientificNotation)
            validator.setLocale(QLocale.c())
            editor.setValidator(validator)
            editor.setAlignment(Qt.AlignmentFlag.AlignRight)
        elif col.kind is ColumnKind.JSON:
            editor.setPlaceholderText('{"key": "value"}   (Shift+F2 for a big editor)')
        elif col.max_length:
            editor.setMaxLength(col.max_length)
        return editor

    def _draw_key_edge(self, painter, rect, column: int) -> None:
        """One side of the outline round the natural-key block."""
        first, last = self._model.key_span
        painter.save()
        painter.setPen(QPen(C.staged_invalid_edge, 1))
        painter.drawLine(rect.topLeft(), rect.topRight())
        painter.drawLine(
            rect.bottomLeft() - QPoint(0, 1), rect.bottomRight() - QPoint(0, 1)
        )
        if column == first:
            painter.drawLine(rect.topLeft(), rect.bottomLeft())
        if column == last:
            edge = rect.right() - 1
            painter.drawLine(edge, rect.top(), edge, rect.bottom() - 1)
        painter.restore()

    def _fill_combo(self, combo: QComboBox, col, select: Optional[str] = None) -> None:
        """(Re)load a drop-down, with the "add a value" entry on foreign keys."""
        choices = (
            list(col.enum_names)
            if col.kind is ColumnKind.ENUM
            else self._model.lookups.choices(col)
        )
        combo.blockSignals(True)
        combo.clear()
        if col.fk is not None:
            combo.addItem(f"+  New {col.header}...", NEW_VALUE)
        if col.nullable:
            combo.addItem("")
        combo.addItems(choices)
        completer = QCompleter(choices, combo)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        combo.setCompleter(completer)
        if select is not None:
            # setCurrentText only touches the line edit on an editable combo,
            # so move the index too or currentData() still says "+ New ...".
            at = combo.findText(select, Qt.MatchFlag.MatchFixedString)
            if at >= 0:
                combo.setCurrentIndex(at)
            else:
                combo.setCurrentText(select)
        combo.blockSignals(False)

    def _offer_new_value(self, at: int, combo: QComboBox, col) -> None:
        """The "+ New ..." entry: invent a dimension value for this session."""
        if combo.itemData(at) != NEW_VALUE:
            return
        previous = combo.property("_previous") or ""
        text, ok = QInputDialog.getText(
            combo,
            f"New {col.header}",
            f"{col.header} value to add.\n\n"
            "It appears in every drop-down for this column straight away, but "
            "it is not written to the database -- the changeset carries it so "
            "your API can create it.",
        )
        text = (text or "").strip()
        if not ok or not text:
            self._fill_combo(combo, col, select=previous)
            return
        try:
            self._model.lookups.add_value(col, text)
        except ValueError as exc:
            self._model.message.emit(f"{col.header}: {exc}", True)
            self._fill_combo(combo, col, select=previous)
            return
        self._fill_combo(combo, col, select=text)
        self._model.message.emit(
            f"{col.header} '{text}' added for this session - it is not in the "
            "database, your API has to create it",
            False,
        )

    def setEditorData(self, editor: QWidget, index: QModelIndex) -> None:
        text = index.data(Qt.ItemDataRole.EditRole) or ""
        if isinstance(editor, QComboBox):
            editor.setProperty("_previous", text)
            at = editor.findText(text, Qt.MatchFlag.MatchFixedString)
            if at >= 0:
                editor.setCurrentIndex(at)
            else:
                editor.setCurrentText(text)
            if editor.lineEdit() is not None:
                editor.lineEdit().selectAll()
            return
        if isinstance(editor, QLineEdit):
            editor.setText(text)
            editor.selectAll()
            return
        super().setEditorData(editor, index)

    def setModelData(self, editor: QWidget, model, index: QModelIndex) -> None:
        if isinstance(editor, QComboBox):
            at = editor.findData(NEW_VALUE)
            if at >= 0 and editor.currentText() == editor.itemText(at):
                return  # the "+ New ..." entry is not a value
            model.setData(index, editor.currentText(), Qt.ItemDataRole.EditRole)
            return
        if isinstance(editor, QLineEdit):
            model.setData(index, editor.text(), Qt.ItemDataRole.EditRole)
            return
        super().setModelData(editor, model, index)

    def eventFilter(self, editor: QWidget, event: QEvent) -> bool:
        # Enter inside a combo should commit, not just close the popup.
        if (
            isinstance(editor, QComboBox)
            and event.type() == QEvent.Type.KeyPress
            and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and not editor.view().isVisible()
        ):
            self.commitData.emit(editor)
            self.closeEditor.emit(editor, QStyledItemDelegate.EndEditHint.SubmitModelCache)
            return True
        return super().eventFilter(editor, event)

    # -------------------------------------------------------------- painting
    def paint(self, painter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        # Excel tints the selection instead of replacing the cell's fill, so a
        # selected error cell still reads as an error. Qt would paint over it,
        # so drop the selected state and lay our own wash on top afterwards.
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        focused = bool(option.state & QStyle.StateFlag.State_HasFocus)
        if selected or focused:
            option = QStyleOptionViewItem(option)
            option.state &= ~QStyle.StateFlag.State_Selected
            option.state &= ~QStyle.StateFlag.State_HasFocus
        super().paint(painter, option, index)
        rect = option.rect
        if selected:
            painter.fillRect(rect, C.selection)
        state = index.data(ROLE_CELL_STATE)
        row = self._model.row_at(index.row())

        if row is not None and row.is_staged:
            # One outline around the whole natural key, rather than a box per
            # cell: the key is one thing, so it should look like one thing.
            if row.errors and self._model.column(index.column()).in_key:
                self._draw_key_edge(painter, rect, index.column())
        elif state in (CELL_ERROR, CELL_WARNING):
            colour = C.error_border if state == CELL_ERROR else C.warning_border
            painter.save()
            painter.setPen(QPen(colour, 1))
            painter.drawRect(rect.adjusted(0, 0, -1, -1))
            painter.restore()

        if focused:  # the active cell, Excel's heavier outline
            painter.save()
            painter.setPen(QPen(C.focus_border, 1))
            painter.drawRect(rect.adjusted(0, 0, -1, -1))
            painter.restore()

        # A green rule between the staging block and the stored rows.
        if index.row() == self._model.staged_count:
            painter.save()
            painter.setPen(QPen(C.staging_separator, 2))
            painter.drawLine(rect.topLeft(), rect.topRight())
            painter.restore()


class FilterHeaderView(QHeaderView):
    """Horizontal header with an Excel-style filter button per column."""

    filterClicked = Signal(int)
    sortRequested = Signal(int, bool, bool)  # section, descending, add_level

    def __init__(self, model, parent=None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._model = model
        self._hover = -1
        self._hover_glyph = False
        self.setSectionsClickable(True)
        self.setHighlightSections(False)
        self.setSortIndicatorShown(False)
        self.setMouseTracking(True)
        self.setMinimumHeight(HEADER_HEIGHT)
        self.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )

    # -------------------------------------------------------------- geometry
    def _glyph_rect(self, rect: QRect) -> QRect:
        return QRect(rect.right() - GLYPH_WIDTH, rect.top(), GLYPH_WIDTH, rect.height())

    def sectionSizeHint(self, logicalIndex: int) -> int:  # pragma: no cover
        return super().sectionSizeHint(logicalIndex) + GLYPH_WIDTH

    # -------------------------------------------------------------- painting
    def paintSection(self, painter: QPainter, rect: QRect, logicalIndex: int) -> None:
        col = self._model.column(logicalIndex)
        active = self._model.filters.get(col.key) is not None
        sort_keys = self._model.sort_keys
        level = next((i for i, (key, _) in enumerate(sort_keys) if key == col.key), None)

        painter.save()
        background = (
            C.header_filtered
            if active
            else (C.header_sorted if level is not None else C.header_bg)
        )
        if self._hover == logicalIndex:
            background = background.lighter(103)
        painter.fillRect(rect, background)
        painter.setPen(QPen(C.grid_line, 1))
        painter.drawLine(rect.topRight(), rect.bottomRight())
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())

        text_rect = rect.adjusted(6, 0, -GLYPH_WIDTH - 2, 0)
        font = painter.font()
        font.setBold(col.in_key)
        painter.setFont(font)
        painter.setPen(QPen(C.header_text))
        label = painter.fontMetrics().elidedText(
            col.header, Qt.TextElideMode.ElideRight, text_rect.width()
        )
        painter.drawText(
            text_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            label,
        )

        if level is not None:
            self._draw_sort(painter, text_rect, sort_keys[level][1], level, len(sort_keys))
        self._draw_funnel(painter, self._glyph_rect(rect), active)
        painter.restore()

    def _draw_funnel(self, painter: QPainter, rect: QRect, active: bool) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        cx = rect.center().x()
        cy = rect.center().y()
        colour = C.filter_icon if active else C.filter_icon_idle
        painter.setPen(QPen(colour, 1))
        painter.setBrush(QBrush(colour) if active else Qt.BrushStyle.NoBrush)
        funnel = QPolygonF(
            [
                QPointF(cx - 5, cy - 4),
                QPointF(cx + 5, cy - 4),
                QPointF(cx + 1, cy + 1),
                QPointF(cx + 1, cy + 5),
                QPointF(cx - 1, cy + 4),
                QPointF(cx - 1, cy + 1),
            ]
        )
        painter.drawPolygon(funnel)
        painter.restore()

    def _draw_sort(
        self, painter: QPainter, rect: QRect, descending: bool, level: int, total: int
    ) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        x = rect.right() - 4
        cy = rect.center().y()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(C.sort_icon))
        if descending:
            arrow = QPolygonF(
                [QPointF(x - 5, cy - 2), QPointF(x + 1, cy - 2), QPointF(x - 2, cy + 3)]
            )
        else:
            arrow = QPolygonF(
                [QPointF(x - 5, cy + 2), QPointF(x + 1, cy + 2), QPointF(x - 2, cy - 3)]
            )
        painter.drawPolygon(arrow)
        if total > 1:
            painter.setPen(QPen(C.sort_icon))
            font = painter.font()
            font.setPointSizeF(max(6.5, font.pointSizeF() - 2.5))
            painter.setFont(font)
            painter.drawText(
                QRect(x - 14, cy - 7, 9, 14),
                int(Qt.AlignmentFlag.AlignCenter),
                str(level + 1),
            )
        painter.restore()

    # ---------------------------------------------------------------- events
    def mouseMoveEvent(self, event) -> None:
        section = self.logicalIndexAt(event.position().toPoint())
        glyph = self._in_glyph(event.position().toPoint(), section)
        if section != self._hover or glyph != self._hover_glyph:
            self._hover, self._hover_glyph = section, glyph
            self.viewport().update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover, self._hover_glyph = -1, False
        self.viewport().update()
        super().leaveEvent(event)

    def _in_glyph(self, pos: QPoint, section: int) -> bool:
        if section < 0:
            return False
        left = self.sectionViewportPosition(section)
        rect = QRect(left, 0, self.sectionSize(section), self.height())
        return self._glyph_rect(rect).contains(pos)

    def mousePressEvent(self, event) -> None:
        pos = event.position().toPoint()
        section = self.logicalIndexAt(pos)
        if section < 0:
            super().mousePressEvent(event)
            return
        if event.button() == Qt.MouseButton.LeftButton and self._in_glyph(pos, section):
            self.filterClicked.emit(section)
            return
        if event.button() == Qt.MouseButton.LeftButton:
            keys = self._model.sort_keys
            col = self._model.column(section)
            current = next((d for k, d in keys if k == col.key), None)
            descending = False if current is None else not current
            add = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
            self.sortRequested.emit(section, descending, add)
            return
        super().mousePressEvent(event)


class GutterHeaderView(QHeaderView):
    """Vertical header: row numbers, state stripe, deletion markers."""

    undeleteClicked = Signal(int)

    def __init__(self, model, parent=None) -> None:
        super().__init__(Qt.Orientation.Vertical, parent)
        self._model = model
        self.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.setDefaultSectionSize(ROW_HEIGHT)
        self.setFixedWidth(GUTTER_WIDTH)
        self.setSectionsClickable(True)
        self.setHighlightSections(False)
        self.setMouseTracking(True)

    def paintSection(self, painter: QPainter, rect: QRect, logicalIndex: int) -> None:
        model = self._model
        row = model.row_at(logicalIndex)
        painter.save()
        painter.fillRect(rect, C.header_bg)
        painter.setPen(QPen(C.grid_line, 1))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        painter.drawLine(rect.topRight(), rect.bottomRight())

        if row is not None:
            stripe = {
                RowState.NEW: C.staging_separator,
                RowState.UPDATED: C.stripe_updated,
                RowState.DELETED: C.deleted_mark,
            }.get(row.state)
            if stripe is not None and not (
                row.state is RowState.NEW and row.is_empty(model.spec)
            ):
                painter.fillRect(
                    QRect(rect.right() - 3, rect.top() + 1, 3, rect.height() - 1), stripe
                )

        label = model.headerData(logicalIndex, Qt.Orientation.Vertical) or ""
        colour, bold = C.gutter_text, False
        if row is not None and row.is_staged:
            if row.is_empty(model.spec):
                colour, bold = C.staged_mark, True    # the "+": put new data here
            elif row.errors:
                colour, bold = C.attention, True      # the "!!!": needs a look
        font = painter.font()
        font.setBold(bold)
        painter.setFont(font)
        painter.setPen(QPen(colour))
        painter.drawText(
            rect.adjusted(MARK_ZONE, 0, -7, 0),
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            str(label),
        )

        if model.deleted_above(logicalIndex):
            self._draw_marker(painter, rect.top(), rect)
        if logicalIndex == model.rowCount() - 1 and model.has_trailing_deleted():
            self._draw_marker(painter, rect.bottom() + 1, rect)
        painter.restore()

    def _draw_marker(self, painter: QPainter, y: int, rect: QRect) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(C.deleted_mark))
        x = rect.left() + 2
        painter.drawPolygon(
            QPolygonF([QPointF(x, y - 4.5), QPointF(x + 7, y), QPointF(x, y + 4.5)])
        )
        painter.restore()

    # ---------------------------------------------------------------- events
    def mousePressEvent(self, event) -> None:
        pos = event.position().toPoint()
        section = self.logicalIndexAt(pos)
        if (
            event.button() == Qt.MouseButton.LeftButton
            and pos.x() <= MARK_ZONE
            and section >= 0
        ):
            target = self._pick_marker(section, pos)
            if target is not None:
                self.undeleteClicked.emit(target)
                return
        super().mousePressEvent(event)

    def contextMenuEvent(self, event) -> None:
        """Right-click an index (or a selection of them) to delete the rows."""
        section = self.logicalIndexAt(event.pos())
        if section < 0:
            return
        selection = self.selectionModel()
        rows = sorted(i.row() for i in selection.selectedRows()) if selection else []
        if section not in rows:  # right-clicking outside the selection picks that row
            self.setCurrentIndex(self._model.index(section, 0))
            if selection is not None:
                selection.select(
                    self._model.index(section, 0),
                    QItemSelectionModel.SelectionFlag.ClearAndSelect
                    | QItemSelectionModel.SelectionFlag.Rows,
                )
            rows = [section]

        menu = QMenu(self)
        what = f"{len(rows)} rows" if len(rows) > 1 else "row"
        menu.addAction(f"Delete {what}\tDel", lambda: self._model.delete_rows(rows))
        if any(
            (r := self._model.row_at(i)) is not None and r.is_dirty and not r.is_staged
            for i in rows
        ):
            menu.addAction(
                f"Revert {what}\tCtrl+Backspace", lambda: self._model.revert_rows(rows)
            )
        if self._model.deleted_above(section):
            menu.addAction(
                "Restore the rows deleted here",
                lambda: self._model.restore_deleted_above(section),
            )
        menu.addSeparator()
        menu.addAction("Add staged row\tCtrl++", lambda: self._model.add_staged_rows(1))
        menu.exec(event.globalPos())

    def _pick_marker(self, section: int, pos: QPoint) -> Optional[int]:
        top = self.sectionViewportPosition(section)
        height = self.sectionSize(section)
        if self._model.deleted_above(section) and pos.y() - top <= 7:
            return section
        if (
            section == self._model.rowCount() - 1
            and self._model.has_trailing_deleted()
            and top + height - pos.y() <= 7
        ):
            return self._model.rowCount()
        return None

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ToolTip:
            pos = event.pos()
            section = self.logicalIndexAt(pos)
            if section >= 0 and pos.x() <= MARK_ZONE:
                target = self._pick_marker(section, pos)
                if target is not None:
                    rows = self._model.deleted_rows_above(target)
                    names = ", ".join(f"#{r.pk(self._model.spec)}" for r in rows[:8])
                    more = "" if len(rows) <= 8 else f" and {len(rows) - 8} more"
                    QToolTip.showText(
                        self.mapToGlobal(pos),
                        f"{len(rows)} row(s) deleted here: {names}{more}"
                        "<br><i>click the marker to restore</i>",
                        self,
                    )
                    return True
        return super().event(event)


class GridView(QTableView):
    """The table view: selection, clipboard, fill, and the context menu."""

    filterRequested = Signal(int)
    statsChanged = Signal(str)

    def __init__(self, model: StagedTableModel, parent=None) -> None:
        super().__init__(parent)
        self._model = model
        self.setModel(model)

        self.setItemDelegate(GridDelegate(model, self))
        self._header = FilterHeaderView(model, self)
        self.setHorizontalHeader(self._header)
        self._gutter = GutterHeaderView(model, self)
        self.setVerticalHeader(self._gutter)

        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.AnyKeyPressed
        )
        self.setTabKeyNavigation(True)
        self.setAlternatingRowColors(False)
        self.setShowGrid(True)
        self.setWordWrap(False)
        self.setCornerButtonEnabled(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        self.horizontalHeader().setStretchLastSection(True)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)

        for i, col in enumerate(model.columns):
            self.setColumnWidth(i, col.width)

        self._header.filterClicked.connect(self.filterRequested)
        self._header.sortRequested.connect(self._on_sort_requested)
        self._gutter.undeleteClicked.connect(model.restore_deleted_above)
        self.customContextMenuRequested.connect(self._show_context_menu)
        model.modelReset.connect(self._emit_stats)

    # ------------------------------------------------------------- selection
    def selectionChanged(self, selected, deselected) -> None:
        super().selectionChanged(selected, deselected)
        self._emit_stats()

    def _emit_stats(self) -> None:
        self.statsChanged.emit(self.selection_summary())

    def selection_summary(self) -> str:
        """Excel's status-bar line. Reads values straight off the rows."""
        model = self._model
        rows, cols = self._selected_block()
        if not rows or not cols:
            return ""
        specs = [model.column(c) for c in cols]
        numbers: list[float] = []
        filled = 0
        cells = 0
        # Deliberately not rows_for: this runs on every selection change, and
        # a query per mouse drag is not worth a status-bar total. In merged
        # mode it therefore covers the rows on screen.
        for visual in rows:
            row = model.row_at(visual)
            if row is None:
                continue
            values = row.values
            for col in specs:
                cells += 1
                value = values.get(col.key)
                if not is_blank(value):
                    filled += 1
                if col.kind.numeric:
                    try:
                        numbers.append(float(value))
                    except (TypeError, ValueError):
                        pass
        bits = [f"{cells} cell(s)", f"{filled} filled"]
        if len(rows) > 1:
            bits.append(f"{len(rows)} rows")
        if numbers:
            total = sum(numbers)
            bits.append(f"sum {total:.6g}")
            bits.append(f"avg {total / len(numbers):.6g}")
            bits.append(f"min {min(numbers):.6g}")
            bits.append(f"max {max(numbers):.6g}")
        return "   |   ".join(bits)

    # -------------------------------------------------------------- keyboard
    def keyPressEvent(self, event) -> None:
        # Ctrl+C/X/V copy/cut/paste (Ctrl+Shift+C with headers), Del clear,
        # Ctrl+D/R fill down/right, Ctrl+Plus/Minus add/delete rows,
        # Ctrl+Backspace revert, F2 / Shift+F2 edit / big editor,
        # Alt+Down open a drop-down, Ctrl+Shift+L filter this column.
        key, mods = event.key(), event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)

        if ctrl and key == Qt.Key.Key_C:
            self.copy_selection(with_headers=shift)
            return
        if ctrl and key == Qt.Key.Key_X:
            self.cut_selection()
            return
        if ctrl and key == Qt.Key.Key_V:
            self.paste()
            return
        if ctrl and key == Qt.Key.Key_D:
            self.fill(down=True)
            return
        if ctrl and key == Qt.Key.Key_R:
            self.fill(down=False)
            return
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and not ctrl:
            # Whole rows picked from the index gutter: delete them.
            # A cell selection: clear the cells, the way Excel does.
            if self.selectionModel().selectedRows():
                self.delete_selected_rows()
            else:
                self.clear_selection_contents()
            return
        if ctrl and key == Qt.Key.Key_Backspace:
            self.revert_selected_rows()
            return
        if ctrl and key in (Qt.Key.Key_Minus, Qt.Key.Key_Underscore):
            self.delete_selected_rows()
            return
        if ctrl and key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self._model.add_staged_rows(1)
            return
        if ctrl and shift and key == Qt.Key.Key_L:
            current = self.currentIndex()
            if current.isValid():
                self.filterRequested.emit(current.column())
            return
        if key == Qt.Key.Key_F2 and shift:
            self.open_big_editor()
            return
        if key == Qt.Key.Key_Down and mods & Qt.KeyboardModifier.AltModifier:
            self._open_dropdown()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.state() != (
            QAbstractItemView.State.EditingState
        ):
            current = self.currentIndex()
            if current.isValid() and current.flags() & Qt.ItemFlag.ItemIsEditable:
                self.edit(current)
                return
        super().keyPressEvent(event)

    def _open_dropdown(self) -> None:
        index = self.currentIndex()
        if not index.isValid():
            return
        col = self._model.column(index.column())
        if not col.kind.choice_based:
            return
        self.edit(index)
        editor = self.indexWidget(index) or self.focusWidget()
        popup = getattr(editor, "showPopup", None)
        if callable(popup):
            popup()

    # ------------------------------------------------------------- clipboard
    def _selected_block(self) -> tuple[list[int], list[int]]:
        """Rows and columns the selection covers.

        Walks the selection's rectangles rather than ``selectedIndexes()``,
        which would build one QModelIndex per selected cell -- 50k of them
        after Ctrl+A on a full table.
        """
        ranges = self.selectionModel().selection()
        if not ranges:
            current = self.currentIndex()
            if not current.isValid():
                return [], []
            return [current.row()], [current.column()]
        rows: set[int] = set()
        cols: set[int] = set()
        for block in ranges:
            rows.update(range(block.top(), block.bottom() + 1))
            cols.update(range(block.left(), block.right() + 1))
        return sorted(rows), sorted(cols)

    def copy_selection(self, with_headers: bool = False) -> None:
        rows, cols = self._selected_block()
        if not rows:
            return
        # Read the rows straight out of the model rather than through a
        # QModelIndex and data() per cell: same text, a fraction of the work,
        # which is what makes copying the whole table bearable.
        model = self._model
        specs = [model.column(c) for c in cols]
        lookups = model.lookups
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter="\t", lineterminator="\r\n")
        if with_headers:
            writer.writerow([c.header for c in specs])
        # rows_for, not row_at: a copy that silently left out the rows the
        # window had not reached would be worse than a slow one.
        for row in model.rows_for(rows):
            if row is None:
                continue
            values = row.values
            writer.writerow(
                [display(c, values.get(c.key), lookups) for c in specs]
            )
        QGuiApplication.clipboard().setText(buffer.getvalue().rstrip("\r\n"))
        self._model.message.emit(
            f"Copied {len(rows)} row(s) x {len(cols)} column(s)", False
        )

    def cut_selection(self) -> None:
        self.copy_selection()
        self.clear_selection_contents()

    def paste(self) -> None:
        text = QGuiApplication.clipboard().text()
        if not text:
            return
        grid = _parse_clipboard(text)
        if not grid:
            return
        anchor = self.currentIndex()
        if not anchor.isValid():
            anchor = self._model.index(0, 1)

        rows, cols = self._selected_block()
        single = len(grid) == 1 and len(grid[0]) == 1
        if single and (len(rows) > 1 or len(cols) > 1):
            self._fill_cells(
                [(r, c) for r in rows for c in cols], grid[0][0], "paste value"
            )
            return
        self._paste_block(anchor.row(), anchor.column(), grid)

    def _paste_block(self, top: int, left: int, grid: list[list[str]]) -> None:
        model = self._model
        skipped = 0
        failed = 0

        # Pasting anywhere in the staging block keeps the whole paste in the
        # staging block, growing it as needed -- that is what makes "paste
        # several rows into the blank row" work.
        anchor_in_staging = top < model.staged_count

        staging_before = model.staging_snapshot()
        staging_after = [_copy_row(r) for r in staging_before]
        db_changes: list[tuple[int, str, Any]] = []
        new_values: list[str] = []

        for row_offset, line in enumerate(grid):
            target_row = top + row_offset
            if anchor_in_staging:
                while target_row >= len(staging_after):
                    staging_after.append(model.blank_staged())
                row = staging_after[target_row]
            else:
                if target_row >= model.rowCount():
                    break
                row = model.row_at(target_row)
                if row is None or row.state is RowState.DELETED:
                    continue

            for col_offset, raw in enumerate(line):
                column_index = left + col_offset
                if column_index >= len(model.columns):
                    break  # the paste is wider than the grid: drop the overflow
                col = model.columns[column_index]
                editable = (
                    col.editable_on_staged
                    if anchor_in_staging
                    else col.editable_on_existing
                )
                if not editable:
                    skipped += 1
                    continue
                try:
                    value, invented = model.value_for(col, raw)
                except ValueError:
                    failed += 1
                    continue
                if invented:
                    new_values.append(invented)
                if anchor_in_staging:
                    row.values[col.key] = value
                else:
                    db_changes.append((row.uid, col.key, value))

        grew = len(staging_after) != len(staging_before) or any(
            a.values != b.values for a, b in zip(staging_after, staging_before)
        )
        if not grew and not db_changes:
            model.message.emit("Nothing could be pasted here", True)
            return

        model.undo_stack.beginMacro("paste")
        if grew:
            model.undo_stack.push(
                StagingCommand(model, staging_before, staging_after, "paste new rows")
            )
        if db_changes:
            model.undo_stack.push(CellEditCommand(model, db_changes, "paste cells"))
        model.undo_stack.endMacro()

        note = [f"Pasted {len(grid)} row(s)"]
        if skipped:
            note.append(f"{skipped} read-only cell(s) skipped")
        if failed:
            note.append(f"{failed} cell(s) rejected")
        if new_values:
            note.append(
                f"{len(set(new_values))} new value(s) added for this session, "
                "which your API has to create"
            )
        model.message.emit("; ".join(note), bool(failed))

    # ------------------------------------------------------------------ fill
    def _fill_cells(
        self, cells: Iterable[tuple[int, int]], text: str, label: str
    ) -> None:
        model = self._model
        staging_before = model.staging_snapshot()
        staging_after = [_copy_row(r) for r in staging_before]
        db_changes: list[tuple[int, str, Any]] = []
        new_values: list[str] = []
        touched_staging = False
        failed = 0

        for row_index, column_index in cells:
            col = model.column(column_index)
            row = model.row_at(row_index)
            if row is None:
                continue
            editable = (
                col.editable_on_staged if row.is_staged else col.editable_on_existing
            )
            if not editable or row.state is RowState.DELETED:
                continue
            try:
                value, invented = model.value_for(col, text)
            except ValueError:
                failed += 1
                continue
            if invented:
                new_values.append(invented)
            if row.is_staged:
                if row_index < len(staging_after):
                    staging_after[row_index].values[col.key] = value
                    touched_staging = True
            else:
                db_changes.append((row.uid, col.key, value))

        if not touched_staging and not db_changes:
            if failed:
                model.message.emit(f"{failed} cell(s) rejected", True)
            return
        model.undo_stack.beginMacro(label)
        if touched_staging:
            model.undo_stack.push(
                StagingCommand(model, staging_before, staging_after, label)
            )
        if db_changes:
            model.undo_stack.push(CellEditCommand(model, db_changes, label))
        model.undo_stack.endMacro()
        if failed:
            model.message.emit(f"{failed} cell(s) rejected", True)

    def fill(self, down: bool = True) -> None:
        rows, cols = self._selected_block()
        if not rows or not cols:
            return
        if down and len(rows) < 2:
            return
        if not down and len(cols) < 2:
            return
        if down:
            for col in cols:
                source = self._model.index(rows[0], col).data(Qt.ItemDataRole.EditRole)
                self._fill_cells(
                    [(r, col) for r in rows[1:]], str(source or ""), "fill down"
                )
        else:
            for row in rows:
                source = self._model.index(row, cols[0]).data(Qt.ItemDataRole.EditRole)
                self._fill_cells(
                    [(row, c) for c in cols[1:]], str(source or ""), "fill right"
                )

    def clear_selection_contents(self) -> None:
        indexes = self.selectedIndexes() or [self.currentIndex()]
        cells = [(i.row(), i.column()) for i in indexes if i.isValid()]
        if cells:
            self._fill_cells(cells, "", "clear cells")

    # ------------------------------------------------------------------ rows
    def delete_selected_rows(self) -> None:
        rows = {i.row() for i in self.selectedIndexes()}
        if not rows:
            current = self.currentIndex()
            if current.isValid():
                rows = {current.row()}
        if rows:
            self._model.delete_rows(rows)

    def revert_selected_rows(self) -> None:
        rows = {i.row() for i in self.selectedIndexes()}
        if rows:
            self._model.revert_rows(rows)

    def open_big_editor(self) -> None:
        index = self.currentIndex()
        if not index.isValid() or not (index.flags() & Qt.ItemFlag.ItemIsEditable):
            return
        col = self._model.column(index.column())
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Edit {col.header}")
        dialog.resize(520, 360)
        layout = QVBoxLayout(dialog)
        editor = QPlainTextEdit()
        value = index.data(ROLE_RAW)
        if col.kind is ColumnKind.JSON and value is not None:
            editor.setPlainText(display_json_pretty(value))
        else:
            editor.setPlainText(str(index.data(Qt.ItemDataRole.EditRole) or ""))
        layout.addWidget(editor)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._model.setData(index, editor.toPlainText(), Qt.ItemDataRole.EditRole)

    def _on_sort_requested(self, section: int, descending: bool, add: bool) -> None:
        order = (
            Qt.SortOrder.DescendingOrder if descending else Qt.SortOrder.AscendingOrder
        )
        if add:
            self._model.add_sort(section, order)
        else:
            self._model.sort(section, order)

    # --------------------------------------------------------- context menu
    def _show_context_menu(self, point: QPoint) -> None:
        index = self.indexAt(point)
        model = self._model
        menu = QMenu(self)

        if index.isValid():
            col = model.column(index.column())
            row = model.row_at(index.row())
            menu.addAction("Copy\tCtrl+C", self.copy_selection)
            menu.addAction(
                "Copy with headers\tCtrl+Shift+C",
                lambda: self.copy_selection(with_headers=True),
            )
            menu.addAction("Paste\tCtrl+V", self.paste)
            menu.addSeparator()
            if col.kind is ColumnKind.JSON or col.kind is ColumnKind.STR:
                menu.addAction("Open editor\tShift+F2", self.open_big_editor)
            menu.addAction("Clear contents\tDel", self.clear_selection_contents)
            menu.addAction("Fill down\tCtrl+D", lambda: self.fill(True))
            menu.addSeparator()
            menu.addAction("Add staged row\tCtrl++", lambda: model.add_staged_rows(1))
            if row is not None and row.is_staged:
                menu.addAction("Remove staged row\tCtrl+-", self.delete_selected_rows)
            else:
                menu.addAction("Delete row(s)\tCtrl+-", self.delete_selected_rows)
                if row is not None and row.is_dirty:
                    menu.addAction(
                        "Revert row(s)\tCtrl+Backspace", self.revert_selected_rows
                    )
            menu.addSeparator()
            if col.kind.filterable:
                value = index.data(Qt.ItemDataRole.DisplayRole)
                menu.addAction(
                    f"Filter by {value!r}" if value else "Filter by blanks",
                    lambda: self._quick_filter(index),
                )
                menu.addAction(
                    f"Filter {col.header}...\tCtrl+Shift+L",
                    lambda: self.filterRequested.emit(index.column()),
                )
                if model.filters.get(col.key) is not None:
                    menu.addAction(
                        f"Clear filter on {col.header}",
                        lambda: model.clear_filters(col.key),
                    )
            menu.addSeparator()
            menu.addAction(
                "Fit column to contents",
                lambda: self.resizeColumnToContents(index.column()),
            )
        menu.addAction("Clear all filters", model.clear_filters)
        menu.exec(self.viewport().mapToGlobal(point))

    def _quick_filter(self, index: QModelIndex) -> None:
        col = self._model.column(index.column())
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        self._model.set_filter(ColumnFilter(key=col.key, allowed=frozenset({str(text)})))


# -- helpers -----------------------------------------------------------------


def _copy_row(row: Row) -> Row:
    clone = Row(
        values=dict(row.values),
        original=None if row.original is None else dict(row.original),
        state=row.state,
        uid=row.uid,
        is_new=row.is_new,
    )
    clone.errors = dict(row.errors)
    clone.warnings = dict(row.warnings)
    clone.key_conflict = row.key_conflict
    return clone


def _equal(a: Any, b: Any) -> bool:
    if isinstance(a, Unresolved) or isinstance(b, Unresolved):
        return a == b
    if is_blank(a) and is_blank(b):
        return True
    return a == b


def _distinct_key(col: ColumnSpec, text: str):
    """Sort the filter dialog's value list: blanks last, numbers numerically."""
    if not text:
        return (2, 0.0, "")
    if col.kind.numeric:
        try:
            return (0, float(text.replace(",", "")), "")
        except ValueError:
            return (1, 0.0, text.casefold())
    if col.kind is ColumnKind.ENUM:
        names = col.enum_names
        return (0, names.index(text) if text in names else len(names), "")
    return (0, 0.0, text.casefold())


def _blend(a: QColor, b: QColor) -> QColor:
    return QColor(
        (a.red() + b.red()) // 2,
        (a.green() + b.green()) // 2,
        (a.blue() + b.blue()) // 2,
    )


def _parse_clipboard(text: str) -> list[list[str]]:
    cleaned = text.replace("\r\n", "\n").rstrip("\n")
    if not cleaned:
        return []
    reader = csv.reader(io.StringIO(cleaned), delimiter="\t")
    return [row for row in reader if row]
