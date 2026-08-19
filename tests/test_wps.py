"""Welding procedures: matching references, reading the register, judging welds.

Matching is the hard part and most of this file is about it. One procedure is
written four ways across the corpus — ``XTO-X60-6010/8010 Rev.1`` on a weld
log, ``XTO-X60-6010-8010 Rev.1`` in a certificate filename (a slash cannot go
in a filename), ``XTO-ASME PI HYP NACE`` for ``XTO-ASME-P1-HYP-NACE`` (a
letter I for the digit 1), and ``XTO-SS`` for ``XTO-SS-Sec. IX``. Three
different mechanisms resolve those, and the tests below pin the boundary of
each, because the failure mode is silently merging procedures that really are
distinct: the register genuinely contains ``XTO-X42-6010``,
``XTO-X42-6010/7018`` and ``XTO-X42-6010/8010`` as separate recipes.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402
from weldaudit.rules import wps as rules  # noqa: E402
from weldaudit.wps import (  # noqa: E402
    base_key, full_key, nearest_procedures, parse_page, parse_register,
    resolve, split_revision,
)


# -- reading a reference ----------------------------------------------------

@pytest.mark.parametrize("text,base,revision", [
    ("XTO-X60-6010/8010 Rev.1", "XTO-X60-6010/8010", "1"),
    ("XTO-X60-6010-8010 Rev. 1", "XTO-X60-6010-8010", "1"),
    ("XTO-ASME PI HYP NACE Rev.0", "XTO-ASME PI HYP NACE", "0"),
    ("XTO-ASME-P1-HYP-NACE", "XTO-ASME-P1-HYP-NACE", ""),
    ("XTO-SS", "XTO-SS", ""),
    ("", "", ""),
])
def test_the_revision_splits_off(text, base, revision):
    assert split_revision(text) == (base, revision)


def test_a_missing_revision_is_not_guessed_at():
    # Certificates routinely omit it. Treating unstated as revision 0 would
    # assert something the paperwork does not say.
    assert split_revision("XTO-SS")[1] == ""


def test_slash_and_dash_are_the_same_procedure():
    assert base_key("XTO-X60-6010/8010 Rev.1") == base_key("XTO-X60-6010-8010 Rev. 1")


def test_the_revision_is_kept_when_it_is_the_question():
    assert full_key("XTO-X60-6010/8010 Rev.1") != full_key("XTO-X60-6010/8010 Rev.2")
    assert full_key("XTO-X60-6010/8010 Rev.1") == full_key("XTO-X60-6010-8010 REV 1")


# -- matching a reference to the register -----------------------------------

REGISTER = {base_key(w) for w in (
    "XTO-X42-6010", "XTO-X42-6010/7018", "XTO-X42-6010/8010",
    "XTO-X60-6010/7018", "XTO-X60-6010/8010", "XTO-SS-Sec. IX",
)}


def test_an_exact_reference_resolves_exactly():
    key, how = resolve("XTO-X60-6010/8010 Rev.1", REGISTER)
    assert (key, how) == (base_key("XTO-X60-6010/8010"), "exact")


def test_a_filename_spelling_resolves_exactly():
    key, how = resolve("XTO-X60-6010-8010 Rev.1", REGISTER)
    assert (key, how) == (base_key("XTO-X60-6010/8010"), "exact")


def test_an_unambiguous_abbreviation_resolves():
    # A certificate says XTO-SS where the register says XTO-SS-Sec. IX.
    key, how = resolve("XTO-SS", REGISTER)
    assert (key, how) == (base_key("XTO-SS-Sec. IX"), "abbreviated")


def test_an_ambiguous_prefix_does_not_resolve():
    # XTO-X42-6010 prefixes two other procedures — but it is also a procedure
    # in its own right, so the exact match must win.
    key, how = resolve("XTO-X42-6010", REGISTER)
    assert (key, how) == (base_key("XTO-X42-6010"), "exact")


def test_a_prefix_of_several_procedures_is_not_guessed():
    # These really are three different recipes; merging them would be worse
    # than reporting the reference as unknown.
    assert resolve("XTO-X65-6010", REGISTER) == ("", "")


def test_a_one_character_slip_resolves_as_a_near_miss():
    known = {base_key("XTO-ASME-P1-HYP-NACE")}
    key, how = resolve("XTO-ASME PI HYP NACE Rev.0", known)
    assert (key, how) == (base_key("XTO-ASME-P1-HYP-NACE"), "near")


def test_an_unrelated_procedure_resolves_to_nothing():
    assert resolve("CONTRACTOR-WPS-7", REGISTER) == ("", "")


def test_the_p_number_slip_is_found():
    known = {base_key("XTO-ASME-P1-HYP-NACE"), base_key("XTO-ASME-P1-LT-NACE")}
    assert nearest_procedures("XTO-ASME PI HYP NACE", known) == [
        base_key("XTO-ASME-P1-HYP-NACE")]


# -- reading the register ---------------------------------------------------

PAGE = """
XTO Energy XTO Global Projects Permian Basin
WPS NO: XTO-X60-6010/8010 Rev. 1
SUPPORTING PQR: XTO-X60-6010/8010-01 & XTO-X60-6010/8010-02
API 1104 WELDING PROCEDURE SPECIFICATION (WPS)
COMPANY NAME: XTO Energy QUALIFIED TO: API 1104
WELDING PROCESS: SHIELDED METAL ARC WELDING (SMAW), MANUAL
MATERIAL DESCRIPTION: PIPE OUTSTIDE DIAMETER: >= 2.375" thru Unlimited Diameters
PIPE WALL THICKNESS RANGE: 0.188" TO Unlimited
NUMBER OF WELDERS: For Pipe >= 12.750" O.D., 2 or more welders are REQUIRED for
Root and Hot Pass; 1 welder may Fill and Cap
"""


def test_a_procedure_page_reads_its_essential_variables():
    p = parse_page(PAGE, page_no=43)
    assert p is not None
    assert (p.wps, p.revision) == ("XTO-X60-6010/8010", "1")
    assert p.pqr.startswith("XTO-X60-6010/8010-01")
    assert p.code == "API 1104"
    assert "SHIELDED METAL ARC" in p.process
    assert (p.min_diameter, p.min_wall) == (2.375, 0.188)
    assert p.two_welder_over == 12.75
    assert p.page_no == 43


def test_the_two_welder_threshold_is_read_not_assumed():
    # A standard that says something different must be followed instead.
    altered = PAGE.replace("12.750", "8.625").replace("2 or more", "3 or more")
    assert parse_page(altered).two_welder_over == 8.625


def test_a_page_that_is_not_a_procedure_is_skipped():
    assert parse_page("DAILY FIELD COATING INSPECTION REPORT") is None
    assert parse_page("") is None


def test_the_two_pages_of_one_procedure_merge():
    # The variables are on one page and the parameters on the next, and both
    # repeat the WPS number.
    second = ("WPS NO: XTO-X60-6010/8010 Rev. 1 "
              "SUPPORTING PQR: XTO-X60-6010/8010-01 "
              "WELD PARAMETERS & ELECTRICAL CHARACTERISTICS")
    procedures = parse_register([(43, PAGE), (44, second)])
    assert len(procedures) == 1
    assert procedures[0].two_welder_over == 12.75      # kept from page 43


def test_distinct_procedures_stay_distinct():
    other = PAGE.replace("X60", "X65")
    procedures = parse_register([(43, PAGE), (51, other)])
    assert [p.wps for p in procedures] == [
        "XTO-X60-6010/8010", "XTO-X65-6010/8010"]


# -- the rules --------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "w.db")
    pid = database.upsert_project("W", str(tmp_path))
    with database.tx() as c:
        c.execute(
            """INSERT INTO document(id, project_id, path, filename, ext, kind,
                                    segment, fingerprint)
               VALUES(1, ?, 'p', 'GPPB-0110 Welding Procedures.pdf', '.pdf',
                      'wps', '', 'fp1')""",
            (pid,),
        )
    return database, pid


def approve(db, pid, wps, *, revision="1", pqr="PQR-01", code="API 1104",
            two_welder_over=12.75):
    with db.tx() as c:
        c.execute(
            """INSERT INTO procedure(project_id, document_id, wps, wps_key,
                                     revision, pqr, code, min_diameter,
                                     min_wall, two_welder_over, page_no, source)
               VALUES(?, 1, ?, ?, ?, ?, ?, 2.375, 0.188, ?, 43, 'wps_standard')""",
            (pid, wps, base_key(wps), revision, pqr, code, two_welder_over),
        )


def weld(db, pid, weld_no, *, wps="", size="16\"", root="AAA, BBB"):
    with db.tx() as c:
        c.execute(
            """INSERT INTO weld(project_id, segment, line, weld_no, wps,
                                weld_size, welder_root, source)
               VALUES(?, 'SEG A', '16 LP', ?, ?, ?, ?, 'weld_log_csv')""",
            (pid, weld_no, wps, size, root),
        )


def cert(db, pid, stencil, wps):
    with db.tx() as c:
        c.execute(
            """INSERT INTO welder_cert(project_id, stencil, wps, evidence)
               VALUES(?, ?, ?, 'filename')""",
            (pid, stencil, wps),
        )


def fire(db, pid, rule):
    return rule(db, pid, "run")


# -- WPS-01 nothing filed ---------------------------------------------------

def test_no_procedures_filed_is_one_finding_for_the_job(db):
    # PLU's shape: three procedures on certificates, no specification anywhere.
    database, pid = db
    for wps in ("XTO-ASME-P1-HYP-NACE", "XTO-ASME-P1-LT-NACE",
                "XTO-X60-6010-8010 Rev.1"):
        cert(database, pid, "AAA", wps)
    found = fire(database, pid, rules.no_procedures_filed)
    assert len(found) == 1 and found[0]["subject"] == "3 procedures"
    assert "24 welder certificates" not in found[0]["message"]
    assert "XTO-ASME-P1-LT-NACE" in found[0]["message"]


def test_a_single_missing_procedure_reads_as_singular(db):
    database, pid = db
    weld(database, pid, "1", wps="XTO-X60-6010/8010 Rev.1")
    found = fire(database, pid, rules.no_procedures_filed)
    assert found[0]["subject"] == "1 procedure"
    assert "cite one: XTO-X60-6010/8010 Rev.1" in found[0]["message"]
    assert "without it the line" in found[0]["message"]


def test_a_filed_register_silences_the_not_filed_rule(db):
    database, pid = db
    approve(database, pid, "XTO-X60-6010/8010")
    weld(database, pid, "1", wps="XTO-X60-6010/8010 Rev.1")
    assert fire(database, pid, rules.no_procedures_filed) == []


def test_a_job_referencing_no_procedure_reports_nothing(db):
    database, pid = db
    weld(database, pid, "1")
    assert fire(database, pid, rules.no_procedures_filed) == []


# -- WPS-02 not approved ----------------------------------------------------

def test_an_approved_procedure_passes(db):
    database, pid = db
    approve(database, pid, "XTO-X60-6010/8010")
    weld(database, pid, "1", wps="XTO-X60-6010-8010 Rev.1")
    assert fire(database, pid, rules.procedure_not_approved) == []


def test_an_unapproved_procedure_is_reported(db):
    database, pid = db
    approve(database, pid, "XTO-X60-6010/8010")
    weld(database, pid, "1", wps="CONTRACTOR-WPS-7")
    found = fire(database, pid, rules.procedure_not_approved)
    assert len(found) == 1 and "CONTRACTOR-WPS-7" in found[0]["message"]
    assert "reviewed and approved" in found[0]["message"]


def test_with_no_register_the_approval_rule_stays_quiet(db):
    # WPS-01 reports this once for the job; two headings for one gap is noise.
    database, pid = db
    weld(database, pid, "1", wps="CONTRACTOR-WPS-7")
    assert fire(database, pid, rules.procedure_not_approved) == []


# -- WPS-03 spelling --------------------------------------------------------

def test_an_abbreviation_is_reported_as_a_spelling_not_a_gap(db):
    database, pid = db
    approve(database, pid, "XTO-SS-Sec. IX", pqr="")
    cert(database, pid, "AAA", "XTO-SS")
    assert fire(database, pid, rules.procedure_not_approved) == []
    found = fire(database, pid, rules.procedure_spelling)
    assert len(found) == 1 and "written short" in found[0]["message"]
    assert "XTO-SS-Sec. IX" in found[0]["message"]


def test_two_spellings_of_one_procedure_in_one_job(db):
    database, pid = db
    weld(database, pid, "1", wps="XTO-X60-6010/8010 Rev.1")
    cert(database, pid, "AAA", "XTO-X60-6010-8010 Rev.1")
    found = fire(database, pid, rules.procedure_spelling)
    assert len(found) == 1 and "2 different ways" in found[0]["message"]


def test_a_p_number_slip_across_two_records(db):
    database, pid = db
    weld(database, pid, "1", wps="XTO-ASME PI HYP NACE Rev.0")
    cert(database, pid, "AAA", "XTO-ASME-P1-HYP-NACE")
    found = fire(database, pid, rules.procedure_spelling)
    assert len(found) == 1 and "differ by one character" in found[0]["message"]


def test_one_consistent_spelling_reports_nothing(db):
    database, pid = db
    weld(database, pid, "1", wps="XTO-X60-6010/8010 Rev.1")
    weld(database, pid, "2", wps="XTO-X60-6010/8010 Rev.1")
    assert fire(database, pid, rules.procedure_spelling) == []


# -- WPS-04 PQR -------------------------------------------------------------

def test_a_procedure_with_a_pqr_passes(db):
    database, pid = db
    approve(database, pid, "XTO-X60-6010/8010")
    assert fire(database, pid, rules.procedure_without_pqr) == []


def test_a_procedure_with_no_pqr_is_reported(db):
    database, pid = db
    approve(database, pid, "XTO-SS-Sec. IX", pqr="")
    found = fire(database, pid, rules.procedure_without_pqr)
    assert len(found) == 1 and "no supporting PQR" in found[0]["message"]


# -- WPS-05 blank procedure column ------------------------------------------

def test_a_log_with_some_blank_procedures_is_reported(db):
    # GL-33's shape: a log that names a procedure for 25 welds and not for 232.
    database, pid = db
    for i in range(3):
        weld(database, pid, f"n{i}", wps="XTO-X60-6010/8010 Rev.1")
    for i in range(7):
        weld(database, pid, f"b{i}")
    found = fire(database, pid, rules.weld_without_procedure)
    assert len(found) == 1 and "7 of 10 welds" in found[0]["message"]


def test_a_register_with_no_procedure_column_is_not_missing_one(db):
    # The daily weld report form has no WPS field, so a job recorded only that
    # way is not missing something it was ever asked for.
    database, pid = db
    for i in range(5):
        weld(database, pid, f"b{i}")
    assert fire(database, pid, rules.weld_without_procedure) == []


def test_a_fully_filled_log_reports_nothing(db):
    database, pid = db
    for i in range(3):
        weld(database, pid, f"n{i}", wps="XTO-X60-6010/8010 Rev.1")
    assert fire(database, pid, rules.weld_without_procedure) == []


# -- WPS-06 unused ----------------------------------------------------------

def test_unused_procedures_are_noted_once(db):
    database, pid = db
    for name in ("XTO-X42-6010", "XTO-X60-6010/8010", "XTO-X65-6010/8010"):
        approve(database, pid, name)
    weld(database, pid, "1", wps="XTO-X60-6010/8010 Rev.1")
    found = fire(database, pid, rules.procedure_unused)
    assert len(found) == 1 and found[0]["severity"] == "info"
    assert "2 of the 3" in found[0]["message"]
    assert "XTO-X60-6010/8010" not in found[0]["detail"]


def test_an_abbreviated_reference_still_counts_as_used(db):
    database, pid = db
    approve(database, pid, "XTO-SS-Sec. IX")
    approve(database, pid, "XTO-X60-6010/8010")
    cert(database, pid, "AAA", "XTO-SS")
    found = fire(database, pid, rules.procedure_unused)
    assert len(found) == 1 and "XTO-SS-Sec. IX" not in found[0]["message"]


def test_a_register_nothing_uses_is_not_reported(db):
    # Every procedure unused means nothing on the job cites one at all, which
    # is a different finding.
    database, pid = db
    approve(database, pid, "XTO-X60-6010/8010")
    assert fire(database, pid, rules.procedure_unused) == []


# -- WPS-07 crew size -------------------------------------------------------

def test_two_welders_on_a_large_root_passes(db):
    database, pid = db
    approve(database, pid, "XTO-X60-6010/8010")
    weld(database, pid, "ATI-02", wps="XTO-X60-6010/8010 Rev.1",
         size='16"', root="AM53, OM64")
    assert fire(database, pid, rules.too_few_welders) == []


def test_one_welder_on_a_large_root_is_reported(db):
    database, pid = db
    approve(database, pid, "XTO-X60-6010/8010")
    weld(database, pid, "ATI-02", wps="XTO-X60-6010/8010 Rev.1",
         size='16"', root="AM53")
    found = fire(database, pid, rules.too_few_welders)
    assert len(found) == 1 and "ATI-02" in found[0]["message"]
    assert "12.75" in found[0]["message"]


def test_small_bore_needs_only_one_welder(db):
    database, pid = db
    approve(database, pid, "XTO-X60-6010/8010")
    weld(database, pid, "GFB-01", wps="XTO-X60-6010/8010 Rev.1",
         size='6"', root="AM53")
    assert fire(database, pid, rules.too_few_welders) == []


def test_the_threshold_comes_from_the_procedure(db):
    database, pid = db
    approve(database, pid, "XTO-X60-6010/8010", two_welder_over=4.0)
    weld(database, pid, "GFB-01", wps="XTO-X60-6010/8010 Rev.1",
         size='6"', root="AM53")
    assert len(fire(database, pid, rules.too_few_welders)) == 1


def test_a_mangled_inch_mark_still_parses_the_size(db):
    # The CSV export writes 16 with a replacement character for the inch mark
    # on two thirds of GL 31's rows.
    database, pid = db
    approve(database, pid, "XTO-X60-6010/8010")
    weld(database, pid, "ATI-01", wps="XTO-X60-6010/8010 Rev.1",
         size="16�", root="AM53")
    assert len(fire(database, pid, rules.too_few_welders)) == 1


def test_a_weld_with_no_procedure_is_not_judged(db):
    database, pid = db
    approve(database, pid, "XTO-X60-6010/8010")
    weld(database, pid, "ATI-02", size='16"', root="AM53")
    assert fire(database, pid, rules.too_few_welders) == []


def test_with_no_register_the_crew_rule_stays_quiet(db):
    database, pid = db
    weld(database, pid, "ATI-02", wps="XTO-X60-6010/8010 Rev.1",
         size='16"', root="AM53")
    assert fire(database, pid, rules.too_few_welders) == []


# -- the summary ------------------------------------------------------------

def test_the_summary_folds_abbreviations_onto_the_procedure(db):
    database, pid = db
    approve(database, pid, "XTO-SS-Sec. IX")
    cert(database, pid, "AAA", "XTO-SS")
    rows = {r["wps"]: r for r in rules.procedure_summary(database, pid)}
    assert list(rows) == ["XTO-SS-Sec. IX"]
    assert rows["XTO-SS-Sec. IX"]["certs"] == 1
    assert rows["XTO-SS-Sec. IX"]["filed"] is True


def test_the_summary_shows_a_referenced_but_unfiled_procedure(db):
    database, pid = db
    weld(database, pid, "1", wps="XTO-X60-6010/8010 Rev.1")
    row = rules.procedure_summary(database, pid)[0]
    assert row["filed"] is False and row["welds"] == 1
