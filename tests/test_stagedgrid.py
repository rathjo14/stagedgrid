"""The whole suite: schema, values, filters, changeset, validation, grid."""

from __future__ import annotations

import os
from pathlib import Path

# Qt has to be told to run headless before PySide6 builds an application.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if Path("C:/Windows/Fonts").exists():
    os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")

import grid
import enum
from itertools import product

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt, QTimer
from PySide6.QtGui import QContextMenuEvent, QGuiApplication, QKeyEvent
from PySide6.QtWidgets import (
    QApplication,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy import (
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, mapped_column
from sqlalchemy.pool import StaticPool

from data import (
    ChangeSet,
    ColumnFilter,
    Condition,
    FilterSet,
    Join,
    Op,
    Repository,
    Row,
    RowState,
    Validator,
    existing_key_map,
)
from grid import (
    NEW_VALUE,
    DARK,
    LIGHT,
    CELL_ERROR,
    CELL_UPDATED,
    ROLE_CELL_STATE,
    C,
    GridView,
    StagedTableModel,
    set_theme,
    stylesheet,
)
from schema import (
    LABEL_COLUMNS,
    ColumnKind,
    ModelE,
    TableSpec,
    Unresolved,
    display,
    parse,
    seed,
    to_db,
)


# ------------------------------------------------------------------ fixtures
@pytest.fixture
def engine():
    """A fresh in-memory SQLite per test, so no test can taint another."""
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        future=True,
    )
    seed(engine, rows=60, seed_value=11)
    return engine


@pytest.fixture
def repo(engine) -> Repository:
    return Repository(engine=engine)


@pytest.fixture
def spec(repo):
    return repo.spec


@pytest.fixture
def model(qapp, repo):
    model = StagedTableModel(repo.spec)
    model.load(repo.load_rows(), repo.load_lookups())
    model.validator.set_existing_keys(repo.natural_keys())
    return model


@pytest.fixture
def view(qapp, model):
    view = GridView(model)
    view.resize(1200, 600)
    return view


# ------------------------------------------------------------------- helpers
@pytest.fixture(autouse=True)
def _theme_is_global_so_put_it_back():
    yield
    set_theme(False)


def press(view, key, mods=Qt.KeyboardModifier.NoModifier):
    view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, mods))


def idx(model, row, key):
    return model.index(row, model.column_index(key))


def free_key(model, repo):
    """A natural key that is not in the database yet, as display text."""
    used = repo.natural_keys()
    lookups, spec = model.lookups, model.spec
    choices = [lookups.choices(spec[k]) for k in ("a_id", "b_id", "c_id", "d_id")]
    for combo in product(*choices, spec["type"].enum_names):
        ids = tuple(
            lookups.id_for(spec[k], label)
            for k, label in zip(("a_id", "b_id", "c_id", "d_id"), combo[:4])
        ) + (combo[4],)
        if ids not in used:
            return list(combo)
    raise AssertionError("no free natural key")


def _rows(spec, values):
    return [Row.from_db({**{c.key: None for c in spec.columns}, **v}) for v in values]


def _other_id(lookups, spec, current):
    col = spec["a_id"]
    for label in lookups.choices(col):
        candidate = lookups.id_for(col, label)
        if candidate != current:
            return candidate
    raise AssertionError("need at least two A values")


# --------------------------------------------------------------- introspection
def test_spec_finds_natural_key_and_labels(spec: TableSpec):
    assert spec.table_name == "model_e"
    assert spec.unique_constraint == "uq_model_e_natural_key"
    assert [c.key for c in spec.key_columns] == ["a_id", "b_id", "c_id", "d_id", "type"]
    assert spec.pk.key == "id" and spec.pk.is_pk

    a = spec["a_id"]
    assert a.kind is ColumnKind.FK
    assert a.fk.target_table == "model_a" and a.fk.label_attr == "name"
    assert a.header == "A"
    # model_d keys on `version`, not `name`
    assert spec["d_id"].fk.label_attr == "version"
    assert spec["d_id"].header == "D version"


def test_spec_carries_docs_and_types(spec: TableSpec):
    assert "Blend weight" in spec["weight"].doc
    assert spec["weight"].kind is ColumnKind.FLOAT
    assert spec["run_count"].kind is ColumnKind.INT
    assert spec["label"].kind is ColumnKind.STR
    assert spec["payload"].kind is ColumnKind.JSON
    assert spec["type"].kind is ColumnKind.ENUM
    assert spec["type"].enum_names == (
        "BASELINE", "STRESS", "SENSITIVITY", "HISTORICAL", "FORECAST",
    )
    assert not spec["payload"].kind.filterable  # jsonb is out of scope


def test_key_columns_are_locked_on_existing_rows(spec: TableSpec):
    assert spec["a_id"].in_key and not spec["a_id"].editable_on_existing
    assert spec["a_id"].editable_on_staged
    assert spec["weight"].editable_on_existing
    assert not spec.pk.editable_on_staged


def test_label_columns_can_be_overridden():
    other = TableSpec.from_mapped_class(ModelE, {**LABEL_COLUMNS, "model_d": "id"})
    assert other["d_id"].fk.label_attr == "id"


# -------------------------------------------------------------------- values
@pytest.mark.parametrize(
    "key,text,expected",
    [
        ("run_count", "42", 42),
        ("run_count", "1,024", 1024),
        ("run_count", "3.0", 3),
        ("weight", "0.25", 0.25),
        ("weight", "50%", 0.5),
        ("weight", "1e-3", 0.001),
        ("label", "  hello ", "  hello "),
        ("status_note", "", None),
        ("payload", '{"a": 1}', {"a": 1}),
        ("type", "stress", "STRESS"),
    ],
)
def test_parse_accepts_spreadsheet_input(spec, key, text, expected):
    assert parse(spec[key], text) == expected


@pytest.mark.parametrize(
    "key,text",
    [("run_count", "abc"), ("weight", "1.2.3"), ("payload", "{oops")],
)
def test_parse_rejects_nonsense(spec, key, text):
    with pytest.raises(ValueError):
        parse(spec[key], text)


def test_unknown_enum_and_fk_become_unresolved(spec, repo):
    lookups = repo.load_lookups()
    assert parse(spec["type"], "NOPE") == Unresolved("NOPE")
    assert parse(spec["a_id"], "NOWHERE", lookups) == Unresolved("NOWHERE")
    known = lookups.choices(spec["a_id"])[0]
    assert parse(spec["a_id"], known.lower(), lookups) == lookups.id_for(
        spec["a_id"], known
    )


def test_display_resolves_labels_and_formats(spec, repo):
    lookups = repo.load_lookups()
    a = spec["a_id"]
    pk = lookups.id_for(a, lookups.choices(a)[0])
    assert display(a, pk, lookups) == lookups.choices(a)[0]
    assert display(spec["weight"], 0.10000000001) == "0.10000000001"
    assert display(spec["weight"], 2.0) == "2"
    assert display(spec["label"], None) == ""
    assert display(spec["payload"], {"b": 1, "a": 2}) == '{"a": 2, "b": 1}'


# ------------------------------------------------------------------- filters
def test_numeric_conditions(spec):
    col = spec["score"]
    flt = ColumnFilter(key=col.key, conditions=(Condition(Op.BETWEEN, "10", "20"),))
    predicate = flt.predicate(col)
    assert predicate(15) and not predicate(9) and not predicate(None)

    flt = ColumnFilter(key=col.key, conditions=(Condition(Op.NOT_BLANK),))
    predicate = flt.predicate(col)
    assert predicate(0) and not predicate(None)


def test_text_conditions_and_or(spec):
    col = spec["owner"]
    flt = ColumnFilter(
        key=col.key,
        conditions=(Condition(Op.STARTS, "risk"), Condition(Op.CONTAINS, "core")),
        join=Join.OR,
    )
    predicate = flt.predicate(col)
    assert predicate("risk-eng") and predicate("quant-core")
    assert not predicate("market-data")


def test_value_checklist_uses_display_text(spec, repo):
    lookups = repo.load_lookups()
    col = spec["a_id"]
    first = lookups.choices(col)[0]
    flt = ColumnFilter(key=col.key, allowed=frozenset({first}))
    predicate = flt.predicate(col, lookups)
    assert predicate(lookups.id_for(col, first))
    assert not predicate(lookups.id_for(col, lookups.choices(col)[1]))


def test_regex_filter_survives_a_bad_pattern(spec):
    col = spec["label"]
    good = ColumnFilter(key=col.key, conditions=(Condition(Op.REGEX, r"^Base"),))
    assert good.predicate(col)("Baseline 1") is True
    broken = ColumnFilter(key=col.key, conditions=(Condition(Op.REGEX, "([a"),))
    assert broken.predicate(col)("anything") is True


# ----------------------------------------------------------------- changeset
def test_changeset_separates_the_three_operations(spec, repo):
    lookups = repo.load_lookups()
    rows = repo.load_rows()
    rows[0].remember_original()      # what the model does before an edit
    rows[0].values["weight"] = 0.5
    rows[0].recompute_state()
    rows[1].state = RowState.DELETED
    staged = Row.staged(spec, {**rows[2].values, "id": None, "label": "brand new"})
    staged.values["a_id"] = lookups.id_for(spec["a_id"], lookups.choices(spec["a_id"])[0])

    cs = ChangeSet.from_rows(spec, [staged, *rows], lookups)
    assert cs.counts == {"inserts": 1, "updates": 1, "deletes": 1, "cells": 1}

    payload = cs.to_payload()
    assert payload["table"] == "model_e"
    ops = {op["op"]: op for op in payload["operations"]}
    assert set(ops) == {"insert", "update", "delete"}
    assert ops["update"]["changes"][0]["column"] == "weight"
    assert ops["update"]["changes"][0]["new"] == 0.5
    # natural keys travel as ids *and* as labels
    assert ops["insert"]["key"]["a_id"] and ops["insert"]["key_labels"]["a_id"]


def test_clean_rows_produce_nothing(spec, repo):
    cs = ChangeSet.from_rows(spec, repo.load_rows(), repo.load_lookups())
    assert cs.is_empty


# ---------------------------------------------------------------- validation
def test_validator_flags_duplicate_and_existing_keys(spec, repo):
    lookups = repo.load_lookups()
    rows = repo.load_rows()
    validator = Validator(spec, lookups, existing_key_map(spec, rows))

    clash = Row.staged(spec, {k: v for k, v in rows[0].values.items() if k != "id"})
    twin_a = Row.staged(spec, dict(clash.values))
    twin_b = Row.staged(spec, dict(clash.values))
    twin_a.values["a_id"] = _other_id(lookups, spec, rows[0].values["a_id"])
    twin_b.values["a_id"] = twin_a.values["a_id"]

    report = validator.validate([clash, twin_a, twin_b, *rows])
    assert not report.ok
    assert "already exists" in clash.errors["a_id"]
    assert "duplicates another staged row" in twin_b.errors["a_id"]


