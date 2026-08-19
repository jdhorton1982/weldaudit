"""Recovering welds from a scanned daily weld report, and keeping them.

The payloads here mirror a real Kestrel 8 report: three rows, no weld numbers
filled in, ditto marks on rows two and three, and dashes in the fill and cap
columns. Those three habits are what a naive reader gets wrong.

The durability tests matter as much as the extraction ones. Re-indexing clears
every table the vision pass writes into, so without replay an ``audit`` run
after a vision run would silently discard results the user paid for.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402
from weldaudit.extract import welders as welder_extract  # noqa: E402
from weldaudit.extract.vision_pass import (  # noqa: E402
    VISION_WELD_SOURCE, Target, replay, run,
)


def _row(**kw):
    base = {
        "weld_no": None, "size": None, "weld_type": None, "process": None,
        "welder_root": None, "welder_hot_pass": None, "welder_fill": None,
        "welder_cap": None, "notes": None, "expanded_from_ditto": False,
    }
    base.update(kw)
    return base


#: The 5/12/25 LP Seg. D report, as a correct reading would come back:
#: weld numbers blank, rows 2-3 expanded from ditto marks, dashes dropped.
PLU22_PAGE = {
    "page_is_weld_report": True,
    "report_date": "5/12/25", "afe": "NI.2024.14306.CAP.01", "unit": "PLU",
    "job_name": "Kestrel 8 Takeaways", "contractor": "Marden",
    "inspector": "Sando Perez", "drawing_no": "LP Seg. D", "system": "Dogtown",
    "line_size": '16"', "wall_thickness": "STD", "material": "CS", "service": "LP",
    "rows": [
        _row(size='16"', weld_type="BW", process="SMAW", welder_root="ARS/ARO",
             welder_fill="-", welder_cap="-", notes="DTD22MP-LP-16-1B"),
        _row(size='16"', weld_type="BW", process="SMAW", welder_root="ARS/ARO",
             welder_fill="-", welder_cap="-", notes="DTD22MP-LP-16-1B",
             expanded_from_ditto=True),
        _row(size='16"', weld_type="BW", process="SMAW", welder_root="ARS/ARO",
             welder_fill="-", welder_cap="-", notes="DTD22MP-LP-16-1B",
             expanded_from_ditto=True),
    ],
}


class StubReader:
    def __init__(self, pages):
        self.pages = pages

    def cached(self, fingerprint, page_no, kind):
        return None

    def read_page(self, path, page_no, kind, fingerprint):
        return self.pages[page_no]


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "w.db")
    pid = database.upsert_project("P", str(tmp_path))
    with database.tx() as c:
        c.execute(
            """INSERT INTO document(id, project_id, path, filename, ext, fingerprint,
                                    segment, kind)
               VALUES(1, ?, 'dwr.pdf', 'DTD22 DWR 5.12.25.pdf', '.pdf', 'fpW',
                      '16IN LP', 'daily_weld_report')""",
            (pid,),
        )
    return database, pid


def _target():
    return Target(1, "dwr.pdf", "DTD22 DWR 5.12.25.pdf", "fpW", 1, "test", "16IN LP")


# -- extraction -------------------------------------------------------------

def test_all_rows_become_welds(db):
    database, pid = db
    result = run(database, pid, "daily_weld_report", StubReader([PLU22_PAGE]), [_target()])
    assert result.updated == 3
    assert database.one(
        "SELECT COUNT(*) n FROM weld WHERE project_id=? AND source=?",
        (pid, VISION_WELD_SOURCE))["n"] == 3


def test_blank_weld_numbers_become_positional_not_invented(db):
    database, pid = db
    run(database, pid, "daily_weld_report", StubReader([PLU22_PAGE]), [_target()])
    numbers = [r["weld_no"] for r in database.q(
        "SELECT weld_no FROM weld WHERE project_id=? ORDER BY id", (pid,))]
    # Positional identifiers, clearly marked as such — not fabricated weld ids.
    assert numbers == ["row 1", "row 2", "row 3"]


def test_a_real_weld_number_is_kept_verbatim(db):
    database, pid = db
    page = dict(PLU22_PAGE, rows=[_row(weld_no="GFB-37", process="SMAW",
                                       welder_root="ARS")])
    run(database, pid, "daily_weld_report", StubReader([page]), [_target()])
    assert database.one("SELECT weld_no FROM weld WHERE project_id=?",
                        (pid,))["weld_no"] == "GFB-37"


def test_dashes_in_welder_columns_are_not_welders(db):
    database, pid = db
    run(database, pid, "daily_weld_report", StubReader([PLU22_PAGE]), [_target()])
    row = database.one("SELECT * FROM weld WHERE project_id=? LIMIT 1", (pid,))
    assert row["welder_root"] == "ARS/ARO"
    assert row["welder_fill"] == "" and row["welder_cap"] == ""


def test_header_fields_land_on_every_weld(db):
    database, pid = db
    run(database, pid, "daily_weld_report", StubReader([PLU22_PAGE]), [_target()])
    row = database.one("SELECT * FROM weld WHERE project_id=? LIMIT 1", (pid,))
    assert row["line"] == "LP"
    assert row["date_welded"] == "2025-05-12"
    assert row["weld_size"] == '16"'
    assert row["process"] == "SMAW" and row["weld_type"] == "BW"


def test_the_notes_column_is_kept_verbatim(db):
    # It may hold an NDE report, a spool number, or a bore reference; the
    # extractor does not decide which.
    database, pid = db
    run(database, pid, "daily_weld_report", StubReader([PLU22_PAGE]), [_target()])
    assert database.one("SELECT note FROM weld WHERE project_id=? LIMIT 1",
                        (pid,))["note"] == "DTD22MP-LP-16-1B"


def test_a_page_that_is_not_a_weld_report_creates_nothing(db):
    database, pid = db
    page = {"page_is_weld_report": False, "rows": []}
    assert run(database, pid, "daily_weld_report", StubReader([page]),
               [_target()]).updated == 0
    assert database.one("SELECT COUNT(*) n FROM weld WHERE project_id=?", (pid,))["n"] == 0


def test_rereading_a_page_does_not_duplicate_its_welds(db):
    database, pid = db
    for _ in range(3):
        run(database, pid, "daily_weld_report", StubReader([PLU22_PAGE]), [_target()])
    assert database.one("SELECT COUNT(*) n FROM weld WHERE project_id=?", (pid,))["n"] == 3


# -- the welders those welds imply ------------------------------------------

def test_recovered_welds_feed_the_welder_extractor(db):
    database, pid = db
    run(database, pid, "daily_weld_report", StubReader([PLU22_PAGE]), [_target()])
    rows, stencils = welder_extract.extract_passes(database, pid)
    # Two welders on the root pass of each of three welds.
    assert stencils == 2 and rows == 6
    assert {r["stencil"] for r in database.q(
        "SELECT DISTINCT stencil FROM welder_pass WHERE project_id=?", (pid,))} == {
        "ARS", "ARO"}


# -- durability across a re-audit -------------------------------------------

def test_replay_restores_welds_from_the_cache(db):
    database, pid = db
    # A pass has run: the page is in the cache and the welds are in place.
    database.ocr_put("fpW:daily_weld_report:2000", 0, "claude-opus-5", PLU22_PAGE)
    run(database, pid, "daily_weld_report", StubReader([PLU22_PAGE]), [_target()])

    # Re-indexing clears the derived tables, exactly as `audit` does.
    with database.tx() as c:
        c.execute("DELETE FROM weld WHERE project_id=?", (pid,))
    assert database.one("SELECT COUNT(*) n FROM weld WHERE project_id=?", (pid,))["n"] == 0

    counts = replay(database, pid, ("daily_weld_report",))
    assert counts["daily_weld_report"] == 3
    assert database.one("SELECT COUNT(*) n FROM weld WHERE project_id=?", (pid,))["n"] == 3


def test_replay_finds_results_read_by_a_different_model(db):
    # A pass run with --model claude-haiku-4-5 must not be discarded by a
    # later audit just because the default model differs.
    database, pid = db
    database.ocr_put("fpW:daily_weld_report:1400", 0, "claude-haiku-4-5", PLU22_PAGE)
    assert replay(database, pid, ("daily_weld_report",))["daily_weld_report"] == 3


def test_replay_ignores_pages_that_failed(db):
    database, pid = db
    database.ocr_put("fpW:daily_weld_report:2000", 0, "claude-opus-5",
                     {"_error": "refused"})
    assert replay(database, pid, ("daily_weld_report",)) == {}


def test_replay_is_idempotent(db):
    database, pid = db
    database.ocr_put("fpW:daily_weld_report:2000", 0, "claude-opus-5", PLU22_PAGE)
    for _ in range(3):
        replay(database, pid, ("daily_weld_report",))
    assert database.one("SELECT COUNT(*) n FROM weld WHERE project_id=?", (pid,))["n"] == 3
