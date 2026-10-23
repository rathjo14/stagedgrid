# stagedgrid

An Excel-like PySide6 editor for one SQLAlchemy fact table (`model_e`), with a
staging area for inserts, cell-level change tracking and a reviewable
changeset that is handed to another API on commit.

![main window](shots/01-main.png)

![a row needing attention, and Commit blocked](shots/11-attention-and-commit.png)

## Run it

```bash
source .venv/Scripts/activate     # or .venv\Scripts\activate.bat on cmd
python app.py                     # the grid
python schema.py --reset --rows 400   # (re)seed the demo data
python schema.py --reset --rows 10000 --one-version   # a realistic single version
python -m pytest                  # 166 tests, no server needed (sqlite)
```

There is no package and nothing to install: five modules in one folder that
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
override with `STAGEDGRID_DSN` or `--dsn`. `--window N` sets how many rows are
held at once (1,000 by default) and `--changesets DIR` says where committed
payloads are written.

## The five files

```
schema.py    the models, and everything that reads them
data.py      what is pending, and talking to the database   <- your entry point
sqlview.py   the same view, in SQL, and the window merged onto it
grid.py      the table widget
app.py       the window around it                           <- run this one
```

Imports only ever go one way: `schema <- data <- {sqlview, grid} <- app`.