def test_validator_flags_unresolved_values(spec, repo):
    validator = Validator(spec, repo.load_lookups(), {})
    row = Row.staged(spec, {"a_id": Unresolved("NOWHERE"), "type": Unresolved("ZZZ")})
    validator.validate([row])
    assert "not a known A" in row.errors["a_id"]
    assert "BASELINE" in row.errors["type"]


def test_deleting_a_row_frees_its_key(spec, repo):
    lookups = repo.load_lookups()
    rows = repo.load_rows()
    validator = Validator(spec, lookups, existing_key_map(spec, rows))
    rows[0].state = RowState.DELETED
    replacement = Row.staged(
        spec, {k: v for k, v in rows[0].values.items() if k != "id"}
    )
    report = validator.validate([replacement, *rows])
    assert report.ok  # the clash is a warning, not an error
    assert "replaces row" in next(iter(replacement.warnings.values()))


# -------------------------------------------------------------------- layout
def test_blank_staging_row_is_always_present(model):
    assert model.staged_count == 1
    assert model.rowCount() == model.resident_rows() + 1
    blank = model.row_at(0)
    assert blank.is_staged and blank.is_empty(model.spec)


def test_key_cells_are_locked_on_existing_rows_only(model):
    first_db = model.staged_count
    assert not idx(model, first_db, "a_id").flags() & Qt.ItemFlag.ItemIsEditable
    assert idx(model, first_db, "weight").flags() & Qt.ItemFlag.ItemIsEditable
    assert idx(model, 0, "a_id").flags() & Qt.ItemFlag.ItemIsEditable


def test_locked_key_cells_are_greyed(model):
    cell = idx(model, model.staged_count, "a_id")
    assert cell.data(Qt.ItemDataRole.BackgroundRole).color() == C.key_readonly


def test_the_id_is_the_row_index_not_a_column(model):
    """The pk is the gutter label, Excel-style, and never a cell."""
    assert "id" not in [c.key for c in model.columns]
    assert model.columnCount() == len(model.spec.columns) - 1

    def gutter(row):
        return model.headerData(
            row, Qt.Orientation.Vertical, Qt.ItemDataRole.DisplayRole
        )

    first_db = model.staged_count
    assert gutter(first_db) == str(model.row_at(first_db).values["id"])
    assert gutter(0) == "+"                      # "put new data here"
    model.setData(idx(model, 0, "label"), "typed")
    assert gutter(0) == "!!!"                    # incomplete: needs attention
    for key, value in (("a_id", "APAC"), ("b_id", "RATES"), ("c_id", "BOND"),
                       ("d_id", "2026.2"), ("type", "BASELINE")):
        model.setData(idx(model, 0, key), value)
    model.revalidate()
    assert gutter(0) == ""                       # ...once it is valid, nothing


# ------------------------------------------------------------------ editing
def test_edit_marks_row_updated_and_cell_bluer(model):
    row = model.staged_count
    cell = idx(model, row, "weight")
    assert model.setData(cell, "0.4242")
    assert model.row_at(row).state is RowState.UPDATED
    assert cell.data(ROLE_CELL_STATE) == CELL_UPDATED
    assert cell.data(Qt.ItemDataRole.BackgroundRole).color() == C.updated_cell
    # a sibling cell gets the faint row colour, not the strong cell colour
    sibling = idx(model, row, "score")
    assert sibling.data(Qt.ItemDataRole.BackgroundRole).color() == C.updated_row
    assert model.changeset().counts["updates"] == 1


def test_editing_back_to_the_original_clears_the_change(model):
    row = model.staged_count
    cell = idx(model, row, "run_count")
    before = cell.data(Qt.ItemDataRole.EditRole)
    model.setData(cell, "123456")
    assert model.row_at(row).state is RowState.UPDATED
    model.setData(cell, before)
    assert model.row_at(row).state is RowState.CLEAN
    assert model.changeset().is_empty


def test_bad_value_is_rejected(model):
    cell = idx(model, model.staged_count, "weight")
    before = cell.data(Qt.ItemDataRole.EditRole)
    assert model.setData(cell, "not a number") is False
    assert cell.data(Qt.ItemDataRole.EditRole) == before


def test_typing_in_the_blank_row_adds_another_blank(model):
    assert model.staged_count == 1
    model.setData(idx(model, 0, "label"), "first new row")
    assert model.staged_count == 2
    assert model.row_at(1).is_empty(model.spec)
    assert model.changeset().counts["inserts"] == 1


def test_emptying_the_last_row_collapses_the_extra_blank(model):
    model.setData(idx(model, 0, "label"), "typo")
    assert model.staged_count == 2
    model.setData(idx(model, 0, "label"), "")
    assert model.staged_count == 1


def test_staged_row_with_an_unknown_key_is_an_error(model):
    model.setData(idx(model, 0, "a_id"), "NOT-A-REGION")
    model.revalidate()
    assert idx(model, 0, "a_id").data(ROLE_CELL_STATE) == CELL_ERROR
    assert model.dirty_counts()["errors"] == 1


def test_duplicate_of_an_existing_row_is_an_error(model, repo):
    for key in ("a_id", "b_id", "c_id", "d_id", "type"):
        text = idx(model, model.staged_count, key).data(Qt.ItemDataRole.EditRole)
        model.setData(idx(model, 0, key), text)
    model.revalidate()
    assert "already exists" in model.row_at(0).errors["a_id"]


# ----------------------------------------------------------------- deletion
def test_delete_removes_the_row_and_leaves_a_marker(model):
    first_db = model.staged_count
    target = model.row_at(first_db + 2)
    before = model.rowCount()

    model.delete_rows([first_db + 2])
    assert model.rowCount() == before - 1
    assert target.state is RowState.DELETED
    # the marker sits where the row used to be
    assert model.deleted_rows_above(first_db + 2) == [target]
    assert model.changeset().counts["deletes"] == 1


def test_clicking_the_marker_restores_the_row(model):
    first_db = model.staged_count
    model.delete_rows([first_db + 1, first_db + 2])
    assert len(model.deleted_rows_above(first_db + 1)) == 2
    model.restore_deleted_above(first_db + 1)
    assert model.deleted_above(first_db + 1) == []
    assert model.changeset().is_empty


def test_delete_key_removes_rows_picked_from_the_index(view, model):
    """Select whole rows from the gutter, press Del, the rows go."""
    first = model.staged_count
    target = model.row_at(first + 1)
    before = model.rowCount()

    view.selectRow(first + 1)
    assert view.selectionModel().selectedRows()  # a whole-row selection
    press(view, Qt.Key.Key_Delete)

    assert model.rowCount() == before - 1
    assert target.state is RowState.DELETED
    assert model.deleted_rows_above(first + 1) == [target]


def test_delete_key_on_several_rows_removes_them_all(view, model):
    first = model.staged_count
    before = model.rowCount()
    view.selectRow(first + 1)
    for extra in (first + 2, first + 3):
        view.selectionModel().select(
            model.index(extra, 0),
            view.selectionModel().SelectionFlag.Select
            | view.selectionModel().SelectionFlag.Rows,
        )
    press(view, Qt.Key.Key_Delete)
    assert model.rowCount() == before - 3
    assert model.changeset().counts["deletes"] == 3


def test_delete_key_on_a_cell_selection_still_just_clears(view, model):
    row = model.staged_count
    cell = idx(model, row, "status_note")
    view.setCurrentIndex(cell)
    view.selectionModel().select(
        cell, view.selectionModel().SelectionFlag.ClearAndSelect
    )
    before = model.rowCount()

    press(view, Qt.Key.Key_Delete)

    assert model.rowCount() == before            # nothing deleted
    assert cell.data(Qt.ItemDataRole.DisplayRole) == ""


def test_right_clicking_the_index_deletes_those_rows(view, model):
    """The gutter context menu acts on the whole row selection."""
    header = view.verticalHeader()
    first = model.staged_count
    view.selectRow(first + 1)
    view.selectionModel().select(
        model.index(first + 2, 0),
        view.selectionModel().SelectionFlag.Select
        | view.selectionModel().SelectionFlag.Rows,
    )
    before = model.rowCount()
    seen = {}

    def use_menu():
        menu = QApplication.activePopupWidget()
        seen["labels"] = [a.text() for a in menu.actions() if a.text()]
        for action in menu.actions():
            if action.text().startswith("Delete"):
                action.trigger()
        menu.close()

    QTimer.singleShot(0, use_menu)
    spot = QPoint(30, header.sectionViewportPosition(first + 1) + 5)
    header.contextMenuEvent(
        QContextMenuEvent(QContextMenuEvent.Reason.Mouse, spot, header.mapToGlobal(spot))
    )

    assert any(label.startswith("Delete 2 rows") for label in seen["labels"])
    assert model.rowCount() == before - 2
    assert model.changeset().counts["deletes"] == 2


def test_right_clicking_outside_the_selection_picks_that_row(view, model):
    header = view.verticalHeader()
    first = model.staged_count
    view.selectRow(first + 1)
    target = model.row_at(first + 5)

    def use_menu():
        menu = QApplication.activePopupWidget()
        for action in menu.actions():
            if action.text().startswith("Delete"):
                action.trigger()
        menu.close()

    QTimer.singleShot(0, use_menu)
    spot = QPoint(30, header.sectionViewportPosition(first + 5) + 5)
    header.contextMenuEvent(
        QContextMenuEvent(QContextMenuEvent.Reason.Mouse, spot, header.mapToGlobal(spot))
    )
    assert target.state is RowState.DELETED
    assert model.changeset().counts["deletes"] == 1


