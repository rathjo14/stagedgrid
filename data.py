"""Row state, column filters, validation, the pending changeset, the repository.

THIS IS THE ENTRY POINT. Everything the grid knows is reachable from here
without touching the UI::

    from data import Repository
    from schema import make_engine

    repo = Repository(engine=make_engine())   # or model=YourTable
    rows = repo.load_rows()                   # list[Row] -- the data
    lookups = repo.load_lookups()             # id <-> label for every foreign key
    spec = repo.spec                          # the columns, derived from the mapper

    changes = window.model.changeset()        # ChangeSet -- what is pending
    changes.operations                        # [Operation(kind="insert"|"update"|"delete")]
    changes.inserts, changes.updates, changes.deletes
    payload = changes.to_payload()            # JSON-ready dict for your API

    repo.apply(changes, dry_run=True)         # ask the database without writing
    repo.apply(changes)                       # write it

``window.model`` is the ``StagedTableModel`` in grid.py; ``changeset()`` builds
a fresh ``ChangeSet`` from the live rows every time, so there is no state to
keep in sync. To be handed the payload instead of asking for it, pass a sink:
``MainWindow(repo, sink=my_api.submit_changes)`` -- it is called on commit.
"""

from __future__ import annotations

import datetime as dt
import enum
import itertools
import operator
import re
from dataclasses import dataclass, field
from functools import cmp_to_key
from typing import Any, Callable, Iterable, Optional, Sequence

from sqlalchemy import delete, func, insert, null, select, update
from sqlalchemy import Table
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from schema import (
    LABEL_COLUMNS,
    Updates,
    UpdatesSpec,
    natural_sort_key,
    ColumnKind,
    ColumnSpec,
    Lookups,
    ModelE,
    TableSpec,
    Unresolved,
    display,
    is_blank,
    column_of,
    make_engine,
    sort_key,
    to_db,
)

_uid_counter = itertools.count(1)


# ---------------------------------------------------------------------- rows


class RowState(enum.Enum):
    CLEAN = "clean"
    NEW = "new"
    UPDATED = "updated"
    DELETED = "deleted"


@dataclass
class Row:
    """One grid row.

    ``values`` is live. ``original`` is what the database had, and is only
    taken on the first edit -- a table of ten thousand untouched rows should
    not carry ten thousand copies of itself. Anything that writes to
    ``values`` on a stored row must call :meth:`remember_original` first, or
    the edit will not be seen as a change; the model does this for you.
    """

    values: dict[str, Any]
    original: Optional[dict[str, Any]] = None
    state: RowState = RowState.CLEAN
    uid: int = field(default_factory=lambda: next(_uid_counter))
    errors: dict[str, str] = field(default_factory=dict)
    #: Set when the error is the natural key as a whole -- a duplicate --
    #: rather than any one cell. The grid paints those as one block, and the
    #: cells with their own problem more strongly, so "these are the ones to
    #: look at" is visible without reading a tooltip.
    key_conflict: Optional[str] = None
    warnings: dict[str, str] = field(default_factory=dict)
    is_new: bool = False
    search_text: Optional[str] = field(default=None, repr=False, compare=False)
    """Every searchable cell of this row, folded and joined.

    The quick-search box would otherwise re-render every cell of every row on
    each keystroke. Built on demand by the model and dropped whenever a value
    changes.
    """

    @classmethod
    def from_db(cls, values: dict[str, Any]) -> "Row":
        # No copy of the values yet: `original` is taken on the first edit, so
        # the thousands of rows nobody touches cost one dict, not two.
        return cls(values=dict(values), original=None, state=RowState.CLEAN)

    @classmethod
    def staged(cls, spec: TableSpec, values: Optional[dict[str, Any]] = None) -> "Row":
        blank = {c.key: None for c in spec.columns}
        if values:
            blank.update(values)
        return cls(values=blank, original=None, state=RowState.NEW, is_new=True)

    def remember_original(self) -> None:
        """Snapshot the stored values. Call before changing a stored row."""
        if self.original is None and not self.is_new:
            self.original = dict(self.values)

    @property
    def stored(self) -> dict[str, Any]:
        """What the database holds: the snapshot if there is one, else now."""
        return self.values if self.original is None else self.original

    @property
    def is_staged(self) -> bool:
        return self.is_new

    def pk(self, spec: TableSpec) -> Optional[int]:
        return self.values.get(spec.pk.key)

    def natural_key(self, spec: TableSpec) -> tuple:
        return tuple(self.values.get(c.key) for c in spec.key_columns)

    def natural_key_complete(self, spec: TableSpec) -> bool:
        return all(
            not is_blank(v) and not isinstance(v, Unresolved)
            for v in self.natural_key(spec)
        )

    def changed_keys(self) -> set[str]:
        if self.is_new:
            return {k for k, v in self.values.items() if not is_blank(v)}
        if self.original is None:
            return set()            # never edited, so nothing can have changed
        return {k for k, v in self.values.items() if not _same(v, self.original.get(k))}

    def is_cell_changed(self, key: str) -> bool:
        if self.is_new or self.original is None:
            return False
        return not _same(self.values.get(key), self.original.get(key))

    @property
    def is_dirty(self) -> bool:
        return self.state is not RowState.CLEAN

    def recompute_state(self) -> RowState:
        """Promote CLEAN <-> UPDATED after an edit (NEW and DELETED are sticky)."""
        if self.state in (RowState.NEW, RowState.DELETED):
            return self.state
        self.state = RowState.UPDATED if self.changed_keys() else RowState.CLEAN
        return self.state

    def is_empty(self, spec: TableSpec) -> bool:
        """True for an untouched staging row.

        The primary key and the version do not count: one is assigned by the
        database and the other is inherited from the view, so a row holding
        only those is still a row nobody has typed in.
        """
        ignore = {spec.pk.key}
        version = spec.version_column
        if version is not None:
            ignore.add(version.key)
        return all(is_blank(v) for k, v in self.values.items() if k not in ignore)


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, Unresolved) or isinstance(b, Unresolved):
        return False
    if is_blank(a) and is_blank(b):
        return True
    if isinstance(a, float) or isinstance(b, float):
        try:
            return float(a) == float(b)
        except (TypeError, ValueError):
            return a == b
    return a == b


