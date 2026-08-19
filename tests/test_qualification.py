"""Welder qualification scope: what a ticket covers, and what was actually welded.

The qualification ranges are read off the certificate rather than derived from
API 1104 / ASME IX tables, so these tests pin the parsing and the comparison —
and, just as importantly, pin the cases where the answer must be *silence*.
A wrong finding here says a real welder was not qualified.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402
from weldaudit.qualification import (  # noqa: E402
    normalise_wps, parse_diameter_range, parse_processes, positions_covered,
)
from weldaudit.rules import welders as wrules  # noqa: E402
from weldaudit.welders import parse_cert_filename  # noqa: E402


# -- processes --------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("SMAW", {"SMAW"}),
    ("GTAW", {"GTAW"}),
    ("SMAW/GTAW", {"SMAW", "GTAW"}),
    ("TIG", {"GTAW"}),          # alias
    ("Stick", {"SMAW"}),
])
def test_processes_are_canonicalised(text, expected):
    assert parse_processes(text) == expected


@pytest.mark.parametrize("text", ["BW", "ML", "TIE-IN", "FILLET", "XR", "", "N/A"])
def test_joint_types_are_not_processes(text):
    # The PROCESS column on these weld reports is used inconsistently — it
    # frequently holds a joint type. Reading "BW" as a process would flag
    # every butt weld on the job as an unqualified process.
    assert parse_processes(text) == set()


# -- diameter ranges --------------------------------------------------------

def test_all_means_unlimited():
    r = parse_diameter_range("ALL")
    assert r.unlimited and r.allows(2) and r.allows(48)


@pytest.mark.parametrize("text,ok,bad", [
    ("12.75 and above", 20, 4),
    ("2.375 to 12.75", 8, 20),
    ("up to 8", 6, 20),
    ("6 and smaller", 4, 12),
])
def test_bounded_ranges(text, ok, bad):
    r = parse_diameter_range(text)
    assert r.understood and r.allows(ok) and not r.allows(bad)


@pytest.mark.parametrize("text", ["12.75", "", "see attached", None])
def test_an_unreadable_range_permits_everything(text):
    # A bare number on an API 1104 form implies a diameter *group*, and
    # resolving which one is code inference this deliberately refuses to do.
    # Silence beats disqualifying a welder who is in fact qualified.
    r = parse_diameter_range(text)
    assert not r.understood and r.allows(2) and r.allows(48)


# -- positions --------------------------------------------------------------

def test_5g_does_not_cover_2g():
    covered = positions_covered("5G")
    assert "5G" in covered and "1G" in covered and "2G" not in covered


def test_6g_covers_everything_below_it():
    assert {"1G", "2G", "5G", "6G"} <= positions_covered("6G")


def test_all_covers_every_position():
    assert "6GR" in positions_covered("ALL")


# -- WPS identity -----------------------------------------------------------

def test_a_slash_and_a_dash_are_the_same_procedure():
    # A slash is illegal in a filename, so the same WPS is written two ways.
    assert normalise_wps("XTO-X60-6010/8010 Rev.1") == normalise_wps(
        "XTO-X60-6010-8010 Rev.1")


def test_different_procedures_stay_different():
    assert normalise_wps("XTO-ASME-P1-LT-NACE") != normalise_wps("XTO-ASME-P1-HYP-NACE")


# -- scoped certificate filenames -------------------------------------------

def test_scoped_filename_yields_stencil_wps_and_date():
    cert = parse_cert_filename(
        "Javier Vazquez_XTO-X60-6010-8010 Rev.1_042425_ARS.pdf", {"ARS"})
    assert cert.stencil == "ARS"
    assert cert.wps == "XTO-X60-6010-8010 Rev.1"
    assert cert.cert_date == "2025-04-24"
    assert cert.name == "Javier Vazquez"


def test_a_bare_stencil_certificate_has_no_procedure_scope():
    assert parse_cert_filename("ABF.pdf", {"ABF"}).wps == ""


# -- the rules --------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "q.db")
    pid = database.upsert_project("Q", str(tmp_path))
    with database.tx() as c:
        c.execute(
            """INSERT INTO document(id, project_id, path, filename, ext, fingerprint,
                                    segment, kind)
               VALUES(1, ?, 'c.pdf', 'c.pdf', '.pdf', 'fp1', 'SEG A', 'welder_cert')""",
            (pid,),
        )
    return database, pid


def _weld(db, pid, *, wps="", process="", size="", stencil="ARS"):
    with db.tx() as c:
        cur = c.execute(
            """INSERT INTO weld(project_id, segment, line, weld_no, wps, process,
                                weld_size, welder_root, source)
               VALUES(?, 'SEG A', '16 LP', '1', ?, ?, ?, ?, 'weld_log_csv')""",
            (pid, wps, process, size, stencil),
        )
        c.execute(
            """INSERT INTO welder_pass(project_id, weld_id, document_id, segment,
                                       line, weld_no, stencil, date_welded)
               VALUES(?, ?, 1, 'SEG A', '16 LP', '1', ?, '2025-06-01')""",
            (pid, cur.lastrowid, stencil),
        )


def _cert(db, pid, **kw):
    cols = {"project_id": pid, "document_id": 1, "segment": "SEG A",
            "stencil": "ARS", "evidence": "vision"}
    cols.update(kw)
    names = ", ".join(cols)
    with db.tx() as c:
        c.execute(f"INSERT INTO welder_cert({names}) VALUES({','.join('?' * len(cols))})",
                  tuple(cols.values()))


def test_welding_under_an_uncertified_procedure_is_critical(db):
    database, pid = db
    _cert(database, pid, wps="XTO-ASME-P1-LT-NACE", evidence="filename")
    _weld(database, pid, wps="XTO-X60-6010/8010 Rev.1")

    findings = wrules.wps_not_certified(database, pid, "r1")
    assert len(findings) == 1
    assert findings[0]["severity"] == "critical"
    assert "XTO-X60-6010/8010 Rev.1" in findings[0]["message"]


def test_the_same_procedure_written_two_ways_is_not_a_finding(db):
    database, pid = db
    _cert(database, pid, wps="XTO-X60-6010-8010 Rev.1", evidence="filename")
    _weld(database, pid, wps="XTO-X60-6010/8010 Rev.1")
    assert wrules.wps_not_certified(database, pid, "r1") == []


def test_a_welder_with_no_scoped_ticket_is_left_to_wld_01(db):
    # A bare-stencil certificate carries no procedure scope; inventing a
    # WPS violation from its absence would double-report WLD-01.
    database, pid = db
    _cert(database, pid, wps="", evidence="filename")
    _weld(database, pid, wps="XTO-X60-6010/8010 Rev.1")
    assert wrules.wps_not_certified(database, pid, "r1") == []


def test_welding_an_unqualified_process_is_critical(db):
    database, pid = db
    _cert(database, pid, qual_process="SMAW")
    _weld(database, pid, process="GTAW")

    findings = wrules.process_not_qualified(database, pid, "r1")
    assert len(findings) == 1
    assert "GTAW" in findings[0]["message"] and "SMAW" in findings[0]["message"]


def test_a_qualified_process_is_not_reported(db):
    database, pid = db
    _cert(database, pid, qual_process="SMAW")
    _weld(database, pid, process="SMAW")
    assert wrules.process_not_qualified(database, pid, "r1") == []


def test_a_joint_type_in_the_process_column_is_not_a_violation(db):
    database, pid = db
    _cert(database, pid, qual_process="SMAW")
    _weld(database, pid, process="BW")
    assert wrules.process_not_qualified(database, pid, "r1") == []


def test_the_qualification_range_beats_the_tested_process(db):
    # The coupon was welded with SMAW but the range block qualifies both;
    # using the as-tested value would produce a false finding.
    database, pid = db
    _cert(database, pid, process="SMAW", qual_process="SMAW/GTAW")
    _weld(database, pid, process="GTAW")
    assert wrules.process_not_qualified(database, pid, "r1") == []


def test_welding_outside_the_qualified_diameter_is_critical(db):
    database, pid = db
    _cert(database, pid, qual_diameter="2.375 to 12.75")
    _weld(database, pid, size='20"')

    findings = wrules.diameter_not_qualified(database, pid, "r1")
    assert len(findings) == 1
    assert "20" in findings[0]["message"]


def test_an_all_diameter_ticket_never_fires(db):
    database, pid = db
    _cert(database, pid, qual_diameter="ALL")
    _weld(database, pid, size='20"')
    assert wrules.diameter_not_qualified(database, pid, "r1") == []


def test_an_unreadable_diameter_range_never_fires(db):
    database, pid = db
    _cert(database, pid, qual_diameter="12.75")
    _weld(database, pid, size='20"')
    assert wrules.diameter_not_qualified(database, pid, "r1") == []


def test_a_failed_qualification_record_is_critical(db):
    database, pid = db
    _cert(database, pid, result="FAIL", name="Taylor Phillips")
    findings = wrules.cert_not_passed(database, pid, "r1")
    assert len(findings) == 1 and findings[0]["severity"] == "critical"


def test_a_passed_record_is_not_reported(db):
    database, pid = db
    _cert(database, pid, result="PASS")
    assert wrules.cert_not_passed(database, pid, "r1") == []


def test_a_test_witnessed_after_the_inspectors_ticket_lapsed_is_flagged(db):
    database, pid = db
    _cert(database, pid, cert_date="2025-01-30", qualifier_name="Lee Vermillion",
          qualifier_cwi="17060031", qualifier_expiry="2024-06-01")
    findings = wrules.qualifier_lapsed(database, pid, "r1")
    assert len(findings) == 1
    assert "17060031" in findings[0]["message"]


def test_a_current_inspector_is_not_flagged(db):
    database, pid = db
    _cert(database, pid, cert_date="2025-01-30", qualifier_expiry="2026-06-01")
    assert wrules.qualifier_lapsed(database, pid, "r1") == []


def test_unrecorded_position_is_reported_once_as_a_gap(db):
    database, pid = db
    _cert(database, pid, qual_position="ALL")
    _weld(database, pid, process="SMAW")

    findings = wrules.position_not_recorded(database, pid, "r1")
    assert len(findings) == 1
    assert findings[0]["severity"] == "info"
    assert "position" in findings[0]["message"].lower()


def test_no_position_gap_reported_when_certs_were_never_read(db):
    database, pid = db
    _cert(database, pid, qual_position="", evidence="filename")
    assert wrules.position_not_recorded(database, pid, "r1") == []
