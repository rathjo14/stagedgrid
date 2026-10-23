"""Doing the ordering, filtering and searching in the database instead.

The grid normally holds every row and sorts in Python, which is fast and rich
but means holding every row. This module is the other half of that trade: it
turns the same :class:`~data.FilterSet`, sort order and search text into one
SQL statement, so only the rows on screen ever come back.

The translation has to agree with the in-memory version or the two modes would
disagree about what a filter means, so the rules are the same ones
``schema.display`` and ``data.ColumnFilter`` use:

* a foreign key sorts and filters by its **label**, not its id, which means
  joining the dimension;
* an enum sorts in **declaration order**, which means a CASE;
* blanks sort last whichever way the column is sorted;
* the value checklist holds display text, so it is parsed back to real values
  before it reaches the database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import String, and_, case, cast, func, or_, select
from sqlalchemy.orm import Session, aliased

from data import ColumnFilter, FilterSet, Join, Op, Row, _enum_name
from schema import ColumnKind, ColumnSpec, Lookups, TableSpec, parse


class Untranslatable(Exception):
    """This view cannot be expressed in SQL; fall back to doing it in Python."""


@dataclass
class ViewQuery:
    """What the grid wants to see, independent of how it is fetched."""

    sort: tuple[tuple[str, bool], ...] = ()      # (column key, descending)
    filters: Optional[FilterSet] = None
    search: str = ""
    version_id: Optional[int] = None
    limit: Optional[int] = None
    offset: int = 0

    def narrowed(self) -> bool:
        return bool(self.search or (self.filters and self.filters.active_keys()))


@dataclass
class _Joins:
    """Dimension tables pulled in so a foreign key can sort by its label."""

    spec: TableSpec
    model: type
    aliases: dict[str, Any] = field(default_factory=dict)

    def label_of(self, col: ColumnSpec):
        alias = self.aliases.get(col.key)
        if alias is None:
            alias = aliased(col.fk.target_class)
            self.aliases[col.key] = alias
        return getattr(alias, col.fk.label_attr)

    def apply(self, stmt):
        for key, alias in self.aliases.items():
            col = self.spec[key]
            stmt = stmt.join(
                alias,
                getattr(self.model, key)
                == getattr(alias, col.fk.target_pk_attr),
                isouter=col.nullable,
            )
        return stmt


class SqlView:
    """Builds and runs the statement for a :class:`ViewQuery`."""

    def __init__(self, model: type, spec: TableSpec, lookups: Lookups) -> None:
        self.model = model
        self.spec = spec
        self.lookups = lookups

    # -- expressions ------------------------------------------------------
    def _column(self, col: ColumnSpec, joins: _Joins):
        """What the user sees, as a SQL expression."""
        if col.fk is not None:
            return joins.label_of(col)
        return getattr(self.model, col.key)

    def _is_blank(self, col: ColumnSpec, joins: _Joins):
        """What the grid counts as empty: NULL, and "" for text."""
        expr = self._column(col, joins)
        if col.kind in (ColumnKind.STR, ColumnKind.FK, ColumnKind.JSON):
            return or_(expr.is_(None), cast(expr, String) == "")
        return expr.is_(None)

    def _order_by(self, col: ColumnSpec, descending: bool, joins: _Joins) -> list:
        """Order terms for one sort level.

        Two details have to match the in-memory sort exactly or the same view
        comes out in a different order depending on where it was computed:
        blanks go last whichever way the column is sorted, and ties keep the
        order the rows arrived in -- which for us means the primary key, since
        Python's sort is stable and the rows were loaded by id.
        """
        if col.kind is ColumnKind.ENUM and col.enum_class is not None:
            raw = getattr(self.model, col.key)
            expr = case(
                *[
                    (raw == col.enum_class[name], position)
                    for position, name in enumerate(col.enum_names)
                ],
                else_=len(col.enum_names),
            )
        else:
            expr = self._column(col, joins)
        blanks_last = case((self._is_blank(col, joins), 1), else_=0).asc()
        return [blanks_last, expr.desc() if descending else expr.asc()]

    # -- filters ----------------------------------------------------------
    def _values_for(self, col: ColumnSpec, allowed: frozenset[str]) -> tuple[list, bool]:
        """Checklist display text -> values the database would recognise."""
        values, wants_blank = [], False
        for text in allowed:
            if text == "":
                wants_blank = True
                continue
            if col.fk is not None:
                values.append(text)       # compared against the joined label
            elif col.kind is ColumnKind.ENUM and col.enum_class is not None:
                values.append(col.enum_class[text])
            else:
                try:
                    values.append(parse(col, text, self.lookups))
                except ValueError as exc:
                    raise Untranslatable(f"{col.header}: {exc}") from None
        return values, wants_blank

    def _condition(self, col: ColumnSpec, cond, joins: _Joins):
        expr = self._column(col, joins)
        op = cond.op
        if op is Op.BLANK:
            return expr.is_(None)
        if op is Op.NOT_BLANK:
            return expr.is_not(None)

        if col.kind.numeric:
            try:
                first = float(cond.operand.replace(",", ""))
                second = (
                    float(cond.operand2.replace(",", "")) if cond.operand2 else None
                )
            except ValueError as exc:
                raise Untranslatable(str(exc)) from None
        else:
            first, second = cond.operand, cond.operand2

        if op is Op.EQ:
            return expr == first
        if op is Op.NE:
            return or_(expr != first, expr.is_(None))
        if op is Op.GT:
            return expr > first
        if op is Op.GE:
            return expr >= first
        if op is Op.LT:
            return expr < first
        if op is Op.LE:
            return expr <= first
        if op in (Op.BETWEEN, Op.NOT_BETWEEN):
            if second is None:
                raise Untranslatable(f"{col.header}: {op.value} needs two values")
            low, high = sorted((first, second))
            inside = expr.between(low, high)
            return inside if op is Op.BETWEEN else or_(~inside, expr.is_(None))
        if op is Op.CONTAINS:
            return cast(expr, String).ilike(f"%{first}%")
        if op is Op.NOT_CONTAINS:
            return or_(~cast(expr, String).ilike(f"%{first}%"), expr.is_(None))
        if op is Op.STARTS:
            return cast(expr, String).ilike(f"{first}%")
        if op is Op.ENDS:
            return cast(expr, String).ilike(f"%{first}")
        raise Untranslatable(f"{col.header}: {op.value} is not available in SQL")

    def _filter(self, flt: ColumnFilter, joins: _Joins):
        col = self.spec[flt.key]
        clauses = []
        if flt.allowed is not None:
            values, wants_blank = self._values_for(col, flt.allowed)
            expr = self._column(col, joins)
            choices = []
            if values:
                choices.append(expr.in_(values))
            if wants_blank:
                choices.append(expr.is_(None))
            clauses.append(or_(*choices) if choices else expr.is_(None) & ~expr.is_(None))
        if flt.conditions:
            built = [self._condition(col, c, joins) for c in flt.conditions]
            clauses.append(and_(*built) if flt.join is Join.AND else or_(*built))
        return and_(*clauses)

    def _search(self, needle: str, joins: _Joins):
        """The quick-search box: any searchable column containing the text."""
        parts = []
        for col in self.spec.columns:
            if col.is_pk or not col.kind.filterable:
                continue
            parts.append(cast(self._column(col, joins), String).ilike(f"%{needle}%"))
        return or_(*parts) if parts else None

    # -- statements -------------------------------------------------------
    def _where(self, query: ViewQuery, joins: _Joins) -> list:
        clauses = []
        version = next(
            (c for c in self.spec.key_columns
             if c.fk is not None and c.fk.label_attr in ("version", "revision")),
            None,
        )
        if version is not None and query.version_id is not None:
            clauses.append(getattr(self.model, version.key) == query.version_id)
        if query.filters:
            for key in query.filters.active_keys():
                flt = query.filters.get(key)
                if flt is not None:
                    clauses.append(self._filter(flt, joins))
        if query.search:
            found = self._search(query.search, joins)
            if found is not None:
                clauses.append(found)
        return clauses

    def statement(self, query: ViewQuery):
        joins = _Joins(self.spec, self.model)
        columns = [getattr(self.model, c.key) for c in self.spec.columns]
        where = self._where(query, joins)          # may add joins
        order = []
        for key, descending in query.sort:
            order.extend(self._order_by(self.spec[key], descending, joins))
        # The stable-sort tie-break, and the order the grid loads in anyway.
        order.append(getattr(self.model, self.spec.pk.key).asc())

        stmt = select(*columns)
        stmt = joins.apply(stmt)
        if where:
            stmt = stmt.where(and_(*where))
        stmt = stmt.order_by(*order)
        if query.limit:
            stmt = stmt.limit(query.limit)
        if query.offset:
            stmt = stmt.offset(query.offset)
        return stmt

    def count_statement(self, query: ViewQuery):
        joins = _Joins(self.spec, self.model)
        where = self._where(query, joins)
        stmt = select(func.count()).select_from(self.model)
        stmt = joins.apply(stmt)
        return stmt.where(and_(*where)) if where else stmt

    # -- running ----------------------------------------------------------
    def rows(self, engine, query: ViewQuery) -> list[Row]:
        keys = [c.key for c in self.spec.columns]
        enums = {c.key for c in self.spec.columns if c.kind is ColumnKind.ENUM}
        out = []
        with Session(engine) as session:
            for record in session.execute(self.statement(query)):
                values = dict(zip(keys, record))
                for key in enums:
                    if values[key] is not None:
                        values[key] = _enum_name(values[key])
                out.append(Row.from_db(values))
        return out

    def count(self, engine, query: ViewQuery) -> int:
        with Session(engine) as session:
            return int(session.execute(self.count_statement(query)).scalar_one())
