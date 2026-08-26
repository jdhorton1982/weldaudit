"""When a hole in the NDE numbering is not a missing reader sheet.

NDE-04 reads a gap in a numbered run as paperwork that was never filed, and
says so in its own docstring: "shots are numbered as they are taken". That
holds where the NDE has its own sequence. It does not hold where the reader
sheets are numbered by the weld they examine -- and on these packages they
always are. Only a share of joints are radiographed, so the holes are welds
that were never shot, which is the normal condition of a line rather than a
defect.

Left unguarded the rule buried the audit it belongs to: one real job produced
215 of these against 94 genuine findings, so three quarters of the report was
noise and the eight welds whose report number really was mistyped sat in the
middle of it.

The guard is a detection, not an assumption. A prefix that also appears in the
as-built weld stamps is the weld numbering; anything else is still gap-checked,
which is where the rule earns its keep.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402
from weldaudit.rules.nde_coverage import (  # noqa: E402
    sequence_gap, weld_numbered_prefixes,
)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "g.db")
    return database, database.upsert_project("G", str(tmp_path))


def shot(db, pid, prefix, number, *, segment="TP-1"):
    with db.tx() as c:
        c.execute(
            """INSERT INTO nde_shot(project_id, nde_id, prefix, number, suffix,
                                    segment, segments, evidence)
               VALUES(?, ?, ?, ?, '', ?, ?, 'text')""",
            (pid, f"{prefix}-{number:03d}", prefix, number, segment, segment),
        )


def stamp(db, pid, tag, *, drawing="D-1"):
    with db.tx() as c:
        c.execute(
            """INSERT INTO weldtrace_stamp(project_id, segment, drawing, weld_tag,
                                           raw_tag, source)
               VALUES(?, 'TP-1', ?, ?, ?, 'as-built')""",
            (pid, drawing, tag, tag),
        )


def rules(db, pid):
    return sequence_gap(db, pid, "run-1")


# -- detecting the weld numbering -------------------------------------------

def test_a_prefix_on_the_as_built_is_the_weld_numbering(db):
    database, pid = db
    stamp(database, pid, "AFW-003")
    stamp(database, pid, "BW-19")
    assert weld_numbered_prefixes(database, pid) == {"AFW", "BW"}


def test_a_job_with_no_as_built_names_no_prefixes(db):
    database, pid = db
    assert weld_numbered_prefixes(database, pid) == set()


def test_the_match_ignores_case(db):
    database, pid = db
    stamp(database, pid, "afw-003")
    assert weld_numbered_prefixes(database, pid) == {"AFW"}


# -- the guard --------------------------------------------------------------

def test_gaps_in_the_weld_numbering_are_not_missing_sheets(db):
    """The failure this whole file exists for.

    Fifty-eight sheets numbered AFW-003..AFW-107 are fifty-eight radiographed
    welds, not fifty-eight sheets with forty-seven of their fellows lost.
    """
    database, pid = db
    for n in range(3, 108, 2):          # every other weld shot
        shot(database, pid, "AFW", n)
    for n in range(3, 108):
        stamp(database, pid, f"AFW-{n:03d}")
    assert rules(database, pid) == []


def test_a_genuine_shot_sequence_is_still_gap_checked(db):
    """What the rule is for, and what the guard must not cost.

    FXR is nowhere in the as-built, so its numbers are its own; a run of
    twenty with one absent is a sheet that was never filed.
    """
    database, pid = db
    for n in list(range(1, 10)) + list(range(11, 21)):
        shot(database, pid, "FXR", n)
    stamp(database, pid, "AFW-003")     # an as-built that says nothing of FXR
    got = rules(database, pid)
    assert [f["subject"] for f in got] == ["FXR-010"]
    assert got[0]["rule"] == "NDE-04"


def test_a_sparse_run_is_not_a_sequence_even_with_no_as_built(db):
    """The backstop for a job that filed no as-built at all.

    Without stamps to check the prefix against, the shape of the run has to
    answer for it: eight numbers spread across sixty-six were never a sequence.
    """
    database, pid = db
    for n in range(1, 67, 8):
        shot(database, pid, "AW", n)
    assert rules(database, pid) == []


def test_a_dense_run_with_no_as_built_is_still_checked(db):
    database, pid = db
    for n in list(range(1, 20)) + list(range(21, 41)):
        shot(database, pid, "GFB", n)
    assert [f["subject"] for f in rules(database, pid)] == ["GFB-020"]


def test_an_unbroken_run_reports_nothing(db):
    database, pid = db
    for n in range(1, 21):
        shot(database, pid, "FXR", n)
    assert rules(database, pid) == []


def test_one_guarded_prefix_does_not_silence_another(db):
    """The guard is per series, not per project."""
    database, pid = db
    for n in range(3, 108, 2):
        shot(database, pid, "AFW", n)
        stamp(database, pid, f"AFW-{n:03d}")
    for n in list(range(1, 10)) + list(range(11, 21)):
        shot(database, pid, "FXR", n)
    assert [f["subject"] for f in rules(database, pid)] == ["FXR-010"]
