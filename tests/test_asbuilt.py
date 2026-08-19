"""As-builts: reading a drawing rendered into a spreadsheet, and using it.

The reading is the hard half. An as-built is one **column per joint**, six
columns to a block, with several **bands** of joints stacked down each sheet —
and the values inside a block do not share a column, because merged cells push
the station two columns left of the heat. The tests below use the real
geometry from `25404-6-FG-North MAINLINE AS-BUILT.xlsx`.

The rules half is mostly about not over-claiming. The as-built adds 1,976
joints and 1,974 X-ray numbers to a corpus whose other registers are smaller,
so a naive comparison reports the difference as a defect rather than as the
as-built simply knowing more.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.asbuilt import (  # noqa: E402
    format_station, parse_length, parse_sheet, parse_station,
)
from weldaudit.db import Database  # noqa: E402
from weldaudit.rules import asbuilt as rules  # noqa: E402


# -- stationing -------------------------------------------------------------

@pytest.mark.parametrize("text,feet", [
    ("0+02", 2.0),
    ("1+22", 122.0),
    ("130+00", 13000.0),
    ("135+00", 13500.0),
    ("2+05.5", 205.5),
    ("  1 + 63 ", 163.0),
    # The unfilled template writes the station as underscores. Reading that as
    # station zero would put every joint at the head of the line.
    ("____+____", None),
    ("", None),
    ("Continue from Fab", None),
])
def test_station_parsing(text, feet):
    assert parse_station(text) == feet


@pytest.mark.parametrize("feet,text", [(13000.0, "130+00"), (2.0, "0+02"),
                                       (163.0, "1+63"), (None, "")])
def test_station_formatting(feet, text):
    assert format_station(feet) == text


@pytest.mark.parametrize("text,feet", [("44.5'", 44.5), ("40.2", 40.2),
                                       ("", None), ("N/A", None)])
def test_length_parsing(text, feet):
    assert parse_length(text) == feet


# -- reading the sheet ------------------------------------------------------

def band(sta, length, heat, joint, xray, base=6, pitch=6):
    """One band of joints in the real column geometry.

    Labels sit at the block start; the station lands one column before it and
    the X-ray two before that, because of how the template is merged.
    """
    width = base + pitch * (len(sta) + 1)
    rows = []

    def row(label, values, offset):
        cells = [None] * width
        cells[3] = label if label in ("STA:", "LENGTH:", "X-RAY #:") else None
        for k, v in enumerate(values):
            col = base + pitch * k
            if label in ("HT#:", "JT#:"):
                cells[col] = label
                cells[col + 1] = v
            else:
                cells[col + offset] = v
        rows.append(cells)

    row("STA:", sta, -1)
    row("LENGTH:", length, 1)
    row("HT#:", heat, 0)
    row("JT#:", joint, 0)
    row("X-RAY #:", xray, -2)
    return rows


SHEET = (
    [[None] * 40,
     [None, None, None, "PIPE SIZE:", '6\'\'', None, "GRADE:", None, "SCH80",
      None, None, "WT:", "0.280", None, None, None, "SERVICE:", "FUEL GAS"],
     [None] * 40]
    + band(["0+02", "0+42", "0+82"], [40.2, 40.2, 40.2],
           ["1244878", "1251573", "1251573"],
           ["North-1", "North-2", "North-3"], ["CML-27", "CML-26", "CML-25"])
    + band(["1+22", "1+63"], [40.2, 38.5], ["1251573", "1244878"],
           ["North-4", "North-5A"], ["CML-24", "CML-23"])
)


def test_a_sheet_reads_its_joints():
    sheet = parse_sheet("As-Built (1)", SHEET)
    assert [j.station for j in sheet.joints] == ["0+02", "0+42", "0+82",
                                                 "1+22", "1+63"]
    first = sheet.joints[0]
    assert (first.station_ft, first.length) == (2.0, 40.2)
    assert (first.heat, first.joint_no, first.xray) == ("1244878", "North-1", "CML-27")


def test_the_header_strip_is_read():
    sheet = parse_sheet("As-Built (1)", SHEET)
    assert sheet.pipe_size == "6''" and sheet.service == "FUEL GAS"
    assert sheet.grade == "SCH80" and sheet.wall == "0.280"


def test_several_bands_on_one_sheet_are_all_read():
    # `As-Built 8 IN OIL.xlsx` stacks ten bands down one sheet; reading only
    # the first found seven joints where there are a hundred and twenty-one.
    sheet = parse_sheet("s", SHEET)
    assert {j.band for j in sheet.joints} == {1, 2}
    assert len(sheet.joints) == 5


def test_a_joint_repeated_at_a_band_boundary_counts_once():
    # The tie-in station is drawn at the end of one band and the start of the
    # next, and the repeat is usually the sparser copy.
    rows = (SHEET
            + band(["1+63", "1+99"], [None, 6.2], ["", "1244878"],
                   ["", "North-6C"], ["CML-23", "GTI-11"]))
    sheet = parse_sheet("s", rows)
    stations = [j.station for j in sheet.joints]
    assert stations.count("1+63") == 1
    # and the copy kept is the one with the heat on it
    kept = next(j for j in sheet.joints if j.station == "1+63")
    assert kept.heat == "1244878"


def test_an_annotation_is_not_a_joint():
    # Sheets open with "SEE ISO DRAWING" spread across two blocks, which
    # arrives as a joint with a heat of "SEE ISO" and a joint number of
    # "DRAWING" unless a block is required to carry a station or an X-ray.
    rows = band(["", ""], [None, None], ["SEE ISO", ""], ["DRAWING", ""],
                ["", ""])
    assert parse_sheet("s", rows).joints == []


def test_a_sheet_that_is_not_an_asbuilt_yields_nothing():
    assert parse_sheet("s", [["Flange #", "Size"], ["1", "12"]]).joints == []


# -- the rules --------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "a.db")
    pid = database.upsert_project("A", str(tmp_path))
    with database.tx() as c:
        c.execute(
            """INSERT INTO document(id, project_id, path, filename, ext, kind,
                                    segment, fingerprint)
               VALUES(1, ?, 'p', 'AS-BUILT.xlsx', '.xlsx', 'as_built',
                      '6 FG', 'fp1')""",
            (pid,),
        )
        c.execute(
            """INSERT INTO document(id, project_id, path, filename, ext, kind,
                                    segment, fingerprint)
               VALUES(2, ?, 'p2', 'Backfill Release Forms.pdf', '.pdf',
                      'backfill', '6 FG', 'fp2')""",
            (pid,),
        )
    return database, pid


def joint(db, pid, *, station="1+00", length=40.0, heat="1244878",
          nde_id="CML-27", segment="6 FG", seq=1):
    from weldaudit.asbuilt import parse_station as ps
    from weldaudit.mtrname import normalise_heat
    with db.tx() as c:
        c.execute(
            """INSERT INTO asbuilt_joint
               (project_id, document_id, segment, sheet, band, seq, station,
                station_ft, length, heat, heat_key, joint_no, xray, nde_id,
                source)
               VALUES(?, 1, ?, 'As-Built (1)', 1, ?, ?, ?, ?, ?, ?, 'J', ?, ?,
                      'asbuilt_xlsx')""",
            (pid, segment, seq, station, ps(station), length, heat,
             normalise_heat(heat), nde_id, nde_id),
        )


def released(db, pid, frm, to, *, segment="6 FG", page=1):
    # Record the reading as well as the release: the guard asks how much of
    # the bundle was read, not how many releases came out of it.
    db.ocr_put("fp2:backfill:2000", page - 1, "test-model",
               {"page_is_release": True, "from_station": frm, "to_station": to})
    with db.tx() as c:
        c.execute(
            """INSERT INTO backfill_release
               (project_id, document_id, segment, page_no, from_station,
                to_station, inspector_signed, inspector_date, released_on,
                source)
               VALUES(?, 2, ?, ?, ?, ?, 1, '2025-08-01', '2025-08-01',
                      'backfill_vision')""",
            (pid, segment, page, frm, to),
        )


def shot(db, pid, nde_id, segment="6 FG"):
    with db.tx() as c:
        c.execute(
            """INSERT INTO nde_shot(project_id, segment, nde_id, evidence)
               VALUES(?, ?, ?, 'filename')""", (pid, segment, nde_id))


def certificate(db, pid, heat):
    from weldaudit.mtrname import normalise_heat
    with db.tx() as c:
        c.execute(
            """INSERT INTO material(project_id, heat, heat_key, source)
               VALUES(?, ?, ?, 'mtr_file')""", (pid, heat, normalise_heat(heat)))


def weld(db, pid, weld_no, segment="6 FG"):
    with db.tx() as c:
        c.execute(
            """INSERT INTO weld(project_id, segment, line, weld_no, source)
               VALUES(?, ?, '6 FG', ?, 'weld_map_text')""", (pid, segment, weld_no))


def fire(db, pid, rule):
    return rule(db, pid, "run")


@pytest.fixture(autouse=True)
def one_page_releases(monkeypatch):
    """Treat each release document as fully read, as the fixtures seed one page."""
    monkeypatch.setattr("weldaudit.vision.page_count", lambda path: 1)


# -- AB-01 stationing against the release -----------------------------------

def test_a_joint_inside_the_released_stretch_passes(db):
    database, pid = db
    released(database, pid, "0+00", "5+00")
    joint(database, pid, station="1+00")
    assert fire(database, pid, rules.joint_outside_release) == []


def test_a_joint_beyond_every_release_is_critical(db):
    # The check BF-01 could not make: inside the dates, outside the length.
    database, pid = db
    released(database, pid, "130+00", "135+00")
    joint(database, pid, station="131+00", seq=1)
    joint(database, pid, station="200+00", seq=2)
    found = fire(database, pid, rules.joint_outside_release)
    assert len(found) == 1 and found[0]["severity"] == "critical"
    assert "200+00" in found[0]["message"] and "131+00" not in found[0]["message"]
    assert "130+00–135+00" in found[0]["message"]


def test_several_releases_cover_between_them(db, monkeypatch):
    monkeypatch.setattr("weldaudit.vision.page_count", lambda path: 2)
    database, pid = db
    released(database, pid, "0+00", "5+00", page=1)
    released(database, pid, "5+00", "10+00", page=2)
    joint(database, pid, station="7+00")
    assert fire(database, pid, rules.joint_outside_release) == []


def test_the_ends_of_a_release_have_a_little_slack(db):
    # Stations are recorded to the foot; releases are written to round ones.
    database, pid = db
    released(database, pid, "1+00", "5+00")
    joint(database, pid, station="0+90")
    assert fire(database, pid, rules.joint_outside_release) == []


def test_a_partly_read_release_bundle_makes_no_claim(db, monkeypatch):
    monkeypatch.setattr("weldaudit.vision.page_count", lambda path: 27)
    database, pid = db
    released(database, pid, "130+00", "135+00")
    joint(database, pid, station="200+00")
    assert fire(database, pid, rules.joint_outside_release) == []


def test_no_releases_read_means_no_claim(db):
    database, pid = db
    joint(database, pid, station="200+00")
    assert fire(database, pid, rules.joint_outside_release) == []


def test_a_joint_with_no_station_cannot_be_placed(db):
    database, pid = db
    released(database, pid, "0+00", "5+00")
    joint(database, pid, station="____+____")
    assert fire(database, pid, rules.joint_outside_release) == []


# -- AB-02 unknown NDE reports ----------------------------------------------

def test_a_known_report_passes(db):
    database, pid = db
    joint(database, pid, nde_id="CML-27")
    shot(database, pid, "CML-27")
    assert fire(database, pid, rules.unknown_nde_report) == []


def test_an_unknown_report_is_reported(db):
    database, pid = db
    joint(database, pid, nde_id="CML-27", seq=1)
    joint(database, pid, nde_id="CML-99", seq=2)
    shot(database, pid, "CML-27")
    found = fire(database, pid, rules.unknown_nde_report)
    assert len(found) == 1 and found[0]["subject"] == "1 report"
    assert "CML-99" in found[0]["message"]


def test_a_job_with_no_parsed_reader_sheets_makes_no_claim(db):
    # PLU files 65 reader sheets and none yields an id the filename grammar
    # recognises. Without this guard every X-ray on its as-built reads as
    # unfiled — a finding about the NDE package wearing the as-built's name.
    database, pid = db
    joint(database, pid, nde_id="CML-27")
    weld(database, pid, "W1")
    assert fire(database, pid, rules.unknown_nde_report) == []


# -- AB-03 heats ------------------------------------------------------------

def test_a_certified_heat_passes(db):
    database, pid = db
    joint(database, pid, heat="1244878")
    certificate(database, pid, "1244878")
    assert fire(database, pid, rules.uncertified_heat) == []


def test_an_uncertified_heat_is_reported(db):
    database, pid = db
    joint(database, pid, heat="MGOO93", seq=1)
    certificate(database, pid, "1244878")
    found = fire(database, pid, rules.uncertified_heat)
    assert len(found) == 1 and found[0]["subject"] == "1 heat"
    assert "MGOO93" in found[0]["message"]
    assert "1 appears on no other register" in found[0]["message"]


def test_a_job_with_no_certificates_makes_no_claim(db):
    # MTR-10 reports a job whose folders hold no certificates at all.
    database, pid = db
    joint(database, pid, heat="MGOO93")
    assert fire(database, pid, rules.uncertified_heat) == []


# -- AB-04 missing heats ----------------------------------------------------

def test_joints_with_no_heat_are_reported(db):
    database, pid = db
    joint(database, pid, heat="1244878", seq=1)
    joint(database, pid, heat="", station="2+00", seq=2)
    found = fire(database, pid, rules.joint_without_heat)
    assert len(found) == 1 and found[0]["subject"] == "1 joint"
    assert "2+00" in found[0]["message"]


def test_a_sheet_that_never_uses_the_heat_column_is_not_reported(db):
    database, pid = db
    joint(database, pid, heat="", seq=1)
    joint(database, pid, heat="", station="2+00", seq=2)
    assert fire(database, pid, rules.joint_without_heat) == []


def test_the_blank_template_station_is_not_quoted(db):
    database, pid = db
    joint(database, pid, heat="1244878", seq=1)
    joint(database, pid, heat="", station="____+____", seq=2)
    found = fire(database, pid, rules.joint_without_heat)
    assert "____" not in found[0]["message"]


# -- AB-05 length -----------------------------------------------------------

def hydrotest(db, pid, segment="6 FG"):
    with db.tx() as c:
        c.execute(
            """INSERT INTO hydrotest(project_id, segment, started_raw, source)
               VALUES(?, ?, '8/18/25', 'hydrotest_vision')""", (pid, segment))


def test_lengths_that_agree_pass(db):
    database, pid = db
    hydrotest(database, pid)
    for i in range(5):
        joint(database, pid, station=f"{i}+00", length=100.0, seq=i)
    assert fire(database, pid, rules.length_disagrees) == []


def test_lengths_that_disagree_are_reported(db):
    database, pid = db
    hydrotest(database, pid)
    for i in range(5):
        joint(database, pid, station=f"{i}+00", length=40.0, seq=i)
    found = fire(database, pid, rules.length_disagrees)
    assert len(found) == 1 and "0+00 to 4+00" in found[0]["message"]


def test_without_a_pressure_test_there_is_nothing_to_compare(db):
    database, pid = db
    for i in range(5):
        joint(database, pid, station=f"{i}+00", length=40.0, seq=i)
    assert fire(database, pid, rules.length_disagrees) == []


# -- AB-06 missing as-builts ------------------------------------------------

def test_a_welded_segment_with_no_asbuilt_is_reported(db):
    database, pid = db
    joint(database, pid)
    weld(database, pid, "W1", segment="20 LP")
    found = fire(database, pid, rules.segment_without_asbuilt)
    assert len(found) == 1 and found[0]["segment"] == "20 LP"


def test_no_asbuilts_at_all_means_no_claim(db):
    database, pid = db
    weld(database, pid, "W1", segment="20 LP")
    assert fire(database, pid, rules.segment_without_asbuilt) == []


# -- the summary ------------------------------------------------------------

def test_the_summary_reports_the_span_and_completeness(db):
    database, pid = db
    joint(database, pid, station="0+00", length=40.0, seq=1)
    joint(database, pid, station="1+00", length=40.0, heat="", seq=2)
    row = rules.asbuilt_summary(database, pid)[0]
    assert row["joints"] == 2 and row["with_heat"] == 1 and row["with_nde"] == 2
    assert (row["from_station"], row["to_station"]) == ("0+00", "1+00")
    assert row["length"] == 80.0