# ------------------------------------------------------------------- filters


class Op(enum.Enum):
    EQ = "equals"
    NE = "does not equal"
    GT = "is greater than"
    GE = "is greater than or equal to"
    LT = "is less than"
    LE = "is less than or equal to"
    BETWEEN = "is between"
    NOT_BETWEEN = "is not between"
    CONTAINS = "contains"
    NOT_CONTAINS = "does not contain"
    STARTS = "begins with"
    ENDS = "ends with"
    REGEX = "matches regex"
    BLANK = "is blank"
    NOT_BLANK = "is not blank"

    @property
    def arity(self) -> int:
        if self in (Op.BLANK, Op.NOT_BLANK):
            return 0
        if self in (Op.BETWEEN, Op.NOT_BETWEEN):
            return 2
        return 1


NUMERIC_OPS = (
    Op.EQ, Op.NE, Op.GT, Op.GE, Op.LT, Op.LE,
    Op.BETWEEN, Op.NOT_BETWEEN, Op.BLANK, Op.NOT_BLANK,
)
TEXT_OPS = (
    Op.CONTAINS, Op.NOT_CONTAINS, Op.STARTS, Op.ENDS,
    Op.EQ, Op.NE, Op.REGEX, Op.BLANK, Op.NOT_BLANK,
)
CHOICE_OPS = (Op.EQ, Op.NE, Op.BLANK, Op.NOT_BLANK)
DATE_OPS = (Op.EQ, Op.NE, Op.GT, Op.LT, Op.BETWEEN, Op.BLANK, Op.NOT_BLANK)


def ops_for(col: ColumnSpec) -> tuple[Op, ...]:
    """The operators the filter dialog offers for one column."""
    if col.kind.numeric:
        return NUMERIC_OPS
    if col.kind in (ColumnKind.DATE, ColumnKind.DATETIME):
        return DATE_OPS
    if col.kind.choice_based:
        return CHOICE_OPS
    return TEXT_OPS


class Join(enum.Enum):
    AND = "and"
    OR = "or"


@dataclass(frozen=True)
class Condition:
    """One operator plus its operands, as typed in the filter dialog."""

    op: Op
    operand: str = ""
    operand2: str = ""

    def describe(self) -> str:
        if self.op.arity == 0:
            return self.op.value
        if self.op.arity == 2:
            return f"{self.op.value} {self.operand} and {self.operand2}"
        return f"{self.op.value} {self.operand}"


@dataclass
class ColumnFilter:
    """A value checklist and up to two conditions for one column."""

    key: str
    allowed: Optional[frozenset[str]] = None
    conditions: tuple[Condition, ...] = ()
    join: Join = Join.AND

    @property
    def is_active(self) -> bool:
        return self.allowed is not None or bool(self.conditions)

    def describe(self) -> str:
        parts = []
        if self.allowed is not None:
            parts.append(f"{len(self.allowed)} value(s) selected")
        if self.conditions:
            glue = f" {self.join.value} "
            parts.append(glue.join(c.describe() for c in self.conditions))
        return "; ".join(parts) or "no filter"

    def predicate(
        self, col: ColumnSpec, lookups: Optional[Lookups] = None
    ) -> Callable[[Any], bool]:
        """Compile to a plain value -> bool, so nothing is re-parsed per row."""
        checks: list[Callable[[Any], bool]] = []

        if self.allowed is not None:
            allowed = self.allowed
            checks.append(lambda v: display(col, v, lookups) in allowed)

        if self.conditions:
            compiled = [_compile(col, c, lookups) for c in self.conditions]
            if self.join is Join.AND:
                checks.append(lambda v: all(fn(v) for fn in compiled))
            else:
                checks.append(lambda v: any(fn(v) for fn in compiled))

        if not checks:
            return _always
        if len(checks) == 1:
            return checks[0]
        return lambda v: all(fn(v) for fn in checks)


