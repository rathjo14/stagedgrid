"""The database layer: models, the column layout the grid reads off them, and
text <-> value conversion.

Nothing above this file hard-codes a column name.  ``TableSpec.from_mapped_class``
reads a SQLAlchemy mapper and works out the grid's columns, so pointing the app
at another fact table is a configuration change, not a rewrite.
"""

from __future__ import annotations

import datetime as dt
import enum
import json
import math
import os
import random
from dataclasses import dataclass, field
from itertools import product
from typing import Any, Iterable, Optional

from sqlalchemy import (
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    create_engine,
    func,
    inspect,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
)
from sqlalchemy.sql.schema import Column, Table

# ---------------------------------------------------------------------------
# models -- a stand-in for the owner's repo.  Two conventions matter: every
# column carries doc= (the UI tooltip) and comment= (the same text, pushed down
# to PostgreSQL), and the natural key of model_e is a real UniqueConstraint.


class Base(DeclarativeBase):
    """Declarative base for the demo schema."""


# JSONB on PostgreSQL, plain JSON on SQLite so the tests run without a server.
JsonColumn = JSONB().with_variant(JSON(), "sqlite")


class EType(enum.Enum):
    """The five-valued enum that completes model_e's natural key."""

    BASELINE = "BASELINE"
    STRESS = "STRESS"
    SENSITIVITY = "SENSITIVITY"
    HISTORICAL = "HISTORICAL"
    FORECAST = "FORECAST"


class ModelA(Base):
    """A dimension (region / book)."""

    __tablename__ = "model_a"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
        doc="Surrogate key for the A dimension.",
        comment="Surrogate key for the A dimension.",
    )
    name: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
        doc="Business code of the A dimension (region / book).",
        comment="Business code of the A dimension (region / book).",
    )


class ModelB(Base):
    """B dimension (desk / product line)."""

    __tablename__ = "model_b"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
        doc="Surrogate key for the B dimension.",
        comment="Surrogate key for the B dimension.",
    )
    name: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
        doc="Business code of the B dimension (desk / product line).",
        comment="Business code of the B dimension (desk / product line).",
    )


class ModelC(Base):
    """C dimension (instrument class)."""

    __tablename__ = "model_c"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
        doc="Surrogate key for the C dimension.",
        comment="Surrogate key for the C dimension.",
    )
    name: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
        doc="Business code of the C dimension (instrument class).",
        comment="Business code of the C dimension (instrument class).",
    )


class ModelD(Base):
    """Same shape as A/B/C but the natural key column is ``version``."""

    __tablename__ = "model_d"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
        doc="Surrogate key for the D dimension.",
        comment="Surrogate key for the D dimension.",
    )
    version: Mapped[str] = mapped_column(
        String(32),
        unique=True,
        nullable=False,
        doc="Ruleset version the row was produced under (e.g. 2026.1).",
        comment="Ruleset version the row was produced under (e.g. 2026.1).",
    )


