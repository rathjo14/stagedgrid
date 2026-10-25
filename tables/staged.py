"""Editing a table: the staged grid, over a table found the same way.

Everything the editor does needs one thing the browser does not -- a natural
key, so a typed row can be told from one that already exists. That is the only
extra requirement; it is read from the table like everything else, so a
reflected table with a unique constraint is as good as a mapped class.

    StagedTable(dsn, "model_e", updates="updates").open()

``updates`` is the table committed changes are written to. Name it and it is
reflected too; hand over a mapped class if you have one; leave it out and
commits go to the sink and the changesets directory only.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from .base import DatabaseTable


class StagedTable(DatabaseTable):
    """A table you can edit: staging, validation, undo, commit."""

    def __init__(
        self,
        dsn: Optional[str] = None,
        table: Optional[str] = None,
        schema: Optional[str] = None,
        *,
        updates: Any = None,
        **kwargs,
    ) -> None:
        super().__init__(dsn, table, schema, **kwargs)
        if self.spec.unique_constraint is None:
            raise ValueError(
                f"{self.table_name} has no unique constraint, so there is no "
                "way to tell a new row from one that already exists -- open it "
                "with ReadOnlyTable instead"
            )
        #: A name is reflected; a mapped class is taken as it is; None means
        #: commits go to the sink only.
        self.updates_model = (
            self._reflect(updates, schema) if isinstance(updates, str) else updates
        )
        self._repository = None

    def repository(self):
        """The data layer, built once and kept.

        This is the same ``Repository`` the flat app uses, so anything written
        against that -- ``load_rows``, ``changeset()``, ``apply`` -- works the
        same whether the table arrived here reflected or mapped.
        """
        if self._repository is None:
            from data import Repository

            self._repository = Repository(
                engine=self.engine,
                model=self.model,
                label_columns=self.label_columns,
                updates_model=self.updates_model,
            )
        return self._repository

    def window(
        self,
        sink: Optional[Callable[[dict], str]] = None,
        window: Optional[int] = None,
        parent=None,
        **kwargs,
    ):
        from app import MainWindow

        return MainWindow(
            self.repository(), sink=sink, window=window, parent=parent, **kwargs
        )