def _always(value: Any) -> bool:
    return True


def _as_number(text: str) -> Optional[float]:
    try:
        return float(str(text).replace(",", "").replace("_", ""))
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> Optional[float]:
    """The comparable number in a cell, or None when there is not one."""
    if is_blank(value) or isinstance(value, Unresolved):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_COMPARE = {
    Op.EQ: operator.eq,
    Op.GT: operator.gt,
    Op.GE: operator.ge,
    Op.LT: operator.lt,
    Op.LE: operator.le,
}


def _compile(
    col: ColumnSpec, cond: Condition, lookups: Optional[Lookups]
) -> Callable[[Any], bool]:
    op = cond.op

    if op is Op.BLANK:
        return is_blank
    if op is Op.NOT_BLANK:
        return lambda v: not is_blank(v)

    if col.kind.numeric:
        a = _as_number(cond.operand)
        if a is None:
            return _always
        if op in (Op.BETWEEN, Op.NOT_BETWEEN):
            b = _as_number(cond.operand2)
            if b is None:
                return _always
            lo, hi = min(a, b), max(a, b)
            if op is Op.BETWEEN:
                return lambda v: (x := _number(v)) is not None and lo <= x <= hi
            return lambda v: (x := _number(v)) is None or not (lo <= x <= hi)
        if op is Op.NE:
            # A blank cell does not equal a number.
            return lambda v: (x := _number(v)) is None or x != a
        compare = _COMPARE.get(op)
        if compare is None:
            return _always
        return lambda v: (x := _number(v)) is not None and compare(x, a)

    needle = cond.operand
    if op is Op.REGEX:
        try:
            rx = re.compile(needle, re.IGNORECASE)
        except re.error:
            return _always
        return lambda v: rx.search(display(col, v, lookups)) is not None

    folded = needle.casefold()

    def text_of(value: Any) -> str:
        return display(col, value, lookups).casefold()

    tests: dict[Op, Callable[[str], bool]] = {
        Op.EQ: lambda t: t == folded,
        Op.NE: lambda t: t != folded,
        Op.CONTAINS: lambda t: folded in t,
        Op.NOT_CONTAINS: lambda t: folded not in t,
        Op.STARTS: lambda t: t.startswith(folded),
        Op.ENDS: lambda t: t.endswith(folded),
    }
    check = tests.get(op)
    if check is None:
        return _always
    return lambda v: check(text_of(v))


@dataclass
class FilterSet:
    """All active column filters, keyed by column key."""

    filters: dict[str, ColumnFilter] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return any(f.is_active for f in self.filters.values())

    def active_keys(self) -> list[str]:
        return [k for k, f in self.filters.items() if f.is_active]

    def get(self, key: str) -> Optional[ColumnFilter]:
        f = self.filters.get(key)
        return f if (f is not None and f.is_active) else None

    def set(self, flt: ColumnFilter) -> None:
        if flt.is_active:
            self.filters[flt.key] = flt
        else:
            self.filters.pop(flt.key, None)

    def clear(self, key: Optional[str] = None) -> None:
        if key is None:
            self.filters.clear()
        else:
            self.filters.pop(key, None)


# -------------------------------------------------- what the view consists of
# Two questions -- does this row belong in the view, and where does it go --
# asked identically whether the rows are all in memory or only a window of them
# is. Defined once here so the merged mode cannot drift from the grid.


def searchable_columns(spec: TableSpec) -> list[ColumnSpec]:
    """The columns the quick-search box looks in."""
    return [c for c in spec.columns if not c.is_pk and c.kind.filterable]


class RowMatcher:
    """Does this row belong in the view, given the filters and the search?

    Judged on the values the row has *now*, not the ones the database holds --
    editing a cell can move a row out of a filter, and it should disappear just
    as it would have had it always read that way.
    """

    def __init__(
        self,
        spec: TableSpec,
        lookups: Optional[Lookups] = None,
        filters: Optional[FilterSet] = None,
        search: str = "",
    ) -> None:
        self.spec = spec
        self.lookups = lookups or Lookups()
        self.needle = (search or "").casefold()
        self.columns = searchable_columns(spec)
        self.preds: list[tuple[str, Callable[[Any], bool]]] = []
        for key in (filters.active_keys() if filters else ()):
            col = spec.get(key)
            flt = filters.get(key) if filters else None
            if col is None or flt is None:
                continue
            self.preds.append((col.key, flt.predicate(col, self.lookups)))

    #: Joins the searchable cells in the cached haystack. A needle can never
    #: contain it, so a match cannot straddle two columns -- which keeps this
    #: the same question SQL answers, where the search is a per-column LIKE.
    SEPARATOR = "\x00"

    def searched(self, row: Row) -> bool:
        if not self.needle:
            return True
        haystack = row.search_text
        if haystack is None:
            haystack = row.search_text = self.SEPARATOR.join(
                display(col, row.values.get(col.key), self.lookups)
                for col in self.columns
            ).casefold()
        return self.needle in haystack

    def __call__(self, row: Row) -> bool:
        return all(
            pred(row.values.get(key)) for key, pred in self.preds
        ) and self.searched(row)