class ModelE(Base):
    """The fact table the grid edits."""

    __tablename__ = "model_e"
    __table_args__ = (
        UniqueConstraint(
            "a_id", "b_id", "c_id", "d_id", "type", name="uq_model_e_natural_key"
        ),
        {"comment": "Per-dimension calibration inputs, edited through the grid."},
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
        doc="Surrogate key. Acts as the row index in the grid.",
        comment="Surrogate key. Acts as the row index in the grid.",
    )

    # ---- natural key ----------------------------------------------------
    a_id: Mapped[int] = mapped_column(
        ForeignKey("model_a.id", ondelete="RESTRICT"),
        nullable=False,
        doc="A dimension this row belongs to.",
        comment="A dimension this row belongs to.",
    )
    b_id: Mapped[int] = mapped_column(
        ForeignKey("model_b.id", ondelete="RESTRICT"),
        nullable=False,
        doc="B dimension this row belongs to.",
        comment="B dimension this row belongs to.",
    )
    c_id: Mapped[int] = mapped_column(
        ForeignKey("model_c.id", ondelete="RESTRICT"),
        nullable=False,
        doc="C dimension this row belongs to.",
        comment="C dimension this row belongs to.",
    )
    d_id: Mapped[int] = mapped_column(
        ForeignKey("model_d.id", ondelete="RESTRICT"),
        nullable=False,
        doc="Ruleset version this row was produced under.",
        comment="Ruleset version this row was produced under.",
    )
    type: Mapped[EType] = mapped_column(
        SAEnum(
            EType, name="model_e_type", values_callable=lambda e: [m.value for m in e]
        ),
        nullable=False,
        doc="Run flavour. Completes the natural key together with A/B/C/D.",
        comment="Run flavour. Completes the natural key together with A/B/C/D.",
    )

    # ---- 5 integer data columns -----------------------------------------
    run_count: Mapped[Optional[int]] = mapped_column(
        Integer, doc="Number of runs aggregated into this row.",
        comment="Number of runs aggregated into this row.")
    priority: Mapped[Optional[int]] = mapped_column(
        Integer, doc="Scheduling priority, 1 (highest) to 99 (lowest).",
        comment="Scheduling priority, 1 (highest) to 99 (lowest).")
    horizon_days: Mapped[Optional[int]] = mapped_column(
        Integer, doc="Projection horizon in calendar days.",
        comment="Projection horizon in calendar days.")
    retry_limit: Mapped[Optional[int]] = mapped_column(
        Integer, doc="Maximum automatic retries before the run is failed.",
        comment="Maximum automatic retries before the run is failed.")
    batch_size: Mapped[Optional[int]] = mapped_column(
        Integer, doc="Rows per batch handed to the calculation engine.",
        comment="Rows per batch handed to the calculation engine.")

    # ---- 5 float data columns -------------------------------------------
    weight: Mapped[Optional[float]] = mapped_column(
        Float, doc="Blend weight applied when aggregating upwards (0..1).",
        comment="Blend weight applied when aggregating upwards (0..1).")
    threshold: Mapped[Optional[float]] = mapped_column(
        Float, doc="Breach threshold in base currency units.",
        comment="Breach threshold in base currency units.")
    drift: Mapped[Optional[float]] = mapped_column(
        Float, doc="Observed drift against the previous ruleset version.",
        comment="Observed drift against the previous ruleset version.")
    tolerance: Mapped[Optional[float]] = mapped_column(
        Float, doc="Absolute tolerance accepted during reconciliation.",
        comment="Absolute tolerance accepted during reconciliation.")
    score: Mapped[Optional[float]] = mapped_column(
        Float, doc="Quality score produced by the last validation pass.",
        comment="Quality score produced by the last validation pass.")

    # ---- 5 string data columns ------------------------------------------
    label: Mapped[Optional[str]] = mapped_column(
        String(128), doc="Human readable label shown in downstream reports.",
        comment="Human readable label shown in downstream reports.")
    owner: Mapped[Optional[str]] = mapped_column(
        String(64), doc="Team or person accountable for the row.",
        comment="Team or person accountable for the row.")
    status_note: Mapped[Optional[str]] = mapped_column(
        String(256), doc="Free-text note about the current status.",
        comment="Free-text note about the current status.")
    source_ref: Mapped[Optional[str]] = mapped_column(
        String(128), doc="Upstream reference (ticket, file or feed id).",
        comment="Upstream reference (ticket, file or feed id).")
    approval: Mapped[Optional[str]] = mapped_column(
        String(32), doc="Approval state recorded by the reviewer.",
        comment="Approval state recorded by the reviewer.")

    # ---- the jsonb column ------------------------------------------------
    payload: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JsonColumn, doc="Free-form extra attributes passed through to the engine.",
        comment="Free-form extra attributes passed through to the engine.")

    a: Mapped[ModelA] = relationship(lazy="joined")
    b: Mapped[ModelB] = relationship(lazy="joined")
    c: Mapped[ModelC] = relationship(lazy="joined")
    d: Mapped[ModelD] = relationship(lazy="joined")

    def __repr__(self) -> str:
        return (
            f"<ModelE id={self.id} "
            f"key=({self.a_id},{self.b_id},{self.c_id},{self.d_id},{self.type})>"
        )


class Updates(Base):
    """Staged changes to the fact table, one row per branch.

    A stand-in for the real ``Updates`` table, the same way ModelA-E stand in
    for the real fact schema: swap it for yours with ``--updates-model``. Only
    the shape matters -- somewhere to put the JSON, which version it is
    against, and when it last moved.
    """

    __tablename__ = "updates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[Optional[str]] = mapped_column(
        String(128), doc="What this branch of changes is called."
    )
    d_id: Mapped[int] = mapped_column(
        ForeignKey("model_d.id"),
        nullable=False,
        doc="The version these changes were made against.",
    )
    changes: Mapped[dict] = mapped_column(
        JsonColumn, nullable=False, default=dict,
        doc="The staged changes, as json.",
    )
    modified_at: Mapped[Optional[object]] = mapped_column(
        DateTime(timezone=True),
        default=func.now(),
        onupdate=func.now(),
        doc="Bumped every time the branch is pushed to.",
    )