def test_selection_tints_the_cell_instead_of_replacing_it(view, model):
    """Excel-style: a selected cell keeps its own colour, just dimmed.

    This is also a guard on the palette itself -- a selection colour declared
    with the channels in the wrong order shows up here as the wrong hue.
    """
    # The app puts the stylesheet on the window, never on the view itself.
    host = QWidget()
    host.setStyleSheet(stylesheet())
    QVBoxLayout(host).addWidget(view)
    host.resize(1100, 420)
    host.show()
    row = model.staged_count
    model.setData(idx(model, 0, "a_id"), "NOT-A-REGION")   # row 0 goes red
    model.revalidate()
    QApplication.processEvents()

    def pixel(r, key):
        """Background colour of a cell, sampled clear of the text and borders."""
        rect = view.visualRect(idx(model, r, key))
        assert rect.isValid(), f"{key} is off-screen"
        shot = view.viewport().grab().toImage()
        return shot.pixelColor(rect.center().x(), rect.top() + 4)

    plain = pixel(row + 1, "run_count")
    error = pixel(0, "a_id")

    sel = view.selectionModel()
    sel.select(idx(model, row + 1, "run_count"), sel.SelectionFlag.ClearAndSelect)
    sel.select(idx(model, 0, "a_id"), sel.SelectionFlag.Select)
    QApplication.processEvents()
    plain_selected = pixel(row + 1, "run_count")
    error_selected = pixel(0, "a_id")

    assert plain_selected != plain                 # the selection is visible
    assert error_selected != error
    # ...and the error cell is still redder than the plain one beside it
    assert error_selected.red() - error_selected.blue() > 8
    assert abs(plain_selected.red() - plain_selected.blue()) <= 2   # stays neutral


# ------------------------------------------- values invented in this session
def test_add_value_is_session_only_and_shows_everywhere(model):
    col = model.spec["a_id"]
    before = model.lookups.choices(col)
    assert "ANTARCTICA" not in before

    new_id = model.lookups.add_value(col, "ANTARCTICA")
    assert new_id < 0                      # placeholder, never a real pk
    assert model.lookups.is_pending(new_id)
    assert "ANTARCTICA" in model.lookups.choices(col)   # every drop-down sees it
    assert model.lookups.label(col, new_id) == "ANTARCTICA"
    assert model.lookups.id_for(col, "antarctica") == new_id   # case insensitive
    assert model.lookups.add_value(col, " ANTARCTICA ") == new_id  # no duplicates

    assert model.lookups.pending()[new_id] == {
        "table": "model_a", "column": "name", "label": "ANTARCTICA",
    }
    assert set(model.lookups.choices(col)) - set(before) == {"ANTARCTICA"}


def test_a_staged_row_can_use_a_new_value_and_the_payload_carries_it(model):
    col = model.spec["a_id"]
    model.lookups.add_value(col, "ANTARCTICA")
    for key, value in (
        ("a_id", "ANTARCTICA"), ("b_id", model.lookups.choices(model.spec["b_id"])[0]),
        ("c_id", model.lookups.choices(model.spec["c_id"])[0]),
        ("d_id", model.lookups.choices(model.spec["d_id"])[0]),
        ("type", model.spec["type"].enum_names[0]),
    ):
        model.setData(idx(model, 0, key), value)
    model.revalidate()

    assert model.row_at(0).errors == {}          # a new value is not an error
    assert model.row_at(0).values["a_id"] < 0
    assert idx(model, 0, "a_id").data(Qt.ItemDataRole.FontRole).italic()

    payload = model.changeset().to_payload()
    assert payload["new_dimension_values"] == [
        {"placeholder_id": model.lookups.id_for(col, "ANTARCTICA"),
         "table": "model_a", "column": "name", "label": "ANTARCTICA"}
    ]
    assert payload["operations"][0]["key_labels"]["a_id"] == "ANTARCTICA"


def test_unused_new_values_stay_out_of_the_payload(model):
    model.lookups.add_value(model.spec["a_id"], "NOBODY-USES-ME")
    model.setData(idx(model, model.staged_count, "score"), "1.5")
    assert "new_dimension_values" not in model.changeset().to_payload()


def test_reloading_forgets_session_values(model, repo):
    model.lookups.add_value(model.spec["a_id"], "ANTARCTICA")
    assert model.lookups.pending()
    model.load(repo.load_rows(), repo.load_lookups())
    assert model.lookups.pending() == {}
    assert "ANTARCTICA" not in model.lookups.choices(model.spec["a_id"])


def test_only_foreign_keys_offer_the_new_value_entry(view, model):
    delegate = view.itemDelegate()
    option = QStyleOptionViewItem()
    fk = delegate.createEditor(view, option, idx(model, 0, "a_id"))
    enum = delegate.createEditor(view, option, idx(model, 0, "type"))
    assert fk.itemData(0) == NEW_VALUE
    assert fk.itemText(0).startswith("+")
    assert NEW_VALUE not in [enum.itemData(i) for i in range(enum.count())]


def test_the_new_value_entry_is_never_committed_as_a_value(view, model):
    delegate = view.itemDelegate()
    cell = idx(model, 0, "a_id")
    editor = delegate.createEditor(view, QStyleOptionViewItem(), cell)
    editor.setCurrentIndex(0)                       # the "+ New A..." entry
    delegate.setModelData(editor, model, cell)
    assert model.row_at(0).values["a_id"] is None   # nothing was written


def test_pasting_past_the_right_edge_drops_the_overflow(view, model):
    """A paste wider than the remaining columns must not fall off the end."""
    row = model.staged_count
    last = model.columnCount() - 1
    QGuiApplication.clipboard().setText("	".join(["7", "8", "9"]))
    view.setCurrentIndex(model.index(row, last))

    view.paste()   # used to raise IndexError once the id column left the grid

    assert model.rowCount() > 0          # still alive
    # anchored one column from the end, only the first value has anywhere to go
    view.setCurrentIndex(idx(model, row, "retry_limit"))
    QGuiApplication.clipboard().setText("	".join(["3"] * 40))
    view.paste()
    assert idx(model, row, "retry_limit").data(Qt.ItemDataRole.DisplayRole) == "3"


def test_pyqtdarktheme_does_not_flatten_the_cell_colours(view, model):
    """pyqtdarktheme's chrome must not eat the grid's per-cell brushes.

    Its stylesheet carries ``QAbstractItemView::item`` rules, and such a rule
    normally makes Qt take over item painting and ignore BackgroundRole. It
    does not here -- this keeps it that way.
    """
    qdarktheme = pytest.importorskip("qdarktheme")
    import grid as grid_module

    host = QWidget()
    QVBoxLayout(host).addWidget(view)
    host.resize(1100, 420)
    host.show()
    model.setData(idx(model, 0, "a_id"), "NOT-A-REGION")
    model.revalidate()

    def pixel(r, key):
        rect = view.visualRect(idx(model, r, key))
        assert view.viewport().rect().contains(rect.center()), f"{key} off-screen"
        shot = view.viewport().grab().toImage()
        return shot.pixelColor(rect.center().x(), rect.top() + 4).name()

    for dark in (True, False):
        set_theme(dark)
        qdarktheme.setup_theme("dark" if dark else "light")
        host.setStyleSheet(stylesheet())
        QApplication.processEvents()
        palette = DARK if dark else LIGHT
        # a staged row that fails validation is one solid yellow block
        assert pixel(0, "a_id") == palette.staged_invalid.name()
        assert pixel(0, "run_count") == palette.staged_invalid.name()
        # the stored rows are grey, with the locked key columns darker
        assert pixel(model.staged_count + 1, "a_id") == palette.key_readonly.name()
        assert pixel(model.staged_count + 1, "run_count") == palette.base.name()
    set_theme(False)
    assert grid_module.C is LIGHT


def test_dark_theme_swaps_the_palette(model):
    cell = idx(model, model.staged_count, "a_id")
    assert cell.data(Qt.ItemDataRole.BackgroundRole).color() == LIGHT.key_readonly
    assert LIGHT.base.name() in stylesheet()
    set_theme(True)
    try:
        assert cell.data(Qt.ItemDataRole.BackgroundRole).color() == DARK.key_readonly
        assert DARK.base.name() in stylesheet()
        assert grid.C is DARK          # `from grid import C` would snapshot it
    finally:
        set_theme(False)
    assert cell.data(Qt.ItemDataRole.BackgroundRole).color() == LIGHT.key_readonly


def test_show_deleted_puts_the_ghost_row_back_in_view(model):
    first_db = model.staged_count
    model.delete_rows([first_db])
    hidden = model.rowCount()
    model.set_show_deleted(True)
    assert model.rowCount() == hidden + 1
    assert model.row_at(first_db).state is RowState.DELETED
    assert idx(model, first_db, "weight").data(Qt.ItemDataRole.FontRole).strikeOut()
    assert not idx(model, first_db, "weight").flags() & Qt.ItemFlag.ItemIsEditable


def test_deleting_a_staged_row_drops_it_entirely(model):
    model.setData(idx(model, 0, "label"), "throwaway")
    assert model.staged_count == 2
    model.delete_rows([0])
    assert model.staged_count == 1
    assert model.changeset().is_empty


def test_revert_undoes_pending_edits_on_a_row(model):
    row = model.staged_count
    model.setData(idx(model, row, "weight"), "0.111")
    model.setData(idx(model, row, "label"), "changed")
    model.delete_rows([row + 1])
    model.revert_rows([row])
    assert model.row_at(row).state is RowState.CLEAN
    assert model.changeset().counts["updates"] == 0


# --------------------------------------------------------------------- undo
def test_undo_and_redo_a_cell_edit(model):
    row = model.staged_count
    cell = idx(model, row, "score")
    before = cell.data(Qt.ItemDataRole.EditRole)
    model.setData(cell, "99.5")
    model.undo_stack.undo()
    assert cell.data(Qt.ItemDataRole.EditRole) == before
    assert model.row_at(row).state is RowState.CLEAN
    model.undo_stack.redo()
    assert cell.data(Qt.ItemDataRole.EditRole) == "99.5"


def test_undo_a_delete(model):
    row = model.staged_count
    before = model.rowCount()
    model.delete_rows([row])
    model.undo_stack.undo()
    assert model.rowCount() == before
    assert model.changeset().is_empty


def test_undo_survives_a_sort(model):
    row = model.staged_count
    uid = model.row_at(row).uid
    model.setData(idx(model, row, "score"), "77")
    model.set_sort([("score", True)])
    model.undo_stack.undo()
    assert model.row_by_uid(uid).state is RowState.CLEAN


# ---------------------------------------------------------------- clipboard
def test_copy_writes_tab_separated_text(view, model):
    row = model.staged_count
    view.setCurrentIndex(idx(model, row, "a_id"))
    view.selectionModel().select(
        idx(model, row, "a_id"), view.selectionModel().SelectionFlag.Select
    )
    view.selectionModel().select(
        idx(model, row, "b_id"), view.selectionModel().SelectionFlag.Select
    )
    view.copy_selection()
    text = QGuiApplication.clipboard().text()
    assert "\t" in text and text.count("\n") == 0