| file | holds |
| --- | --- |
| `schema.py` | the A-E models (your repo's stand-in), `TableSpec`/`ColumnSpec` introspection, text<->value conversion, the dimension `Lookups`, the engine helper, and the demo seeder with its CLI |
| `data.py` | `Row`/`RowState`, Excel-style filters, the validator, the `ChangeSet` payload, and `Repository` (load rows and lookups, apply a changeset). **Start here** -- its docstring is the API |
| `grid.py` | the colours, `StagedTableModel` (staging + stored rows, sorting, filtering, deletion marks), the three undo commands, the per-type cell editors, the filter header, the gutter, and `GridView` (clipboard, fill, shortcuts, context menu) |
| `sqlview.py` | `SqlView` (the sort, filters and search as one SQL statement, plus `rank`) and `MergedView` (a window of it with this session's edits merged on top). No Qt |
| `app.py` | `FilterDialog`, `MainWindow`, the sink, the theme, and `main()` |

`schema.py`, `data.py` and `sqlview.py` import no Qt, which is what lets the
tests cover the logic without a screen.

## Pointing it at your real models

Nothing in the UI knows a column name, so your own mapped classes drop in
without touching the code:

```bash
# your repo has to be importable: pip install -e it, or put it on PYTHONPATH
python app.py --describe     --model yourrepo.models:FactTable     --updates-model yourrepo.models:RevisionSet     --dsn "postgresql+psycopg2://user:pw@host:5432/yourdb"
```

Flags are the reliable form: they are visible in the command and cannot be
lost by a shell that did not carry an export through. The same settings can
come from the environment instead, which is handy once it works:

```bash
export STAGEDGRID_MODEL="yourrepo.models:FactTable"
export STAGEDGRID_UPDATES_MODEL="yourrepo.models:RevisionSet"
export STAGEDGRID_DSN="postgresql+psycopg2://user:pw@host:5432/yourdb"
python app.py
```

Note that an environment default never appears in `sys.argv` -- printing argv
to check will show no `--model` even when it arrived. `--describe` prints the
variables as the process actually sees them, which is the thing to trust.

Flags work too, and win over the environment: `--dsn`, `--model`,
`--updates-model`, `--labels`.
With none of them set you get the bundled demo schema, which is what makes it
possible to keep developing here.

### The branch table

`--updates-model` is the same idea for the table commits are written to. Its
columns are found by **type**, not by name, so yours does not have to look
like the demo one:

| it needs | found as |
| --- | --- |
| the changes | the first json/jsonb column |
| the version | the first foreign key (to the version dimension, when there is one) |
| modified-at | the first timestamp -- stamped on every push |
| a name | the first string column -- shown in the branch list |

Anything else on the table is left alone, so a `ticket` or `raised_by` column
is no obstacle. `UpdatesSpec.from_mapped_class` does the matching and raises
if there is no json column to write to. Pass `--updates-model none` to commit
to `--changesets` and the sink only, writing to no table at all.

Every run prints which schema and database it is about to use, and where
commits will go, so check those lines first if something looks wrong:

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
 +    | new-data area, lighter grey -- type or paste here
 new  | a new row that passes validation          (green)
 !!!  | a new row that does not                   (yellow, key outlined)
======|========================================== <- green rule
 1234 | rows already in the database              (grey)
```

The two halves are told apart by grey alone, so colour is free to mean
something: **green** accepted, **yellow** needs attention, **blue** edited,
**red** deleted. Selecting cells tints them grey rather than recolouring
them, so a selected row still shows its state.

* **Staging area.** The blank row sits directly above the stored rows. Touch
  any cell of it and a fresh blank row appears below; paste 20 rows into it
  and 20 staged rows appear. Natural-key cells there are *not* greyed and
  each one is a drop-down of the live values from A/B/C/D and the enum.
  The version is not among them: there is one per view, so it reads on a strip
  directly above the sheet and a staged row inherits it, rather than filling a
  column with the same value on every row.

  A key column often needs a value the dimension does not have yet, so there
  are two ways to say so and they do the same thing: **type the label into the
  cell**, or pick **+ New A...** at the top of the drop-down. Either way it is
  added for this session -- it appears in every drop-down for that column
  straight away and shows in italics -- but it is never written to the
  database. The changeset carries it instead (see below), because creating
  dimension values is your API's job, not this app's. Committing locally is
  refused while a changeset refers to one.

  Enum columns are the exception: their members are a Python class, so an
  unknown one stays an error.
  Sorting and filtering never touch the staging block -- it stays pinned at
  the top, so a filter can't hide work in progress.
* **Attention.** A staged row that does not pass yet goes yellow, and the
  cells actually at fault go a stronger yellow -- so "these are the ones" is
  visible without reading a tooltip. The exception is a duplicate: there the
  natural key as a whole is the problem, so it stays one uniform block rather
  than singling out a cell that is individually fine.
* **Commit.** Green and clickable when there is something valid to push,
  yellow and dead while something still needs fixing -- the same yellow the
  rows use, so the button and the grid agree -- and plain when there is
  nothing to do.
* **Validation.** Staged rows are checked against the database's natural keys
  and against each other: duplicates, unknown drop-down values, missing
  required fields. A row that does not pass turns yellow all the way across,
  its gutter reads `!!!`, and the natural key is outlined as a single block
  rather than a box per cell; hover any cell for the reason. A row that does
  pass turns green.
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
* **Only the newest version is loaded.** If a natural-key foreign key points
  at a dimension whose label column is called `version` (or `revision`), the
  query is restricted to its newest value -- by natural sort, so `2026.10`
  beats `2026.2`. Older rows are never fetched, rather than fetched and
  filtered out. `Repository.load_rows(latest_version_only=False)` gets
  everything, and `repo.latest_version()` tells you which one is in force.

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

## Loading: a window, not the table

The grid holds **1,000 rows** at a time. The database does the ordering,
filtering and searching over the whole table, and the rows this session has
edited are merged back on top of its answer. Scrolling fetches the window the
viewport is looking at.

Measured on Python 3.10 / PySide6 6.4.1 against 10,000 rows in one version:

| | |
| --- | --- |
| window opens | 538 ms, holding 1,000 of 10,000 |
| memory for the rows | ~20 MB |
| scroll to the bottom and back | 213 ms |
| sort a column | 43 ms |
| quick search | 187 ms |
| rebuild with 21 edits pending | 78 ms (3.7 ms each) |

A descending sort while holding 1,000 rows puts the table's real maximum at the
top, and a search reports every match in the table -- 1,916 of 10,000 -- not
every match in the window. Footprint stays flat: across 80 window moves it
oscillated between 117 and 121 MB of process memory and ended +3.7 MB, with the
held set sitting at exactly the five rows that had been edited.

Change the default with `WINDOW_ROWS` at the top of `app.py`, or per run with
`--window N`.

### How a row you edited stays in the right place

The database holds the values your edit replaced, so a query would sort and
filter that row by the wrong ones. So the rows split in two:

* A **held** row is one this session has touched -- edited, or marked deleted.
  It is excluded from every query by primary key and merged back in by the
  values the grid has now. It is never dropped, so undo and the changeset keep
  pointing at it.
* Every other row is **clean**. It comes straight from the query and can be
  thrown away and refetched at will, because nothing points at it: no edit, no
  undo target, no snapshot. A clean row that comes back after a scroll is a new
  object with a new uid, and that is safe precisely because it was clean.

A row joins the held set the moment it is edited and leaves it when it is
reverted, so it holds exactly the rows the database disagrees with.

Placing a held row takes one number -- how many rows sort before it -- and
`SqlView.rank` asks the database for exactly that without fetching anything, by
turning the sort order into a lexicographic comparison. Its position in the
view is then that count plus the number of held rows ahead of it. One count
query per held row, so the cost is bounded by how much you have typed rather
than by how big the table is.

Two tests hold this down. `rank` is checked against the truth for *every row*
of nine different views -- a row's rank must equal its own index -- and
`test_a_merged_window_equals_holding_every_row` edits rows to move them to the
front, to the back and out of the sort, then walks the merged view a window at
a time and compares it, primary key by primary key, against the same view with
every row in memory. Window sizes down to one row included.

### Translating the view

`sqlview.py` turns the same `FilterSet`, sort order and search text into one
statement. It has to *mean* what the Python version means, or the merged rows
and the queried rows would disagree about the same view, so it follows the same
four rules -- each of which is there because a test caught it missing:

* a foreign key sorts and filters by its **label**, which means joining the
  dimension;
* an enum sorts in **declaration order**, which means a CASE;
* blanks sort **last** whichever way the column is sorted;
* ties break on the **primary key**, because Python's sort is stable.

Text is compared case-insensitively on both sides (Python casefolds, so SQL
lowers). Getting those right took the count of disagreeing views from 7 of 18
to **0 of 18**, where `test_the_database_view_agrees_with_the_in_memory_one`
holds it.

`matches regex` is the one filter with no SQL equivalent. The database defines
the view, so a filter it cannot express cannot be part of one: the grid drops
that filter and says so, rather than leaving it showing in the header with no
effect.

### What still covers the whole table

| | how |
| --- | --- |
| sorting, filtering, searching | SQL, with held rows merged in |
| the filter dialog's value checklist | counted in SQL, held rows added by their current values |
| duplicate-key validation | the key index: 1.0 MB at 10,000 rows against 35.1 MB of rows |
| delete, revert, copy over a selection | fetched, one query per run of adjacent rows |
| the changeset, undo, commit | the held set, which the window never drops |

Validation is the reason a grid like this would normally hold every row: it has
to know whether a typed key already exists anywhere in the version. It turns
out not to need the rows, only the five unique-constraint columns:

| held, at 10,000 rows | |
| --- | --- |
| whole rows | 35.1 MB, 240 ms |
| the key tuples only | **1.0 MB**, 46 ms |
| the keys as display text | **0.6 MB**, 59 ms |

Keys are **3%** of what rows cost; at 100,000 rows that is 10 MB against
351 MB. `Repository.natural_keys()` reads them straight from the database, so
typing a duplicate of a row the grid is not showing is still caught.

### Two things to know

* **Quick search is the slow one** -- 187 ms against about 20 ms in memory --
  because it scans twenty unindexed columns as `ILIKE` casts. The cost is
  linear in columns scanned (20 columns 80 ms, the 5 string columns 22 ms, one
  column 6 ms), so if it ever needs to be fast the answer is a `tsvector`, not
  a better query.
* **The status bar's selection total covers the rows on screen.** It is
  recomputed on every selection change, and a query per mouse drag is not worth
  a sum in the status bar. Copy, delete and revert do fetch what they were
  asked for.

## Where the changes actually live

Nothing accumulates in a log. The state *is* the rows, and everything else is
derived from them on demand -- which is why there is nothing to keep in sync.

| what | where | holds |
| --- | --- | --- |
| one row | `data.Row` | `values` (live), `original` (what the database had, taken on the first edit only), `state`, `uid`, `errors` |
| new rows | `StagedTableModel._staged: list[Row]` | the staging block, `state=NEW` |
| touched stored rows | `StagedTableModel._held: dict[uid, Row]` | **the pending set.** Every row this session edited or deleted, kept whatever the window is showing |
| the rows on screen | `StagedTableModel._db_rows: list[Row]` | the fetched window: clean rows, plus any held ones that fall in it |
| invented dimension values | `Lookups._pending` | `{placeholder_id: {table, column, label}}` |

A row counts as pending when `row.changed_keys()` is non-empty or its `state`
is not `CLEAN`; `_note_held` keeps `_held` to exactly those, dropping a row
the moment it is reverted.

`model.changeset()` walks `_staged + _held` and builds the hand-off structure
fresh each call:

```
ChangeSet
  .table                 str
  .operations            list[Operation]
     .kind               "insert" | "update" | "delete"
     .pk                 the primary key, None for an insert
     .key / .key_text    the natural key, as ids and as labels
     .values             an insert's data columns
     .changes            list[CellChange(column, header, old, new, old_text, new_text)]
  .new_values            dimension values invented here
  .inserts/.updates/.deletes      the operations, filtered
  .to_payload()          the JSON dict below
```

`Repository.save_branch(payload, version_id, ...)` is what writes that dict
into the branch table's json column, so `UPDATE <updates>.<changes>` is the
only place any of this is persisted.

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

If any row refers to a value invented here, the payload gains a
`new_dimension_values` list naming the table, the column, the label and the
negative placeholder id the inserts use. Create those first, then map the
placeholder ids onto the real ones. The ids come from one counter for the whole
session, so they are unique across dimensions -- two columns each inventing a
value do not both get `-1`:

```json
"new_dimension_values": [
  {"placeholder_id": -1, "table": "model_a", "column": "name", "label": "ANTARCTICA"},
  {"placeholder_id": -2, "table": "model_b", "column": "name", "label": "NEW-DESK"}
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
* A label a foreign key's dimension does not have is added as a new value for
  the session rather than rejected -- typing it is as good as using
  **+ New ...**, because having to find that entry to say what the cell already
  says is a nuisance. It gets a negative placeholder id, italics, and a line in
  `new_dimension_values`. `Unresolved` survives for the cases that cannot be
  invented, such as an unknown enum member, where you still see what you typed
  and get an error on that exact cell.
* Filters apply to stored rows only; the staging block is always visible.
* No pandas. The hot paths are dictionary and set lookups over a few thousand
  rows; a DataFrame in the middle is slower. Copying the whole table is the
  one bulk operation, and there the cost is producing the display text, which
  pandas cannot help with -- its `to_csv` is slower than the `csv` module on
  the same strings.
* The grid holds a window and the database computes the view. Measured on
  Python 3.10 / PySide6 6.4.1 with **10,000 rows in one version**, holding
  1,000 of them:

  | | |
  | --- | --- |
  | window startup | 538 ms |
  | memory for the rows | ~20 MB, and flat as you scroll |
  | keystroke in the staging row | 12 ms |
  | sort a column | 43 ms |
  | quick search | 187 ms |
  | scroll to the bottom and back | 213 ms |
  | repaint the viewport | 86 ms, and independent of row count |

  Everything in the typing path stays far under what anyone notices. Search is
  the one slow entry and the reason is in *Loading* above.
* The model can still sort and filter in Python, over whatever rows it has been
  given, and that path is what the SQL translation is tested against -- it is
  the specification, not dead code. The app never uses it: `set_window_source`
  hands those jobs to the database. It is also what lets the model be tested
  without a database at all.
* Quick search keeps a folded copy of each row's searchable text
  (`Row.search_text`), rebuilt on demand and dropped whenever a value changes.
  The columns are joined with a separator a needle cannot contain, so a match
  cannot straddle two cells -- which is what keeps it the same question SQL
  answers, where the search is a LIKE per column.
* The invariant the windowing rests on: a row the session has touched is
  held and keeps its identity, while a clean row is owned by the database and
  may be refetched freely. Everything else follows from that -- see *Loading*
  above.
* A held row costs one count query per view rebuild, which is why the held set
  is pruned the moment a row is reverted: `_note_held` drops anything the
  database agrees with again.
* The SQL translation is held to the in-memory one by test, not by eye.
  `test_the_database_view_agrees_with_the_in_memory_one` runs thirteen views
  both ways and compares the primary keys **in order**; the four rules that
  make them agree (label ordering for foreign keys, declaration order for
  enums, blanks last, primary-key tie-break) are each there because that test
  caught them missing.
* Nothing checks whether a row changed underneath you between load and
  commit -- that is handled upstream by the API this feeds.