@dataclass
class UpdatesSpec:
    """Where the branch table keeps each thing, worked out from the mapper.

    Nothing here is hard-coded to the demo ``Updates`` class: point
    ``--updates-model`` at yours and the columns are found by type.
    """

    model: type
    pk: str
    changes: str                    # the json column
    version: Optional[str] = None   # foreign key to the version dimension
    modified_at: Optional[str] = None
    label: Optional[str] = None     # a human name for the branch

    @classmethod
    def of(cls, model, version_table: Optional[str] = None) -> "UpdatesSpec":
        """Read whichever kind of model this is."""
        return cls.from_mapped_class(model, version_table)

    @classmethod
    def from_mapped_class(
        cls, model, version_table: Optional[str] = None
    ) -> "UpdatesSpec":
        """Find each thing by column type, in a mapped class or a Table."""
        table = table_of(model)
        name_of = (
            (lambda col: col.name)
            if isinstance(model, Table)
            else (lambda col, m=inspect(model): _attr_name(m, col))
        )
        found = {"changes": None, "version": None, "modified_at": None, "label": None}
        for col in table.columns:
            attr = name_of(col)
            kind = _kind_for(col)
            if kind is ColumnKind.JSON and found["changes"] is None:
                found["changes"] = attr
            elif kind is ColumnKind.FK and found["version"] is None:
                target = next(iter(col.foreign_keys)).column.table.name
                if version_table is None or target == version_table:
                    found["version"] = attr
            elif kind is ColumnKind.DATETIME and found["modified_at"] is None:
                found["modified_at"] = attr
            elif kind is ColumnKind.STR and found["label"] is None:
                found["label"] = attr
        if found["changes"] is None:
            raise ValueError(
                f"{model_name(model)} has no json column to keep the changes in"
            )
        pk = name_of(list(table.primary_key.columns)[0])
        return cls(model=model, pk=pk, **found)


def table_of(model) -> Table:
    """The Table behind a model, mapped or already a Table."""
    return model if isinstance(model, Table) else inspect(model).local_table


def column_of(model, key: str):
    """One column of a model, mapped or reflected.

    A mapped class answers to ``getattr``; a reflected table does not, and
    everything that builds a query here only ever wanted the column.
    """
    if isinstance(model, Table):
        return model.c[key]
    return getattr(model, key)


def model_name(model) -> str:
    return model.name if isinstance(model, Table) else model.__name__


#: Parent table -> column shown instead of the raw id.  Auto-detected when a
#: table is missing here; listed so the choice is explicit and overridable.
LABEL_COLUMNS = {
    "model_a": "name",
    "model_b": "name",
    "model_c": "name",
    "model_d": "version",
}


# ---------------------------------------------------------------------------
# column layout


class ColumnKind(enum.Enum):
    """Coarse data type, used to pick editors, filters and alignment."""

    PK = "pk"
    FK = "fk"
    ENUM = "enum"
    INT = "int"
    FLOAT = "float"
    STR = "str"
    BOOL = "bool"
    DATE = "date"
    DATETIME = "datetime"
    JSON = "json"

    @property
    def numeric(self) -> bool:
        return self in (ColumnKind.INT, ColumnKind.FLOAT, ColumnKind.PK)

    @property
    def filterable(self) -> bool:
        return self is not ColumnKind.JSON  # jsonb is out of scope for filtering

    @property
    def choice_based(self) -> bool:
        """True when the natural editor/filter is a closed list of values."""
        return self in (ColumnKind.FK, ColumnKind.ENUM, ColumnKind.BOOL)


@dataclass(frozen=True)
class FkSpec:
    """Where a foreign key points and which column of the parent to show.

    The parent is a :class:`~sqlalchemy.Table`, not a mapped class: a table
    reflected from the database has no class, and every query built from this
    wanted columns rather than attributes in the first place.
    """

    target_table: str      # the name, for headers and messages
    table: Any             # the selectable to join against
    pk_column: str         # what the foreign key points at
    label_column: str      # what to show instead of the id

    def pk(self):
        return self.table.c[self.pk_column]

    def label(self):
        return self.table.c[self.label_column]


@dataclass(frozen=True)
class ColumnSpec:
    """One grid column, derived from one mapped SQLAlchemy column."""

    key: str
    header: str
    doc: str
    kind: ColumnKind
    nullable: bool
    in_key: bool = False
    is_pk: bool = False
    fk: Optional[FkSpec] = None
    enum_class: Optional[type] = None
    #: An enum reflected from the database has labels but no Python class.
    enum_labels: tuple = ()
    max_length: Optional[int] = None
    width: int = 110

    @property
    def enum_names(self) -> tuple[str, ...]:
        """Member names, in declaration order.

        The grid shows the name rather than the value: ``Kind.BASELINE`` reads
        as "BASELINE" whatever it happens to be worth, and it is what
        SQLAlchemy's Enum type stores by default.
        """
        if self.enum_class is None:
            return tuple(self.enum_labels)
        return tuple(m.name for m in self.enum_class)

    @property
    def editable_on_existing(self) -> bool:
        """Natural-key columns and the PK are immutable once the row exists."""
        return not (self.is_pk or self.in_key)

    @property
    def editable_on_staged(self) -> bool:
        return not self.is_pk


