"""The tables package: a table found by name, and what you may do with it."""

import os
import sys

# Before PySide6 is imported, as in the main suite: no display, and a font
# directory or every glyph paints as a box.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")

import pytest
from PySide6.QtCore import Qt
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schema import ModelE, TableSpec, seed          # noqa: E402
from tables import DatabaseTable, ReadOnlyTable, StagedTable  # noqa: E402


@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        future=True,
    )
    seed(engine, rows=60, seed_value=11)
    return engine


# ------------------------------------------------------------ the base class
def test_a_table_is_found_by_name_with_no_model_at_all(engine):
    table = DatabaseTable(engine=engine, table="model_e")

    assert table.table_name == "model_e"
    assert table.spec.pk.key == "id"
    assert table.spec.unique_constraint == "uq_model_e_natural_key"
    # reflecting a table pulls in what its foreign keys point at, which is
    # what lets a key column show a label instead of an id
    assert table.spec["a_id"].fk.target_table == "model_a"
    assert table.spec["a_id"].fk.label_column == "name"
    assert table.spec.version_column.key == "d_id"
    assert "model_e" in table.describe()


def test_the_reflected_spec_agrees_with_the_mapped_one(engine):
    """Same table, two roads to it: the columns should come out the same."""
    reflected = DatabaseTable(engine=engine, table="model_e").spec
    mapped = TableSpec.of(ModelE)

    assert [c.key for c in reflected.columns] == [c.key for c in mapped.columns]
    assert [c.header for c in reflected.columns] == [c.header for c in mapped.columns]
    assert reflected.unique_constraint == mapped.unique_constraint
    assert [c.key for c in reflected.key_columns] == [c.key for c in mapped.key_columns]
    for key in ("a_id", "b_id", "c_id", "d_id"):
        assert reflected[key].fk.label_column == mapped[key].fk.label_column
        assert reflected[key].fk.pk_column == mapped[key].fk.pk_column

    # The one honest difference: SQLite has no enum type, so a reflected enum
    # comes back as text. It still sorts, filters and searches; it just has no
    # fixed list of members. A native enum (Postgres) reflects with its labels.
    assert mapped["type"].enum_names
    assert not reflected["type"].enum_names


def test_the_base_class_opens_nothing_by_itself(engine):
    table = DatabaseTable(engine=engine, table="model_e")
    with pytest.raises(NotImplementedError):
        table.window()


def test_a_missing_table_says_so(engine):
    with pytest.raises(LookupError) as caught:
        DatabaseTable(engine=engine, table="no_such_table")
    assert "no_such_table" in str(caught.value)


def test_a_mapped_class_can_be_handed_over_instead(engine):
    table = DatabaseTable(engine=engine, model=ModelE)
    assert table.model is ModelE
    assert table.spec["type"].enum_names        # the class knows its members


# -------------------------------------------------------------- browsing it
def test_read_only_shows_the_table_and_edits_nothing(qapp, engine):
    table = ReadOnlyTable(engine=engine, table="model_e")
    window = table.window()
    model = window.model

    assert model.rowCount() == window.total_rows > 0
    assert model.staged_count == 0               # nowhere to type
    assert not model.editable
    assert not model.flags(model.index(0, 0)) & Qt.ItemFlag.ItemIsEditable
    # the row numbers are there, as in the editor
    assert model.headerData(0, Qt.Orientation.Vertical, Qt.ItemDataRole.DisplayRole) == "1"


def test_read_only_sorts_and_searches_the_whole_table(qapp, engine):
    table = ReadOnlyTable(engine=engine, table="model_e")
    window = table.window()
    window.window_size = 5
    window._refresh_window(0, structural=True)
    model = window.model
    assert model.resident_rows() == 5 < window.total_rows

    model.set_sort([("weight", True)])
    top = model.row_at(0).values["weight"]
    assert top is not None
    # holding five rows, the top of the sort is still the table's own maximum
    from data import Repository
    rows = Repository(engine=engine, updates_model=None).load_rows(
        latest_version_only=False
    )
    assert top == max(r.values["weight"] for r in rows if r.values["weight"] is not None)

    before = window.total_rows
    model.set_quick_search("RATES")
    assert 0 < window.total_rows < before


