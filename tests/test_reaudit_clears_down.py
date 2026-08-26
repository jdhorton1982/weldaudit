"""Re-auditing a job clears what it is about to rebuild, in a workable order.

Every table an audit fills is emptied before it is filled again, and SQLite is
running with foreign keys on. Delete a parent while a child still points at it
and the whole re-audit dies on the first statement, before a document is read.

That is exactly what happened. ``weld`` sat eighth in the list while
``weldtrace_weld``, which references it, sat twenty-second. Harmless for as
long as the WeldTrace register never matched itself to the weld maps -- and
from the first run that filled in ``weldtrace_weld.weld_id``, every re-audit of
that job failed with "FOREIGN KEY constraint failed". The first audit worked,
the second never could.

So this does not check that ``weld`` is last. It reads the foreign keys out of
the schema and checks the whole order against them, because the next table
added with a reference to something already in the list would break in exactly
the same way and be just as invisible.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402

INDEX_PY = Path(__file__).resolve().parents[1] / "weldaudit" / "index.py"


def clear_down_order() -> list[str]:
    """The tables index_project empties, in the order it empties them."""
    src = INDEX_PY.read_text(encoding="utf-8")
    start = src.index('for table in ("finding"')
    block = src[start:src.index("):", start)]
    return re.findall(r'"(\w+)"', block)


def references(db: Database) -> dict[str, set[str]]:
    """``{table: tables it points at}``, straight from the live schema."""
    out: dict[str, set[str]] = {}
    for row in db.q("SELECT name FROM sqlite_master WHERE type='table'"):
        name = row["name"]
        out[name] = {r["table"] for r in db.q(f"PRAGMA foreign_key_list({name})")}
    return out


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "r.db")


def test_the_list_is_found_at_all():
    """If the loop is rewritten, this file must be rewritten with it rather
    than silently passing against nothing."""
    order = clear_down_order()
    assert len(order) > 15
    assert "weld" in order and "weldtrace_weld" in order


def test_no_table_is_emptied_before_something_that_points_at_it(db):
    order = clear_down_order()
    points_at = references(db)
    place = {t: i for i, t in enumerate(order)}

    wrong = []
    for child, parents in points_at.items():
        if child not in place:
            continue
        for parent in parents:
            if parent in place and place[parent] < place[child]:
                wrong.append(
                    f"{parent} is emptied at {place[parent]} but {child} "
                    f"still references it and is not emptied until {place[child]}")
    assert not wrong, (
        "a re-audit will die on 'FOREIGN KEY constraint failed':\n  "
        + "\n  ".join(wrong))


def test_weld_is_emptied_after_the_weldtrace_register(db):
    """The specific case that broke, named so a reordering cannot lose it."""
    order = clear_down_order()
    assert order.index("weld") > order.index("weldtrace_weld")
    assert order.index("weld") > order.index("welder_pass")


def test_the_deletes_actually_run_against_the_real_schema(db):
    """The order is only worth anything if SQLite accepts it.

    Run against an empty database, so this pins the statements and the order
    rather than any particular job's data.
    """
    pid = db.upsert_project("J", "/tmp/j")
    with db.tx() as c:
        for table in clear_down_order():
            c.execute(f"DELETE FROM {table} WHERE project_id=?", (pid,))
        c.execute("DELETE FROM document WHERE project_id=?", (pid,))
