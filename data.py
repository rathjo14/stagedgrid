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
from typing import Any, Callable, Iterable, Optional

from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from schema import (
    LABEL_COLUMNS,
    ColumnKind,
    ColumnSpec,
    Lookups,
    ModelE,
    TableSpec,
    Unresolved,
    display,
    is_blank,
    make_engine,
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
    """One grid row: ``values`` is live, ``original`` is what the database had."""

    values: dict[str, Any]
    original: Optional[dict[str, Any]] = None
    state: RowState = RowState.CLEAN
    uid: int = field(default_factory=lambda: next(_uid_counter))
    errors: dict[str, str] = field(default_factory=dict)
    warnings: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_db(cls, values: dict[str, Any]) -> "Row":
        return cls(values=dict(values), original=dict(values), state=RowState.CLEAN)

    @classmethod
    def staged(cls, spec: TableSpec, values: Optional[dict[str, Any]] = None) -> "Row":
        blank = {c.key: None for c in spec.columns}
        if values:
            blank.update(values)
        return cls(values=blank, original=None, state=RowState.NEW)

    @property
    def is_staged(self) -> bool:
        return self.original is None

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
        if self.original is None:
            return {k for k, v in self.values.items() if not is_blank(v)}
        return {k for k, v in self.values.items() if not _same(v, self.original.get(k))}

    def is_cell_changed(self, key: str) -> bool:
        if self.original is None:
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
        """True for an untouched staging row."""
        return all(is_blank(v) for k, v in self.values.items() if k != spec.pk.key)


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
                allowed = ", ".join(col.enum_values) or "the allowed values"
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
    """Flag every natural-key cell of a row with the same message."""
    for col in spec.key_columns:
        row.errors[col.key] = message


def _normalise(key: tuple) -> tuple:
    return tuple(getattr(v, "value", v) for v in key)


def existing_key_map(spec: TableSpec, rows: Iterable[Row]) -> dict[tuple, int]:
    """Natural key -> pk for the rows loaded from the database."""
    out: dict[tuple, int] = {}
    for row in rows:
        if row.original is None:
            continue
        key = _normalise(tuple(row.original.get(c.key) for c in spec.key_columns))
        pk = row.original.get(spec.pk.key)
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
    if hasattr(value, "value"):  # enum member
        return value.value
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


class Repository:
    """Loads rows and lookups, and applies a changeset in one transaction."""

    def __init__(
        self,
        engine: Optional[Engine] = None,
        model: type = ModelE,
        label_columns: Optional[dict[str, str]] = None,
    ) -> None:
        self.engine = engine or make_engine()
        self.model = model
        if label_columns is None:
            # The demo mapping belongs to the demo models only: with your own
            # models, let the introspection work the label columns out.
            label_columns = LABEL_COLUMNS if model is ModelE else {}
        self.spec = TableSpec.from_mapped_class(model, label_columns)

    def load_lookups(self) -> Lookups:
        lookups = Lookups()
        with Session(self.engine) as s:
            for col in self.spec.fk_columns:
                target = col.fk.target_class
                pairs = s.execute(
                    select(
                        getattr(target, col.fk.target_pk_attr),
                        getattr(target, col.fk.label_attr),
                    )
                ).all()
                lookups.register(col, pairs)
        return lookups

    def load_rows(self, limit: Optional[int] = None) -> list[Row]:
        keys = [c.key for c in self.spec.columns]
        enum_keys = {c.key for c in self.spec.columns if c.kind is ColumnKind.ENUM}
        stmt = select(*(getattr(self.model, k) for k in keys)).order_by(
            getattr(self.model, self.spec.pk.key)
        )
        if limit:
            stmt = stmt.limit(limit)

        rows: list[Row] = []
        with Session(self.engine) as s:
            for record in s.execute(stmt):
                values = dict(zip(keys, record))
                for key in enum_keys:
                    if values[key] is not None:
                        values[key] = getattr(values[key], "value", values[key])
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
        target = col.fk.target_class
        try:
            with Session(self.engine) as s, s.begin():
                row = target(**{col.fk.label_attr: label})
                s.add(row)
                s.flush()
                return getattr(row, col.fk.target_pk_attr)
        except SQLAlchemyError as exc:
            raise ValueError(_clean_error(exc)) from exc

    def natural_keys(self) -> dict[tuple, int]:
        """Natural key -> pk straight from the database."""
        key_cols = [getattr(self.model, c.key) for c in self.spec.key_columns]
        pk_attr = getattr(self.model, self.spec.pk.key)
        out: dict[tuple, int] = {}
        with Session(self.engine) as s:
            for pk, *key in s.execute(select(pk_attr, *key_cols)):
                out[tuple(getattr(v, "value", v) for v in key)] = pk
        return out

    def apply(self, changeset: ChangeSet, dry_run: bool = False) -> ApplyResult:
        """Deletes, then updates, then inserts, so a freed key can be reused.

        ``dry_run`` replays everything and rolls back, which is the cheapest
        way to find out whether the database agrees with our validation.
        """
        result = ApplyResult(dry_run=dry_run)
        pk_attr = getattr(self.model, self.spec.pk.key)
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
                    obj = self.model(**payload)
                    s.add(obj)
                    s.flush()
                    result.inserted.append(getattr(obj, self.spec.pk.key))

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
