"""Checking the technician named on a sheet, and the register that names them.

Every segment book carries its own copy of the NDE rig log — seventy-one rows
across fifteen books on Bluewater for nine people — and the copies do not always
agree. A certification date is a fact about the person, so two of them cannot
both be right, and NDT-04 reads that date to decide whether a shot was taken on
a lapsed ticket.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402
from weldaudit.rules.ndetech import (  # noqa: E402
    conflicting_cert_date, sheet_technician_mismatch,
)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "t.db")
    pid = database.upsert_project("T", str(tmp_path))
    with database.tx() as c:
        c.execute(
            """INSERT INTO document(id, project_id, path, filename, ext, kind)
               VALUES(1, ?, 'x.pdf', 'GFB-037-040.pdf', '.pdf', 'nde_reader_sheet')""",
            (pid,),
        )
    return database, pid


def logged(db, pid, name, *, rig="D", segment="20 LP", cert="2024-11-25",
           arrived="2025-07-16", company="NORTHFIELD INSPECTION"):
    with db.tx() as c:
        c.execute(
            """INSERT INTO nde_tech(project_id, segment, company, name,
                                    rig_letter, certs, acuity, cert_date, arrived)
               VALUES(?, ?, ?, ?, ?, 'Y', 'Y', ?, ?)""",
            (pid, segment, company, name, rig, cert, arrived),
        )


def shot(db, pid, technician, *, prefix="GFB", nde_id="GFB-037",
         when="2025-09-09", evidence="text"):
    with db.tx() as c:
        c.execute(
            """INSERT INTO nde_shot(project_id, document_id, fingerprint, nde_id,
                                    prefix, number, suffix, sheet_date,
                                    technician, evidence)
               VALUES(?, 1, 'fp1', ?, ?, 37, '', ?, ?, ?)""",
            (pid, nde_id, prefix, when, technician, evidence),
        )


# -- the sheet against the rig log ------------------------------------------

def test_a_text_read_technician_is_checked(db):
    # The rule was written when only a vision pass could read a name off a
    # sheet and was restricted to `evidence='vision'`. The NFI forms carry the
    # technician in their text layer, so that restriction kept the check
    # asleep on 1,929 shots.
    database, pid = db
    logged(database, pid, 'JOHN DAVID "JD" WILLIAMS', rig="G")
    shot(database, pid, "MAREK FERREIRA", evidence="text")
    found = sheet_technician_mismatch(database, pid, "r")
    assert len(found) == 1
    assert "MAREK FERREIRA" in found[0]["message"]


def test_the_matching_name_is_quiet_whatever_read_it(db):
    database, pid = db
    logged(database, pid, 'JOHN DAVID "JD" WILLIAMS', rig="G")
    shot(database, pid, "JD WILLIAMS", evidence="text")
    assert sheet_technician_mismatch(database, pid, "r") == []


# -- the rig log against itself ---------------------------------------------

def test_two_certification_dates_for_one_person(db):
    database, pid = db
    for seg in ("20 LP", "8-in OIL", "6 IN FUEL GAS SEG A"):
        logged(database, pid, "ROWAN LINDQVIST", segment=seg, cert="2024-11-25")
    logged(database, pid, "ROWEN LINDQVIST", segment="16 PW BLUEWATER",
           cert="2025-08-29")
    found = conflicting_cert_date(database, pid, "r")
    assert len(found) == 1
    assert "2024-11-25 in 3 segment books" in found[0]["message"]
    assert "2025-08-29 in 1" in found[0]["message"]


def test_spellings_of_one_name_are_one_person(db):
    # ROWAN / ROWEN, MAREK FERREIR / FERREIRA, MARCO / MARCO SILVA.
    database, pid = db
    logged(database, pid, "ROWAN LINDQVIST", segment="a", cert="2024-11-25")
    logged(database, pid, "ROWEN LINDQVIST", segment="b", cert="2025-08-29")
    found = conflicting_cert_date(database, pid, "r")
    assert len(found) == 1
    assert "also spelled" in found[0]["message"]


def test_two_people_sharing_a_first_name_are_two_people(db):
    """A crew carrying two Juans.

    Marco Silva on rig A and Marco Reyes on rig B share the token MARCO, and
    taking a single shared token as proof merged them into one technician with
    two certification dates. The log was then reported as contradicting itself
    about a man who does not exist - on a real package, on a crew of three.
    """
    database, pid = db
    for seg in ("a", "b", "c"):
        logged(database, pid, "MARCO SILVA", rig="A", segment=seg, cert="2024-05-03")
        logged(database, pid, "MARCO REYES", rig="B", segment=seg, cert="2023-10-22")
    assert conflicting_cert_date(database, pid, "r") == []


def test_a_shared_surname_is_still_not_two_people(db):
    """The same trap from the other end: brothers on one crew."""
    database, pid = db
    logged(database, pid, "MARCO REYES", rig="A", segment="a", cert="2024-05-03")
    logged(database, pid, "ANA REYES", rig="B", segment="a", cert="2023-10-22")
    assert conflicting_cert_date(database, pid, "r") == []


def test_a_surname_only_entry_still_joins_its_person(db):
    """`REYES` on one sheet and `MARCO REYES` on another is one man, and the
    surname is all there is to ask for."""
    database, pid = db
    logged(database, pid, "MARCO REYES", segment="a", cert="2024-05-03")
    logged(database, pid, "REYES", segment="b", cert="2023-10-22")
    found = conflicting_cert_date(database, pid, "r")
    assert len(found) == 1


def test_a_longer_surname_absorbs_a_second_slip(db):
    """MARSH against MARSHE is one man; five-letter names get no such room."""
    database, pid = db
    logged(database, pid, "ELENA MARSH", segment="a", cert="2024-03-24")
    logged(database, pid, "ELENA MARSHE", segment="b", cert="2025-03-24")
    assert len(conflicting_cert_date(database, pid, "r")) == 1


def test_a_consistent_register_is_quiet(db):
    database, pid = db
    for seg in ("20 LP", "8-in OIL", "16 PW BLUEWATER"):
        logged(database, pid, "AVERY OKAFOR", segment=seg, cert="2023-03-31")
    assert conflicting_cert_date(database, pid, "r") == []


def test_differing_arrival_dates_are_not_a_contradiction(db):
    # A technician genuinely arrives on different segments on different days,
    # so the copies differing there is the register working correctly.
    database, pid = db
    logged(database, pid, "ROWAN LINDQVIST", segment="20 LP", arrived="2025-08-09")
    logged(database, pid, "ROWAN LINDQVIST", segment="8-in OIL", arrived="2025-07-16")
    logged(database, pid, "ROWAN LINDQVIST", segment="8 PW", arrived="2025-09-18")
    assert conflicting_cert_date(database, pid, "r") == []


def test_two_people_on_one_rig_are_not_conflated(db):
    # Rig letters are reused as crews rotate: rig E carries Felix Dunmore of
    # Precision and Troy Viner of Cardinal, who are simply different people.
    database, pid = db
    logged(database, pid, "FELIX DUNMORE", rig="E", segment="a",
           cert="2024-12-30", company="PRECISION")
    logged(database, pid, "TROY VINER", rig="E", segment="b",
           cert="2023-09-27", company="CARDINAL")
    assert conflicting_cert_date(database, pid, "r") == []


def test_the_majority_reading_is_named_first(db):
    # Felix Dunmore is 2024-12-30 in six books, 2025-12-30 in four and
    # 2025-08-18 in one. Whichever is true, ten entries are wrong.
    database, pid = db
    for i in range(6):
        logged(database, pid, "FELIX DUNMORE", segment=f"a{i}", cert="2024-12-30")
    for i in range(4):
        logged(database, pid, "FELIX DUNMORE", segment=f"b{i}", cert="2025-12-30")
    logged(database, pid, "FELIX DUNMORE", segment="20 LP", cert="2025-08-18")
    found = conflicting_cert_date(database, pid, "r")
    assert len(found) == 1
    assert "3 different certification dates: 2024-12-30 in 6 segment books" \
        in found[0]["message"]


def test_a_rig_log_with_no_certification_dates_says_nothing(db):
    database, pid = db
    logged(database, pid, "SAMUEL WHITE", cert="")
    assert conflicting_cert_date(database, pid, "r") == []