def test_read_only_opens_a_table_with_no_natural_key(qapp, engine):
    """A dimension has no unique constraint and no foreign keys. It still opens."""
    table = ReadOnlyTable(engine=engine, table="model_a")
    assert table.spec.unique_constraint is None

    window = table.window()
    assert window.total_rows > 0
    assert [c.header for c in window.model.columns] == ["Name"]
    assert window.model.staged_count == 0


def test_read_only_filters_through_the_same_dialog(qapp, engine):
    from data import ColumnFilter

    table = ReadOnlyTable(engine=engine, table="model_e")
    window = table.window()
    before = window.total_rows

    column = window.model.column_index("a_id")
    offered = dict(window.model.distinct_values(column))
    assert len(offered) > 1, "the checklist should span the table"

    window.model.set_filter(
        ColumnFilter(key="a_id", allowed=frozenset(list(offered)[:1]))
    )
    assert 0 < window.total_rows < before


# --------------------------------------------------------------- editing it
def test_staged_opens_the_editor_over_a_reflected_table(qapp, engine):
    table = StagedTable(engine=engine, table="model_e", updates="updates")
    window = table.window()

    assert window.model.staged_count == 1        # somewhere to type
    assert window.model.editable
    assert window.total_rows > 0
    assert "Inputs Version" in window._version_label.text()
    assert table.repository().updates is not None


def test_staged_refuses_a_table_it_cannot_tell_rows_apart_in(engine):
    with pytest.raises(ValueError) as caught:
        StagedTable(engine=engine, table="model_a")
    assert "unique constraint" in str(caught.value)
    assert "ReadOnlyTable" in str(caught.value)   # and says what to use instead


def test_staged_without_an_updates_table_still_opens(qapp, engine):
    table = StagedTable(engine=engine, table="model_e")
    assert table.repository().updates is None
    assert table.window().total_rows > 0


def test_a_commit_through_the_reflected_table_writes_rows(qapp, engine):
    """The insert path has no class to construct, so it goes through Core."""
    table = StagedTable(engine=engine, table="model_e", updates="updates")
    repo = table.repository()
    before = repo.count_rows(latest_version_only=False)

    window = table.window()
    model = window.model
    lookups = model.lookups
    used = repo.natural_keys()
    version = repo.latest_version()[0]

    # a key nothing is using yet
    from itertools import product

    spec = table.spec
    choice = None
    for combo in product(
        lookups.choices(spec["a_id"]),
        lookups.choices(spec["b_id"]),
        lookups.choices(spec["c_id"]),
    ):
        ids = tuple(
            lookups.id_for(spec[k], label)
            for k, label in zip(("a_id", "b_id", "c_id"), combo)
        ) + (version, "BASELINE")
        if ids not in used:
            choice = combo
            break
    assert choice, "no free natural key"

    for key, text in zip(("a_id", "b_id", "c_id"), choice):
        model.setData(
            model.index(0, model.column_index(key)), text, Qt.ItemDataRole.EditRole
        )
    model.setData(
        model.index(0, model.column_index("type")), "BASELINE", Qt.ItemDataRole.EditRole
    )
    model.revalidate()
    assert not model.row_at(0).errors, model.row_at(0).errors

    result = repo.apply(model.changeset())
    assert result.inserted and result.inserted[0] > 0
    assert repo.count_rows(latest_version_only=False) == before + 1


def test_browsing_shows_the_version_as_an_ordinary_column(qapp, engine):
    """The editor hides it -- one version per view, inherited by new rows.

    Reading is not editing: the query can reach across versions, so there the
    version is data like anything else and has to be visible.
    """
    browse = ReadOnlyTable(engine=engine, table="model_e").window()
    assert "d_id" in [c.key for c in browse.model.columns]

    edit = StagedTable(engine=engine, table="model_e").window()
    assert "d_id" not in [c.key for c in edit.model.columns]


