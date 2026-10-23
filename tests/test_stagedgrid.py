"""The whole suite: schema, values, filters, changeset, validation, grid."""

from __future__ import annotations

import os
from pathlib import Path

# Qt has to be told to run headless before PySide6 builds an application.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if Path("C:/Windows/Fonts").exists():
    os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")

import grid
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
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from data import (
    ChangeSet,
    ColumnFilter,
    Condition,
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
def press(view, key, mods=Qt.KeyboardModifier.NoModifier):
    view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, mods))


def idx(model, row, key):
    return model.index(row, model.column_index(key))


def free_key(model, repo):
    """A natural key that is not in the database yet, as display text."""
    used = repo.natural_keys()
    lookups, spec = model.lookups, model.spec
    choices = [lookups.choices(spec[k]) for k in ("a_id", "b_id", "c_id", "d_id")]
    for combo in product(*choices, spec["type"].enum_values):
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
    assert spec["type"].enum_values == (
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
    assert model.rowCount() == model.total_db_rows() + 1
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
    assert gutter(0) == ""                       # the blank staging row
    model.setData(idx(model, 0, "label"), "typed")
    assert gutter(0) == "new"                    # ...once it holds something


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
        ("type", model.spec["type"].enum_values[0]),
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
        v for v in model.spec["type"].enum_values if v != key[4]
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

    reloaded = {r.values["id"]: r for r in repo.load_rows()}
    assert doomed not in reloaded
    assert reloaded[result.inserted[0]].values["label"] == "committed by the test"


def test_dry_run_reports_the_constraint_violation(model, repo):
    """Belt and braces: even if validation were bypassed, the db still says no."""
    existing = repo.load_rows()[0]
    clone = Row.staged(
        model.spec, {k: v for k, v in existing.values.items() if k != "id"}
    )
    changeset = ChangeSet.from_rows(model.spec, [clone], model.lookups)
    result = repo.apply(changeset, dry_run=True)
    assert not result.ok
    assert "unique" in (result.error or "").lower()
    assert repo.count() == len(repo.load_rows())  # nothing was written
