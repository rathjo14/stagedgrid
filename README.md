# stagedgrid

An Excel-like PySide6 editor for one SQLAlchemy fact table (`model_e`), with a
staging area for inserts, cell-level change tracking and a reviewable
changeset that is handed to another API on commit.

![main window](shots/01-main.png)

## Run it

```bash
source .venv/Scripts/activate     # or .venv\Scripts\activate.bat on cmd
python app.py                     # the grid
python schema.py --reset --rows 400   # (re)seed the demo data
python -m pytest                  # 58 tests, no server needed (sqlite)
```

There is no package and nothing to install: four modules in one folder that
import each other by name. `python app.py` works from anywhere, because
Python puts the script's own folder on the import path.

Fresh environment:

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt
```

Built for a locked environment: **Python 3.10, PySide6 6.4.1** (Essentials and
Addons), SQLAlchemy 2.x, psycopg2, pyqtdarktheme. The suite and every runtime
path are verified on exactly that, and also on Python 3.11 / PySide6 6.12 /
SQLAlchemy 2.1, so newer versions are fine.

pandas is deliberately not used -- see the note in `requirements.txt`.

The DSN defaults to `postgresql+psycopg2://postgres:postgres@localhost:5432/benchmark`;
override with `STAGEDGRID_DSN` or `--dsn`. `--limit N` caps the initial load,
`--changesets DIR` says where committed payloads are written.

## The four files

```
schema.py    the models, and everything that reads them
data.py      what is pending, and talking to the database   <- your entry point
grid.py      the table widget
app.py       the window around it                           <- run this one
```

Imports only ever go one way: `schema <- data <- grid <- app`.