@dataclass
class TableSpec:
    """The ordered columns of one fact table, plus its natural key."""

    model: type
    table_name: str
    columns: tuple[ColumnSpec, ...]
    pk: ColumnSpec
    unique_constraint: Optional[str] = None
    _by_key: dict[str, ColumnSpec] = field(
        init=False, default_factory=dict, repr=False
    )

    def __post_init__(self) -> None:
        self._by_key = {c.key: c for c in self.columns}

    def __getitem__(self, key: str) -> ColumnSpec:
        return self._by_key[key]

    def get(self, key: str) -> Optional[ColumnSpec]:
        return self._by_key.get(key)

    def index_of(self, key: str) -> int:
        return self.columns.index(self._by_key[key])

    @property
    def key_columns(self) -> tuple[ColumnSpec, ...]:
        return tuple(c for c in self.columns if c.in_key)

    @property
    def data_columns(self) -> tuple[ColumnSpec, ...]:
        return tuple(c for c in self.columns if not c.in_key and not c.is_pk)

    @property
    def version_column(self) -> Optional[ColumnSpec]:
        """The natural-key foreign key pointing at a version dimension.

        A table like this has one version per set of rows being worked on, so
        the grid shows it once beside the data rather than as a column where
        every cell says the same thing.
        """
        for col in self.key_columns:
            if col.fk is not None and col.fk.label_column in ("version", "revision"):
                return col
        return None

    @property
    def fk_columns(self) -> tuple[ColumnSpec, ...]:
        return tuple(c for c in self.columns if c.fk is not None)

    @classmethod
    def from_mapped_class(
        cls, model: type, label_columns: Optional[dict[str, str]] = None
    ) -> "TableSpec":
        """Read the mapper: pk first, then the natural key, then everything else."""
        mapper = inspect(model)
        return cls._build(
            model,
            mapper.local_table,
            lambda col: _attr_name(mapper, col),
            label_columns or {},
        )

    @classmethod
    def of(
        cls, model, label_columns: Optional[dict[str, str]] = None
    ) -> "TableSpec":
        """Read whichever kind of model this is."""
        if isinstance(model, Table):
            return cls.from_table(model, label_columns)
        return cls.from_mapped_class(model, label_columns)

    @classmethod
    def from_table(
        cls, table: Table, label_columns: Optional[dict[str, str]] = None
    ) -> "TableSpec":
        """The same, for a table reflected from the database.

        There is no class and no attribute names, so a column is known by its
        own name. Everything else -- the natural key, the foreign keys and
        their labels, the enums -- is read from the table itself, which is
        where it came from for a mapped class too.
        """
        return cls._build(table, table, lambda col: col.name, label_columns or {})

    @classmethod
    def _build(cls, model, table: Table, name_of, label_columns) -> "TableSpec":
        natural_key = _largest_unique_constraint(table)
        key_names = [c.name for c in natural_key.columns] if natural_key else []

        specs = {
            col.name: _column_spec(
                col,
                name_of(col),
                in_key=col.name in key_names,
                label_columns=label_columns,
            )
            for col in table.columns
        }

        order = [c.name for c in table.primary_key.columns]
        for name in key_names + [c.name for c in table.columns]:
            if name not in order:
                order.append(name)
        if not order:
            raise ValueError(f"{table.name} has no primary key")

        return cls(
            model=model,
            table_name=table.name,
            columns=tuple(specs[n] for n in order),
            pk=specs[order[0]],
            unique_constraint=natural_key.name if natural_key else None,
        )


def _largest_unique_constraint(table: Table) -> Optional[UniqueConstraint]:
    """The multi-column unique constraint we treat as the natural key."""
    candidates = [
        c
        for c in table.constraints
        if isinstance(c, UniqueConstraint) and len(c.columns) > 1
    ]
    return max(candidates, key=lambda c: len(c.columns)) if candidates else None


def _attr_name(mapper, col: Column) -> str:
    """Attribute name on the mapped class for ``col``."""
    try:
        return mapper.get_property_by_column(col).key
    except Exception:  # a column the class does not map
        return col.name


def _python_type(col: Column) -> type:
    try:
        return col.type.python_type
    except (NotImplementedError, AttributeError):
        return str


def _kind_for(col: Column) -> ColumnKind:
    if col.primary_key:
        return ColumnKind.PK
    if col.foreign_keys:
        return ColumnKind.FK
    if isinstance(col.type, SAEnum) and col.type.enum_class is not None:
        return ColumnKind.ENUM
    if "JSON" in type(col.type).__name__.upper():
        return ColumnKind.JSON

    py = _python_type(col)
    if py is bool:
        return ColumnKind.BOOL
    if py is int:
        return ColumnKind.INT
    if py is float:
        return ColumnKind.FLOAT
    if py is dt.datetime:
        return ColumnKind.DATETIME
    if py is dt.date:
        return ColumnKind.DATE
    return ColumnKind.STR


def _class_for_table(table: Table, registry) -> type:
    """Find the declarative class mapped to ``table`` within one registry."""
    for mapper in registry.mappers:
        if mapper.local_table is table:
            return mapper.class_
    raise LookupError(f"no mapped class for table {table.name!r}")


