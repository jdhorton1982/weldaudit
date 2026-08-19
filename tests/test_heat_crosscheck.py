"""Heat numbers from the as-built, against the certificates on file.

The as-built is the largest material register in the corpus — 1,624 rows on
Bluewater naming 577 heats, against the 32 its heat maps know — and until now no
rule read it. It is also the only register that says *where* each heat sits,
since every joint carries a station.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402
from weldaudit.rules.materials import (  # noqa: E402
    asbuilt_heats, heat_without_certificate, line_without_certificates,
)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "h.db")
    pid = database.upsert_project("H", str(tmp_path))
    with database.tx() as c:
        c.execute(
            """INSERT INTO document(id, project_id, path, filename, ext, kind)
               VALUES(1, ?, 'm.pdf', 'm.pdf', '.pdf', 'mtr')""",
            (pid,),
        )
    return database, pid


def joint(db, pid, heat, *, segment="16 PW BLUEWATER", station="0+18",
          description="ML", n=1):
    from weldaudit.mtrname import normalise_heat
    with db.tx() as c:
        for i in range(n):
            c.execute(
                """INSERT INTO asbuilt_joint(project_id, document_id, segment,
                                             station, heat, heat_key, description,
                                             source)
                   VALUES(?, 1, ?, ?, ?, ?, ?, 'asbuilt_xlsx')""",
                (pid, segment, station, heat, normalise_heat(heat), description),
            )


def cert(db, pid, heat, *, segment="16 PW BLUEWATER"):
    from weldaudit.mtrname import normalise_heat
    with db.tx() as c:
        c.execute(
            """INSERT INTO material(project_id, document_id, segment, heat,
                                    heat_key, source)
               VALUES(?, 1, ?, ?, ?, 'mtr_file')""",
            (pid, segment, heat, normalise_heat(heat)),
        )


# -- reading the register ---------------------------------------------------

def test_the_as_built_heats_are_collected(db):
    database, pid = db
    joint(database, pid, "AN176-25264", n=3)
    heats = asbuilt_heats(database, pid)
    assert len(heats) == 1
    only = next(iter(heats.values()))
    assert only["heat"] == "AN176-25264" and only["joints"] == 3


def test_a_cross_reference_is_not_a_heat(db):
    # The sheets open with "SEE ISO DRAWING" spread over two blocks, and the
    # HT# cell picks it up.
    database, pid = db
    for junk in ("SEE ISO DRAWING", "SEE DRAWING", "SEE ISO", "RISER EDGE BACK",
                 "N/A", "PAD E"):
        joint(database, pid, junk)
    joint(database, pid, "AN176-25264")
    assert [h["heat"] for h in asbuilt_heats(database, pid).values()] \
        == ["AN176-25264"]


# -- against the certificates ------------------------------------------------

def test_an_uncertified_as_built_heat_is_reported_with_its_station(db):
    database, pid = db
    cert(database, pid, "991033")
    joint(database, pid, "991034", station="106+34", n=5)
    found = heat_without_certificate(database, pid, "r")
    assert len(found) == 1
    assert "on the as-built for 16 PW BLUEWATER (5 joints at station 106+34)" \
        in found[0]["message"]


def test_a_certified_as_built_heat_is_quiet(db):
    database, pid = db
    cert(database, pid, "991034")
    joint(database, pid, "991034")
    assert heat_without_certificate(database, pid, "r") == []


def test_a_near_miss_is_a_transcription_error_not_missing_paperwork(db):
    database, pid = db
    cert(database, pid, "3650682")
    cert(database, pid, "0000001")
    joint(database, pid, "3650681", segment="8-in OIL", station="27+22")
    found = heat_without_certificate(database, pid, "r")
    assert len(found) == 1
    assert found[0]["severity"] == "major"
    assert "one character different" in found[0]["message"]


# -- a whole line with nothing certifying it ---------------------------------

def test_a_line_with_no_matching_certificate_is_reported_once(db):
    # `16 PW BLUEWATER` names 466 heats and not one has a certificate. The
    # twenty-two filed under that segment are all stainless fittings.
    database, pid = db
    cert(database, pid, "367985")
    cert(database, pid, "617294")
    for i in range(30):
        joint(database, pid, f"AN1{i:02d}-25264", n=2)
    found = line_without_certificates(database, pid, "r")
    assert len(found) == 1
    assert found[0]["severity"] == "critical"
    assert "names 30 heats across 60 joints" in found[0]["message"]


def test_those_heats_are_not_also_reported_one_by_one(db):
    database, pid = db
    cert(database, pid, "367985")
    for i in range(30):
        joint(database, pid, f"AN1{i:02d}-25264")
    assert heat_without_certificate(database, pid, "r") == []


def test_a_line_with_gaps_is_not_a_line_with_nothing(db):
    # Every other Bluewater segment runs 14% to 60% uncertified. Holes in a line
    # are individual findings; a line where nothing matches is one finding.
    database, pid = db
    cert(database, pid, "111111", segment="20 LP")
    joint(database, pid, "111111", segment="20 LP")
    joint(database, pid, "222222", segment="20 LP")
    joint(database, pid, "333333", segment="20 LP")
    assert line_without_certificates(database, pid, "r") == []
    assert len(heat_without_certificate(database, pid, "r")) == 2


def test_a_single_heat_is_not_a_whole_line(db):
    database, pid = db
    cert(database, pid, "111111", segment="20 LP")
    joint(database, pid, "999999", segment="8-in OIL")
    assert line_without_certificates(database, pid, "r") == []


def test_nothing_certified_anywhere_is_left_to_mtr_10(db):
    database, pid = db
    for i in range(5):
        joint(database, pid, f"AN1{i:02d}-25264")
    assert line_without_certificates(database, pid, "r") == []
    assert heat_without_certificate(database, pid, "r") == []