| file | holds |
| --- | --- |
| `schema.py` | the A-E models (your repo's stand-in), `TableSpec`/`ColumnSpec` introspection, text<->value conversion, the dimension `Lookups`, the engine helper, and the demo seeder with its CLI |
| `data.py` | `Row`/`RowState`, Excel-style filters, the validator, the `ChangeSet` payload, and `Repository` (load rows and lookups, apply a changeset). **Start here** -- its docstring is the API |
| `grid.py` | the colours, `StagedTableModel` (staging + stored rows, sorting, filtering, deletion marks), the three undo commands, the per-type cell editors, the filter header, the gutter, and `GridView` (clipboard, fill, shortcuts, context menu) |
| `app.py` | `FilterDialog`, `MainWindow`, the sink, the theme, and `main()` |

`schema.py` and `data.py` import no Qt, which is what lets the tests cover the
logic without a screen.

## Pointing it at your real models

Nothing in the UI knows a column name, so your own mapped classes drop in
without touching the code:

```bash
# your repo has to be importable: pip install -e it, or put it on PYTHONPATH
python app.py --describe     --model yourrepo.models:FactTable     --dsn "postgresql+psycopg2://user:pw@host:5432/yourdb"
```

Flags are the reliable form: they are visible in the command and cannot be
lost by a shell that did not carry an export through. The same settings can
come from the environment instead, which is handy once it works:

```bash
export STAGEDGRID_MODEL="yourrepo.models:FactTable"
export STAGEDGRID_DSN="postgresql+psycopg2://user:pw@host:5432/yourdb"
python app.py
```

Note that an environment default never appears in `sys.argv` -- printing argv
to check will show no `--model` even when it arrived. `--describe` prints the
variables as the process actually sees them, which is the thing to trust.

Flags work too, and win over the environment: `--dsn`, `--model`, `--labels`.
With none of them set you get the bundled demo schema, which is what makes it
possible to keep developing here.

Every run prints which schema and database it is about to use, so check that
line first if something looks wrong:

```
stagedgrid: yourrepo.models:FactTable (fact_table) at postgresql+psycopg2://...
stagedgrid: the bundled demo schema (model_e) at postgresql+psycopg2://...
```

The second line is the one to watch for. Setting only the DSN and forgetting
the model points the *demo* models at *your* database, and the first thing you
see is `relation "model_a" does not exist` -- a table from this repo's demo
schema, which is nothing to do with yours. The load error says so now.

`--describe` prints what the introspection worked out and exits, which is the
first thing to run against a real schema:

```
Reading -> table 'reading'
  primary key      reading_id   (shown as the row index, not a column)
  natural key      uq_reading

  column          kind      header          flags     source
  region_id       fk        Region          -K-       region.code
  product_id      fk        Product sku     -K-       product.sku
  bucket          str       Bucket          -K-
  value           float     Value           --N
```

Check three things in that output:

| | |
| --- | --- |
| **natural key** | the `UniqueConstraint` it chose -- the one whose columns get greyed out on stored rows and become drop-downs on staged ones. It picks the constraint with the most columns, so if your table has several, confirm it chose the right one. |
| **source** on each fk | which column of the dimension table is shown instead of the id. It guesses the single unique non-key column. Override with `--labels region=name,product=title` when the guess is wrong. |
| **flags** | `K` on exactly the columns that make up your natural key, `N` on the nullable ones (non-nullable data columns become required fields). |

Headers are derived from the attribute names (`_header_for` and
`_header_for_fk` in `schema.py`, one short function each) -- change those two
if you want different wording.

From code rather than the command line:

```python
from data import Repository
from app import MainWindow
from yourrepo.models import FactTable

repo = Repository(engine=your_engine, model=FactTable,
                  label_columns={"region": "name"})   # only if a guess is wrong
MainWindow(repo, sink=your_api.submit_changes).show()
```

What it reads off the mapper:

| what it reads | what the UI does with it |
| --- | --- |
| primary key | the row index down the left-hand gutter, Excel-style; click it to select the row, right-click to delete |
| largest multi-column `UniqueConstraint` | the natural key: greyed out on existing rows, drop-downs on staged rows, validated for duplicates |
| `ForeignKey` + the parent's single unique column | shows `EU-WEST`, not `17`; fills the drop-down |
| `Column(doc=...)`, falling back to `comment` | header and cell tooltips |
| python type of each column | editor, validator, alignment, filter operators |
| `Enum(YourEnum)` | a drop-down of the member **names**: `Kind.BASELINE = "bl"` reads as `BASELINE`, not `bl` |
| `nullable` | required-field validation |

A sink is just a callable taking the payload dict and returning a one-line
string for the status bar. `file_sink("changesets")` (the default) writes
`changesets/changeset-<stamp>.json`. If your API should be the only writer,
drop the `self.repository.apply(...)` call in `MainWindow.commit` and keep the
`sink(...)` -- the validation and dry-run machinery in front of it stays useful.

The demo models and the seeder stay in `schema.py` and are only used when
`--model` is unset, so this repo keeps working as a sandbox. The test suite
proves the seam both ways: it drives the demo schema, and
`test_the_grid_drives_a_completely_different_schema` builds an unrelated one
(different table names, a different primary-key name, a three-column natural
key with a plain string in it, dimension labels not called `name`) and runs
the whole flow against it.

### Enum columns

The grid shows the member **name**, not its value, which is also what
SQLAlchemy's `Enum` type stores by default. So an enum declared as

```python
class Kind(enum.Enum):
    BASELINE = "bl"
```

shows, sorts, filters and copies as `BASELINE`. Typing or pasting accepts
either spelling -- `BASELINE`, `baseline`, or the raw `bl` -- and stores the
name; anything else is flagged on the cell. The changeset payload carries the
name too, and the value written back to the database is the member itself.

## How the grid behaves

```
+---------------------------------------------+
| staged insert rows   (green, key editable)  |  <- validated against the UC
| the always-blank row (type or paste here)   |
|=============================================|  <- green rule
| rows from the database                      |  <- key greyed, data editable
+---------------------------------------------+
```

* **Staging area.** The blank row sits directly above the stored rows. Touch
  any cell of it and a fresh blank row appears below; paste 20 rows into it
  and 20 staged rows appear. Natural-key cells there are *not* greyed and
  each one is a drop-down of the live values from A/B/C/D and the enum.
  Each foreign-key drop-down also starts with **+ New A...**, which asks for a
  value and adds it for this session: it appears in every drop-down for that
  column straight away and shows in italics, but it is never written to the
  database. The changeset carries it instead (see below), because creating
  dimension values is your API's job, not this app's. Committing locally is
  refused while a changeset refers to one.
  Sorting and filtering never touch the staging block -- it stays pinned at
  the top, so a filter can't hide work in progress.
* **Validation.** Staged rows are checked against the database's natural keys
  and against each other: duplicates, unknown drop-down values, missing
  required fields. Bad cells get a red border and the reason in the tooltip;
  Commit is disabled until they are clean. If a staged row collides with a
  row you deleted in the same session, that is a *warning* ("replaces #123"),
  not an error. Before anything is written, the changeset is replayed inside
  a transaction and rolled back, so PostgreSQL gets the last word.
* **Updates.** The row goes faint blue, the edited cell goes bluer and bold,
  and the tooltip shows `old -> new`. Reverting the value clears the change.
* **Deletes.** The row leaves the grid and a red triangle stays in the gutter
  where it was, VS Code style. Click the triangle to bring the rows back,
  hover it to see which ids they are, or tick *Show deleted* to see them
  in place, struck through.
* **Filtering** is per column and type-aware: a searchable value checklist
  with counts (Excel's drop-down) plus up to two conditions joined by
  And/Or -- `>`, `between`, `contains`, `begins with`, regex, `is blank`...
  The checklist is computed with the *other* columns' filters applied. The
  funnel in the header turns blue when a filter is live; `jsonb` is excluded.
* **Sorting**: click a header; Ctrl+click adds a second/third level (the
  arrow shows the level number). Blanks always sort last.

### Keyboard

| | |
| --- | --- |
| `Ctrl+C` / `Ctrl+Shift+C` | copy / copy with headers (TSV, pastes into Excel) |
| `Ctrl+X` / `Ctrl+V` | cut / paste; a single copied cell fills the selection |
| `Del` | clear the selected cells -- or, with whole rows selected, delete them |
| `Ctrl+D` / `Ctrl+R` | fill down / fill right |
| `Ctrl+Plus` / `Ctrl+Minus` | add a staged row / delete the selected rows |
| `Ctrl+Backspace` | revert pending edits on the selected rows |
| `Ctrl+Z` / `Ctrl+Y` | undo / redo (addressed by row identity, so it survives sorting) |
| `F2` / `Shift+F2` | edit / open the large editor (json, long text) |
| `Alt+Down` | open a key cell's drop-down |
| `Ctrl+Shift+L` | filter the current column |
| `Ctrl+F` | the search box (matches any column) |
| `F1` | the shortcut list; *Help > Colour legend* explains the colours |
| click / right-click an id | select the row / delete, revert or restore it |

## Getting at the changes

There is no changes panel -- the grid holds the state and you read it when you
want it. Everything is reachable from **data.py**, whose docstring is the API:

```python
repo = Repository(engine=make_engine())   # or model=YourTable
rows = repo.load_rows()                   # the data
changes = window.model.changeset()        # what is pending, built fresh each call
changes.inserts, changes.updates, changes.deletes
payload = changes.to_payload()            # JSON-ready, this is what the sink gets
repo.apply(changes, dry_run=True)         # ask the database without writing
```

Or be handed it: `MainWindow(repo, sink=my_api.submit_changes)` calls your
callable with the payload on every commit. One operation looks like this:

```json
{"op": "update", "pk": 196,
 "key": {"a_id": 5, "b_id": 5, "c_id": 1, "d_id": 5, "type": "BASELINE"},
 "key_labels": {"a_id": "APAC", "b_id": "COMMODITY", "c_id": "BOND",
                "d_id": "2026.2", "type": "BASELINE"},
 "changes": [{"column": "weight", "old": 0.33, "new": 0.98765,
              "old_text": "0.33", "new_text": "0.98765"}]}
```

If any row refers to a value you invented with **+ New ...**, the payload
gains a `new_dimension_values` list naming the table, the column, the label and
the negative placeholder id the inserts use. Create those first, then map the
placeholder ids onto the real ones:

```json
"new_dimension_values": [
  {"placeholder_id": -1, "table": "model_a", "column": "name", "label": "ANTARCTICA"}
]
```

Natural keys travel as both ids and labels so the receiving service can
check what it is about to do without re-reading the dimension tables. On
commit the rows are written in one transaction (deletes, then updates, then
inserts, so a key can be reused) and the payload goes to the sink.

## Colours and dark mode

The window chrome -- menus, toolbar, dialogs, scrollbars -- comes from
**pyqtdarktheme**. The grid paints its own cells on top, from the palette in
`grid.py` (`LIGHT` / `DARK`, with `grid.C` the one in force). Read it as
`grid.C` rather than `from grid import C`, which would snapshot the light
palette at import time.

That the two compose is not an accident worth assuming: pyqtdarktheme's
stylesheet contains `QAbstractItemView::item` rules, which is normally enough
to make Qt take over item painting and drop every per-cell brush. Measured,
the state colours come through unchanged, and a test samples real pixels to
keep it that way.

pyqtdarktheme is required, not optional -- the whole of `apply_theme` is four
lines:

```python
qdarktheme.setup_theme(theme)                 # "auto" | "light" | "dark"
dark = theme == "dark" or (theme == "auto" and bool(darkdetect.isDark()))
set_theme(dark)                               # which grid palette to paint with
return dark
```

`auto` follows the desktop through darkdetect rather than Qt's
`colorScheme()`, which only exists from Qt 6.5 and so always answers "light"
on 6.4.1. Override with `--theme light|dark`, or flip it live from
*Data > Dark mode*.

The selection works the way Excel's does: a neutral translucent wash laid over
the cell rather than a coloured fill, so a selected cell still shows whether it
is staged, edited or invalid. The active cell keeps its colour too and gets a
plain outline. Two Qt details make that work, and both are easy to undo by
accident:

* `GridDelegate.paint` strips `State_Selected` and `State_HasFocus` before
  letting Qt draw the cell, then paints the wash and the outline itself.
* `stylesheet()` must go on the *window*. A stylesheet set directly on the
  QTableView -- or any `QTableView::item` rule in it -- makes Qt take over item
  painting and silently ignore every per-cell background brush.

Colours with alpha are written as `(r, g, b, a)` tuples, never as 8-digit hex:
Qt reads `#RRGGBBAA` as `#AARRGGBB`.

## Decisions worth knowing

* Sorting/filtering happen inside the model rather than a
  `QSortFilterProxyModel`, because staged rows must stay pinned regardless of
  sort order and deleted rows must leave a marker behind at their position --
  neither fits a proxy's index mapping.
* Undo commands address rows by a stable `uid`, never by position, so undo
  still works after you re-sort or filter.
* Unknown drop-down text (from a paste) is kept as `Unresolved("NOT-A-REGION")`
  rather than dropped, so you see what you typed and get an error on that
  exact cell.
* Filters apply to stored rows only; the staging block is always visible.
* No pandas. The hot paths are dictionary and set lookups over a few thousand
  rows; a DataFrame in the middle is slower. Copying the whole table is the
  one bulk operation, and there the cost is producing the display text, which
  pandas cannot help with -- its `to_csv` is slower than the `csv` module on
  the same strings.
* Rows are loaded eagerly into memory (400 rows here; fine into the tens of
  thousands). Past that, `Repository.load_rows` is the one function to swap
  for a paged or threaded load.
* Nothing checks whether a row changed underneath you between load and
  commit -- that is handled upstream by the API this feeds.
