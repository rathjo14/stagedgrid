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

from dataclasses import dataclass, field, replace
from typing import Any, Optional, Sequence

from sqlalchemy import String, and_, case, cast, func, not_, or_, select
from sqlalchemy.orm import Session, aliased

from data import (
    ColumnFilter,
    FilterSet,
    Join,
    Op,
    Row,
    RowMatcher,
    RowState,
    _enum_name,
)
from schema import (
    ColumnKind,
    ColumnSpec,
    Lookups,
    TableSpec,
    display,
    is_blank,
    parse,
)


class Untranslatable(Exception):
    """This view cannot be expressed in SQL.

    The database defines the view, so a filter it cannot express cannot be
    part of one. ``key`` names the column at fault, which is what lets the
    window drop that filter and say why rather than show an answer the filter
    was never applied to.
    """

    def __init__(self, message: str, key: Optional[str] = None) -> None:
        super().__init__(message)
        self.key = key


@dataclass
class ViewQuery:
    """What the grid wants to see, independent of how it is fetched."""

    sort: tuple[tuple[str, bool], ...] = ()      # (column key, descending)
    filters: Optional[FilterSet] = None
    search: str = ""
    version_id: Optional[int] = None
    limit: Optional[int] = None
    offset: int = 0
    exclude_pks: tuple = ()
    """Rows the caller holds a newer version of, so the query must not
    return them: their stored values would sort and filter them wrongly."""


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

    def _sortable(self, col: ColumnSpec, joins: _Joins):
        """The expression the ordering is computed on, not the stored value.

        `schema.sort_key` is the in-memory version of this and the two have to
        agree: an enum orders by its position in the declaration, text orders
        case-insensitively (Python casefolds, so SQL has to lower -- without
        this the two modes disagree the moment a label is not all one case),
        and a foreign key orders by its joined label.
        """
        if col.kind is ColumnKind.ENUM and col.enum_class is not None:
            raw = getattr(self.model, col.key)
            return case(
                *[
                    (raw == col.enum_class[name], position)
                    for position, name in enumerate(col.enum_names)
                ],
                else_=len(col.enum_names),
            )
        expr = self._column(col, joins)
        if col.kind in (ColumnKind.STR, ColumnKind.FK, ColumnKind.JSON):
            return func.lower(cast(expr, String))
        return expr

    def _order_by(self, col: ColumnSpec, descending: bool, joins: _Joins) -> list:
        """Order terms for one sort level.

        Two details have to match the in-memory sort exactly or the same view
        comes out in a different order depending on where it was computed:
        blanks go last whichever way the column is sorted, and ties keep the
        order the rows arrived in -- which for us means the primary key, since
        Python's sort is stable and the rows were loaded by id.
        """
        expr = self._sortable(col, joins)
        blanks_last = case((self._is_blank(col, joins), 1), else_=0).asc()
        return [blanks_last, expr.desc() if descending else expr.asc()]

    # -- where one held row falls in the order -----------------------------
    def _probe(self, col: ColumnSpec, value) -> tuple:
        """One value of a row we hold, in the same terms as `_sortable`.

        Returns (is_blank, comparable). Blankness is settled here in Python so
        it never has to be compared in SQL, which is what keeps NULL out of the
        predicate below. A numeric cell holding text that will not parse is
        treated as blank -- it sorts last either way, and the row carries a
        validation error already.
        """
        if is_blank(value):
            return True, None
        if col.kind is ColumnKind.ENUM and col.enum_class is not None:
            name = _enum_name(value)
            names = col.enum_names
            return False, names.index(name) if name in names else len(names)
        if col.kind in (ColumnKind.STR, ColumnKind.FK, ColumnKind.JSON):
            return False, display(col, value, self.lookups).lower()
        if col.kind.numeric:
            try:
                return False, float(value)
            except (TypeError, ValueError):
                return True, None
        if col.kind is ColumnKind.BOOL:
            return False, bool(value)
        return False, value

    def _before(self, query: ViewQuery, values: dict, joins: _Joins):
        """Rows this query would place strictly before the given row.

        The order is lexicographic over (blanks-last, value) pairs and then the
        primary key, so "before" expands to one disjunct per level: tied on
        every earlier level, and ahead on this one.

        Because the probe row's blankness is known in Python, each level
        collapses to something with no NULL in it. If the probe is blank then
        every non-blank row is ahead of it and every blank row ties -- which is
        also why two blank rows reach the primary-key tie-break instead of
        comparing NULL to NULL and vanishing.
        """
        pk_key = self.spec.pk.key
        pk_value = values.get(pk_key)
        if pk_value is None:
            raise Untranslatable("a row with no primary key has no place among stored rows")

        disjuncts: list = []
        prefix: list = []
        for key, descending in query.sort:
            col = self.spec[key]
            blank_row = self._is_blank(col, joins)
            probe_blank, probe = self._probe(col, values.get(key))
            if probe_blank:
                ahead, tied = not_(blank_row), blank_row
            else:
                expr = self._sortable(col, joins)
                compare = expr > probe if descending else expr < probe
                ahead = and_(not_(blank_row), compare)
                tied = and_(not_(blank_row), expr == probe)
            disjuncts.append(and_(*prefix, ahead) if prefix else ahead)
            prefix.append(tied)

        ahead = getattr(self.model, pk_key) < pk_value
        disjuncts.append(and_(*prefix, ahead) if prefix else ahead)
        return or_(*disjuncts)

    def rank(self, engine, query: ViewQuery, values: dict) -> int:
        """How many rows this query would place before the given row.

        This is the whole trick behind merging held rows into a windowed view:
        a row we have edited is excluded from the query (its stored values
        would sort it wrongly), and this says where to put it back.
        """
        joins = _Joins(self.spec, self.model)
        where = self._where(query, joins)
        where.append(self._before(query, values, joins))
        stmt = select(func.count()).select_from(self.model)
        stmt = joins.apply(stmt)
        with Session(engine) as session:
            return int(session.execute(stmt.where(and_(*where))).scalar_one())

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
                    raise Untranslatable(f"{col.header}: {exc}", col.key) from None
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
                raise Untranslatable(str(exc), col.key) from None
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
                raise Untranslatable(
                    f"{col.header}: {op.value} needs two values", col.key
                )
            low, high = sorted((first, second))
            inside = expr.between(low, high)
            return inside if op is Op.BETWEEN else or_(~inside, expr.is_(None))
        if op in (Op.CONTAINS, Op.NOT_CONTAINS, Op.STARTS, Op.ENDS):
            safe = self.like_escape(str(first))
            pattern = {
                Op.CONTAINS: f"%{safe}%",
                Op.NOT_CONTAINS: f"%{safe}%",
                Op.STARTS: f"{safe}%",
                Op.ENDS: f"%{safe}",
            }[op]
            like = cast(expr, String).ilike(pattern, escape="\\")
            if op is Op.NOT_CONTAINS:
                return or_(~like, expr.is_(None))
            return like
        raise Untranslatable(
            f"{col.header}: {op.value} is not available in SQL", col.key
        )

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

    @staticmethod
    def like_escape(text: str) -> str:
        """Make text literal inside a LIKE pattern.

        Without this, typing % in the search box matches everything and typing
        _ matches any character -- so the search would quietly mean something
        different from the one done in memory.
        """
        for char in ("\\", "%", "_"):
            text = text.replace(char, "\\" + char)
        return text

    def _search(self, needle: str, joins: _Joins):
        """The quick-search box: any searchable column containing the text."""
        pattern = f"%{self.like_escape(needle)}%"
        parts = []
        for col in self.spec.columns:
            if col.is_pk or not col.kind.filterable:
                continue
            parts.append(
                cast(self._column(col, joins), String).ilike(pattern, escape="\\")
            )
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
        if query.exclude_pks:
            pk = getattr(self.model, self.spec.pk.key)
            clauses.append(pk.notin_(tuple(query.exclude_pks)))
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

    def distinct(self, engine, query: ViewQuery, col: ColumnSpec) -> dict:
        """Stored value -> how many rows have it, for the filter checklist.

        Grouped on the raw column rather than on anything display-shaped, so
        the caller can run each value through ``schema.display`` and get text
        identical to what the cells show -- which matters for floats, enums
        and JSON, where SQL and Python format differently.
        """
        joins = _Joins(self.spec, self.model)
        where = self._where(query, joins)
        raw = getattr(self.model, col.key)
        stmt = select(raw, func.count()).select_from(self.model)
        stmt = joins.apply(stmt)
        if where:
            stmt = stmt.where(and_(*where))
        stmt = stmt.group_by(raw)
        with Session(engine) as session:
            return {value: int(n) for value, n in session.execute(stmt)}