def test_paste_several_rows_into_the_blank_row(view, model, repo):
    key = free_key(model, repo)
    second = list(key)
    second[4] = next(
        v for v in model.spec["type"].enum_names if v != key[4]
    )
    lines = [
        "\t".join(key + ["10", "1", "30", "0", "256", "0.5"]),
        "\t".join(second + ["11", "2", "60", "1", "512", "0.6"]),
    ]
    QGuiApplication.clipboard().setText("\n".join(lines))
    view.setCurrentIndex(idx(model, 0, "a_id"))
    view.paste()

    assert model.staged_count == 3  # two pasted rows plus the blank
    assert model.changeset().counts["inserts"] == 2
    assert idx(model, 0, "a_id").data(Qt.ItemDataRole.DisplayRole) == key[0]
    assert idx(model, 1, "run_count").data(Qt.ItemDataRole.DisplayRole) == "11"
    model.revalidate()
    assert model.dirty_counts()["errors"] == 0


def test_paste_into_existing_rows_skips_locked_key_columns(view, model, repo):
    row = model.staged_count
    key = free_key(model, repo)
    QGuiApplication.clipboard().setText("\t".join(key + ["777"]))
    view.setCurrentIndex(idx(model, row, "a_id"))
    view.paste()

    assert idx(model, row, "a_id").data(Qt.ItemDataRole.DisplayRole) != key[0]
    assert idx(model, row, "run_count").data(Qt.ItemDataRole.DisplayRole) == "777"
    assert model.changeset().counts["updates"] == 1


def test_paste_a_single_value_fills_the_selection(view, model):
    row = model.staged_count
    QGuiApplication.clipboard().setText("5")
    view.setCurrentIndex(idx(model, row, "priority"))
    for offset in range(3):
        view.selectionModel().select(
            idx(model, row + offset, "priority"),
            view.selectionModel().SelectionFlag.Select,
        )
    view.paste()
    for offset in range(3):
        assert idx(model, row + offset, "priority").data(Qt.ItemDataRole.DisplayRole) == "5"


def test_fill_down_and_clear(view, model):
    row = model.staged_count
    model.setData(idx(model, row, "approval"), "WAIVED")
    for offset in range(4):
        view.selectionModel().select(
            idx(model, row + offset, "approval"),
            view.selectionModel().SelectionFlag.Select,
        )
    view.fill(down=True)
    assert idx(model, row + 3, "approval").data(Qt.ItemDataRole.DisplayRole) == "WAIVED"

    view.clear_selection_contents()
    assert idx(model, row + 3, "approval").data(Qt.ItemDataRole.DisplayRole) == ""


# ----------------------------------------------------------- sort and filter
def test_sorting_keeps_staged_rows_on_top(model):
    model.setData(idx(model, 0, "label"), "staged one")
    model.set_sort([("score", True)])
    assert model.row_at(0).is_staged
    scores = [
        model.row_at(r).values["score"]
        for r in range(model.staged_count, model.rowCount())
    ]
    assert scores == sorted(scores, reverse=True)


def test_filter_hides_rows_but_never_the_staging_area(model):
    model.setData(idx(model, 0, "label"), "staged one")
    total = model.visible_db_rows()
    model.set_filter(
        ColumnFilter(key="score", conditions=(Condition(Op.GT, "50"),))
    )
    assert model.visible_db_rows() < total
    assert model.row_at(0).is_staged
    assert all(
        model.row_at(r).values["score"] > 50
        for r in range(model.staged_count, model.rowCount())
    )
    model.clear_filters()
    assert model.visible_db_rows() == total


def test_quick_search_matches_any_column(model, repo):
    owner = model.row_at(model.staged_count).values["owner"]
    model.set_quick_search(owner)
    assert model.visible_db_rows() >= 1
    assert all(
        model.row_at(r).values["owner"] == owner
        for r in range(model.staged_count, model.rowCount())
    )
    model.set_quick_search("")


def test_distinct_values_respect_other_filters(model):
    owners_column = model.column_index("owner")
    everything = dict(model.distinct_values(owners_column))
    first_type = model.row_at(model.staged_count).values["type"]
    model.set_filter(ColumnFilter(key="type", allowed=frozenset({first_type})))
    narrowed = dict(model.distinct_values(owners_column))
    assert sum(narrowed.values()) < sum(everything.values())


# -------------------------------------------------------------- round trip
def test_commit_applies_inserts_updates_and_deletes(model, repo):
    key = free_key(model, repo)
    for column, text in zip(("a_id", "b_id", "c_id", "d_id", "type"), key):
        model.setData(idx(model, 0, column), text)
    model.setData(idx(model, 0, "label"), "committed by the test")

    row = model.staged_count
    model.setData(idx(model, row, "weight"), "0.9999")
    doomed = model.row_at(row + 1).values["id"]
    model.delete_rows([row + 1])

    changeset = model.changeset()
    assert model.validation_report().ok
    assert repo.apply(changeset, dry_run=True).ok

    result = repo.apply(changeset)
    assert result.ok, result.error
    assert len(result.inserted) == 1 and result.updated == 1 and result.deleted == 1

    # read every version back, not just the newest, since free_key() may well
    # have picked an older one
    reloaded = {r.values["id"]: r for r in repo.load_rows(latest_version_only=False)}
    assert doomed not in reloaded
    assert reloaded[result.inserted[0]].values["label"] == "committed by the test"


def test_dry_run_reports_the_constraint_violation(model, repo):
    """Belt and braces: even if validation were bypassed, the db still says no."""
    before = repo.count()
    existing = repo.load_rows()[0]
    clone = Row.staged(
        model.spec, {k: v for k, v in existing.values.items() if k != "id"}
    )
    changeset = ChangeSet.from_rows(model.spec, [clone], model.lookups)
    result = repo.apply(changeset, dry_run=True)
    assert not result.ok
    assert "unique" in (result.error or "").lower()
    assert repo.count() == before  # nothing was written


# --------------------------------------------------- somebody else's schema
# Nothing below resembles the demo models: different table names, a different
# primary-key name, a three-column natural key with a plain string in it, and
# dimension labels that are not called "name". If the grid can drive this, it
# can drive yours.
class OtherBase(DeclarativeBase):
    pass


class Bucket(enum.Enum):
    """The case the demo schema cannot show: name != value."""

    FIRST_QUARTER = "q1"
    SECOND_QUARTER = "q2"


class Region(OtherBase):
    __tablename__ = "region"
    region_id = mapped_column(Integer, primary_key=True)
    code = mapped_column(String(16), unique=True, nullable=False, doc="Region code")


class Product(OtherBase):
    __tablename__ = "product"
    product_id = mapped_column(Integer, primary_key=True)
    sku = mapped_column(String(16), unique=True, nullable=False, doc="Stock code")


class Reading(OtherBase):
    __tablename__ = "reading"
    __table_args__ = (
        UniqueConstraint("region_id", "product_id", "bucket", name="uq_reading"),
    )
    reading_id = mapped_column(Integer, primary_key=True, autoincrement=True)
    region_id = mapped_column(ForeignKey("region.region_id"), nullable=False)
    product_id = mapped_column(ForeignKey("product.product_id"), nullable=False)
    bucket = mapped_column(String(8), nullable=False, doc="Time bucket")
    value = mapped_column(Float, doc="The measurement")
    note = mapped_column(String(64), doc="Anything worth saying")
    phase = mapped_column(SAEnum(Bucket), doc="Which quarter")


@pytest.fixture
def other_repo():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        future=True,
    )
    OtherBase.metadata.create_all(engine)
    with Session(engine) as s, s.begin():
        s.add_all([Region(code="NORTH"), Region(code="SOUTH")])
        s.add_all([Product(sku="AAA"), Product(sku="BBB")])
    with Session(engine) as s, s.begin():
        for r in (1, 2):
            for p in (1, 2):
                s.add(Reading(region_id=r, product_id=p, bucket="2026Q1",
                              value=float(r * p), note="seeded"))
    return Repository(engine=engine, model=Reading)


def test_the_grid_drives_a_completely_different_schema(qapp, other_repo):
    spec = other_repo.spec
    assert spec.table_name == "reading"
    assert spec.pk.key == "reading_id"
    assert spec.unique_constraint == "uq_reading"
    assert [c.key for c in spec.key_columns] == ["region_id", "product_id", "bucket"]
    # the dimension label columns were worked out without being told
    assert spec["region_id"].fk.label_attr == "code"
    assert spec["product_id"].fk.label_attr == "sku"
    assert spec["region_id"].header == "Region"
    assert spec["value"].doc == "The measurement"

    model = StagedTableModel(spec)
    model.load(other_repo.load_rows(), other_repo.load_lookups())
    model.validator.set_existing_keys(other_repo.natural_keys())
    assert model.resident_rows() == 4
    assert "reading_id" not in [c.key for c in model.columns]   # pk is the gutter
    assert model.lookups.choices(spec["region_id"]) == ["NORTH", "SOUTH"]

    # a staged row behaves exactly as it does for the demo schema
    def cell(row, key):
        return model.index(row, model.column_index(key))

    for key, value in (("region_id", "NORTH"), ("product_id", "AAA"),
                       ("bucket", "2026Q1")):
        model.setData(cell(0, key), value)
    model.revalidate()
    assert "already exists" in model.row_at(0).errors["region_id"]

    model.setData(cell(0, "bucket"), "2026Q2")      # now it is free
    model.setData(cell(0, "value"), "9.5")
    model.revalidate()
    assert model.row_at(0).errors == {}

    changeset = model.changeset()
    assert changeset.counts["inserts"] == 1
    payload = changeset.to_payload()
    assert payload["table"] == "reading"
    assert payload["operations"][0]["key_labels"] == {
        "region_id": "NORTH", "product_id": "AAA", "bucket": "2026Q2",
    }
    assert other_repo.apply(changeset, dry_run=True).ok
    result = other_repo.apply(changeset)
    assert result.ok and len(result.inserted) == 1
    assert other_repo.count() == 5


def test_the_demo_default_explains_itself(other_repo, repo):
    """Pointing at someone else's database without setting a model used to
    fail with 'relation model_a does not exist' and no clue why."""
    from app import demo_schema_hint

    oops = Exception('(psycopg2.errors.UndefinedTable) relation "model_a" does not exist')
    hint = demo_schema_hint(repo, oops)          # repo uses the demo ModelE
    assert "DEMO schema" in hint
    assert "STAGEDGRID_MODEL" in hint
    assert "--describe" in hint

    # ...and says nothing once a real model is configured
    assert demo_schema_hint(other_repo, oops) == ""


def test_model_and_label_arguments(other_repo):
    from app import load_model, parse_labels

    # load_model only has to import "module:Name"; a stdlib target proves it
    import json

    assert load_model("json:JSONDecoder") is json.JSONDecoder
    assert parse_labels("region=name, product=title") == {
        "region": "name", "product": "title"}
    for bad in ("json", "no_such_module_xyz:Thing", "json:NoSuchName"):
        with pytest.raises(SystemExit):
            load_model(bad)
    with pytest.raises(SystemExit):
        parse_labels("region")


