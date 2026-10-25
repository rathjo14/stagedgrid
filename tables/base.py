"""One table in a database, reflected, and what can be done with it.

``DatabaseTable`` is the part that is the same whatever you want to do: find
the table, work out its columns, and know how to ask the database for a view
of it. What it deliberately does *not* decide is what kind of window the table
deserves -- that is what a subclass is for:

    DatabaseTable                    a dsn, a schema, a table name
      ReadOnlyTable                  browse it: sort, filter, search, copy
      StagedTable                    edit it: staging, validation, commit

Nothing here needs a mapped class. The table is reflected, so the columns, the
natural key, the foreign keys and their labels all come from the database
itself -- which is also where they came from for a mapped class, just by a
longer road. Pass ``model=`` instead when you do have one and would rather use
it, and everything downstream behaves the same.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import MetaData, Table, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from schema import Lookups, TableSpec, describe, table_of


class DatabaseTable:
    """A table, found by name, with the columns worked out from it.

    ``dsn`` and ``table`` are the whole requirement; ``schema`` matters only
    where the table is not on the search path. Give ``engine`` instead of
    ``dsn`` to share a connection pool with something else.
    """

    #: What a subclass opens. Set it to a class taking (table, **kwargs).
    window_class: Optional[type] = None

    def __init__(
        self,
        dsn: Optional[str] = None,
        table: Optional[str] = None,
        schema: Optional[str] = None,
        *,
        engine: Optional[Engine] = None,
        model: Optional[type] = None,
        label_columns: Optional[dict[str, str]] = None,
    ) -> None:
        if engine is None and not dsn:
            raise ValueError("a dsn or an engine is required")
        if model is None and not table:
            raise ValueError("a table name is required (or pass model=)")

        self.dsn = dsn
        self.schema = schema
        self.engine: Engine = engine if engine is not None else create_engine(dsn)
        self.label_columns = label_columns

        if model is not None:
            # Already mapped: use it, and take the spec from the mapper so the
            # attribute names are the ones its own code uses.
            self.model = model
            self.table: Table = table_of(model)
            self.spec = TableSpec.of(model, label_columns)
        else:
            self.table = self._reflect(table, schema)
            self.model = self.table
            self.spec = TableSpec.of(self.table, label_columns)

        self.table_name = self.spec.table_name
        self._lookups: Optional[Lookups] = None

    # -- finding it -------------------------------------------------------
    def _reflect(self, name: str, schema: Optional[str]) -> Table:
        """Read the table out of the database, and its dimensions with it.

        Reflecting a table pulls in whatever its foreign keys point at, which
        is what makes a key column able to show a label instead of an id.
        """
        metadata = MetaData()
        try:
            return Table(
                name, metadata, schema=schema, autoload_with=self.engine
            )
        except SQLAlchemyError as exc:
            where = f"{schema}.{name}" if schema else name
            raise LookupError(f"cannot read {where}: {exc}") from None

    # -- what it is -------------------------------------------------------
    @property
    def lookups(self) -> Lookups:
        """Label caches for the foreign keys, read once and kept."""
        if self._lookups is None:
            self._lookups = self.load_lookups()
        return self._lookups

    def load_lookups(self) -> Lookups:
        from data import Repository

        return Repository(
            engine=self.engine,
            model=self.model,
            label_columns=self.label_columns,
            updates_model=None,
        ).load_lookups()

    def describe(self) -> str:
        """What the grid worked out about the table, as text."""
        return describe(self.spec)

    def __repr__(self) -> str:
        where = f"{self.schema}.{self.table_name}" if self.schema else self.table_name
        return f"<{type(self).__name__} {where}>"

    # -- opening it -------------------------------------------------------
    def window(self, **kwargs):
        """The window for this table. Subclasses say what that is."""
        if self.window_class is None:
            raise NotImplementedError(
                f"{type(self).__name__} has no window_class; "
                "subclass DatabaseTable and set one"
            )
        return self.window_class(self, **kwargs)

    def open(self, **kwargs):
        """Build the window and show it. Returns it, so a caller can keep it."""
        made = self.window(**kwargs)
        made.show()
        return made