def row_order_key(
    spec: TableSpec,
    lookups: Optional[Lookups],
    sort_keys: Sequence[tuple[str, bool]],
):
    """Key function putting rows in the order the grid shows them.

    A tuple key cannot express per-column directions, so this compares level
    by level instead. The rules it encodes are the ones the SQL translation
    also has to honour: blanks go last whichever way the column is sorted, and
    anything still tied falls back to the primary key ascending, which is what
    makes the order total and therefore stable across a refetch.
    """
    levels = [
        (spec[key], descending)
        for key, descending in sort_keys
        if spec.get(key) is not None
    ]
    pk = spec.pk.key

    def compare(a: Row, b: Row) -> int:
        for col, descending in levels:
            left, right = a.values.get(col.key), b.values.get(col.key)
            left_blank, right_blank = is_blank(left), is_blank(right)
            if left_blank != right_blank:
                return 1 if left_blank else -1
            if left_blank:
                continue
            one = sort_key(col, left, lookups)
            two = sort_key(col, right, lookups)
            if one != two:
                return -1 if ((one < two) != descending) else 1
        first, second = a.values.get(pk), b.values.get(pk)
        if first == second:
            return 0
        if first is None:
            return 1
        if second is None:
            return -1
        return -1 if first < second else 1

    return cmp_to_key(compare)


# ---------------------------------------------------------------- validation


@dataclass
class Report:
    """What one validation pass found."""

    error_rows: int = 0
    warning_rows: int = 0
    messages: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error_rows == 0

    def summary(self) -> str:
        if self.ok and not self.warning_rows:
            return "No problems found"
        bits = []
        if self.error_rows:
            bits.append(f"{self.error_rows} row(s) with errors")
        if self.warning_rows:
            bits.append(f"{self.warning_rows} row(s) with warnings")
        return ", ".join(bits)


class Validator:
    """Validates rows in place, writing into ``row.errors`` / ``row.warnings``."""

    def __init__(
        self,
        spec: TableSpec,
        lookups: Optional[Lookups] = None,
        existing_keys: Optional[dict[tuple, int]] = None,
    ) -> None:
        self.spec = spec
        self.lookups = lookups
        self.existing_keys = existing_keys or {}

    def set_existing_keys(self, keys: dict[tuple, int]) -> None:
        self.existing_keys = keys

    def validate(self, rows: Iterable[Row]) -> Report:
        rows = list(rows)
        report = Report()
        spec = self.spec

        deleted_pks = {r.pk(spec) for r in rows if r.state is RowState.DELETED}
        seen: dict[tuple, Row] = {}  # staged rows compete with each other too
        key_label = " / ".join(c.header for c in spec.key_columns)

        for row in rows:
            row.errors.clear()
            row.warnings.clear()
            row.key_conflict = None
            if row.state is RowState.DELETED or row.state is RowState.CLEAN:
                continue
            if row.state is RowState.NEW and row.is_empty(spec):
                continue

            self._check_unresolved(row)
            self._check_required(row)

            if row.state is RowState.NEW:
                self._check_key_complete(row)
                if row.natural_key_complete(spec):
                    key = _normalise(row.natural_key(spec))
                    if key in seen:
                        _key_error(
                            row,
                            spec,
                            f"duplicates another staged row with the same {key_label}",
                        )
                    else:
                        seen[key] = row
                    pk = self.existing_keys.get(key)
                    if pk is not None:
                        if pk in deleted_pks:  # deleting then re-adding is fine
                            row.warnings[spec.key_columns[0].key] = (
                                f"replaces row #{pk}, deleted in this session"
                            )
                        else:
                            _key_error(
                                row,
                                spec,
                                f"{key_label} already exists in the database "
                                f"(row #{pk})",
                            )

            if row.errors:
                report.error_rows += 1
            elif row.warnings:
                report.warning_rows += 1

        for row in rows:
            for key, msg in row.errors.items():
                col = spec.get(key)
                report.messages.append(f"{col.header if col else key}: {msg}")
        return report

    def _check_unresolved(self, row: Row) -> None:
        for col in self.spec.columns:
            value = row.values.get(col.key)
            if not isinstance(value, Unresolved):
                continue
            if col.fk is not None:
                hint = ""
                if self.lookups is not None:
                    choices = ", ".join(self.lookups.choices(col)[:6])
                    hint = f" (expected one of {choices}, ...)"
                row.errors[col.key] = f"{value.text!r} is not a known {col.header}{hint}"
            else:
                allowed = ", ".join(col.enum_names) or "the allowed values"
                row.errors[col.key] = f"{value.text!r} is not one of {allowed}"

    def _check_required(self, row: Row) -> None:
        for col in self.spec.data_columns:
            if not col.nullable and is_blank(row.values.get(col.key)):
                row.errors.setdefault(col.key, f"{col.header} is required")

    def _check_key_complete(self, row: Row) -> None:
        for col in self.spec.key_columns:
            if is_blank(row.values.get(col.key)):
                row.errors.setdefault(col.key, f"{col.header} is required")