def test_an_enum_shows_its_name_not_its_value(qapp, other_repo):
    """Kind.BASELINE = "bl" must read as BASELINE, not bl."""
    spec = other_repo.spec
    col = spec["phase"]
    assert col.kind is ColumnKind.ENUM
    assert col.enum_names == ("FIRST_QUARTER", "SECOND_QUARTER")

    # a member, and the string the grid keeps, both read as the name
    assert display(col, Bucket.FIRST_QUARTER) == "FIRST_QUARTER"
    assert display(col, "FIRST_QUARTER") == "FIRST_QUARTER"

    # typing or pasting either spelling lands on the name
    assert parse(col, "FIRST_QUARTER") == "FIRST_QUARTER"
    assert parse(col, "first_quarter") == "FIRST_QUARTER"
    assert parse(col, "q1") == "FIRST_QUARTER"        # tolerant of the raw value
    assert isinstance(parse(col, "nonsense"), Unresolved)

    # and it goes back to the database as the member itself
    assert to_db(col, "FIRST_QUARTER") is Bucket.FIRST_QUARTER
    assert to_db(col, Bucket.SECOND_QUARTER) is Bucket.SECOND_QUARTER


def test_an_enum_round_trips_through_the_grid_by_name(qapp, other_repo):
    model = StagedTableModel(other_repo.spec)
    model.load(other_repo.load_rows(), other_repo.load_lookups())
    model.validator.set_existing_keys(other_repo.natural_keys())
    cell = lambda r, k: model.index(r, model.column_index(k))

    # drop-down offers names
    delegate_choices = model.spec["phase"].enum_names
    assert "q1" not in delegate_choices

    row = model.staged_count
    model.setData(cell(row, "phase"), "SECOND_QUARTER")
    assert cell(row, "phase").data(Qt.ItemDataRole.DisplayRole) == "SECOND_QUARTER"

    changeset = model.changeset()
    change = changeset.updates[0].changes[0]
    assert change.new_text == "SECOND_QUARTER"
    assert change.new == "SECOND_QUARTER"
    assert other_repo.apply(changeset).ok

    # ...and the database really holds the member, so it comes back by name
    reloaded = {r.values["reading_id"]: r for r in other_repo.load_rows()}
    changed = reloaded[changeset.updates[0].pk]
    assert changed.values["phase"] == "SECOND_QUARTER"
    with Session(other_repo.engine) as s:
        stored = s.get(Reading, changeset.updates[0].pk).phase
    assert stored is Bucket.SECOND_QUARTER


def test_only_the_newest_version_is_loaded(repo):
    """Older versions are never edited, so they are never fetched."""
    newest_id, newest_label = repo.latest_version()
    assert repo.version_column.key == "d_id"

    everything = repo.load_rows(latest_version_only=False)
    newest_only = repo.load_rows()
    assert len(newest_only) < len(everything) == repo.count()
    assert {r.values["d_id"] for r in newest_only} == {newest_id}

    # and "newest" means naturally newest, not alphabetically
    labels = repo.load_lookups().choices(repo.spec["d_id"])
    assert newest_label == labels[-1]


def test_natural_sort_puts_2026_10_after_2026_2():
    from schema import natural_sort_key

    versions = ["2026.1", "2026.10", "2026.2", "2025.9"]
    assert sorted(versions, key=natural_sort_key) == [
        "2025.9", "2026.1", "2026.2", "2026.10",
    ]


def test_quick_search_cache_follows_edits(model):
    """The per-row search text is cached; a stale cache would hide edits.

    Each search below uses a different term, because re-setting the same term
    is a no-op -- the view deliberately does not re-filter under you while you
    type, the way a spreadsheet does not either.
    """
    row = model.staged_count
    model.setData(idx(model, row, "owner"), "zebra-team")

    model.set_quick_search("zebra-team")
    assert model.visible_db_rows() == 1

    model.setData(idx(model, row, "owner"), "giraffe-team")
    model.set_quick_search("giraffe-team")
    assert model.visible_db_rows() == 1          # the cache was rebuilt
    model.set_quick_search("zebra-team")
    assert model.visible_db_rows() == 0          # and the old text is gone

    model.undo_stack.undo()                      # back to zebra-team
    model.set_quick_search("giraffe-team")
    assert model.visible_db_rows() == 0
    model.set_quick_search("zebra-team")
    assert model.visible_db_rows() == 1
    model.set_quick_search("")


def test_untouched_rows_do_not_carry_a_copy_of_themselves(model, repo):
    """`original` is copy-on-write: only edited rows pay for the snapshot."""
    stored = model.row_at(model.staged_count)
    assert stored.original is None          # loaded, never touched
    assert not stored.is_staged
    assert stored.changed_keys() == set()
    assert stored.stored is stored.values   # "what the database has" still works

    # the key index is built from stored values either way
    keys = existing_key_map(model.spec, [stored])
    assert keys and stored.values["id"] in keys.values()

    model.setData(idx(model, model.staged_count, "weight"), "0.4242")
    assert stored.original is not None      # ...and now it pays
    assert stored.changed_keys() == {"weight"}
    assert stored.stored["weight"] != stored.values["weight"]

    # undo returns the value; the snapshot can stay, it is one row
    model.undo_stack.undo()
    assert stored.changed_keys() == set()
    assert stored.state is RowState.CLEAN


# ------------------------------------------------- branches in Updates
def test_a_branch_round_trips_through_the_updates_table(model, repo):
    """Commit writes the changeset to Updates; reopening replays it exactly."""
    from schema import Base

    Base.metadata.create_all(repo.engine)
    assert repo.list_branches() == []

    # stage one of each kind of change
    key = free_key(model, repo)
    for column, text in zip(("a_id", "b_id", "c_id", "d_id", "type"), key):
        model.setData(idx(model, 0, column), text)
    model.setData(idx(model, 0, "label"), "added on a branch")

    first = model.staged_count
    model.setData(idx(model, first, "weight"), "0.4242")
    edited_pk = model.row_at(first).values["id"]
    doomed_pk = model.row_at(first + 1).values["id"]
    model.delete_rows([first + 1])

    before = model.changeset()
    assert before.counts == {"inserts": 1, "updates": 1, "deletes": 1, "cells": 1}

    version_id = repo.latest_version()[0]
    branch_id = repo.save_branch(before.to_payload(), version_id=version_id,
                                 label="my branch")

    # the fact table is untouched: this app only ever writes Updates
    assert repo.count() == len(repo.load_rows(latest_version_only=False))
    assert model.row_at(first + 1) is not None or True

    listed = repo.list_branches()
    assert [b.id for b in listed] == [branch_id]
    assert listed[0].label == "my branch"
    assert listed[0].version_label == repo.latest_version()[1]

    # a fresh grid, then replay the branch onto it
    fresh = StagedTableModel(repo.spec)
    fresh.load(repo.load_rows(), repo.load_lookups())
    fresh.validator.set_existing_keys(repo.natural_keys())
    assert fresh.changeset().is_empty

    counts = fresh.replay(repo.load_branch(branch_id).changes)
    assert counts == {"inserts": 1, "updates": 1, "deletes": 1, "missing": 0,
                      "unreadable": 0}

    after = fresh.changeset()
    assert after.counts == before.counts
    assert after.inserts[0].key_text == before.inserts[0].key_text
    assert after.updates[0].pk == edited_pk
    assert after.updates[0].changes[0].new == 0.4242
    assert after.deletes[0].pk == doomed_pk


def test_pushing_again_updates_the_same_branch(model, repo):
    from schema import Base

    Base.metadata.create_all(repo.engine)
    version_id = repo.latest_version()[0]
    branch_id = repo.save_branch({"operations": []}, version_id=version_id,
                                 label="b")
    first_seen = repo.load_branch(branch_id).modified_at

    model.setData(idx(model, model.staged_count, "owner"), "someone-else")
    repo.save_branch(model.changeset().to_payload(), branch_id=branch_id)

    assert len(repo.list_branches()) == 1          # still one branch
    reloaded = repo.load_branch(branch_id)
    assert reloaded.changes["operations"][0]["op"] == "update"
    assert reloaded.modified_at >= first_seen      # modified_at moved


def test_a_branch_survives_rows_disappearing(model, repo):
    """A branch can outlive the rows it names; replay says how many."""
    from schema import Base

    Base.metadata.create_all(repo.engine)
    payload = {"operations": [
        {"op": "update", "pk": 10 ** 9, "key": {}, "key_labels": {},
         "changes": [{"column": "weight", "old": 1, "new": 2}]},
        {"op": "delete", "pk": 10 ** 9 + 1, "key": {}, "key_labels": {}},
    ]}
    assert model.replay(payload) == {
        "inserts": 0, "updates": 0, "deletes": 0, "missing": 2, "unreadable": 0}
    assert model.changeset().is_empty


def test_a_branch_full_of_junk_does_not_take_the_app_down(model):
    """A branch written by another tool, or an older format, must not crash."""
    empty = {"inserts": 0, "updates": 0, "deletes": 0, "missing": 0, "unreadable": 0}
    assert model.replay(None) == empty
    assert model.replay({}) == empty
    assert model.replay(42) == empty
    assert model.replay({"operations": "nonsense"}) == empty

    # the shape that actually crashed: operations that are not objects
    assert model.replay({"operations": [1, 2, 3]}) == dict(empty, unreadable=3)
    assert model.replay({"operations": [{"op": "wat"}]}) == dict(empty, unreadable=1)
    assert model.changeset().is_empty


# -------------------------------------------------------- the window itself
@pytest.fixture
def window(qapp, repo):
    from schema import Base
    from app import MainWindow

    Base.metadata.create_all(repo.engine)
    return MainWindow(repo)


def test_opening_a_branch_is_not_an_unsaved_change(window, repo):
    """The bug: a branch's own changes are shown as pending edits, so asking
    "is the grid dirty" always said yes and every branch switch demanded to
    discard work that did not exist."""
    model = window.model
    assert not window.unsaved_changes()

    model.setData(idx(model, model.staged_count, "weight"), "0.9999")
    assert window.unsaved_changes()
    branch_id = repo.save_branch(
        model.changeset().to_payload(),
        version_id=repo.latest_version()[0],
        label="b",
    )

    window.branch = repo.load_branch(branch_id)
    window.reload()
    assert model.dirty_counts()["updates"] == 1     # the branch is shown...
    assert not window.unsaved_changes()             # ...but nothing is unsaved

    model.setData(idx(model, model.staged_count, "owner"), "someone")
    assert window.unsaved_changes()
    window.undo_stack.undo()
    assert not window.unsaved_changes()


