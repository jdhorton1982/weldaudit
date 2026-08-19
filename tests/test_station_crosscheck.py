"""The as-built's two statements about geometry, against each other.

Every joint carries a survey station and the pipe carries a length. They are
independent measurements of the same run and they agree closely — 96% of
Bluewater's eleven hundred pipe-to-pipe steps land within two feet — which is
what makes a disagreement worth reporting.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402
from weldaudit.rules.asbuilt import station_length_conflict  # noqa: E402


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "a.db")
    pid = database.upsert_project("A", str(tmp_path))
    with database.tx() as c:
        c.execute(
            """INSERT INTO document(id, project_id, path, filename, ext, kind)
               VALUES(1, ?, 'a.xlsx', 'a.xlsx', '.xlsx', 'as_built')""",
            (pid,),
        )
    return database, pid


def joint(db, pid, seq, station_ft, length, *, description="ML",
          segment="16 PW BLUEWATER", sheet="As-Built (022)", band=2):
    station = f"{int(station_ft) // 100}+{int(station_ft) % 100:02d}"
    with db.tx() as c:
        c.execute(
            """INSERT INTO asbuilt_joint(project_id, document_id, segment, sheet,
                                         band, seq, station, station_ft, length,
                                         description, source)
               VALUES(?, 1, ?, ?, ?, ?, ?, ?, ?, ?, 'asbuilt_xlsx')""",
            (pid, segment, sheet, band, seq, station, station_ft,
             str(length) if length is not None else None, description),
        )


def run(db, pid):
    return station_length_conflict(db, pid, "r")


# -- the comparison ---------------------------------------------------------

def test_a_run_that_adds_up_is_quiet(db):
    database, pid = db
    for i in range(6):
        joint(database, pid, i, 12091 + 39 * i, 39.0)
    assert run(database, pid) == []


def test_a_mistyped_leading_digit_is_caught(db):
    # 120+91, 121+29, **221+68**, 122+06, 122+44 — every neighbour in the
    # 12,000s, and 121+29 to 121+68 is exactly one 39-foot joint.
    database, pid = db
    for i, ft in enumerate((12091, 12129, 22168, 12206, 12244, 12282)):
        joint(database, pid, i, ft, 39.0)
    found = run(database, pid)
    assert any("221+68" in f["subject"] for f in found)


def test_survey_rounding_is_not_a_finding(db):
    # 82% of real steps land within a foot and 96% within two; a joint laid to
    # 39.0 ft and surveyed at 38 is the register working.
    database, pid = db
    for i, ft in enumerate((0, 38, 77, 115, 154)):
        joint(database, pid, i, ft, 39.0)
    assert run(database, pid) == []


def test_the_discrepancy_must_be_a_whole_joint(db):
    # No tolerance is chosen out of the air: the survey has to be claiming a
    # joint's worth of pipe more or less than the drawing says was laid. A
    # 59 ft step against a 39 ft joint is 20 ft out — under one joint, quiet.
    database, pid = db
    for i, ft in enumerate((0, 39, 78, 137, 176)):
        joint(database, pid, i, ft, 39.0)
    assert run(database, pid) == []


def test_a_hundred_foot_jump_is_reported(db):
    database, pid = db
    for i, ft in enumerate((593, 732, 771, 810, 849)):
        joint(database, pid, i, ft, 39.0)
    found = run(database, pid)
    assert len(found) == 1
    assert "100 ft" in found[0]["message"]


# -- what is not measurable --------------------------------------------------

def test_a_fitting_is_not_measured(db):
    # A coupling's LENGTH cell holds 39.0 — the pipe beside it, not itself —
    # so the one-foot step it really occupies looks like a 38 ft error.
    database, pid = db
    joint(database, pid, 0, 7655, 39.0, description="COUPLING")
    joint(database, pid, 1, 7656, 39.0)
    joint(database, pid, 2, 7695, 39.0)
    assert run(database, pid) == []


def test_a_pup_is_not_measured(db):
    database, pid = db
    joint(database, pid, 0, 6680, 39.0, description="PUP")
    joint(database, pid, 1, 6684, 39.0)
    joint(database, pid, 2, 6723, 39.0)
    assert run(database, pid) == []


def test_a_joint_with_no_length_cannot_be_measured_from(db):
    # The LENGTH cell is merged and filled on only every other joint, so the
    # step out of a blank one has nothing to check it against — which is why
    # the real 221+68 is caught on the step *into* 122+06, not out of 121+29.
    database, pid = db
    joint(database, pid, 0, 12091, 39.0)
    joint(database, pid, 1, 12129, None)     # blank: 121+29 -> 221+68 unmeasured
    joint(database, pid, 2, 22168, 39.0)     # 221+68 -> 122+06 is measured
    joint(database, pid, 3, 12206, None)
    assert [f["subject"] for f in run(database, pid)] == ["221+68 to 122+06"]


def test_a_descending_line_is_read_in_its_own_direction(db):
    # Some lines are stationed descending; the direction comes from the run's
    # own majority, not from an assumption.
    database, pid = db
    for i, ft in enumerate((20000, 19961, 19922, 19883, 19844)):
        joint(database, pid, i, ft, 39.0)
    assert run(database, pid) == []


def test_bands_are_not_joined_end_to_end(db):
    # A band's last joint is repeated as the next band's first, so the two are
    # not consecutive on the ground.
    database, pid = db
    joint(database, pid, 0, 12091, 39.0, band=2)
    joint(database, pid, 1, 12130, 39.0, band=2)
    joint(database, pid, 0, 30000, 39.0, band=3)
    joint(database, pid, 1, 30039, 39.0, band=3)
    assert run(database, pid) == []