# ------------------------------------------------- an enum with no Python class
def _classless_enum_table(engine):
    """A table whose enum is only a database type, as reflection gives it.

    Built here rather than reflected so it works on any backend: what matters
    is a column whose SAEnum has labels and no ``enum_class``, which is what
    comes back from a database that has a real enum type.
    """
    from sqlalchemy import Column, Enum as SAEnum, Integer, MetaData, String, Table

    metadata = MetaData()
    table = Table(
        "sized",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("name", String(20)),
        # declared smallest to largest, which is not alphabetical
        Column("size", SAEnum("SMALL", "MEDIUM", "LARGE", "HUGE", name="size_t")),
    )
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            table.insert(),
            [
                {"name": f"row {n}", "size": size}
                for n, size in enumerate(
                    ["HUGE", "SMALL", "LARGE", "MEDIUM", "SMALL", "HUGE"]
                )
            ],
        )
    return table


def test_an_enum_with_no_python_class_is_still_an_enum(engine):
    """Reflected, there is no class -- only the labels, which is enough."""
    from schema import ColumnKind, TableSpec

    table = _classless_enum_table(engine)
    spec = TableSpec.of(table)
    size = spec["size"]

    assert size.enum_class is None
    assert size.kind is ColumnKind.ENUM        # not merely text
    assert size.enum_names == ("SMALL", "MEDIUM", "LARGE", "HUGE")
    assert size.kind.filterable


def test_a_classless_enum_sorts_by_declaration_not_alphabet(engine):
    """The whole reason an enum is not text: SMALL < MEDIUM < LARGE < HUGE."""
    from schema import TableSpec
    from sqlview import SqlView, ViewQuery

    table = _classless_enum_table(engine)
    spec = TableSpec.of(table)
    view = SqlView(table, spec, None)

    ordered = [
        r.values["size"]
        for r in view.rows(engine, ViewQuery(sort=(("size", False),)))
    ]
    declared = list(spec["size"].enum_names)
    assert ordered == ["SMALL", "SMALL", "MEDIUM", "LARGE", "HUGE", "HUGE"]
    assert [v for n, v in enumerate(ordered) if n == 0 or ordered[n - 1] != v] == declared
    assert ordered != sorted(ordered), "that is alphabetical, not declared order"


def test_a_classless_enum_filters_by_its_labels(engine):
    from data import ColumnFilter, FilterSet
    from schema import TableSpec
    from sqlview import SqlView, ViewQuery

    table = _classless_enum_table(engine)
    spec = TableSpec.of(table)
    view = SqlView(table, spec, None)

    filters = FilterSet()
    filters.set(ColumnFilter(key="size", allowed=frozenset({"SMALL", "HUGE"})))
    rows = view.rows(engine, ViewQuery(filters=filters))
    assert sorted(r.values["size"] for r in rows) == [
        "HUGE", "HUGE", "SMALL", "SMALL"
    ]


def test_a_classless_enum_opens_read_only(qapp, engine):
    """Handed the table, the window treats the enum as one."""
    table = _classless_enum_table(engine)
    window = ReadOnlyTable(engine=engine, model=table).window()

    assert window.total_rows == 6
    assert "Size" in [c.header for c in window.model.columns]
    window.model.set_sort([("size", False)])
    assert window.model.row_at(0).values["size"] == "SMALL"
    # the filter checklist offers the members
    column = window.model.column_index("size")
    assert set(dict(window.model.distinct_values(column))) == {
        "SMALL", "MEDIUM", "LARGE", "HUGE"
    }


def test_what_sqlite_loses_when_the_enum_is_reflected(qapp, engine):
    """An enum reflects only as well as the database records it.

    Postgres has a real enum type, so reflecting gives back the members and
    the column keeps its order and its drop-down. SQLite does not, so the same
    column arrives as text: still sortable, filterable and searchable, but
    alphabetically and with no fixed list of members. Verified against
    Postgres separately; stated here so the difference is not a surprise.
    """
    from schema import ColumnKind

    _classless_enum_table(engine)
    reflected = ReadOnlyTable(engine=engine, table="sized")
    size = reflected.spec["size"]

    assert size.kind is ColumnKind.STR          # SQLite has no enum type
    assert size.enum_names == ()

    window = reflected.window()
    window.model.set_sort([("size", False)])
    assert window.model.row_at(0).values["size"] == "HUGE"   # alphabetical
    # and it is still perfectly usable as text
    assert window.total_rows == 6
    window.model.set_quick_search("SMALL")
    assert window.total_rows == 2