def _detect_label_column(target_table: Table) -> str:
    """Pick the column of a dimension table that humans recognise."""
    single_uc = {
        list(c.columns)[0].name
        for c in target_table.constraints
        if isinstance(c, UniqueConstraint) and len(c.columns) == 1
    }
    for col in target_table.columns:
        if not col.primary_key and (col.unique or col.name in single_uc):
            return col.name
    for col in target_table.columns:  # no unique column: first text column
        if not col.primary_key and _python_type(col) is str:
            return col.name
    return list(target_table.primary_key.columns)[0].name


def _fk_spec(col: Column, label_columns: dict[str, str]) -> FkSpec:
    fk = next(iter(col.foreign_keys))
    target_table = fk.column.table
    return FkSpec(
        target_table=target_table.name,
        table=target_table,
        pk_column=fk.column.name,
        label_column=label_columns.get(target_table.name)
        or _detect_label_column(target_table),
    )


def _header_for(name: str) -> str:
    return name.replace("_", " ").title()


def _header_for_fk(fk: FkSpec) -> str:
    """``model_a`` + label ``name`` -> ``A``; ``model_d`` + ``version`` -> ``D version``."""
    stem = fk.target_table
    for prefix in ("model_", "dim_", "tbl_", "ref_"):
        if stem.startswith(prefix):
            stem = stem[len(prefix) :]
            break
    label = stem.upper() if len(stem) <= 2 else stem.replace("_", " ").title()
    if fk.label_column not in ("name", "code", "label", "title"):
        label = f"{label} {fk.label_column}"
    return label


def _column_spec(
    col: Column,
    attr: str,
    in_key: bool,
    label_columns: dict[str, str],
) -> ColumnSpec:
    kind = _kind_for(col)
    fk = _fk_spec(col, label_columns) if kind is ColumnKind.FK else None

    if kind is ColumnKind.PK:
        header, width = _header_for(col.name).upper(), 64
    elif fk is not None:
        header, width = _header_for_fk(fk), 110
    elif kind is ColumnKind.JSON:
        header, width = _header_for(attr), 220
    elif kind is ColumnKind.STR:
        header, width = _header_for(attr), 150
    else:
        header, width = _header_for(attr), 95

    return ColumnSpec(
        key=attr,
        header=header,
        doc=(col.doc or col.comment or "").strip(),
        kind=kind,
        nullable=bool(col.nullable),
        in_key=in_key,
        is_pk=bool(col.primary_key),
        fk=fk,
        enum_class=getattr(col.type, "enum_class", None),
        enum_labels=tuple(getattr(col.type, "enums", ()) or ()),
        max_length=getattr(col.type, "length", None),
        width=width,
    )


# ---------------------------------------------------------------------------
# values -- rows hold database values (fks are ids, enums their string value);
# the grid shows labels.  Text that cannot be resolved is kept as Unresolved so
# the user sees what they typed and gets an error instead of a silent drop.

NULL_TEXTS = {"", "-", "--", "null", "none", "nan", "n/a", "#n/a"}
TRUE_TEXTS = {"1", "true", "t", "yes", "y", "on"}
FALSE_TEXTS = {"0", "false", "f", "no", "n", "off"}


@dataclass(frozen=True)
class Unresolved:
    """A label the user supplied that does not exist in the parent table."""

    text: str

    def __str__(self) -> str:
        return self.text


def is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, Unresolved):
        return not value.text.strip()
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, float) and math.isnan(value):
        return True
    return False


def display(col: ColumnSpec, value: Any, lookups: Optional["Lookups"] = None) -> str:
    """Text shown in a cell."""
    if isinstance(value, Unresolved):
        return value.text
    if value is None:
        return ""
    if col.kind is ColumnKind.FK:
        if lookups is None:
            return str(value)
        return lookups.label(col, value) or f"<{value}>"
    if col.kind is ColumnKind.ENUM:
        return value.name if isinstance(value, enum.Enum) else str(value)
    if col.kind is ColumnKind.FLOAT:
        return f"{float(value):.12g}"
    if col.kind is ColumnKind.JSON:
        return json.dumps(value, separators=(", ", ": "), sort_keys=True, default=str)
    if col.kind is ColumnKind.BOOL:
        return "TRUE" if value else "FALSE"
    return str(value)


