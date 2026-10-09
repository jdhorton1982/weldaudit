"""Reader sheets the weld maps never account for.

A sheet whose shot id is on no weld map is either a weld missing from the
as-built or a sheet belonging to another job. Both are worth saying.

The rule will not say it about a series the maps barely cover, and that guard
is right: one real package has two isometrics with a text layer against
nineteen hundred welds recorded on daily reports, and judging fourteen hundred
sheets against those ten welds reported two hundred and seventy-five orphans,
every one a statement about the map's size rather than the sheet.

**The guard was measuring the wrong thing.** A sheet lists a weld once per
shot - the root, the cap, a repeat after a repair - so counting rows measured
how often a series was radiographed and compared that against how many welds
were claimed. On a package averaging 1.8 shots per weld that halved every
ratio: a series with 94% of its welds on a map read as 45% covered, the three
best-covered series in the job were all dismissed as too thin to judge, and
the rule said nothing at all about seventy-four orphaned sheets.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402
from weldaudit.rules.nde_coverage import sheet_without_weld  # noqa: E402


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "s.db")
    pid = database.upsert_project("S", str(tmp_path))
    with database.tx() as c:
        c.execute(
            """INSERT INTO document(id, project_id, path, filename, ext, kind)
               VALUES(1, ?, 'm.pdf', 'm.pdf', '.pdf', 'weld_map')""",
            (pid,),
        )
    return database, pid


def weld(db, pid, nde_id, *, segment="SEG"):
    with db.tx() as c:
        c.execute(
            """INSERT INTO weld(project_id, document_id, segment, line, weld_no,
                                nde_id, source)
               VALUES(?, 1, ?, 'L-1', ?, ?, 'weld_map_vision')""",
            (pid, segment, nde_id, nde_id),
        )


def shot(db, pid, nde_id, *, segment="SEG", times=1):
    """A sheet lists a weld once per shot: root, cap, and any repeat."""
    prefix, _, number = nde_id.partition("-")
    with db.tx() as c:
        for _ in range(times):
            c.execute(
                """INSERT INTO nde_shot(project_id, document_id, segment, segments,
                                        nde_id, prefix, number, suffix, evidence)
                   VALUES(?, 1, ?, ?, ?, ?, ?, '', 'text')""",
                (pid, segment, segment, nde_id, prefix,
                 int("".join(ch for ch in number if ch.isdigit()) or 0)),
            )


def subjects(found):
    return sorted(f["subject"] for f in found)


# -- the case the counting bug hid ------------------------------------------

def test_a_series_shot_twice_per_weld_is_still_well_covered(db):
    """The regression. Ten welds, every one on a map, every one shot twice.

    Counted as rows that is 10 claimed against 20 on file - half covered, and
    judged too thin. Counted as welds it is complete, and the one sheet with
    no weld behind it is worth reporting.
    """
    database, pid = db
    for i in range(1, 11):
        weld(database, pid, f"AFB-{i:03d}")
        shot(database, pid, f"AFB-{i:03d}", times=2)
    shot(database, pid, "AFB-099", times=2)          # on no weld map

    assert subjects(sheet_without_weld(database, pid, "r")) == ["AFB-099"]


def test_a_weld_shot_once_is_unaffected(db):
    database, pid = db
    for i in range(1, 11):
        weld(database, pid, f"AFB-{i:03d}")
        shot(database, pid, f"AFB-{i:03d}")
    shot(database, pid, "AFB-099")
    assert subjects(sheet_without_weld(database, pid, "r")) == ["AFB-099"]


def test_repeat_shots_of_the_orphan_report_it_once(db):
    database, pid = db
    for i in range(1, 11):
        weld(database, pid, f"AFB-{i:03d}")
        shot(database, pid, f"AFB-{i:03d}", times=2)
    shot(database, pid, "AFB-099", times=4)
    assert subjects(sheet_without_weld(database, pid, "r")) == ["AFB-099"]


# -- the guard still guards -------------------------------------------------

def test_a_series_the_maps_barely_cover_is_not_judged(db):
    """The failure the guard exists for: 275 orphans reported because the
    yardstick was 0.5% of the job."""
    database, pid = db
    weld(database, pid, "AXR-001")
    for i in range(1, 41):
        shot(database, pid, f"AXR-{i:03d}", times=2)
    assert sheet_without_weld(database, pid, "r") == []


def test_a_series_with_no_welds_at_all_is_not_judged(db):
    database, pid = db
    weld(database, pid, "AFB-001")
    shot(database, pid, "AFB-001")
    for i in range(1, 6):
        shot(database, pid, f"BXR-{i:03d}")
    assert subjects(sheet_without_weld(database, pid, "r")) == []


def test_nothing_is_reported_when_every_sheet_has_its_weld(db):
    database, pid = db
    for i in range(1, 11):
        weld(database, pid, f"AFB-{i:03d}")
        shot(database, pid, f"AFB-{i:03d}", times=3)
    assert sheet_without_weld(database, pid, "r") == []


def test_a_project_with_no_weld_maps_says_nothing(db):
    database, pid = db
    for i in range(1, 6):
        shot(database, pid, f"AFB-{i:03d}")
    assert sheet_without_weld(database, pid, "r") == []
