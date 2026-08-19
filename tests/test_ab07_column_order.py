"""A drawing written out of sequence is not a drawing that disagrees with itself.

An as-built is a column per joint, and the columns usually run in the order the
pipe was laid. Usually. A pup welded in beside a coupling gets written in the
next free column, which can put it after the joint it physically precedes.
Reading the columns in order then shows the survey running three feet backwards
where thirty-nine feet of pipe sits, and reports a stretch that is correct.

What must survive the check is a mistyped station. Sorting by station puts a
wrong value in the wrong place and leaves wide holes either side, so it stays
reported.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402
from weldaudit.rules.asbuilt import station_length_conflict  # noqa: E402


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "t.db")
    root = tmp_path / "Job"
    root.mkdir()
    return database, database.upsert_project("Job", root)


def _feet(station):
    whole, part = station.split("+")
    return int(whole) * 100 + float(part)


def joints(database, pid, rows, sheet="As-Built (011)"):
    """rows: (column, station, description, length)."""
    with database.tx() as c:
        for seq, station, desc, length in rows:
            c.execute(
                """INSERT INTO asbuilt_joint(project_id, sheet, band, seq, station,
                                             station_ft, length, description, source)
                   VALUES(?,?,1,?,?,?,?,?,'sheet')""",
                (pid, sheet, seq, station, _feet(station), length, desc))


def test_a_pup_written_in_the_next_free_column_is_not_reported(db):
    """The real case: the pup at 63+25 sits in the column after the ML at
    63+28 that it physically precedes. Read in station order every step
    agrees within three feet."""
    database, pid = db
    joints(database, pid, [
        (0, "62+89", "ML", 39.0),
        (1, "63+28", "ML", 39.0),
        (2, "63+25", "PUP", 5.0),      # out of column order
        (3, "63+68", "ML", 39.0),
    ])
    assert station_length_conflict(database, pid, "r") == []


def test_a_mistyped_station_is_still_reported(db):
    """42+17 for 42+72. Sorting by station does not rescue it: twenty-four and
    thirty-eight foot holes remain either side."""
    database, pid = db
    joints(database, pid, [
        (0, "41+93", "ML", 39.0),
        (1, "42+32", "PUP", 3.0),
        (2, "42+33", "ML", 39.0),
        (3, "42+17", "ML", 39.0),      # should be 42+72
        (4, "43+10", "ML", 39.0),
    ], sheet="As-Built (008)")
    found = station_length_conflict(database, pid, "r")
    assert found, "a mistyped station must survive the out-of-order check"
    assert any("42+17" in f["subject"] for f in found)


def test_a_wrong_leading_digit_is_still_reported(db):
    """221+68 for 121+68, among neighbours all in the 12,000s."""
    database, pid = db
    joints(database, pid, [
        (0, "120+91", "ML", 39.0),
        (1, "121+29", "ML", 39.0),
        (2, "221+68", "ML", 39.0),
        (3, "122+06", "ML", 39.0),
        (4, "122+44", "ML", 39.0),
    ], sheet="As-Built (022)")
    found = station_length_conflict(database, pid, "r")
    assert any("221+68" in f["subject"] for f in found)


def test_a_run_already_in_order_is_judged_as_before(db):
    """The check must not become a way for any run to excuse itself."""
    database, pid = db
    joints(database, pid, [
        (0, "10+00", "ML", 39.0),
        (1, "10+39", "ML", 39.0),
        (2, "12+00", "ML", 39.0),      # 122 ft where 39 ft of pipe sits
        (3, "12+39", "ML", 39.0),
    ])
    assert station_length_conflict(database, pid, "r")


def test_a_correct_run_reports_nothing(db):
    database, pid = db
    joints(database, pid, [
        (0, "10+00", "ML", 39.0),
        (1, "10+39", "ML", 39.0),
        (2, "10+78", "ML", 39.0),
    ])
    assert station_length_conflict(database, pid, "r") == []