def _key_error(row: Row, spec: TableSpec, message: str) -> None:
    """Flag the natural key as a whole: every key cell, one message."""
    row.key_conflict = message
    for col in spec.key_columns:
        row.errors[col.key] = message


def _enum_name(value):
    """Enum members live in the grid as their NAME, never their value."""
    return value.name if isinstance(value, enum.Enum) else value


def _normalise(key: tuple) -> tuple:
    return tuple(_enum_name(v) for v in key)


def existing_key_map(spec: TableSpec, rows: Iterable[Row]) -> dict[tuple, int]:
    """Natural key -> pk for the rows loaded from the database."""
    out: dict[tuple, int] = {}
    for row in rows:
        if row.is_new:
            continue
        stored = row.stored
        key = _normalise(tuple(stored.get(c.key) for c in spec.key_columns))
        pk = stored.get(spec.pk.key)
        if pk is not None:
            out[key] = pk
    return out


# ----------------------------------------------------------------- changeset


@dataclass(frozen=True)
class CellChange:
    """One edited cell, with both the stored values and what the user sees."""

    column: str
    header: str
    old: Any
    new: Any
    old_text: str
    new_text: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "column": self.column,
            "old": _plain(self.old),
            "new": _plain(self.new),
            "old_text": self.old_text,
            "new_text": self.new_text,
        }


@dataclass
class Operation:
    """One pending insert, update or delete."""

    kind: str  # "insert" | "update" | "delete"
    key: dict[str, Any]
    key_text: dict[str, str]
    pk: Optional[int] = None
    uid: Optional[int] = None
    values: dict[str, Any] = field(default_factory=dict)
    values_text: dict[str, str] = field(default_factory=dict)
    changes: list[CellChange] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"op": self.kind}
        if self.pk is not None:
            out["pk"] = self.pk
        out["key"] = {k: _plain(v) for k, v in self.key.items()}
        out["key_labels"] = dict(self.key_text)
        if self.kind == "update":
            out["changes"] = [c.as_dict() for c in self.changes]
        else:
            out["values"] = {k: _plain(v) for k, v in self.values.items()}
        return out


@dataclass
class ChangeSet:
    """Everything pending for one table, derived from row state."""

    table: str
    operations: list[Operation] = field(default_factory=list)
    created_at: dt.datetime = field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc)
    )
    new_values: list[dict[str, str]] = field(default_factory=list)
    """Dimension values invented in this session that these rows refer to.

    stagedgrid never creates them; the receiving service has to, before it can
    apply the inserts that point at them.
    """

    def _of_kind(self, kind: str) -> list[Operation]:
        return [op for op in self.operations if op.kind == kind]

    @property
    def inserts(self) -> list[Operation]:
        return self._of_kind("insert")

    @property
    def updates(self) -> list[Operation]:
        return self._of_kind("update")

    @property
    def deletes(self) -> list[Operation]:
        return self._of_kind("delete")

    @property
    def is_empty(self) -> bool:
        return not self.operations

    @property
    def counts(self) -> dict[str, int]:
        updates = self.updates
        return {
            "inserts": len(self.inserts),
            "updates": len(updates),
            "deletes": len(self.deletes),
            "cells": sum(len(u.changes) for u in updates),
        }

    def summary(self) -> str:
        c = self.counts
        return (
            f"{c['inserts']} insert(s), {c['updates']} update(s) "
            f"({c['cells']} cell(s)), {c['deletes']} delete(s)"
        )

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "table": self.table,
            "created_at": self.created_at.isoformat(),
            "summary": self.counts,
            "operations": [
                op.as_dict() for op in (*self.inserts, *self.updates, *self.deletes)
            ],
        }
        if self.new_values:
            # Create these first: the inserts below reference them by a
            # negative placeholder id that only means something here.
            payload["new_dimension_values"] = self.new_values
        return payload

    @classmethod
    def from_rows(
        cls,
        spec: TableSpec,
        rows: Iterable[Row],
        lookups: Optional[Lookups] = None,
    ) -> "ChangeSet":
        cs = cls(table=spec.table_name)
        key_cols = spec.key_columns
        data_cols = spec.data_columns
        invented = lookups.pending() if lookups is not None else {}
        referenced: set[int] = set()

        for row in rows:
            if row.state is RowState.CLEAN:
                continue  # the vast majority: never pay to build their key dicts
            key = _subset(row.values, key_cols)
            key_text = _subset_text(row.values, key_cols, lookups)

            if row.state is RowState.NEW:
                if row.is_empty(spec):
                    continue
                cs.operations.append(
                    Operation(
                        kind="insert",
                        key=key,
                        key_text=key_text,
                        uid=row.uid,
                        values=_subset(row.values, data_cols, skip_blank=True),
                        values_text=_subset_text(
                            row.values, data_cols, lookups, skip_blank=True
                        ),
                    )
                )
            elif row.state is RowState.UPDATED:
                changes = [
                    CellChange(
                        column=col.key,
                        header=col.header,
                        old=row.original.get(col.key),
                        new=row.values.get(col.key),
                        old_text=display(col, row.original.get(col.key), lookups),
                        new_text=display(col, row.values.get(col.key), lookups),
                    )
                    for col in data_cols
                    if row.is_cell_changed(col.key)
                ]
                if not changes:
                    continue
                cs.operations.append(
                    Operation(
                        kind="update",
                        key=key,
                        key_text=key_text,
                        pk=row.pk(spec),
                        uid=row.uid,
                        changes=changes,
                    )
                )
            elif row.state is RowState.DELETED:
                cs.operations.append(
                    Operation(
                        kind="delete",
                        key=key,
                        key_text=key_text,
                        pk=row.pk(spec),
                        uid=row.uid,
                        values=_subset(row.values, data_cols, skip_blank=True),
                        values_text=_subset_text(
                            row.values, data_cols, lookups, skip_blank=True
                        ),
                    )
                )
            if invented:
                referenced.update(v for v in key.values() if v in invented)

        cs.new_values = [
            {"placeholder_id": pk, **invented[pk]} for pk in sorted(referenced)
        ]
        return cs


