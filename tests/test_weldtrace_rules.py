"""The WT- rules, from an unpacked download to a finding.

Each test builds the smallest download that provokes one rule and indexes it
the way an audit run would, so what is being checked is the whole path -
filename to document kind to extractor to table to rule - rather than a
function in isolation. That path is where the interesting failures are: a
download filed under the wrong kind produces no findings at all, which reads
exactly like a clean package.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402
from weldaudit.extract import weldtrace as load  # noqa: E402
from weldaudit.index import index_project  # noqa: E402
from weldaudit.rules import weldtrace as wt  # noqa: E402


# -- building a download ----------------------------------------------------

WELD_COLUMNS = (
    "Test Pack # - Rev", "Test Pack Reference", "Weld Number",
    "Drawing # - Revision", "Line Number", "Category", "Joint Type",
    "WPs# - Revision", "Welder ID Root", "Welder ID Fill", "Welder ID Cap",
    "Date Planned", "Date Welded", "Material1", "Material 1 - Heat Number",
    "Material2", "Material 2 - Heat Number", "RT Test Requested",
    "RT Result & Report-Rev & Date", "RT Retest Requested",
    "RT Retest Result & Report-Rev & Date", "Test Result",
)

PACK_REF = "NDEFIELD-8-21-2026-40300118"

WELD_DEFAULTS = {
    "Test Pack # - Rev": "TP-1-1", "Test Pack Reference": PACK_REF,
    "Weld Number": "FW-104", "Drawing # - Revision": "2-D1-0-GL-4012-1-0",
    "Line Number": "6-B1-0V-PF-4112", "Category": "Field", "Joint Type": "BW",
    "WPs# - Revision": "XTO-ASME-P1-HYP-NACE-0", "Welder ID Root": "AOO;",
    "Welder ID Fill": "AOO;", "Welder ID Cap": "AOO;",
    "Date Planned": "Aug-21-2026", "Date Welded": "Aug-21-2026",
    "Material1": "PIPE", "Material 1 - Heat Number": "70097",
    "Material2": "PIPE", "Material 2 - Heat Number": "70097",
    "RT Test Requested": "Yes",
    "RT Result & Report-Rev & Date": f"Passed;{PACK_REF}-0;Aug-21-2026;",
    "RT Retest Requested": "No", "RT Retest Result & Report-Rev & Date": "-",
    "Test Result": "Accepted",
}

HEAT_COLUMNS = ("Heat Number", "Material Name", "Product Form",
                "Pipe Fitting Type", "Supplier", "Spec No.",
                "Alloy Type or Grade", "P-No.", "File Name", "Status")

HEAT_DEFAULTS = {
    "Heat Number": "70097", "Material Name": "6in Pipe", "Product Form": "PIPE",
    "Pipe Fitting Type": "-", "Supplier": "Northgate Tube", "Spec No.": "A106",
    "Alloy Type or Grade": "B", "P-No.": "1", "File Name": "70097-MTR.pdf",
    "Status": "Active",
}

STAMP_COLUMNS = ("Type", "Text inside bubble", "Drawing Number",
                 "Drawing Revision", "Date Welded", "Sheet Number")

STAMP_DEFAULTS = {
    "Type": "Weld", "Text inside bubble": "AFW-104\nAOO",
    "Drawing Number": "2-D1-0-GL-4012-1", "Drawing Revision": "0",
    "Date Welded": "8/21/2026", "Sheet Number": "1",
}


def _csv(path: Path, columns, defaults, rows, raw=()) -> None:
    """Write an export the way WeldTrace writes one.

    Columns named in ``raw`` are emitted unquoted, which is how the annotation
    export writes the bubble text: the welder's initials sit under the weld
    tag in the same cell, and the line break between them goes into the file
    with no quoting around it, so the row arrives split across two lines.
    Quoting that field in a fixture would hide the one thing worth testing.
    """
    lines = [",".join(f'"{c}"' for c in columns)]
    for row in rows:
        filled = dict(defaults, **row)
        lines.append(",".join(
            filled[c] if c in raw else f'"{filled[c]}"' for c in columns))
    path.write_text("\n".join(lines), encoding="utf-8")


@pytest.fixture
def download(tmp_path):
    """``build(welds, heats, stamps) -> (db, project_id)``.

    Passing ``None`` for a register leaves that export out of the download
    altogether, which is a different thing from an export with no rows.
    """
    def build(welds=(), heats=(), stamps=()):
        root = tmp_path / "Merlin 3 Pad C"
        root.mkdir(exist_ok=True)
        if welds is not None:
            _csv(root / "TestPackExport.csv", WELD_COLUMNS, WELD_DEFAULTS,
                 welds or [{}])
        if heats is not None:
            _csv(root / "projectMaterialsExport.csv", HEAT_COLUMNS,
                 HEAT_DEFAULTS, heats or [{}])
        if stamps is not None:
            _csv(root / "AnnotationAttachments_TEST 1.csv", STAMP_COLUMNS,
                 STAMP_DEFAULTS, stamps or [{}], raw=("Text inside bubble",))
        db = Database(tmp_path / "a.db")
        project_id, _stats = index_project(db, "Merlin 3", root)
        load.extract(db, project_id)
        return db, project_id
    return build


def codes(findings) -> list[str]:
    return [f["rule"] for f in findings]


# -- the download is read at all --------------------------------------------

def test_a_download_fills_the_shared_weld_table(download):
    db, pid = download()
    welds = db.q("SELECT * FROM weld WHERE project_id=? AND source='weldtrace'",
                 (pid,))
    assert len(welds) == 1
    assert welds[0]["weld_no"] == "FW-104"
    assert welds[0]["wps"] == "XTO-ASME-P1-HYP-NACE"
    assert welds[0]["wps_revision"] == "0"


def test_the_drawing_does_not_end_up_in_the_notes_column(download):
    # NDE-09 buckets every note on a weld with no nde_id and reports the ones
    # that match no NDE series. A drawing number parked there makes it fire on
    # every WeldTrace job.
    db, pid = download()
    assert db.one("SELECT note FROM weld WHERE project_id=?", (pid,))["note"] in ("", None)


def test_a_test_pack_is_a_segment_on_the_coverage_tab(download):
    # A WeldTrace register cites NX-20260331RT01, which is not an NdeId and is
    # deliberately kept out of nde_id. Counting only the ids would show a fully
    # examined pack at 0% referenced - a clean package reported as an empty one.
    from weldaudit.rules.nde_coverage import coverage_summary

    db, pid = download()
    coverage = coverage_summary(db, pid)
    assert [c["segment"] for c in coverage] == ["TP-1"]
    assert coverage[0]["welds"] == 1
    assert coverage[0]["welds_with_nde"] == 1
    assert coverage[0]["pct_referenced"] == 100
    assert coverage[0]["registers"][0]["register"] == "the WeldTrace register"


def test_a_weld_citing_nothing_is_still_uncovered(download):
    from weldaudit.rules.nde_coverage import coverage_summary

    db, pid = download(welds=[{"RT Test Requested": "No",
                               "RT Result & Report-Rev & Date": "-"}])
    assert coverage_summary(db, pid)[0]["pct_referenced"] == 0


def test_a_clean_download_produces_nothing(download):
    db, pid = download()
    for _code, (_title, fn) in _wt_rules():
        assert fn(db, pid, "r1") == [], _code


def _wt_rules():
    from weldaudit import rules
    return sorted((c, v) for c, v in rules.registry().items()
                  if c.startswith("WT-"))


# -- WT-01: the download itself ---------------------------------------------

def test_a_download_with_no_heat_register_says_so_once(download):
    db, pid = download(heats=None)
    found = wt.download_incomplete(db, pid, "r1")
    assert len(found) == 1
    assert "heat register" in found[0]["message"]
    assert "unknown heat" in found[0]["message"]


def test_a_job_with_no_weldtrace_download_at_all_is_silent(tmp_path):
    root = tmp_path / "paper job"
    root.mkdir()
    (root / "11 NDE reader sheet.pdf").write_bytes(b"%PDF-1.4\n")
    db = Database(tmp_path / "a.db")
    pid, _ = index_project(db, "paper", root)
    assert wt.download_incomplete(db, pid, "r1") == []


def test_a_missing_register_does_not_also_fire_the_weld_rules(download):
    # Without the heat register every weld would otherwise report an unknown
    # heat, which reads as a hundred material failures rather than one absent
    # file.
    db, pid = download(heats=None)
    assert wt.heat_unknown(db, pid, "r1") == []


def test_a_missing_annotation_export_does_not_unstamp_every_weld(download):
    db, pid = download(stamps=None)
    assert wt.stamp_missing(db, pid, "r1") == []
    assert wt.stamp_orphan(db, pid, "r1") == []


# -- WT-02..WT-06: the weld register on its own -----------------------------

def test_a_weld_with_no_procedure(download):
    db, pid = download(welds=[{"WPs# - Revision": "-"}])
    found = wt.wps_missing(db, pid, "r1")
    assert codes(found) == ["WT-02"]
    assert found[0]["subject"] == "TP-1/FW-104"
    assert found[0]["severity"] == "critical"


def test_a_pass_with_no_welder_names_the_pass(download):
    db, pid = download(welds=[{"Welder ID Cap": ";"}])
    found = wt.welder_missing(db, pid, "r1")
    assert len(found) == 1
    assert "cap pass" in found[0]["message"]
    assert "root" not in found[0]["message"]


def test_three_empty_passes_are_one_finding(download):
    db, pid = download(welds=[{"Welder ID Root": "-", "Welder ID Fill": "-",
                               "Welder ID Cap": "-"}])
    found = wt.welder_missing(db, pid, "r1")
    assert len(found) == 1
    assert "root, fill, cap passes" in found[0]["message"]


def test_a_weld_with_no_date(download):
    db, pid = download(welds=[{"Date Welded": "-"}])
    assert codes(wt.date_welded_missing(db, pid, "r1")) == ["WT-04"]


def test_a_date_no_parser_accepts_is_the_same_finding(download):
    db, pid = download(welds=[{"Date Welded": "8/222/2026"}])
    assert codes(wt.date_welded_missing(db, pid, "r1")) == ["WT-04"]


def test_welded_before_the_planned_date_is_minor(download):
    db, pid = download(welds=[{"Date Planned": "Aug-21-2026",
                               "Date Welded": "Aug-20-2026"}])
    found = wt.date_before_plan(db, pid, "r1")
    assert codes(found) == ["WT-05"]
    assert found[0]["severity"] == "minor"


def test_one_pack_with_two_reference_numbers(download):
    db, pid = download(welds=[
        {"Weld Number": "FW-1"},
        {"Weld Number": "FW-2", "Test Pack Reference": PACK_REF[:-1]},
    ])
    found = wt.pack_reference_split(db, pid, "r1")
    assert codes(found) == ["WT-06"]
    assert found[0]["subject"] == "TP-1"


# -- WT-07..WT-10: against the heat register --------------------------------

def test_a_joint_with_no_heat_on_one_side(download):
    db, pid = download(welds=[{"Material 2 - Heat Number": "-"}])
    found = wt.heat_missing(db, pid, "r1")
    assert codes(found) == ["WT-07"]
    assert "Material 2" in found[0]["message"]


def test_a_heat_that_is_in_no_material_register(download):
    db, pid = download(welds=[{"Material 1 - Heat Number": "70999"}])
    found = wt.heat_unknown(db, pid, "r1")
    assert codes(found) == ["WT-08"]
    assert "70999" in found[0]["message"]


def test_a_product_form_the_two_registers_disagree_about(download):
    db, pid = download(welds=[{"Material1": "FLANGE"}])
    found = wt.heat_form_mismatch(db, pid, "r1")
    assert codes(found) == ["WT-09"]
    assert "FLANGE" in found[0]["message"] and "PIPE" in found[0]["message"]


def test_a_form_that_differs_only_in_case_is_not_a_disagreement(download):
    db, pid = download(welds=[{"Material1": "pipe"}])
    assert wt.heat_form_mismatch(db, pid, "r1") == []


def test_a_register_that_states_no_form_is_a_gap_not_a_disagreement(download):
    db, pid = download(heats=[{"Product Form": "-", "Pipe Fitting Type": "-"}])
    assert wt.heat_form_mismatch(db, pid, "r1") == []


def test_a_heat_the_register_no_longer_calls_active(download):
    db, pid = download(heats=[{"Status": "Quarantined"}])
    found = wt.heat_inactive(db, pid, "r1")
    assert codes(found) == ["WT-10"]
    assert "Quarantined" in found[0]["message"]


# -- WT-11..WT-13: the heat register on its own -----------------------------

def test_a_heat_with_no_certificate_attached(download):
    db, pid = download(heats=[{"File Name": "-"}])
    found = wt.mtr_missing(db, pid, "r1")
    assert codes(found) == ["WT-11"]
    assert found[0]["subject"] == "70097"


def test_a_heat_the_approved_list_cannot_be_applied_to(download):
    db, pid = download(heats=[{"Supplier": "-", "Spec No.": "-",
                               "Alloy Type or Grade": "-", "P-No.": "-"}])
    found = wt.mtr_fields_blank(db, pid, "r1")
    assert codes(found) == ["WT-12"]
    assert "Supplier, Spec No., Grade or P-No." in found[0]["message"]


def test_one_blank_field_is_enough_to_stop_the_approved_list_check(download):
    db, pid = download(heats=[{"Supplier": "-"}])
    found = wt.mtr_fields_blank(db, pid, "r1")
    assert "no Supplier in the material register" in found[0]["message"]


def test_a_heat_that_is_welded_into_nothing(download):
    db, pid = download(heats=[{}, {"Heat Number": "70100"}])
    found = wt.heat_unused(db, pid, "r1")
    assert codes(found) == ["WT-13"]
    assert found[0]["subject"] == "70100"
    assert found[0]["severity"] == "minor"


# -- WT-14..WT-18: examinations ---------------------------------------------

def test_a_method_requested_and_never_reported(download):
    db, pid = download(welds=[{"RT Result & Report-Rev & Date": "-"}])
    found = wt.result_missing(db, pid, "r1")
    assert codes(found) == ["WT-14"]
    assert "RT was requested" in found[0]["message"]


def test_a_rejection_with_no_retest(download):
    db, pid = download(welds=[
        {"RT Result & Report-Rev & Date": f"Rejected;{PACK_REF}-0;Aug-21-2026;"}])
    found = wt.fail_no_retest(db, pid, "r1")
    assert codes(found) == ["WT-15"]
    assert found[0]["severity"] == "critical"


def test_a_rejection_that_was_retested_is_not_reported(download):
    db, pid = download(welds=[{
        "RT Result & Report-Rev & Date": f"Rejected;{PACK_REF}-0;Aug-21-2026;",
        "RT Retest Requested": "Yes",
        "RT Retest Result & Report-Rev & Date": f"Passed;{PACK_REF}-1;Aug-22-2026;",
    }])
    assert wt.fail_no_retest(db, pid, "r1") == []


def test_a_result_that_is_neither_a_pass_nor_a_fail(download):
    db, pid = download(welds=[
        {"RT Result & Report-Rev & Date": "In progress;-;-;"}])
    found = wt.result_unclear(db, pid, "r1")
    assert codes(found) == ["WT-16"]
    assert "In progress" in found[0]["message"]


def test_a_report_number_one_digit_off_the_pack_reference(download):
    # The sample download's twenty-four-weld version of this: one dropped
    # digit, propagated across a whole pack, and every row looks right beside
    # the last.
    wrong = PACK_REF.replace("40300118", "4030118")
    assert wrong != PACK_REF, "the fixture must actually drop a digit"
    db, pid = download(welds=[
        {"RT Result & Report-Rev & Date": f"Passed;{wrong}-0;Aug-21-2026;"}])
    found = wt.report_reference_mismatch(db, pid, "r1")
    assert codes(found) == ["WT-17"]
    assert wrong in found[0]["message"] and PACK_REF in found[0]["message"]


def test_a_report_cited_with_a_revision_still_matches_its_pack(download):
    db, pid = download()
    assert wt.report_reference_mismatch(db, pid, "r1") == []


def test_the_same_number_written_vendor_last_is_not_a_mistype(download):
    """One report, two house styles.

    A vendor's own system writes VENDOR-TV-...RT01 and the pack is issued
    as TV-...RT01-VENDOR. Compared as strings that is a mismatch, and it
    reported sixteen welds on a real job for a difference that was only in the
    order the parts were written.
    """
    parts = PACK_REF.split("-")
    reordered = "-".join(parts[1:] + parts[:1])
    assert reordered != PACK_REF, "the fixture must actually reorder something"
    db, pid = download(welds=[
        {"RT Result & Report-Rev & Date": f"Passed;{reordered}-0;Aug-21-2026;"}])
    assert wt.report_reference_mismatch(db, pid, "r1") == []


def test_case_and_separators_do_not_make_a_mismatch(download):
    db, pid = download(welds=[
        {"RT Result & Report-Rev & Date":
         f"Passed;{PACK_REF.lower()}-0;Aug-21-2026;"}])
    assert wt.report_reference_mismatch(db, pid, "r1") == []


def test_reordering_does_not_hide_a_dropped_digit(download):
    """The guard must not swallow the defect it sits next to."""
    parts = PACK_REF.replace("40300118", "4030118").split("-")
    wrong = "-".join(parts[1:] + parts[:1])
    db, pid = download(welds=[
        {"RT Result & Report-Rev & Date": f"Passed;{wrong}-0;Aug-21-2026;"}])
    assert codes(wt.report_reference_mismatch(db, pid, "r1")) == ["WT-17"]


def test_a_repeated_component_is_not_treated_as_the_same_report():
    """Compared as a sorted list, not a set: A-A-B is not A-B-B."""
    assert not wt.same_report("A-A-B", "A-B-B")
    assert wt.same_report("A-B-C", "C-A-B")
    assert not wt.same_report("", "A-1")


def test_a_weld_nothing_was_asked_of(download):
    db, pid = download(welds=[{"RT Test Requested": "No",
                               "RT Result & Report-Rev & Date": "-"}])
    found = wt.nde_none_requested(db, pid, "r1")
    assert codes(found) == ["WT-18"]


# -- WT-19..WT-21: the register against the drawings ------------------------

def test_a_registered_weld_stamped_on_no_drawing(download):
    db, pid = download(welds=[{"Weld Number": "FW-16"}],
                       stamps=[{"Text inside bubble": "AFW-104\nAOO"}])
    assert codes(wt.stamp_missing(db, pid, "r1")) == ["WT-19"]


def test_a_prefixed_stamp_still_counts_as_stamped(download):
    # AFW-104 on the drawing and FW-104 in the register are one weld. In the
    # sample download this reading is the difference between 19 matches and
    # 103 out of 107.
    db, pid = download()
    assert wt.stamp_missing(db, pid, "r1") == []
    assert db.one("SELECT stamps FROM weldtrace_weld WHERE project_id=?",
                  (pid,))["stamps"] == 1


def test_a_weld_stamped_on_the_wrong_isometric(download):
    db, pid = download(stamps=[{"Drawing Number": "2-D1-0-GL-4015-1"}])
    found = wt.stamp_drawing_mismatch(db, pid, "r1")
    assert codes(found) == ["WT-20"]
    assert "2-D1-0-GL-4012-1" in found[0]["message"]
    assert "2-D1-0-GL-4015-1" in found[0]["message"]


def test_a_reissued_revision_is_not_a_drawing_mismatch(download):
    db, pid = download(stamps=[{"Drawing Revision": "2"}])
    assert wt.stamp_drawing_mismatch(db, pid, "r1") == []


def test_a_stamp_that_is_in_no_test_pack(download):
    db, pid = download(stamps=[{}, {"Text inside bubble": "BFW-81\nARV",
                                    "Drawing Number": "2-D1-0-GL-4015-1"}])
    found = wt.stamp_orphan(db, pid, "r1")
    assert codes(found) == ["WT-21"]
    assert found[0]["subject"] == "BFW-81"
    assert "2-D1-0-GL-4015-1" in found[0]["message"]


def test_the_two_sides_of_a_prefix_typo_are_both_reported(download):
    # W-81 in the register against BFW-81 on the drawing is almost certainly
    # one joint mistyped - and almost is not something to write into a
    # turnover package, so both halves are reported and neither is resolved.
    db, pid = download(welds=[{"Weld Number": "W-81"}],
                       stamps=[{"Text inside bubble": "BFW-81\nARV"}])
    assert codes(wt.stamp_missing(db, pid, "r1")) == ["WT-19"]
    assert codes(wt.stamp_orphan(db, pid, "r1")) == ["WT-21"]