def display_json_pretty(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def parse(col: ColumnSpec, text: str, lookups: Optional["Lookups"] = None) -> Any:
    """Turn user text into a stored value.

    Raises ``ValueError`` when the text cannot be a value of this column at all
    (bad number, bad json).  Unknown drop-down values are not an error here --
    they come back as :class:`Unresolved` for the validator to flag on the
    exact cell.
    """
    raw = text if isinstance(text, str) else str(text)
    stripped = raw.strip()

    if col.kind is ColumnKind.JSON:
        if stripped.lower() in NULL_TEXTS:
            return None
        try:
            return json.loads(stripped)
        except ValueError as exc:
            raise ValueError(f"not valid JSON: {exc.args[0]}") from exc

    if stripped.lower() in NULL_TEXTS:
        return None

    if col.kind is ColumnKind.FK:
        if lookups is None:
            raise ValueError("no lookups available")
        found = lookups.id_for(col, stripped)
        return found if found is not None else Unresolved(stripped)

    if col.kind is ColumnKind.ENUM:
        wanted = stripped.casefold()
        for name in col.enum_names:
            if name.casefold() == wanted:
                return name
        # Tolerate a pasted value as well, but store the name.
        for member in col.enum_class or ():
            if str(member.value).casefold() == wanted:
                return member.name
        return Unresolved(stripped)

    if col.kind in (ColumnKind.INT, ColumnKind.PK):
        cleaned = stripped.replace(",", "").replace("_", "")
        try:
            return int(cleaned)
        except ValueError:
            pass
        try:  # accept "3.0" style input from spreadsheets
            as_float = float(cleaned)
        except ValueError:
            raise ValueError(f"{stripped!r} is not a whole number") from None
        if as_float.is_integer():
            return int(as_float)
        raise ValueError(f"{stripped!r} is not a whole number")

    if col.kind is ColumnKind.FLOAT:
        cleaned = stripped.replace(",", "").replace("_", "").rstrip("%")
        try:
            value = float(cleaned)
        except ValueError:
            raise ValueError(f"{stripped!r} is not a number") from None
        return value / 100.0 if stripped.endswith("%") else value

    if col.kind is ColumnKind.BOOL:
        low = stripped.lower()
        if low in TRUE_TEXTS:
            return True
        if low in FALSE_TEXTS:
            return False
        raise ValueError(f"{stripped!r} is not a true/false value")

    if col.max_length and len(raw) > col.max_length:
        raise ValueError(f"longer than {col.max_length} characters")
    return raw


def sort_key(col: ColumnSpec, value: Any, lookups: Optional["Lookups"] = None) -> Any:
    """Comparable key; blanks are handled separately by the caller."""
    if col.kind is ColumnKind.FK:
        return (display(col, value, lookups) or "").casefold()
    if col.kind is ColumnKind.ENUM:
        names = col.enum_names
        text = display(col, value, lookups)
        return names.index(text) if text in names else len(names)
    if col.kind.numeric:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float("inf")
    if col.kind is ColumnKind.BOOL:
        return 1 if value else 0
    if col.kind is ColumnKind.JSON:
        return display(col, value, lookups)
    return display(col, value, lookups).casefold()


def to_db(col: ColumnSpec, value: Any) -> Any:
    """Final conversion applied just before writing to SQLAlchemy."""
    if isinstance(value, Unresolved):
        raise ValueError(f"{col.header}: unknown value {value.text!r}")
    if value is None:
        return None
    if col.kind is ColumnKind.ENUM and col.enum_class is not None:
        if isinstance(value, col.enum_class):
            return value
        try:
            return col.enum_class[value]          # by name, as the grid holds it
        except KeyError:
            return col.enum_class(value)          # tolerate a raw value
    return value


# ---------------------------------------------------------------------------
# lookups


def natural_sort_key(text: str) -> list:
    """Digits compare as numbers, so ``2026.2`` sorts before ``2026.10``."""
    parts: list[object] = []
    digits = ""
    for ch in text:
        if ch.isdigit():
            digits += ch
            continue
        if digits:
            parts.append((0, int(digits)))
            digits = ""
        parts.append((1, ch.casefold()))
    if digits:
        parts.append((0, int(digits)))
    return parts


class Lookups:
    """id <-> label caches for the dimension tables the foreign keys point at."""

    def __init__(self) -> None:
        self._labels: dict[str, dict[int, str]] = {}
        self._ids: dict[str, dict[str, int]] = {}
        self._choices: dict[str, list[str]] = {}
        self._pending: dict[str, dict[int, str]] = {}
        #: Placeholder ids are handed out from one counter for the whole
        #: session, not per dimension: `pending()` is keyed by id alone, so
        #: two dimensions minting -1 would have one overwrite the other and
        #: the payload would lose a value the API needs to create.
        self._placeholder = 0

    @staticmethod
    def _key(col: ColumnSpec) -> str:
        return f"{col.fk.target_table}.{col.fk.label_column}"

    def register(self, col: ColumnSpec, pairs: Iterable[tuple[int, str]]) -> None:
        """Cache ``col``'s dimension from (pk, label) pairs."""
        key = self._key(col)
        labels = {int(pk): "" if label is None else str(label) for pk, label in pairs}
        self._labels[key] = labels
        self._ids[key] = {text.casefold(): pk for pk, text in labels.items()}
        self._choices[key] = sorted(labels.values(), key=natural_sort_key)
        self._pending.pop(key, None)  # the database is the truth again

    # -- values invented in this session --------------------------------
    def add_value(self, col: ColumnSpec, label: str) -> int:
        """Add a dimension value that is not in the database.

        It gets a negative placeholder id so staged rows can reference it like
        any other, and it shows up in every drop-down for that column. Nothing
        here writes it anywhere: :meth:`pending` hands it to the payload so the
        receiving service can create it.
        """
        key = self._key(col)
        text = label.strip()
        if not text:
            raise ValueError("a value is required")
        known = self._ids[key].get(text.casefold())
        if known is not None:
            return known
        new_id = self._mint()
        self._labels[key][new_id] = text
        self._ids[key][text.casefold()] = new_id
        self._choices[key] = sorted(
            self._labels[key].values(), key=natural_sort_key
        )
        self._pending.setdefault(key, {})[new_id] = text
        return new_id

    def _mint(self) -> int:
        """The next placeholder id: unique across every dimension.

        Below zero, and below anything already in use, so it cannot collide
        with a real id either.
        """
        floor = min(
            [0, self._placeholder]
            + [min(labels, default=0) for labels in self._labels.values()]
        )
        self._placeholder = floor - 1
        return self._placeholder

    @staticmethod
    def is_pending(pk: Optional[int]) -> bool:
        """True for a value added in this session (placeholder ids are negative)."""
        return pk is not None and isinstance(pk, int) and pk < 0

    def pending(self) -> dict[int, dict[str, str]]:
        """Placeholder id -> {table, column, label} for everything added here."""
        out: dict[int, dict[str, str]] = {}
        for key, items in self._pending.items():
            table, _, attr = key.partition(".")
            for pk, label in items.items():
                out[pk] = {"table": table, "column": attr, "label": label}
        return out

    def dimension(self, col: ColumnSpec) -> dict[int, str]:
        """The id -> label map behind ``col``."""
        return self._labels[self._key(col)]

    def label(self, col: ColumnSpec, pk: Optional[int]) -> Optional[str]:
        return None if pk is None else self.dimension(col).get(pk)

    def id_for(self, col: ColumnSpec, text: str) -> Optional[int]:
        return self._ids[self._key(col)].get(text.casefold())

    def choices(self, col: ColumnSpec) -> list[str]:
        """Drop-down values, naturally sorted."""
        return list(self._choices[self._key(col)])

    def __contains__(self, col: ColumnSpec) -> bool:
        return col.fk is not None and self._key(col) in self._labels


def describe(spec: "TableSpec") -> str:
    """What the grid worked out about a table -- print this when wiring up
    your own models, to check it read them the way you expect."""
    lines = [
        f"{model_name(spec.model)} -> table {spec.table_name!r}",
        f"  primary key      {spec.pk.key}   (shown as the row index, not a column)",
        f"  natural key      {spec.unique_constraint or '(none found!)'}",
        "",
        f"  {'column':<16}{'kind':<10}{'header':<16}{'flags':<10}source",
    ]
    for col in spec.columns:
        flags = "".join(
            ("P" if col.is_pk else "-", "K" if col.in_key else "-",
             "N" if col.nullable else "-")
        )
        source = ""
        if col.fk is not None:
            source = f"{col.fk.target_table}.{col.fk.label_column}"
        elif col.enum_class is not None:
            source = ", ".join(col.enum_names)
        lines.append(
            f"  {col.key:<16}{col.kind.value:<10}{col.header:<16}{flags:<10}{source}"
        )
    lines += [
        "",
        "  P = primary key, K = part of the natural key (greyed on stored rows,",
        "  a drop-down on staged rows), N = nullable.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# engine and demo data

DEFAULT_DSN = "postgresql+psycopg2://postgres:postgres@localhost:5432/benchmark"


def dsn() -> str:
    return os.environ.get("STAGEDGRID_DSN", DEFAULT_DSN)


def make_engine(url: Optional[str] = None, echo: bool = False) -> Engine:
    return create_engine(url or dsn(), echo=echo, future=True, pool_pre_ping=True)


A_VALUES = ["EU-WEST", "EU-EAST", "US-EAST", "US-WEST", "APAC", "LATAM", "MEA", "GLOBAL"]
B_VALUES = ["RATES", "CREDIT", "EQUITY", "FX", "COMMODITY", "REPO", "XVA"]
C_VALUES = ["BOND", "SWAP", "OPTION", "FUTURE", "LOAN", "CASH"]
D_VALUES = ["2024.4", "2025.1", "2025.2", "2026.1", "2026.2"]

OWNERS = ["risk-eng", "quant-core", "market-data", "finance-ops", "model-val"]
APPROVALS = ["PENDING", "APPROVED", "REJECTED", "WAIVED"]
NOTES = [
    "waiting on upstream refresh",
    "signed off by model validation",
    "recalibrated after the 2026.1 cut",
    "manual override, see ticket",
    "",
]


def reset(engine: Engine) -> None:
    """Drop and recreate every table."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def _grown(base: list[str], wanted: int) -> list[str]:
    """``base``, extended with numbered variants until it is long enough."""
    values = list(base)
    while len(values) < wanted:
        values.append(f"{base[len(values) % len(base)]}-{len(values):03d}")
    return values


def _dimension_sizes(rows: int, versions: int) -> tuple[int, int, int]:
    """How many A/B/C values it takes to hold ``rows`` rows.

    The natural key is A x B x C x D x type, so the dimensions have to be big
    enough to make that many distinct combinations.
    """
    import math

    # Twice the minimum, so the table is about half full and there is room to
    # actually add rows: sized to just fit, almost every key is taken and
    # every new row would be a duplicate.
    need = 2 * math.ceil(rows / max(versions * len(EType), 1))
    a = max(len(A_VALUES), math.ceil(need ** (1 / 3)))
    b = max(len(B_VALUES), math.ceil((need / a) ** 0.5))
    c = max(len(C_VALUES), math.ceil(need / (a * b)))
    return a, b, c


def seed(
    engine: Engine,
    rows: int = 400,
    seed_value: int = 7,
    one_version: bool = False,
) -> int:
    """Fill the dimensions and add up to ``rows`` new model_e rows.

    ``one_version`` puts every row in the newest version, which is how a real
    table tends to look: the grid only ever loads the newest one.
    """
    rng = random.Random(seed_value)
    Base.metadata.create_all(engine)

    size_a, size_b, size_c = _dimension_sizes(rows, 1 if one_version else len(D_VALUES))
    with Session(engine) as s, s.begin():
        if s.execute(select(ModelA)).first() is None:
            s.add_all([ModelA(name=v) for v in _grown(A_VALUES, size_a)])
            s.add_all([ModelB(name=v) for v in _grown(B_VALUES, size_b)])
            s.add_all([ModelC(name=v) for v in _grown(C_VALUES, size_c)])
            s.add_all([ModelD(version=v) for v in D_VALUES])

    with Session(engine) as s, s.begin():
        a_ids = [r[0] for r in s.execute(select(ModelA.id)).all()]
        b_ids = [r[0] for r in s.execute(select(ModelB.id)).all()]
        c_ids = [r[0] for r in s.execute(select(ModelC.id)).all()]
        d_ids = [r[0] for r in s.execute(select(ModelD.id)).all()]

        existing = {
            tuple(r)
            for r in s.execute(
                select(ModelE.a_id, ModelE.b_id, ModelE.c_id, ModelE.d_id, ModelE.type)
            ).all()
        }
        if one_version:
            d_ids = [max(
                s.execute(select(ModelD.id, ModelD.version)).all(),
                key=lambda pair: natural_sort_key(str(pair[1])),
            )[0]]
        combos = [
            combo
            for combo in product(a_ids, b_ids, c_ids, d_ids, list(EType))
            if combo not in existing
        ]
        rng.shuffle(combos)
        chosen = combos[:rows]

        for a_id, b_id, c_id, d_id, etype in chosen:
            s.add(
                ModelE(
                    a_id=a_id,
                    b_id=b_id,
                    c_id=c_id,
                    d_id=d_id,
                    type=etype,
                    run_count=rng.randint(1, 500),
                    priority=rng.choice([1, 5, 10, 20, 50, 99]),
                    horizon_days=rng.choice([1, 7, 30, 90, 180, 365]),
                    retry_limit=rng.randint(0, 5),
                    batch_size=rng.choice([256, 512, 1024, 4096, 8192]),
                    weight=round(rng.random(), 4),
                    threshold=round(rng.uniform(1_000, 5_000_000), 2),
                    drift=round(rng.gauss(0, 0.05), 6),
                    tolerance=round(rng.choice([1e-6, 1e-4, 1e-3, 0.01]), 6),
                    score=round(rng.uniform(0, 100), 3),
                    label=f"{etype.value.title()} {rng.randint(100, 999)}",
                    owner=rng.choice(OWNERS),
                    status_note=rng.choice(NOTES) or None,
                    source_ref=f"JIRA-{rng.randint(1000, 9999)}",
                    approval=rng.choice(APPROVALS),
                    payload={
                        "engine": rng.choice(["v2", "v3", "v3-beta"]),
                        "retries": rng.randint(0, 3),
                        "flags": rng.sample(["fast", "strict", "audit", "trace"], k=2),
                    },
                )
            )
    return len(chosen)


if __name__ == "__main__":
    # python schema.py --reset --rows 400
    import argparse

    parser = argparse.ArgumentParser(description="create the demo schema and fill it")
    parser.add_argument("--rows", type=int, default=400)
    parser.add_argument("--reset", action="store_true", help="drop and recreate")
    parser.add_argument("--dsn", default=None)
    parser.add_argument(
        "--one-version",
        action="store_true",
        help="put every row in the newest version, as a real table tends to be",
    )
    args = parser.parse_args()

    target = make_engine(args.dsn)
    print(f"target: {args.dsn or dsn()}")
    if args.reset:
        reset(target)
        print("schema dropped and recreated")
    added = seed(target, rows=args.rows, one_version=args.one_version)
    print(f"inserted {added} {ModelE.__tablename__} rows")