def _subset(
    values: dict[str, Any], cols: Iterable[ColumnSpec], skip_blank: bool = False
) -> dict[str, Any]:
    return {
        col.key: values.get(col.key)
        for col in cols
        if not (skip_blank and is_blank(values.get(col.key)))
    }


def _subset_text(
    values: dict[str, Any],
    cols: Iterable[ColumnSpec],
    lookups: Optional[Lookups],
    skip_blank: bool = False,
) -> dict[str, str]:
    return {
        col.key: display(col, values.get(col.key), lookups)
        for col in cols
        if not (skip_blank and is_blank(values.get(col.key)))
    }


def _plain(value: Any) -> Any:
    """JSON-safe representation of a stored value."""
    if value is None or isinstance(value, (int, float, str, bool, dict, list)):
        return value
    if isinstance(value, enum.Enum):  # enum member
        return value.name
    if hasattr(value, "text"):  # Unresolved
        return value.text
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return str(value)


# ---------------------------------------------------------------- repository


@dataclass
class ApplyResult:
    """What ``Repository.apply`` did, or why it did not."""

    inserted: list[int] = field(default_factory=list)
    updated: int = 0
    deleted: int = 0
    error: Optional[str] = None
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None

    def summary(self) -> str:
        if self.error:
            return f"Failed: {self.error}"
        what = "would be" if self.dry_run else ""
        return (
            f"{len(self.inserted)} inserted, {self.updated} updated, "
            f"{self.deleted} deleted {what}".strip()
        )


@dataclass
class Branch:
    """One row of the Updates table: a set of staged changes against a version."""

    id: int
    label: str
    version_id: Optional[int]
    version_label: str
    modified_at: Optional[dt.datetime]
    changes: Optional[dict] = None   # only loaded when the branch is opened

    def summary(self) -> str:
        when = self.modified_at.strftime("%Y-%m-%d %H:%M") if self.modified_at else "-"
        return f"{self.label or f'#{self.id}'}  ({self.version_label}, {when})"


