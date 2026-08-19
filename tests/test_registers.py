"""Reconciling two records of the same welds.

The awkward case, and the one Kestrel 8 actually has, is a pair of registers where
only one numbers its welds. Nothing can be lined up joint by joint there, and
the rules must say so rather than manufacture a pairing or fall silent.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402
from weldaudit.rules import registers as rr  # noqa: E402


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "r.db")
    return database, database.upsert_project("R", str(tmp_path))


def add(db, pid, source, *, nde_id="", weld_no="1", welders="", segment="SEG A",
        date="2025-06-01"):
    with db.tx() as c:
        c.execute(
            """INSERT INTO weld(project_id, segment, line, weld_no, nde_id,
                                welder_root, date_welded, source)
               VALUES(?, ?, '16 LP', ?, ?, ?, ?, ?)""",
            (pid, segment, weld_no, nde_id, welders, date, source),
        )


DWR = "daily_weld_report_vision"
MAP = "weld_map_vision"


# -- identity matching ------------------------------------------------------

def test_a_weld_only_on_the_map_is_reported(db):
    database, pid = db
    add(database, pid, MAP, nde_id="AFB-018", welders="AFM/ARV")
    add(database, pid, MAP, nde_id="AFB-019", welders="ARO/ARV")
    add(database, pid, DWR, nde_id="AFB-018", welders="AFM/ARV")

    findings = rr.missing_from_register(database, pid, "r1")
    assert len(findings) == 1
    assert "AFB-019" in findings[0]["message"]
    assert "not on the daily weld reports" in findings[0]["message"]


def test_registers_that_agree_produce_nothing(db):
    database, pid = db
    for nde in ("AFB-018", "AFB-019"):
        add(database, pid, MAP, nde_id=nde, welders="AFM/ARV")
        add(database, pid, DWR, nde_id=nde, welders="AFM/ARV")
    assert rr.missing_from_register(database, pid, "r1") == []


def test_the_two_daily_report_sources_are_one_register(db):
    # A job part-parsed from spreadsheets and part-read from scans must not be
    # reconciled against itself.
    database, pid = db
    add(database, pid, "daily_weld_report", nde_id="AFB-018")
    add(database, pid, DWR, nde_id="AFB-019")
    assert rr.missing_from_register(database, pid, "r1") == []
    assert rr.register_overlap(database, pid, "r1") == []


def test_segments_are_compared_independently(db):
    database, pid = db
    add(database, pid, MAP, nde_id="AFB-018", segment="SEG A")
    add(database, pid, DWR, nde_id="AFB-018", segment="SEG A")
    add(database, pid, MAP, nde_id="AFB-050", segment="SEG B")
    add(database, pid, DWR, nde_id="AFB-051", segment="SEG B")

    segments = {f["segment"] for f in rr.missing_from_register(database, pid, "r1")}
    assert segments == {"SEG B"}


# -- welder disagreement ----------------------------------------------------

def test_disagreeing_welders_are_flagged(db):
    database, pid = db
    add(database, pid, MAP, nde_id="AFB-018", welders="AFM/ARV")
    add(database, pid, DWR, nde_id="AFB-018", welders="ARO/ARS")

    findings = rr.welder_disagreement(database, pid, "r1")
    assert len(findings) == 1
    assert "AFM" in findings[0]["message"] and "ARO" in findings[0]["message"]
    assert findings[0]["message"].startswith("The ")


def test_a_partial_welder_overlap_is_not_a_disagreement(db):
    # One welder in common means the records are consistent, not contradictory.
    database, pid = db
    add(database, pid, MAP, nde_id="AFB-018", welders="AFM/ARV")
    add(database, pid, DWR, nde_id="AFB-018", welders="AFM")
    assert rr.welder_disagreement(database, pid, "r1") == []


def test_a_blank_welder_column_is_not_a_disagreement(db):
    database, pid = db
    add(database, pid, MAP, nde_id="AFB-018", welders="AFM/ARV")
    add(database, pid, DWR, nde_id="AFB-018", welders="")
    assert rr.welder_disagreement(database, pid, "r1") == []


# -- the unmatched case, which is the real Kestrel 8 shape ---------------------

def test_unnumbered_welds_are_compared_by_count_not_silently_skipped(db):
    database, pid = db
    for n in ("AFB-018", "AFB-019", "AFB-020"):
        add(database, pid, MAP, nde_id=n, welders="AFM/ARV")
    # The daily reports leave WELD # blank, so nothing carries an NDE id.
    for i in range(5):
        add(database, pid, DWR, weld_no=f"row {i + 1}", welders="AFM/ARV")

    # Nothing can be matched, so REG-01 must stay quiet rather than claim all
    # five daily-report welds are missing from the map.
    assert rr.missing_from_register(database, pid, "r1") == []

    overlap = rr.register_overlap(database, pid, "r1")
    assert len(overlap) == 1
    msg = overlap[0]["message"]
    assert "records 3" in msg and "record 5" in msg
    assert "counts differ by 2" in msg
    assert overlap[0]["severity"] == "major"
    # The register's name governs the verb, and the phrase is not doubled.
    assert "the daily weld reports name no weld numbers" in msg
    assert msg.count("could be matched") == 1
    assert "between them, because" not in msg


def test_equal_counts_with_no_match_is_informational(db):
    database, pid = db
    for n in ("AFB-018", "AFB-019"):
        add(database, pid, MAP, nde_id=n)
    for i in range(2):
        add(database, pid, DWR, weld_no=f"row {i + 1}")

    overlap = rr.register_overlap(database, pid, "r1")
    assert overlap[0]["severity"] == "info"
    assert "counts differ" not in overlap[0]["message"]


def test_the_overlap_note_states_the_deduplicated_figure(db):
    database, pid = db
    add(database, pid, MAP, nde_id="AFB-018")
    add(database, pid, DWR, nde_id="AFB-018")
    msg = rr.register_overlap(database, pid, "r1")[0]["message"]
    assert "1 weld could be matched" in msg
    # The same physical weld on both registers: the table must say 1, not 2.
    assert "coverage table shows 1 weld " in msg
    assert "deduplicated estimate" in msg


def test_the_stated_figure_matches_the_coverage_table(db):
    from weldaudit.rules.nde_coverage import coverage_summary

    database, pid = db
    for i in range(3):
        add(database, pid, DWR, weld_no=f"row {i}")
    for i in range(5):
        add(database, pid, MAP, nde_id=f"AFB-{i:03d}")

    stated = rr.register_overlap(database, pid, "r1")[0]["detail"]
    table = coverage_summary(database, pid)[0]["welds"]
    msg = rr.register_overlap(database, pid, "r1")[0]["message"]
    assert f"coverage table shows {table} welds" in msg
    assert '"matched_by_id": 0' in stated


def test_a_single_register_needs_no_reconciliation(db):
    database, pid = db
    add(database, pid, MAP, nde_id="AFB-018")
    assert rr.register_overlap(database, pid, "r1") == []
    assert rr.missing_from_register(database, pid, "r1") == []


def test_sources_that_are_not_weld_registers_are_ignored(db):
    database, pid = db
    add(database, pid, MAP, nde_id="AFB-018")
    add(database, pid, "some_other_source", nde_id="AFB-099")
    assert rr.register_overlap(database, pid, "r1") == []


# -- grammar ----------------------------------------------------------------

def test_register_names_agree_with_their_verb(db):
    assert rr._records("the daily weld reports") == "record"
    assert rr._records("the weld map") == "records"
    assert rr._verb("the daily weld reports") == "name"
    assert rr._verb("the weld log export") == "names"