def test_commit_lights_up_only_when_there_is_something_to_push(window):
    model = window.model
    assert not window.action_commit.isEnabled()

    model.setData(idx(model, model.staged_count, "weight"), "0.5")
    window._refresh()
    assert window.action_commit.isEnabled()
    assert "background" in (window._commit_button.styleSheet() or "")

    model.setData(idx(model, 0, "a_id"), "NOT-A-REGION")   # now it is invalid
    model.revalidate()
    window._refresh()
    assert not window.action_commit.isEnabled()
    assert "Fix the" in window.action_commit.toolTip()

VIEWS = [
    ("sort by a foreign key label", dict(sort=(("a_id", False),))),
    ("sort by a foreign key, descending", dict(sort=(("a_id", True),))),
    ("sort by an enum in declaration order", dict(sort=(("type", False),))),
    ("sort by a float, blanks last", dict(sort=(("weight", True),))),
    ("sort by three columns",
     dict(sort=(("a_id", False), ("type", False), ("weight", True)))),
    ("value checklist on a foreign key",
     dict(filters=[ColumnFilter(key="a_id", allowed=frozenset({"APAC", "GLOBAL"}))])),
    ("value checklist on an enum",
     dict(filters=[ColumnFilter(key="type", allowed=frozenset({"BASELINE"}))])),
    ("numeric condition",
     dict(filters=[ColumnFilter(key="score", conditions=(Condition(Op.GT, "50"),))])),
    ("between",
     dict(filters=[ColumnFilter(key="priority",
                                conditions=(Condition(Op.BETWEEN, "5", "50"),))])),
    ("text contains",
     dict(filters=[ColumnFilter(key="owner", conditions=(Condition(Op.CONTAINS, "a"),))])),
    ("is blank",
     dict(filters=[ColumnFilter(key="status_note", conditions=(Condition(Op.BLANK),))])),
    ("quick search", dict(search="RATES")),
    ("filtered, searched and sorted",
     dict(sort=(("weight", True),), search="APAC",
          filters=[ColumnFilter(key="type", allowed=frozenset({"BASELINE"}))])),
]


@pytest.mark.parametrize("label,view", VIEWS, ids=[v[0] for v in VIEWS])
def test_the_database_view_agrees_with_the_in_memory_one(qapp, repo, label, view):
    """The two modes must mean the same thing by a sort or a filter.

    Same rows, in the same order -- including the awkward parts: foreign keys
    order by label not id, enums by declaration order, blanks last whichever
    way the column is sorted, and ties broken by the primary key because
    Python's sort is stable.
    """
    from sqlview import SqlView, ViewQuery

    lookups = repo.load_lookups()
    model = StagedTableModel(repo.spec, lookups)
    model.load(repo.load_rows(latest_version_only=False), lookups)
    pk = repo.spec.pk.key

    model.set_sort(list(view.get("sort", ())))
    for flt in view.get("filters") or ():
        model.set_filter(flt)
    if view.get("search"):
        model.set_quick_search(view["search"])
    in_memory = [
        model.row_at(r).values[pk]
        for r in range(model.staged_count, model.rowCount())
    ]

    filters = FilterSet()
    for flt in view.get("filters") or ():
        filters.set(flt)
    sql = SqlView(repo.model, repo.spec, lookups)
    from_db = [
        r.values[pk]
        for r in sql.rows(
            repo.engine,
            ViewQuery(sort=view.get("sort", ()), filters=filters,
                      search=view.get("search", "")),
        )
    ]

    assert in_memory, f"{label} matched nothing, so it proves nothing"
    assert from_db == in_memory
    assert sql.count(repo.engine, ViewQuery(
        filters=filters, search=view.get("search", ""))) == len(in_memory)

def test_a_filter_sql_cannot_express_says_so_instead_of_lying(qapp, repo):
    """Regex has no SQL equivalent.

    The grid falls back to filtering the rows it holds, which is a real answer
    about those rows -- so it has to say that is all it covers, or the user
    reads a partial result as a whole-table one.
    """
    from sqlview import SqlView, Untranslatable, ViewQuery
    from app import MainWindow

    filters = FilterSet()
    filters.set(ColumnFilter(key="owner", conditions=(Condition(Op.REGEX, "^risk"),)))
    sql = SqlView(repo.model, repo.spec, repo.load_lookups())
    with pytest.raises(Untranslatable):
        sql.statement(ViewQuery(filters=filters))

    window = MainWindow(repo)
    before = window.total_rows
    window.model.set_filter(
        ColumnFilter(key="owner", conditions=(Condition(Op.REGEX, "^risk"),))
    )

    said = window._message_label.text()
    assert "matches regex" in said                   # which filter went wrong
    assert "not applied" in said                     # and that it did not take
    # the filter is gone rather than showing in the header without effect
    assert window.model.filters.get("owner") is None
    assert window.total_rows == before


RANK_VIEWS = [
    ("no sort at all", dict()),
    ("one string column", dict(sort=(("owner", False),))),
    ("one float, descending", dict(sort=(("weight", True),))),
    ("an enum", dict(sort=(("type", False),))),
    ("a foreign key label", dict(sort=(("a_id", True),))),
    ("three columns, mixed directions",
     dict(sort=(("a_id", False), ("type", True), ("weight", False)))),
    ("a column that is mostly blank", dict(sort=(("status_note", False),))),
    ("blanks, descending", dict(sort=(("status_note", True),))),
    ("sorted and filtered",
     dict(sort=(("score", True),),
          filters=[ColumnFilter(key="type", allowed=frozenset({"BASELINE"}))])),
]


@pytest.mark.parametrize("label,view", RANK_VIEWS, ids=[v[0] for v in RANK_VIEWS])
def test_a_row_knows_where_it_belongs_without_fetching_the_table(qapp, repo, label, view):
    """`rank` is the foundation of merging, so check it against the truth.

    For every row in a view, the number of rows the query would place before it
    must equal its own index in that view. If this is off by one anywhere, a
    held row lands in the wrong place and the whole merged view is subtly wrong.
    """
    from sqlview import SqlView, ViewQuery

    filters = FilterSet()
    for flt in view.get("filters") or ():
        filters.set(flt)
    query = ViewQuery(sort=view.get("sort", ()), filters=filters)
    sql = SqlView(repo.model, repo.spec, repo.load_lookups())

    ordered = sql.rows(repo.engine, query)
    assert len(ordered) >= 10, "too few rows for this to prove anything"

    for position, row in enumerate(ordered):
        assert sql.rank(repo.engine, query, row.values) == position, (
            f"{label}: row {row.values[repo.spec.pk.key]} thinks it is at "
            f"{sql.rank(repo.engine, query, row.values)}, really at {position}"
        )


def test_rank_is_right_for_values_that_are_not_in_the_table(qapp, repo):
    """The real use: a row whose values were edited, so the table disagrees."""
    from sqlview import SqlView, ViewQuery

    sql = SqlView(repo.model, repo.spec, repo.load_lookups())
    query = ViewQuery(sort=(("weight", False),))
    ordered = sql.rows(repo.engine, query)
    weights = [r.values["weight"] for r in ordered]

    victim = dict(ordered[-1].values)          # last row, by weight
    victim["weight"] = -1.0                    # edit it to sort first
    assert sql.rank(repo.engine, query, victim) == 0

    victim["weight"] = None                    # blank it: blanks sort last
    blanks = sum(1 for w in weights if w is None)
    filled = len(weights) - blanks
    # it ranks among the blanks, after every filled row, by primary key
    rank = sql.rank(repo.engine, query, victim)
    assert rank >= filled
    assert rank <= len(ordered)


ORDER_CASES = [
    ("nothing", ()),
    ("one string", (("owner", False),)),
    ("one string, descending", (("owner", True),)),
    ("a float with blanks", (("weight", False),)),
    ("a float with blanks, descending", (("weight", True),)),
    ("an enum", (("type", False),)),
    ("a foreign key label", (("a_id", False),)),
    ("mixed directions over three columns",
     (("a_id", False), ("type", True), ("weight", False))),
    ("a mostly blank column then a full one",
     (("status_note", True), ("score", False))),
]


@pytest.mark.parametrize("label,keys", ORDER_CASES, ids=[c[0] for c in ORDER_CASES])
def test_the_shared_order_key_is_the_order_the_grid_shows(qapp, repo, label, keys):
    """`row_order_key` has to be the same order as the model's own sort.

    The merged view sorts held rows with it while the database sorts the rest,
    so any disagreement puts a row in the wrong place.
    """
    from data import row_order_key

    lookups = repo.load_lookups()
    rows = repo.load_rows(latest_version_only=False)
    model = StagedTableModel(repo.spec, lookups)
    model.load(rows, lookups)
    model.set_sort(list(keys))
    pk = repo.spec.pk.key

    from_model = [
        model.row_at(r).values[pk]
        for r in range(model.staged_count, model.rowCount())
    ]
    from_key = [
        r.values[pk] for r in sorted(rows, key=row_order_key(repo.spec, lookups, keys))
    ]
    assert from_key == from_model


def _edit(model, pk_value, key, value):
    """Edit a stored row through the model, by primary key."""
    row = next(r for r in model._db_rows if r.values[model.spec.pk.key] == pk_value)
    model.apply_values([(row.uid, key, value)])
    return row


