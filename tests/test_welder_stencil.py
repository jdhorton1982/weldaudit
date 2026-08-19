"""Who welded the joint, according to the reader sheet and to the weld report.

Two records of the same joint disagreeing is a traceability break. Getting
the comparison right is entirely a matter of joining the two correctly: an
NDE prefix is reused with independent numbering on each line, so matching on
the identifier alone compares welds that have nothing to do with each other.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402
from weldaudit.rules.nde_coverage import sheet_welder_mismatch  # noqa: E402


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "s.db")
    pid = database.upsert_project("S", str(tmp_path))
    with database.tx() as c:
        c.execute(
            """INSERT INTO document(id, project_id, path, filename, ext, kind)
               VALUES(1, ?, 'x.pdf', 'sheet.pdf', '.pdf', 'nde_reader_sheet')""",
            (pid,),
        )
    return database, pid


def shot(db, pid, nde_id, welder, *, segment="4IN FG SEG C", segments=None,
         evidence="text"):
    with db.tx() as c:
        c.execute(
            """INSERT INTO nde_shot(project_id, document_id, fingerprint, segments,
                                    segment, nde_id, prefix, number, suffix,
                                    welder, evidence)
               VALUES(?, 1, 'fp1', ?, ?, ?, 'AFB', 6, '', ?, ?)""",
            (pid, segments if segments is not None else segment, segment,
             nde_id, welder, evidence),
        )


def weld(db, pid, nde_id, welder, *, segment="4IN FG SEG C", weld_no=None):
    with db.tx() as c:
        c.execute(
            """INSERT INTO weld(project_id, document_id, segment, line, weld_no,
                                nde_id, welder_root, source)
               VALUES(?, 1, ?, 'FG-4', ?, ?, ?, 'weld_map_text')""",
            (pid, segment, weld_no or nde_id, nde_id, welder),
        )


def run(db, pid):
    return sheet_welder_mismatch(db, pid, "r")


# -- the comparison ---------------------------------------------------------

def test_a_disagreement_is_reported(db):
    # `AFB-006` on 4IN FG SEG C: the sheet prints ARU, the map balloon says
    # ARW, and both are welders on PLU's roster.
    database, pid = db
    shot(database, pid, "AFB-006", "ARU")
    weld(database, pid, "AFB-006", "ARW")
    found = run(database, pid)
    assert len(found) == 1
    assert "names welder ARU" in found[0]["message"]
    assert "names ARW" in found[0]["message"]


def test_agreement_is_quiet(db):
    database, pid = db
    shot(database, pid, "AFB-006", "ARW")
    weld(database, pid, "AFB-006", "ARW")
    assert run(database, pid) == []


def test_a_text_read_stencil_is_checked(db):
    # The rule was restricted to `evidence='vision'` and so had never examined
    # a sheet; the IIA forms carry the stencil in their text layer.
    database, pid = db
    shot(database, pid, "AFB-006", "ARU", evidence="text")
    weld(database, pid, "AFB-006", "ARW")
    assert len(run(database, pid)) == 1


# -- the join ---------------------------------------------------------------

def test_the_same_id_on_another_segment_is_another_weld(db):
    # PLU files seven different welds called AFB-001P, one per segment,
    # welded by five different people. Matching on the identifier alone
    # cross-joins all seven against all seven — 507 findings, 15 of them real.
    database, pid = db
    shot(database, pid, "AFB-001P", "ARV", segment="6IN GL")
    weld(database, pid, "AFB-001P", "ARV", segment="6IN GL")
    shot(database, pid, "AFB-001P", "ARO", segment="16IN LP")
    weld(database, pid, "AFB-001P", "ARO", segment="16IN LP")
    assert run(database, pid) == []


def test_a_sheet_filed_in_several_books_matches_any_of_them(db):
    # Reader sheets are copied into every segment book they touch, so the
    # segment on the row is only one of the places it is filed.
    database, pid = db
    shot(database, pid, "AFB-006", "ARW", segment="6IN GL",
         segments="6IN GL; 4IN FG SEG C")
    weld(database, pid, "AFB-006", "ARW", segment="4IN FG SEG C")
    assert run(database, pid) == []


def test_a_weld_is_judged_on_everything_said_about_it(db):
    # `AFB-008` on GL 31 has one sheet naming AM53 and another naming EM93,
    # and the register names both. Pair-by-pair that is two disagreements;
    # taken as a joint it is none.
    database, pid = db
    shot(database, pid, "AFB-008", "AM53")
    shot(database, pid, "AFB-008", "EM93")
    weld(database, pid, "AFB-008", "AM53")
    weld(database, pid, "AFB-008", "EM93")
    assert run(database, pid) == []


def test_a_joint_welded_by_two_people_needs_only_one_to_match(db):
    database, pid = db
    shot(database, pid, "AFB-023", "AFM/ARV")
    weld(database, pid, "AFB-023", "ARV")
    assert run(database, pid) == []


def test_one_finding_per_weld_not_per_pair(db):
    database, pid = db
    shot(database, pid, "AFB-007", "ARU")
    shot(database, pid, "AFB-007", "ARU")
    weld(database, pid, "AFB-007", "ARW")
    weld(database, pid, "AFB-007", "ARW")
    assert len(run(database, pid)) == 1


def test_a_blank_side_is_not_a_disagreement(db):
    database, pid = db
    shot(database, pid, "AFB-006", "ARU")
    weld(database, pid, "AFB-006", "")
    assert run(database, pid) == []
