"""The NDE coverage table, and how it survives a segment with two registers.

Adding registers up counts the same physical weld twice; they also cannot be
matched joint by joint when one of them numbers nothing. The headline figure is
therefore a documented estimate, and these tests pin both the arithmetic and
the fact that the breakdown always travels with it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402
from weldaudit.rules.nde_coverage import coverage_summary  # noqa: E402

DWR = "daily_weld_report_vision"
MAP = "weld_map_vision"
CSV = "weld_log_csv"


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "c.db")
    return database, database.upsert_project("C", str(tmp_path))


def add(db, pid, source, *, nde_id="", weld_no="1", segment="SEG A"):
    with db.tx() as c:
        c.execute(
            """INSERT INTO weld(project_id, segment, line, weld_no, nde_id, source)
               VALUES(?, ?, '16 LP', ?, ?, ?)""",
            (pid, segment, weld_no, nde_id, source),
        )


def only(db, pid):
    rows = coverage_summary(db, pid)
    assert len(rows) == 1
    return rows[0]


# -- one register -----------------------------------------------------------

def test_a_single_register_is_counted_not_estimated(db):
    database, pid = db
    for i in range(4):
        add(database, pid, MAP, nde_id=f"AFB-{i:03d}")
    row = only(database, pid)
    assert row["welds"] == 4 and row["welds_with_nde"] == 4
    assert row["pct_referenced"] == 100
    assert row["multiple_registers"] is False
    assert row["registers"][0]["register"] == "the weld map"


def test_one_register_is_never_deduplicated_against_itself(db):
    # Two rows citing the same report is normal within a daily-report register
    # (the same weld written up twice, or one report covering two passes).
    # Collapsing them would move a segment's percentage for a reason that has
    # nothing to do with having two registers.
    database, pid = db
    add(database, pid, DWR, nde_id="DTI-005", weld_no="1")
    add(database, pid, DWR, nde_id="DTI-005", weld_no="2")
    row = only(database, pid)
    assert row["welds"] == 2 and row["welds_with_nde"] == 2
    assert row["pct_referenced"] == 100


def test_unnumbered_welds_lower_the_percentage(db):
    database, pid = db
    add(database, pid, DWR, nde_id="AFB-001")
    for i in range(3):
        add(database, pid, DWR, weld_no=f"row {i}")
    row = only(database, pid)
    assert row["welds"] == 4 and row["welds_with_nde"] == 1
    assert row["pct_referenced"] == 25


# -- two registers ----------------------------------------------------------

def test_two_registers_are_not_summed(db):
    # The real Kestrel 8 shape: 3 unnumbered daily-report welds and a weld map
    # numbering 5. Summing would claim 8 welds on a line that has 5.
    database, pid = db
    for i in range(3):
        add(database, pid, DWR, weld_no=f"row {i}")
    for i in range(5):
        add(database, pid, MAP, nde_id=f"AFB-{i:03d}")

    row = only(database, pid)
    assert row["welds"] == 5
    assert row["multiple_registers"] is True


def test_the_breakdown_travels_with_the_estimate(db):
    database, pid = db
    for i in range(3):
        add(database, pid, DWR, weld_no=f"row {i}")
    for i in range(5):
        add(database, pid, MAP, nde_id=f"AFB-{i:03d}")

    registers = {r["register"]: r for r in only(database, pid)["registers"]}
    assert registers["the daily weld reports"]["welds"] == 3
    assert registers["the weld map"]["welds"] == 5
    # Largest register first, so the headline's origin is obvious.
    assert only(database, pid)["registers"][0]["register"] == "the weld map"


def test_fully_overlapping_registers_count_once(db):
    database, pid = db
    for i in range(5):
        add(database, pid, DWR, nde_id=f"AFB-{i:03d}")
        add(database, pid, MAP, nde_id=f"AFB-{i:03d}")
    row = only(database, pid)
    assert row["welds"] == 5 and row["welds_with_nde"] == 5
    assert row["pct_referenced"] == 100


def test_partially_overlapping_registers_union_their_ids(db):
    # The map has 001-004, the log has 003-006: six distinct welds, not eight.
    database, pid = db
    for i in range(1, 5):
        add(database, pid, MAP, nde_id=f"AFB-{i:03d}")
    for i in range(3, 7):
        add(database, pid, CSV, nde_id=f"AFB-{i:03d}")
    row = only(database, pid)
    assert row["welds"] == 6 and row["welds_with_nde"] == 6


def test_the_estimate_is_never_below_the_largest_register(db):
    # One register numbers nothing but is the bigger view of the segment.
    database, pid = db
    for i in range(9):
        add(database, pid, DWR, weld_no=f"row {i}")
    add(database, pid, MAP, nde_id="AFB-001")
    assert only(database, pid)["welds"] == 9


def test_the_two_daily_report_sources_are_one_register(db):
    # A job part-parsed from spreadsheets and part-read from scans has one
    # register recorded two ways, and must not look like two.
    database, pid = db
    add(database, pid, "daily_weld_report", nde_id="AFB-001")
    add(database, pid, DWR, nde_id="AFB-002")
    row = only(database, pid)
    assert row["multiple_registers"] is False
    assert row["welds"] == 2


def test_segments_are_summarised_independently(db):
    database, pid = db
    add(database, pid, MAP, nde_id="AFB-001", segment="SEG A")
    for i in range(4):
        add(database, pid, DWR, weld_no=f"row {i}", segment="SEG B")
    rows = {r["segment"]: r for r in coverage_summary(database, pid)}
    assert rows["SEG A"]["welds"] == 1 and rows["SEG B"]["welds"] == 4


def test_rows_are_ordered_worst_coverage_first(db):
    database, pid = db
    add(database, pid, MAP, nde_id="AFB-001", segment="GOOD")
    add(database, pid, DWR, weld_no="row 1", segment="BAD")
    assert [r["segment"] for r in coverage_summary(database, pid)] == ["BAD", "GOOD"]


def test_no_welds_yields_no_rows(db):
    database, pid = db
    assert coverage_summary(database, pid) == []
