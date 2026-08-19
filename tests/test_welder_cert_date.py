"""The welder log's certification date against the certificate's own.

Two records of the same fact. The certificate is signed by the CWI who
witnessed the test; the welder log is a contractor spreadsheet kept alongside
it, copied into every segment book. Where they disagree the log is the likelier
to be wrong — and it is the log's date that decides whether a weld predates its
welder's ticket.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402
from weldaudit.rules.welders import cert_date_conflict  # noqa: E402


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "w.db")
    return database, database.upsert_project("W", str(tmp_path))


def certificate(db, pid, stencil, when, *, name="Clinton Wilson",
                wps="XTO-X60-6010/8010 Rev.1", evidence="vision"):
    with db.tx() as c:
        c.execute(
            """INSERT INTO welder_cert(project_id, segment, stencil, name,
                                       cert_date, wps, evidence)
               VALUES(?, '20 LP', ?, ?, ?, ?, ?)""",
            (pid, stencil, name, when, wps, evidence),
        )


def roster(db, pid, stencil, when, *, name="CLINT WILSON", segment="20 LP"):
    with db.tx() as c:
        c.execute(
            """INSERT INTO welder_roster(project_id, segment, name, stencil,
                                         cert_date, source)
               VALUES(?, ?, ?, ?, ?, 'welder_log_xlsx')""",
            (pid, segment, name, stencil, when),
        )


def run(db, pid):
    return cert_date_conflict(db, pid, "r")


def test_a_year_out_is_reported(db):
    # Bluewater's log dates Clinton Wilson's qualification 2025-11-14; the
    # certificate Lee Vermillion signed says 2024-11-14. A year out in that
    # direction puts all 538 of his passes before his own ticket.
    database, pid = db
    certificate(database, pid, "AEA", "2024-11-14")
    roster(database, pid, "AEA", "2025-11-14")
    found = run(database, pid)
    assert len(found) == 1
    assert "2025-11-14" in found[0]["message"] and "2024-11-14" in found[0]["message"]


def test_agreement_is_quiet(db):
    database, pid = db
    certificate(database, pid, "AEA", "2024-11-14")
    roster(database, pid, "AEA", "2024-11-14")
    assert run(database, pid) == []


def test_a_welder_with_several_tickets_matches_any_of_them(db):
    # A welder holds one certificate per procedure — ARS has three — and the
    # log records a single date.
    database, pid = db
    certificate(database, pid, "ARS", "2025-04-24", wps="XTO-X60-6010-8010")
    certificate(database, pid, "ARS", "2025-04-26", wps="XTO-ASME-P1-HYP-NACE")
    roster(database, pid, "ARS", "2025-04-26", name="Javier Vazquez")
    assert run(database, pid) == []


def test_the_roster_copies_are_one_claim(db):
    # The welder log is copied into every segment book; a disagreement is with
    # the certificate, not between the copies.
    database, pid = db
    certificate(database, pid, "AEA", "2024-11-14")
    for seg in ("20 LP", "8-in OIL", "6 IN FUEL GAS SEG A"):
        roster(database, pid, "AEA", "2025-11-14", segment=seg)
    found = run(database, pid)
    assert len(found) == 1
    assert '"roster_copies": 3' in found[0]["detail"]


def test_a_filename_derived_date_cannot_settle_it(db):
    # Only the certificate itself carries the date the inspector witnessed.
    # A date scraped from a filename is the same class of evidence as the log.
    database, pid = db
    certificate(database, pid, "AEA", "2024-11-14", evidence="filename")
    roster(database, pid, "AEA", "2025-11-14")
    assert run(database, pid) == []


def test_a_stencil_with_no_certificate_is_left_to_wld_01(db):
    database, pid = db
    certificate(database, pid, "AEA", "2024-11-14")
    roster(database, pid, "ZZZ", "2025-01-01", name="Nobody")
    assert run(database, pid) == []
