"""Reading a WeldTrace download.

Six things in these exports break a straightforward reader, and every one of
them was found the expensive way - by a first implementation getting it wrong
against a real download and producing a number that was obviously false. One
test each, because each is a silent failure rather than a crash: a mangled
header reads as a missing column, a hyphen reads as a filled-in field, and
stripping one revision group too many turns four genuine mismatches into a
hundred and three.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit import taxonomy, weldtrace as wt  # noqa: E402


# -- fixtures ---------------------------------------------------------------

WELD_HEADER = (
    'Test Pack # - Rev,Test Pack Reference,Weld Number,Drawing # - Revision,'
    'Line Number,Category,Joint Type,"Weld Size ("in),WPs# - Revision,'
    'Welder ID Root,Welder ID Fill,Welder ID Cap,Date Planned,Date Welded,'
    'Material1,Material 1 - Heat Number,Material2,Material 2 - Heat Number,'
    'RT Test Requested,RT Result & Report-Rev & Date,RT Retest Requested,'
    'RT Retest Result & Report-Rev & Date,VI Test Requested,'
    'VI Result & Report-Rev & Date,Test Result,Penalty'
)


def weld_row(**over) -> str:
    row = {
        "pack": "TP-1-1", "reference": "IIAFIELD-8-21-2026-17600252",
        "weld": "FW-104", "drawing": "2-D1-0-GL-4012-1-0",
        "line": "6-B1-0V-PF-4112", "category": "Field", "joint": "BW",
        "size": "6", "wps": "XTO-ASME-P1-HYP-NACE-0",
        "root": "AOO;", "fill": "AOO;", "cap": "AOO;",
        "planned": "Aug-21-2026", "welded": "Aug-21-2026",
        "material1": "PIPE", "heat1": "70097", "material2": "PIPE",
        "heat2": "70097",
        "rt_requested": "Yes",
        "rt": "Passed;IIAFIELD-8-21-2026-17600252-0;Aug-21-2026;",
        "rt_retest_requested": "No", "rt_retest": "-",
        "vi_requested": "Yes", "vi": "Passed;-;Aug-21-2026;",
        "result": "Accepted", "penalty": "-",
    }
    row.update(over)
    return ",".join(f'"{row[k]}"' for k in (
        "pack", "reference", "weld", "drawing", "line", "category", "joint",
        "size", "wps", "root", "fill", "cap", "planned", "welded",
        "material1", "heat1", "material2", "heat2", "rt_requested", "rt",
        "rt_retest_requested", "rt_retest", "vi_requested", "vi", "result",
        "penalty"))


def weld_export(tmp_path, *rows, name="TestPackExport.csv") -> Path:
    path = tmp_path / name
    path.write_text("\n".join([WELD_HEADER, *(rows or [weld_row()])]),
                    encoding="utf-8")
    return path


HEAT_HEADER = ("Heat Number,Material Name,Product Form,Pipe Fitting Type,"
               "Supplier,Spec No.,Alloy Type or Grade,P-No.,File Name,Status")


def heat_export(tmp_path, *rows, name="projectMaterialsExport.csv") -> Path:
    path = tmp_path / name
    body = rows or ('"70097","6in Pipe","PIPE","-","Welspun","A106","B","1",'
                    '"70097-MTR.pdf","Active"',)
    path.write_text("\n".join([HEAT_HEADER, *body]), encoding="utf-8")
    return path


# -- quirk 1: the malformed header quote ------------------------------------

def test_the_mangled_weld_size_header_is_repaired(tmp_path):
    # Column 19's header is `"Weld Size ("in)` - an unescaped quote inside a
    # quoted field. The rows survive it; the header token does not.
    welds = wt.parse_weld_register(weld_export(tmp_path, weld_row(size="6")))
    assert welds[0].size == "6"


def test_the_header_is_repaired_on_content_not_on_position():
    # The column sits at index 19 in a test pack export and at index 9 in a
    # welds export, so a positional repair fixes one by breaking the other.
    assert wt.repair_header(['"Weld Size ("in)']) == ["Weld Size (in)"]
    assert wt.repair_header(["A", "B", 'Weld Size ("in']) == [
        "A", "B", "Weld Size (in)"]


# -- quirk 2: a dash means null ---------------------------------------------

def test_a_hyphen_is_read_as_empty_not_as_a_value(tmp_path):
    # Every empty cell exports as a single hyphen. Without this, every
    # "is this filled in?" check on the whole download silently passes.
    welds = wt.parse_weld_register(
        weld_export(tmp_path, weld_row(wps="-", welded="-", heat2="-")))
    assert welds[0].wps == ""
    assert welds[0].date_welded == ""
    assert welds[0].heats[1] == ""


# -- quirk 3: drawing and revision are fused --------------------------------

def test_exactly_one_trailing_revision_group_is_stripped():
    # The CSV writes drawing and revision fused; the annotation export keeps
    # them apart. Stripping both sides was the bug that turned four genuine
    # mismatches into 103 out of 107.
    assert wt.split_trailing_revision("2-D1-0-GL-4012-1-0") == (
        "2-D1-0-GL-4012-1", "0")
    assert wt.split_trailing_revision("2-D1-0-GL-4012-1") == (
        "2-D1-0-GL-4012", "1")


def test_a_drawing_with_no_revision_group_is_left_alone():
    assert wt.split_trailing_revision("ISO-A") == ("ISO-A", "")


def test_the_register_side_is_split_and_the_stamp_side_is_not(tmp_path):
    welds = wt.parse_weld_register(
        weld_export(tmp_path, weld_row(drawing="2-D1-0-GL-4012-1-0")))
    assert welds[0].drawing == "2-D1-0-GL-4012-1"
    assert welds[0].revision == "0"


# -- quirk 4: stamps carry a side prefix ------------------------------------

def test_a_stamp_prefix_and_a_trailing_p_are_looser_readings_of_a_tag():
    # AFW-104 and FW-104 are the same weld; the exact reading is tried first
    # so that a register which numbers its own welds BFW-4 still matches.
    tag = wt.parse_tag("AFW-104P")
    assert (tag.prefix, tag.number, tag.suffix) == ("AFW", 104, "P")
    assert wt.WeldTag("FW", 104) in tag.variants
    assert tag.variants[0] == tag


def test_an_exact_tag_beats_a_looser_one():
    exact = wt.WeldTag("BFW", 4)
    loose = wt.WeldTag("FW", 4)
    by_tag = {exact: ["stamped BFW-4"], loose: ["stamped FW-4"]}
    weld = _weld_numbered("BFW-4")
    assert wt.match_stamps(weld, by_tag) == ["stamped BFW-4"]


def test_a_register_tag_matches_a_prefixed_stamp():
    by_tag = {wt.WeldTag("AFW", 104): ["stamped AFW-104"]}
    assert wt.match_stamps(_weld_numbered("FW-104"), by_tag) == [
        "stamped AFW-104"]


def _weld_numbered(number: str) -> wt.WeldRow:
    return wt.WeldRow(
        weld_number=number, tag=wt.parse_tag(number), test_pack="",
        pack_reference="", drawing="", revision="", line="", line_class="",
        category="", joint_type="", size="", wps="", wps_revision="",
        welders={"root": [], "fill": [], "cap": []}, date_planned="",
        date_welded="", materials=("", ""), heats=("", ""), exams={},
        result="", penalty="")


# -- quirk 5: welder IDs are semicolon lists --------------------------------

def test_a_bare_semicolon_is_not_a_welder(tmp_path):
    welds = wt.parse_weld_register(
        weld_export(tmp_path, weld_row(root="AOO;", fill=";", cap="AOO;BRT;")))
    assert welds[0].welders["root"] == ["AOO"]
    assert welds[0].welders["fill"] == []
    assert welds[0].welders["cap"] == ["AOO", "BRT"]


# -- quirk 6: two date formats, and typos -----------------------------------

def test_both_date_formats_are_accepted():
    assert wt.parse_date("Aug-14-2026") == wt.parse_date("8/14/2026")
    assert wt.parse_date("Aug-14-2026")


def test_a_malformed_date_is_refused_rather_than_guessed_at():
    # 8/222/2026 is in the sample download. Reading it as August 2026 would
    # put a weld date on a joint that has none.
    assert wt.parse_date("8/222/2026") == ""


def test_dates_are_comparable_across_the_two_formats():
    assert wt.parse_date("Aug-20-2026") < wt.parse_date("8/21/2026")


# -- the material register --------------------------------------------------

def test_a_heat_register_reports_the_fields_the_aml_check_needs(tmp_path):
    heats = wt.parse_material_register(heat_export(
        tmp_path, '"70097","6in Pipe","PIPE","-","-","-","-","-","-","Active"'))
    assert heats["70097"].missing_aml_fields == list(wt.HeatRow.AML_FIELDS)
    assert heats["70097"].mtr_file == ""


def test_a_complete_heat_reports_nothing_missing(tmp_path):
    heats = wt.parse_material_register(heat_export(tmp_path))
    assert heats["70097"].missing_aml_fields == []


# -- examinations -----------------------------------------------------------

def test_a_result_splits_into_verdict_report_and_date(tmp_path):
    welds = wt.parse_weld_register(weld_export(tmp_path))
    rt = welds[0].exams["RT"]
    assert rt.passed and not rt.failed
    assert rt.report == "IIAFIELD-8-21-2026-17600252"
    assert rt.revision == "0"


def test_a_method_with_no_request_column_is_wanted_if_it_reported(tmp_path):
    # A welds export carries results and no request columns. Reading a missing
    # column as "not requested" would report every weld as never examined.
    path = tmp_path / "weldsExport.csv"
    path.write_text(
        "Weld Number,RT Result & Report-Rev & Date\n"
        '"W-1","Passed;CQ-1-0;Aug-21-2026;"\n', encoding="utf-8")
    weld = wt.parse_weld_register(path)[0]
    assert weld.exams["RT"].requested is True
    assert weld.exams["UT"].requested is None
    assert weld.asks_for_nde


def test_a_weld_nothing_was_asked_of_says_so(tmp_path):
    welds = wt.parse_weld_register(
        weld_export(tmp_path, weld_row(rt_requested="No", rt="-",
                                       vi_requested="No", vi="-")))
    assert welds[0].requested == []


def test_a_method_the_export_is_silent_about_is_not_a_refusal(tmp_path):
    # "Not requested" and "this export has no such column" are different
    # answers, and only the first of them is a weld nobody asked to examine.
    path = tmp_path / "TestPackExport.csv"
    columns = ",".join(f"{m} Test Requested" for m in wt.NDE_METHODS)
    rows = ["Weld Number," + columns,
            '"W-1"' + ',"No"' * len(wt.NDE_METHODS)]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    assert not wt.parse_weld_register(path)[0].asks_for_nde

    path.write_text("\n".join(['Weld Number,RT Test Requested', '"W-1","No"']),
                    encoding="utf-8")
    assert wt.parse_weld_register(path)[0].asks_for_nde


def test_a_blank_heat_column_is_a_missing_heat_not_the_product_form(tmp_path):
    # `Material2` reads "PIPE" on a test pack export. Falling back to it when
    # the heat column is present and empty invents a heat number called PIPE.
    welds = wt.parse_weld_register(
        weld_export(tmp_path, weld_row(material2="PIPE", heat2="-")))
    assert welds[0].heats[1] == ""


def test_a_welds_export_still_takes_the_heat_out_of_the_material_code(tmp_path):
    # Where there is no heat column at all, the leading group of the material
    # code is the heat - that is the only place a welds export records it.
    path = tmp_path / "weldsExport.csv"
    path.write_text("\n".join(
        ["Weld Number,Material1", '"W-1","PRC-2-FLG-WN-RF-S160-A105N"']),
        encoding="utf-8")
    assert wt.parse_weld_register(path)[0].heats[0] == "PRC"


# -- filing an unpacked download --------------------------------------------

@pytest.mark.parametrize("filename, kind", [
    ("TestPackExport.csv", "weldtrace_welds"),
    ("BD16 PAD C weldsExport.csv", "weldtrace_welds"),
    ("projectMaterialsExport.csv", "weldtrace_materials"),
    ("AnnotationAttachments_TEST 1.pdf", "weldtrace_stamps"),
    ("TEST 1 - Test Plan.docx", "weldtrace_test_plan"),
    ("TEST 1 - ISOS AND PIDS.pdf", "weldtrace_isos"),
])
def test_a_download_is_filed_correctly_unrenamed(filename, kind):
    # The exports are named for the report that produced them, so an unpacked
    # download has to be recognised as it arrives rather than after somebody
    # renames it into the book.
    assert taxonomy.kind_for(f"C:/jobs/BD16 PAD C/{filename}") == kind


def test_the_signed_drawing_set_satisfies_the_as_built_section():
    assert taxonomy.section_for("TEST 1 - ISOS AND PIDS.pdf").number == 3


def test_the_test_plan_satisfies_the_hydro_section():
    assert taxonomy.section_for("TEST 1 - Test Plan.pdf").number == 17


def test_the_material_export_does_not_count_as_a_filed_certificate():
    # It names the MTRs rather than being them; counting it would hide a
    # package whose certificates were never filed.
    section = taxonomy.section_for("projectMaterialsExport.csv")
    assert section is None or section.number != 7