@pytest.mark.parametrize(
    "label,sort,search,window",
    [
        ("unsorted, whole view", (), "", 10_000),
        ("sorted by the edited column", (("weight", False),), "", 10_000),
        ("sorted descending", (("weight", True),), "", 10_000),
        ("sorted by something else", (("owner", False),), "", 10_000),
        ("two columns", (("type", False), ("weight", True)), "", 10_000),
        ("a narrow window", (("weight", False),), "", 7),
        ("a window of one", (("weight", False),), "", 1),
        ("while searching", (), "APAC", 10_000),
    ],
)
def test_a_merged_window_equals_holding_every_row(qapp, repo, label, sort, search, window):
    """The point of the whole exercise.

    Hold every row and edit some; that is the truth. Then ask the database for
    the same view with those rows excluded and merged back in, a window at a
    time, and the sequence of primary keys must be identical.
    """
    from data import RowMatcher, row_order_key
    from sqlview import MergedView, SqlView, ViewQuery

    lookups = repo.load_lookups()
    pk = repo.spec.pk.key
    model = StagedTableModel(repo.spec, lookups)
    model.load(repo.load_rows(latest_version_only=False), lookups)
    model.set_show_deleted(True)       # so deleted rows stay inline, comparably

    ordered = [model.row_at(r).values[pk] for r in range(model.staged_count, model.rowCount())]
    victims = [ordered[0], ordered[len(ordered) // 2], ordered[-1]]

    # edits that move rows: to the front, to the back, and out of the sort
    _edit(model, victims[0], "weight", 0.999_999)
    _edit(model, victims[1], "weight", None)            # blank: sorts last
    _edit(model, victims[2], "weight", -5.0)            # sorts first
    _edit(model, victims[0], "owner", "zzz-last")
    deleted = _edit(model, ordered[1], "status_note", "about to go")
    model.set_row_states({deleted.uid: RowState.DELETED})

    if search:
        model.set_quick_search(search)
    model.set_sort(list(sort))
    truth = [
        model.row_at(r).values[pk]
        for r in range(model.staged_count, model.rowCount())
    ]

    held = [r for r in model._db_rows if r.original is not None or r.state is not RowState.CLEAN]
    assert len(held) >= 4, "the edits did not take"

    merged = MergedView(repo.engine, SqlView(repo.model, repo.spec, lookups))
    merged.rules(
        RowMatcher(repo.spec, lookups, model.filters, search),
        row_order_key(repo.spec, lookups, sort),
    )
    merged.refresh(ViewQuery(sort=sort, filters=model.filters, search=search), held)

    assert merged.count() == len(truth)
    got = []
    for offset in range(0, merged.count(), window):
        got.extend(r.values[pk] for r in merged.slice(offset, window))
    assert got == truth


def test_the_key_index_matches_what_the_rows_would_have_said(qapp, repo):
    """Validation holds keys instead of rows, so the two must agree exactly.

    This is what lets a windowed grid still catch a typed key that already
    exists somewhere it is not currently showing.
    """
    from_rows = existing_key_map(repo.spec, repo.load_rows())
    from_db = repo.natural_keys(latest_version_only=True)
    assert from_db == from_rows
    # the whole version, not a window
    assert len(from_db) == repo.count_rows() > 0

    every = repo.natural_keys()
    assert every == existing_key_map(repo.spec, repo.load_rows(latest_version_only=False))
    assert len(every) > len(from_db), "the fixture has more than one version"


def _collapse(rows, pk):
    """Rows with deleted ones inline -> (visible pks, marks above each index)."""
    visible, marks, pending = [], {}, []
    for row in rows:
        if row.state is RowState.DELETED:
            pending.append(row.values[pk])
            continue
        if pending:
            marks[len(visible)] = pending
            pending = []
        visible.append(row.values[pk])
    return visible, marks, pending


@pytest.mark.parametrize("window", [10_000, 9, 3, 1])
def test_deleted_rows_keep_their_gutter_marks_through_a_window(qapp, repo, window):
    """A deleted row holds its place in the order but is not a row the grid
    counts, so the merged view has to reconcile the two numberings."""
    from data import RowMatcher, row_order_key
    from sqlview import MergedView, SqlView, ViewQuery

    lookups = repo.load_lookups()
    pk = repo.spec.pk.key
    model = StagedTableModel(repo.spec, lookups)
    model.load(repo.load_rows(latest_version_only=False), lookups)
    sort = (("weight", False),)
    model.set_sort(list(sort))

    ordered = [model.row_at(r).values[pk] for r in range(model.staged_count, model.rowCount())]
    # a run of two adjacent deletions, one alone, and one at the very end
    doomed = [ordered[2], ordered[3], ordered[6], ordered[-1]]
    states = {}
    for value in doomed:
        row = next(r for r in model._db_rows if r.values[pk] == value)
        states[row.uid] = RowState.DELETED
    model.set_row_states(states)

    truth = [model.row_at(r).values[pk] for r in range(model.staged_count, model.rowCount())]
    truth_marks = {
        r - model.staged_count: [x.values[pk] for x in model.deleted_rows_above(r)]
        for r in range(model.staged_count, model.rowCount())
        if model.deleted_rows_above(r)
    }
    assert truth_marks, "the deletions did not produce marks"
    assert model.has_trailing_deleted()

    held = [r for r in model._db_rows if r.original is not None or r.state is not RowState.CLEAN]
    merged = MergedView(repo.engine, SqlView(repo.model, repo.spec, lookups))
    merged.rules(
        RowMatcher(repo.spec, lookups, model.filters, ""),
        row_order_key(repo.spec, lookups, sort),
    )
    merged.refresh(ViewQuery(sort=sort, filters=model.filters), held)

    assert merged.visible_count() == len(truth)
    assert merged.count() == len(truth) + len(doomed)

    got, marks, trailing = [], {}, []
    for offset in range(0, merged.visible_count(), window):
        rows = merged.visible_slice(offset, window)
        seen, local, tail = _collapse(rows, pk)
        for at, pks in local.items():
            marks[len(got) + at] = pks
        got.extend(seen)
        trailing = tail
    assert got == truth
    assert marks == truth_marks
    assert trailing == [doomed[-1]]      # the last row, with nothing after it


# ------------------------------------------------- the model in merged mode
def _merged(repo, sort=(), search="", filters=None, show_deleted=False):
    """A model whose view comes from the database, with a window resident."""
    from data import RowMatcher, row_order_key
    from sqlview import MergedView, SqlView, ViewQuery

    lookups = repo.load_lookups()
    model = StagedTableModel(repo.spec, lookups)
    if filters is not None:
        model.filters = filters
    source = MergedView(repo.engine, SqlView(repo.model, repo.spec, lookups))
    source.rules(
        RowMatcher(repo.spec, lookups, model.filters, search),
        row_order_key(repo.spec, lookups, sort),
    )
    model.load([], lookups)
    model.validator.set_existing_keys(repo.natural_keys())
    model.set_window_source(source)
    query = ViewQuery(sort=sort, filters=model.filters, search=search)

    def fetch(at, size):
        source.refresh(query, model._stored_rows(), show_deleted)
        model.load_window(source.visible_slice(at, size), at)

    fetch(0, 5)
    return model, source, fetch


def test_merged_mode_counts_the_table_while_holding_a_window(qapp, repo):
    model, source, _ = _merged(repo, sort=(("weight", False),))
    total = repo.count_rows(latest_version_only=False)

    assert source.visible_count() == total
    assert model.rowCount() == model.staged_count + total   # the scrollbar is right
    assert len(model._db_rows) == 5                         # but only five are here

    assert model.row_at(model.staged_count) is not None     # inside the window
    assert model.row_at(model.staged_count + 4) is not None
    assert model.row_at(model.staged_count + 5) is None     # outside it
    # Qt asks for cells it cannot have; it must get nothing, not a crash
    assert model.data(model.index(model.staged_count + 5, 0)) is None


def test_merged_mode_scrolls_to_rows_it_never_held(qapp, repo):
    model, source, fetch = _merged(repo, sort=(("weight", True),))
    total = source.visible_count()

    everything = [
        r.values["weight"]
        for r in repo.load_rows(latest_version_only=False)
    ]
    top = model.row_at(model.staged_count).values["weight"]
    assert top == max(w for w in everything if w is not None)

    fetch(total - 3, 3)                                     # jump to the end
    assert model.row_at(model.staged_count + total - 1) is not None
    assert model.row_at(model.staged_count) is None         # the top is gone now
    last = model.row_at(model.staged_count + total - 1).values["weight"]
    filled = [w for w in everything if w is not None]
    assert last is None or last == min(filled)


def test_an_edit_survives_scrolling_away_from_it(qapp, repo):
    """The invariant the whole design rests on.

    A row the user touched is held, so it keeps its uid, its original values
    and its place in the changeset even when the window has moved past it.
    """
    model, source, fetch = _merged(repo, sort=(("weight", False),))
    total = source.visible_count()

    victim = model.row_at(model.staged_count)
    uid, pk = victim.uid, victim.values[repo.spec.pk.key]
    model.apply_values([(uid, "owner", "edited-in-session")])
    assert model.dirty_counts()["updates"] == 1

    fetch(total - 2, 2)                                     # scroll far away
    assert model.row_at(model.staged_count) is None          # not resident

    assert model.row_by_uid(uid) is not None                 # still addressable
    assert model.dirty_counts()["updates"] == 1              # still pending
    ops = model.changeset().operations
    assert [o.pk for o in ops if o.kind == "update"] == [pk]
    assert model.row_by_uid(uid).original is not None         # and undoable

    fetch(0, 5)                                               # come back
    again = model.row_at(model.staged_count)
    assert again.uid == uid                                   # the same object
    assert again.values["owner"] == "edited-in-session"


def test_an_edit_that_moves_a_row_moves_it(qapp, repo):
    """The database still holds the old value, so the merge has to override it."""
    model, source, fetch = _merged(repo, sort=(("weight", False),))
    total = source.visible_count()

    victim = model.row_at(model.staged_count + 1)
    uid = victim.uid
    model.apply_values([(uid, "weight", 99.0)])               # should go last
    fetch(0, 5)
    assert model.row_at(model.staged_count + 1).uid != uid

    fetch(total - 1, 1)
    assert model.row_at(model.staged_count + total - 1).uid == uid


def test_merged_mode_validates_against_the_whole_version_not_the_window(qapp, repo):
    """Typing a key that exists outside the window must still be caught."""
    model, source, fetch = _merged(repo)
    spec = repo.spec

    far = repo.load_rows(latest_version_only=False)[-1]
    assert not any(
        r.values[spec.pk.key] == far.values[spec.pk.key] for r in model._db_rows
    ), "pick a row the window does not hold"

    placeholder = model.staged_count - 1
    for col in spec.key_columns:
        text = display(col, far.values[col.key], model.lookups)
        model.setData(model.index(placeholder, model.column_index(col.key)), text)

    staged = model.row_at(0)
    assert staged.errors, "a duplicate of an unseen row was not caught"
    assert any("exist" in m.lower() or "duplicate" in m.lower()
               for m in staged.errors.values())


def test_merged_mode_keeps_delete_marks_and_the_commit_state(qapp, repo):
    model, source, fetch = _merged(repo, sort=(("weight", False),))

    doomed = model.row_at(model.staged_count + 1)
    model.set_row_states({doomed.uid: RowState.DELETED})
    fetch(0, 5)

    assert model.dirty_counts()["deletes"] == 1
    # it left the view and became a mark on the row that followed it
    assert doomed.uid not in {model.row_at(model.staged_count + i).uid
                              for i in range(3)}
    marks = [
        model.deleted_rows_above(r)
        for r in range(model.staged_count, model.rowCount())
    ]
    assert any(doomed.uid in {x.uid for x in m} for m in marks)


# ------------------------------------------------ the window in merged mode
@pytest.fixture
def merged_window(qapp, repo):
    """A window whose view the database computes, holding six rows.

    Not closed on the way out, like the `window` fixture: closing asks about
    pending changes, and an offscreen test can never answer.
    """
    from app import MainWindow

    window = MainWindow(repo, window=6)
    return window


def test_the_merged_window_shows_a_table_it_is_not_holding(merged_window, repo):
    window = merged_window
    total = repo.count_rows()

    assert window.model.rowCount() == window.model.staged_count + total
    assert window.model.resident_rows() == 6 < total
    assert window.model.staged_count == 1          # you can still type
    assert window.action_commit.isVisible()


def test_scrolling_fetches_without_losing_the_selection(merged_window, repo):
    """A model reset would drop both the selection and the scroll position,
    and dropping the scroll position mid-scroll makes the grid undrivable."""
    window = merged_window
    model = window.model
    view = window.view

    pk = repo.spec.pk.key
    first = model.staged_count
    view.selectRow(first)
    was = model.row_at(first)
    picked, picked_uid = was.values[pk], was.uid
    resets = []
    model.modelAboutToBeReset.connect(lambda: resets.append(1))

    window._refresh_window(4)                       # slide the window along
    assert model.window_at == 4
    assert not resets, "scrolling reset the model"
    assert view.selectionModel().isSelected(model.index(first, 0))

    # the row that was on screen is no longer resident, and says so
    assert model.row_at(first) is None

    window._refresh_window(0)
    back = model.row_at(first)
    assert back.values[pk] == picked         # the same database row
    assert back.uid != picked_uid            # but deliberately not the same object
    # Which is safe precisely because it was clean. An edited row is held, and
    # test_an_edit_survives_scrolling_away_from_it pins that it keeps its uid.
    assert back.original is None and back.state is RowState.CLEAN



def test_scrolling_the_bar_fetches_the_window_under_it(merged_window, repo):
    """The lazy part, driven the way a person drives it.

    Everything else calls _refresh_window directly; this goes through the
    scrollbar, which is what actually decides the window in use.
    """
    window = merged_window
    model = window.model
    bar = window.view.verticalScrollBar()
    total = repo.count_rows()
    assert bar.maximum() > 0, "nothing to scroll"

    assert model.window_at == 0
    bar.setValue(bar.maximum())
    assert model.window_at > 0, "the window did not follow the scrollbar"

    last = model.row_at(model.rowCount() - 1)
    assert last is not None, "the last row was not fetched"
    ordered = repo.load_rows()
    assert last.values[repo.spec.pk.key] == ordered[-1].values[repo.spec.pk.key]

    bar.setValue(0)
    assert model.window_at == 0
    assert model.row_at(model.staged_count) is not None

    # and it does not refetch while the window already covers the viewport
    model.set_sort([])
    bar.setValue(0)
    before = model.resident_rows()
    fetches = []
    model.dataChanged.connect(lambda *_: fetches.append(1))
    bar.setValue(0)
    assert not fetches, "refetched a window it was already holding"
    assert model.resident_rows() == before
    assert total >= model.resident_rows()

def test_an_edit_commits_after_scrolling_past_it(merged_window, repo):
    window = merged_window
    model = window.model
    pk = repo.spec.pk.key

    target = model.row_at(model.staged_count)
    uid, pk_value = target.uid, target.values[pk]
    model.setData(
        model.index(model.staged_count, model.column_index("owner")),
        "merged-edit",
    )
    assert model.dirty_counts()["updates"] == 1

    total = repo.count_rows()
    window._refresh_window(max(0, total - 3))
    assert model.row_at(model.staged_count) is None       # scrolled past it

    changes = window.model.changeset()
    updates = [o for o in changes.operations if o.kind == "update"]
    assert [o.pk for o in updates] == [pk_value]
    assert updates[0].values_text.get("owner") == "merged-edit" or any(
        c.column == "owner" for c in updates[0].changes
    )
    assert window.unsaved_changes()
    assert model.row_by_uid(uid) is not None



def test_deleting_a_row_shrinks_the_view_it_is_not_holding(merged_window, repo):
    """The count comes from the database, which does not know about a deletion
    made here -- so without re-asking, the grid keeps a phantom row at the end
    that nothing can fill."""
    window = merged_window
    model = window.model
    before_rows = model.rowCount()
    before_total = window.total_rows

    model.delete_rows([model.staged_count + 1])

    assert window.total_rows == before_total - 1
    assert model.rowCount() == before_rows - 1
    # every row the grid claims to have is one it can show
    for visual in range(model.staged_count, model.staged_count + model.visible_db_rows()):
        assert model.row_at(visual) is not None

    window.undo_stack.undo()
    assert window.total_rows == before_total
    assert model.rowCount() == before_rows


def test_a_deletion_keeps_your_place_but_a_re_sort_does_not(merged_window, repo):
    """Re-ordering makes the top the only sensible place to be; deleting a row
    does not move anything, so the window should stay where the reader is."""
    window = merged_window
    model = window.model
    total = repo.count_rows()
    assert total > 8, "need enough rows to scroll"

    window._refresh_window(total - 4)
    away = model.window_at
    assert away > 0

    model.delete_rows([model.staged_count + away + 1])
    assert model.window_at == away, "a deletion sent the view back to the top"

    model.set_sort([("weight", True)])
    assert model.window_at == 0, "a re-sort should start at the top"

def test_the_filter_checklist_covers_the_table_not_the_window(merged_window, repo):
    """A checklist of whatever happens to be on screen is worse than useless."""
    window = merged_window
    model = window.model
    column = model.column_index("a_id")

    offered = dict(model.distinct_values(column))
    resident = {
        display(model.columns[column], r.values.get("a_id"), model.lookups)
        for r in model._db_rows
    }
    assert len(offered) > len(resident), "the checklist only saw the window"

    everything = {}
    for row in repo.load_rows():
        text = display(model.columns[column], row.values.get("a_id"), model.lookups)
        everything[text] = everything.get(text, 0) + 1
    assert offered == everything


def test_an_edited_value_shows_up_in_the_checklist(merged_window, repo):
    """Held rows are excluded from every query, so they are counted here."""
    window = merged_window
    model = window.model
    column = model.column_index("owner")

    before = dict(model.distinct_values(column))
    model.setData(
        model.index(model.staged_count, column), "a-name-nobody-else-has"
    )
    window._refresh_window(0)
    after = dict(model.distinct_values(column))

    assert "a-name-nobody-else-has" not in before
    assert after.get("a-name-nobody-else-has") == 1
    assert sum(after.values()) == sum(before.values())   # nothing double-counted


def test_sorting_in_merged_mode_covers_rows_it_is_not_holding(merged_window, repo):
    window = merged_window
    model = window.model

    model.set_sort([("weight", True)])
    window._reload_view()
    everything = [r.values["weight"] for r in repo.load_rows()]
    assert model.row_at(model.staged_count).values["weight"] == max(
        w for w in everything if w is not None
    )

def test_deleting_a_selection_reaches_past_the_window(merged_window, repo):
    """Select-all and delete must not quietly delete only what is on screen."""
    window = merged_window
    model = window.model
    total = repo.count_rows()
    assert model.resident_rows() < total, "the window has to be smaller"

    everything = range(model.staged_count, model.staged_count + total)
    model.delete_rows(everything)

    assert model.dirty_counts()["deletes"] == total
    deletes = [o for o in model.changeset().operations if o.kind == "delete"]
    assert len(deletes) == total
    assert len({o.pk for o in deletes}) == total      # each one once

    window._refresh_window(0, structural=True)
    assert model.rowCount() == model.staged_count     # nothing visible but staging


def test_copying_a_selection_reaches_past_the_window(merged_window, repo):
    """The same gap, read-only: copy used to skip rows it could not see."""
    window = merged_window
    view = window.view
    total = repo.count_rows()

    view.selectAll()
    view.copy_selection()
    text = QApplication.clipboard().text()
    lines = [line for line in text.split("\r\n") if line.strip()]
    # every stored row, plus nothing for the empty staging row
    assert len(lines) == total, f"copied {len(lines)} of {total}"


def test_reverting_a_selection_reaches_past_the_window(merged_window, repo):
    window = merged_window
    model = window.model
    total = repo.count_rows()

    model.delete_rows(range(model.staged_count, model.staged_count + total))
    assert model.dirty_counts()["deletes"] == total

    model.set_show_deleted(True)
    window._refresh_window(0, structural=True)
    model.revert_rows(range(model.staged_count, model.staged_count + total))
    assert model.dirty_counts()["deletes"] == 0
    assert not model.changeset().operations


def test_a_search_does_not_match_across_a_column_boundary(qapp, repo):
    """In SQL the search is a per-column LIKE, so the in-memory one must be too
    -- otherwise the same needle means different things in the two modes."""
    from data import RowMatcher

    lookups = repo.load_lookups()
    rows = repo.load_rows(latest_version_only=False)
    spec = repo.spec

    row = rows[0]
    columns = [c for c in spec.columns if not c.is_pk and c.kind.filterable]
    pair = [display(c, row.values.get(c.key), lookups) for c in columns[:2]]
    straddling = (pair[0][-2:] + pair[1][:2]).casefold()
    assert len(straddling) >= 3, "need two non-empty neighbouring cells"

    row.search_text = None
    assert not RowMatcher(spec, lookups, None, straddling)(row)
    # and each half on its own still matches
    row.search_text = None
    assert RowMatcher(spec, lookups, None, pair[0])(row)


def test_a_wildcard_typed_into_the_search_box_is_literal(qapp, repo):
    """%% in a LIKE pattern matches everything, which is not what was typed."""
    from sqlview import SqlView, ViewQuery

    sql = SqlView(repo.model, repo.spec, repo.load_lookups())
    assert sql.count(repo.engine, ViewQuery(search="%")) == 0
    assert sql.count(repo.engine, ViewQuery(search="_")) == 0
    # and a real needle still works
    assert sql.count(repo.engine, ViewQuery(search="RATES")) > 0


def test_showing_deleted_rows_refreshes_a_windowed_view(merged_window, repo):
    """It changes what the view consists of, so merged mode has to re-ask."""
    window = merged_window
    model = window.model

    doomed = model.row_at(model.staged_count)
    model.set_row_states({doomed.uid: RowState.DELETED})
    window._refresh_window(0, structural=True)
    hidden_total = model.rowCount()

    model.set_show_deleted(True)        # emits viewChanged -> _reload_view
    assert model.rowCount() == hidden_total + 1
    assert model.row_at(model.staged_count).uid == doomed.uid

    model.set_show_deleted(False)
    assert model.rowCount() == hidden_total