def _everything(_row) -> bool:
    return True


def _unordered(_row) -> int:
    return 0


class MergedView:
    """A windowed view of the table with rows held in memory merged on top.

    The database does the ordering, filtering and searching, so only a window
    of rows is ever resident. That works until the user edits something: the
    table still holds the old values, so the query would sort and filter that
    row by them. Hence the split.

    * A **held** row is one this session has touched -- its values were edited,
      or it was marked deleted. It is excluded from the query by primary key
      and merged back in here, judged by the values the grid now has.
    * Every other row is **clean**, comes straight from the query, and may be
      thrown away and refetched at will. Nothing points at it: no edit, no undo
      target, no snapshot. That is what makes refetching safe.

    Placing a held row needs one number -- how many clean rows sort before it,
    which :meth:`SqlView.rank` asks the database without fetching anything. Its
    position in the merged view is then that count plus the number of held rows
    ahead of it, because the two sequences are ordered the same way.

    One count query per held row, and held rows are what a person typed in a
    session, so the cost is bounded by hand speed rather than by table size.
    """

    def __init__(self, engine, view: "SqlView") -> None:
        self.engine = engine
        self.view = view
        #: How held rows are judged: ``matches(row) -> bool`` and
        #: ``order_key(row)``, set by :meth:`rules` before every refresh so
        #: they are exactly the rules the grid uses for everything else. Until
        #: then nothing is held, so these are never consulted.
        self.matches = _everything
        self.order_key = _unordered
        self.query = ViewQuery()
        self.clean_total = 0
        self.placed: list[Row] = []          # held rows this view shows, in order
        self._at: dict[int, Row] = {}        # merged index -> held row
        self.skipped = 0                     # held rows the view filters out
        self.hidden: list[int] = []          # positions of rows deleted this session

    # -- the shape of the view -------------------------------------------
    def refresh(
        self,
        query: ViewQuery,
        held: Sequence[Row],
        show_deleted: bool = False,
    ) -> None:
        """Settle how many rows there are and where the held ones sit."""
        pk = self.view.spec.pk.key
        stored = [r for r in held if r.values.get(pk) is not None]
        self.query = replace(
            query,
            exclude_pks=tuple(r.values[pk] for r in stored),
            limit=None,
            offset=0,
        )
        self.clean_total = self.view.count(self.engine, self.query)

        keeping = [r for r in stored if self.matches(r)]
        self.skipped = len(stored) - len(keeping)
        keeping.sort(key=self.order_key)

        self.placed = keeping
        self._at = {}
        for ahead, row in enumerate(keeping):
            at = self.view.rank(self.engine, self.query, row.values) + ahead
            self._at[at] = row

        # A row deleted in this session still occupies a place in the order --
        # that is where its gutter mark belongs -- but it is not one of the
        # rows the grid counts, so the two numberings differ by however many
        # of them fall above any given point.
        self.hidden = (
            []
            if show_deleted
            else sorted(
                at for at, row in self._at.items() if row.state is RowState.DELETED
            )
        )

    def rules(self, matches, order_key) -> None:
        """Point at the current filters, search and sort order.

        Called before every refresh: the grid's own view changed, so the rules
        held rows are judged by have to change with it.
        """
        self.matches = matches
        self.order_key = order_key

    def count(self) -> int:
        """Places in the order, deleted rows included."""
        return self.clean_total + len(self.placed)

    def visible_count(self) -> int:
        """Rows the grid shows, deleted ones having collapsed into marks."""
        return self.count() - len(self.hidden)

    def distinct(self, col, filters, lookups, held) -> dict:
        """Display text -> count for the filter checklist.

        Excel computes this with the *other* columns' filters applied and this
        does the same, in two parts: the database counts the rows it still owns
        (the held ones are excluded from every query this view makes), then the
        held rows are counted here by the values they have now. Rows deleted in
        this session are left out of both halves.
        """
        others = FilterSet(
            filters={k: v for k, v in filters.filters.items() if k != col.key}
        )
        query = replace(
            self.query, sort=(), filters=others, search="", limit=None, offset=0
        )
        counts: dict[str, int] = {}
        for value, n in self.view.distinct(self.engine, query, col).items():
            text = display(col, _enum_name(value), lookups)
            counts[text] = counts.get(text, 0) + n

        keep = RowMatcher(self.view.spec, lookups, others, "")
        for row in held:
            if row.state is RowState.DELETED or not keep(row):
                continue
            text = display(col, row.values.get(col.key), lookups)
            counts[text] = counts.get(text, 0) + 1
        return counts

    # -- fetching a window -----------------------------------------------
    def slice(self, offset: int, limit: int) -> list[Row]:
        """The rows at [offset, offset + limit) of the merged view."""
        total = self.count()
        offset = max(0, offset)
        end = min(total, offset + limit)
        if offset >= end:
            return []

        # Clean rows carry the positions the held ones do not, so the window
        # starts that many rows earlier in the query's own numbering.
        ahead = sum(1 for at in self._at if at < offset)
        wanted = end - offset - sum(1 for at in self._at if offset <= at < end)
        clean: list[Row] = []
        if wanted > 0:
            clean = self.view.rows(
                self.engine,
                replace(self.query, limit=wanted, offset=offset - ahead),
            )

        out: list[Row] = []
        feed = iter(clean)
        for at in range(offset, end):
            row = self._at.get(at)
            if row is None:
                row = next(feed, None)
                if row is None:
                    break           # the table shrank under us; stop cleanly
            out.append(row)
        return out

    def visible_slice(self, offset: int, limit: int) -> list[Row]:
        """``limit`` rows the grid would show, starting at visible ``offset``.

        Deleted rows come back inline with them, because the grid draws each
        one as a mark above the row that follows it and so needs to see it.
        They are not counted against ``limit``.
        """
        if not self.hidden:
            return self.slice(offset, limit)

        hidden = set(self.hidden)
        start = offset
        for at in self.hidden:          # sorted: each one above us shifts us down
            if at <= start:
                start += 1
        while start - 1 in hidden:      # keep a straddling run with its mark
            start -= 1

        rows = self.slice(start, limit + len(self.hidden))
        # Past the end of the view there is no following row to hang a mark on,
        # so the last window keeps the deleted rows that trail it.
        last = offset + limit >= self.visible_count()
        out: list[Row] = []
        shown = 0
        for row in rows:
            out.append(row)
            if row.state is not RowState.DELETED:
                shown += 1
                if shown == limit and not last:
                    break           # these marks belong to the next window
        return out