class Repository:
    """Loads rows and lookups, and applies a changeset in one transaction."""

    def __init__(
        self,
        engine: Optional[Engine] = None,
        model: type = ModelE,
        label_columns: Optional[dict[str, str]] = None,
        updates_model: Optional[type] = Updates,
    ) -> None:
        self.engine = engine or make_engine()
        self.model = model
        if label_columns is None:
            # The demo mapping belongs to the demo models only: with your own
            # models, let the introspection work the label columns out.
            label_columns = LABEL_COLUMNS if model is ModelE else {}
        self.spec = TableSpec.of(model, label_columns)
        version = self.version_column
        self.updates: Optional[UpdatesSpec] = (
            UpdatesSpec.from_mapped_class(
                updates_model,
                version_table=version.fk.target_table if version else None,
            )
            if updates_model is not None
            else None
        )

    # -- the Updates table: one row per branch of staged changes ----------
    def _version_label(self, session, version_id: Optional[int]) -> str:
        col = self.version_column
        if col is None or version_id is None:
            return ""
        found = session.execute(
            select(col.fk.label()).where(col.fk.pk() == version_id)
        ).scalar()
        return str(found or "")

    def list_branches(self) -> list[Branch]:
        """Every branch, newest first. The json is left behind until opened."""
        if self.updates is None:
            return []
        u = self.updates
        model = u.model
        wanted = [getattr(model, u.pk)]
        for attr in (u.label, u.version, u.modified_at):
            wanted.append(getattr(model, attr) if attr else null())
        out = []
        with Session(self.engine) as s:
            for pk, label, version_id, modified in s.execute(select(*wanted)):
                out.append(
                    Branch(
                        id=pk,
                        label=str(label or ""),
                        version_id=version_id,
                        version_label=self._version_label(s, version_id),
                        modified_at=modified,
                    )
                )
        out.sort(key=lambda b: (b.modified_at is None, b.modified_at), reverse=True)
        return out

    def load_branch(self, branch_id: int) -> Optional[Branch]:
        """One branch, with its changes."""
        if self.updates is None:
            return None
        u = self.updates
        model = u.model
        with Session(self.engine) as s:
            row = s.get(model, branch_id)
            if row is None:
                return None
            version_id = getattr(row, u.version) if u.version else None
            return Branch(
                id=getattr(row, u.pk),
                label=str(getattr(row, u.label) or "") if u.label else "",
                version_id=version_id,
                version_label=self._version_label(s, version_id),
                modified_at=getattr(row, u.modified_at) if u.modified_at else None,
                changes=getattr(row, u.changes) or {},
            )

    def save_branch(
        self,
        payload: dict,
        version_id: Optional[int] = None,
        branch_id: Optional[int] = None,
        label: Optional[str] = None,
    ) -> int:
        """Write the changes to a branch, creating one if needed.

        This is the only thing Commit does: the fact table itself is never
        written here -- applying a branch is somebody else's job.
        """
        if self.updates is None:
            raise ValueError("no Updates table is configured")
        u = self.updates
        values: dict[str, Any] = {u.changes: payload}
        if u.version and version_id is not None:
            values[u.version] = version_id
        if u.label and label is not None:
            values[u.label] = label
        with Session(self.engine) as s, s.begin():
            if branch_id is None:
                row = u.model(**values)
                s.add(row)
                s.flush()
                return getattr(row, u.pk)
            if u.modified_at:
                values[u.modified_at] = dt.datetime.now(dt.timezone.utc)
            s.execute(
                update(u.model)
                .where(getattr(u.model, u.pk) == branch_id)
                .values(**values)
            )
            return branch_id

    def load_lookups(self) -> Lookups:
        lookups = Lookups()
        with Session(self.engine) as s:
            for col in self.spec.fk_columns:
                pairs = s.execute(
                    select(col.fk.pk(), col.fk.label())
                ).all()
                lookups.register(col, pairs)
        return lookups

    @property
    def version_column(self) -> Optional[ColumnSpec]:
        return self.spec.version_column

    def latest_version(self) -> Optional[tuple[int, str]]:
        """(id, label) of the newest value in the version dimension."""
        col = self.version_column
        if col is None:
            return None
        with Session(self.engine) as s:
            pairs = s.execute(select(col.fk.pk(), col.fk.label())).all()
        if not pairs:
            return None
        pk, label = max(pairs, key=lambda pair: natural_sort_key(str(pair[1])))
        return int(pk), str(label)

    def load_rows(
        self,
        limit: Optional[int] = None,
        latest_version_only: bool = True,
        version_id: Optional[int] = None,
    ) -> list[Row]:
        """Rows for the grid.

        Only the newest version is loaded by default: older ones are never
        edited, so there is no reason to carry them around or filter them out
        afterwards. Pass ``version_id`` to open a different one, which is what
        opening a branch does.
        """
        keys = [c.key for c in self.spec.columns]
        enum_keys = {c.key for c in self.spec.columns if c.kind is ColumnKind.ENUM}
        stmt = select(*(column_of(self.model, k) for k in keys)).order_by(
            column_of(self.model, self.spec.pk.key)
        )
        version = self.version_column
        if version is not None:
            if version_id is None and latest_version_only:
                newest = self.latest_version()
                version_id = newest[0] if newest else None
            if version_id is not None:
                stmt = stmt.where(column_of(self.model, version.key) == version_id)
        if limit:
            stmt = stmt.limit(limit)

        rows: list[Row] = []
        with Session(self.engine) as s:
            for record in s.execute(stmt):
                values = dict(zip(keys, record))
                for key in enum_keys:
                    if values[key] is not None:
                        values[key] = _enum_name(values[key])
                rows.append(Row.from_db(values))
        return rows

    def count(self) -> int:
        with Session(self.engine) as s:
            stmt = select(func.count()).select_from(self.model)
            return int(s.execute(stmt).scalar_one())

    def add_dimension_value(self, col: ColumnSpec, label: str) -> int:
        """Add a value to the dimension table ``col`` points at; return its id.

        This writes immediately rather than joining the staged changeset: a
        staged row has to reference the parent by id, so the parent has to
        exist first. Dimension tables are reference data, so a new code is
        additive and safe on its own.
        """
        if col.fk is None:
            raise ValueError(f"{col.header} is not a foreign key")
        try:
            with Session(self.engine) as s, s.begin():
                made = s.execute(
                    insert(col.fk.table)
                    .values(**{col.fk.label_column: label})
                    .returning(col.fk.pk())
                )
                return int(made.scalar_one())
        except SQLAlchemyError as exc:
            raise ValueError(_clean_error(exc)) from exc

    def count_rows(
        self, latest_version_only: bool = True, version_id: Optional[int] = None
    ) -> int:
        """How many rows the grid would load without a page limit."""
        stmt = select(func.count()).select_from(self.model)
        version = self.version_column
        if version is not None:
            if version_id is None and latest_version_only:
                newest = self.latest_version()
                version_id = newest[0] if newest else None
            if version_id is not None:
                stmt = stmt.where(column_of(self.model, version.key) == version_id)
        with Session(self.engine) as s:
            return int(s.execute(stmt).scalar_one())

    def natural_keys(
        self,
        latest_version_only: bool = False,
        version_id: Optional[int] = None,
    ) -> dict[tuple, int]:
        """Natural key -> pk straight from the database.

        This is what validation needs to answer "does this key already exist?",
        and it is the reason a windowed grid can still answer it: the key is a
        few columns out of twenty-odd and carries no JSON, so at 10,000 rows
        the index is about 1 MB against 35 MB for the rows themselves.
        """
        if not self.spec.key_columns:
            return {}
        key_cols = [column_of(self.model, c.key) for c in self.spec.key_columns]
        pk_attr = column_of(self.model, self.spec.pk.key)
        stmt = select(pk_attr, *key_cols)
        version = self.version_column
        if version is not None:
            if version_id is None and latest_version_only:
                newest = self.latest_version()
                version_id = newest[0] if newest else None
            if version_id is not None:
                stmt = stmt.where(column_of(self.model, version.key) == version_id)
        out: dict[tuple, int] = {}
        with Session(self.engine) as s:
            for pk, *key in s.execute(stmt):
                out[tuple(_enum_name(v) for v in key)] = pk
        return out

    def load_rows_by_pk(self, pks: Iterable[int]) -> list[Row]:
        """Specific rows, wherever they are in the table.

        A stored branch names the rows it changed by primary key, and in
        merged mode the window almost certainly does not hold them, so they
        are fetched directly before its changes are replayed.
        """
        wanted = [p for p in pks if p is not None]
        if not wanted:
            return []
        keys = [c.key for c in self.spec.columns]
        enum_keys = {c.key for c in self.spec.columns if c.kind is ColumnKind.ENUM}
        pk_attr = column_of(self.model, self.spec.pk.key)
        stmt = select(*(column_of(self.model, k) for k in keys)).where(
            pk_attr.in_(tuple(wanted))
        )
        rows: list[Row] = []
        with Session(self.engine) as s:
            for record in s.execute(stmt):
                values = dict(zip(keys, record))
                for key in enum_keys:
                    if values[key] is not None:
                        values[key] = _enum_name(values[key])
                rows.append(Row.from_db(values))
        return rows

    def _insert(self, session, payload: dict) -> int:
        """Add one row and return its new primary key.

        A mapped class is constructed and added, which is what gives the
        defaults and any __init__ a say. A reflected table has no class to
        construct, so the same row goes in through Core.
        """
        pk = self.spec.pk.key
        if isinstance(self.model, Table):
            made = session.execute(
                insert(self.model).values(**payload).returning(column_of(self.model, pk))
            )
            return int(made.scalar_one())
        obj = self.model(**payload)
        session.add(obj)
        session.flush()
        return getattr(obj, pk)

    def apply(self, changeset: ChangeSet, dry_run: bool = False) -> ApplyResult:
        """Deletes, then updates, then inserts, so a freed key can be reused.

        ``dry_run`` replays everything and rolls back, which is the cheapest
        way to find out whether the database agrees with our validation.
        """
        result = ApplyResult(dry_run=dry_run)
        pk_attr = column_of(self.model, self.spec.pk.key)
        deletes = changeset.deletes
        try:
            with Session(self.engine) as s, s.begin():
                if deletes:
                    res = s.execute(
                        delete(self.model).where(
                            pk_attr.in_([op.pk for op in deletes])
                        )
                    )
                    result.deleted = res.rowcount or 0

                for op in changeset.updates:
                    values = {
                        c.column: to_db(self.spec[c.column], c.new) for c in op.changes
                    }
                    res = s.execute(
                        update(self.model).where(pk_attr == op.pk).values(**values)
                    )
                    result.updated += res.rowcount or 0

                for op in changeset.inserts:
                    payload = {
                        key: to_db(self.spec[key], value)
                        for key, value in {**op.key, **op.values}.items()
                    }
                    result.inserted.append(self._insert(s, payload))

                if dry_run:
                    s.flush()
                    raise _Rollback()
        except _Rollback:
            pass
        except SQLAlchemyError as exc:
            result.error = _clean_error(exc)
        except ValueError as exc:
            result.error = str(exc)
        return result


class _Rollback(Exception):
    """Internal: unwinds a dry-run transaction."""


def _clean_error(exc: SQLAlchemyError) -> str:
    """The first useful line of a database error, without the traceback."""
    text = str(getattr(exc, "orig", None) or exc)
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith(("DETAIL", "HINT", "CONTEXT")):
            return line
    return exc.__class__.__name__
