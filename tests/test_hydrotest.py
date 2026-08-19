"""The hydrostatic pressure test rules, and the page-merge that feeds them.

A pressure test package is one document holding several kinds of page, so the
first half of this file pins the merge: requirements from one page and the
record from another have to land on the same row, and replaying the pages must
not duplicate anything.  The second half pins the rules, with particular
attention to the two things that would make them useless — firing on the
pressurisation ramp, and firing on every instrument in a package whose
calibration certificates have not been read.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402
from weldaudit.extract.vision_pass import (  # noqa: E402
    Target, _apply_hydrotest, _parse_datetime, hydrotest_targets, serial_key,
)
from weldaudit.rules import hydrotest as rules  # noqa: E402
from weldaudit.taxonomy import kind_for  # noqa: E402


@pytest.fixture(autouse=True)
def fourteen_page_packages(monkeypatch):
    """The fixture package is fourteen pages, like the real Seg. D one.

    HYD-07 reasons from absence and so waits until every page of every package
    has been read; `apply` below seeds them all.
    """
    monkeypatch.setattr("weldaudit.vision.page_count", lambda path: 14)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "h.db")
    pid = database.upsert_project("H", str(tmp_path))
    with database.tx() as c:
        c.execute(
            """INSERT INTO document(id, project_id, path, filename, ext, kind,
                                    segment, fingerprint)
               VALUES(1, ?, 'p', 'Seg D Hydro-Test.pdf', '.pdf', 'hydrotest',
                      '16IN LP', 'fp1')""",
            (pid,),
        )
    return database, pid


TARGET = Target(1, "p", "Seg D Hydro-Test.pdf", "fp1", 14, "test", "16IN LP")


def ramp_and_hold():
    """The Kestrel 8 shape: three ramp readings, then eight hours at pressure."""
    rows = [{"time": t, "pressure_psig": p, "ambient_temp": "80"} for t, p in (
        ("7:00AM", 109), ("7:20AM", 219), ("7:40AM", 326),
    )]
    for hour, press in ((8, 434), (9, 433), (10, 432), (11, 431),
                        (12, 430), (1, 429), (2, 431), (3, 434)):
        half = "AM" if hour >= 8 and hour < 12 else "PM"
        rows.append({"time": f"{hour}:00{half}", "pressure_psig": press,
                     "ambient_temp": "95"})
    return rows


def record(**over):
    payload = {
        "page_type": "test_record", "service": "LP SEG.D", "line_no": None,
        "code": "B31.8", "pipe_size": '16"', "wall": "0.375", "grade": "X52",
        "required_min_pressure": 428, "required_max_pressure": 444,
        "required_duration_hours": 8,
        "started_at": "8/18/25 7:00am", "completed_at": "8/18/25 4:10pm",
        "stated_duration_hours": 8, "test_medium": "Fresh Water",
        "result": "ACCEPTABLE", "deadweight_sn": "1715013650",
        "pressure_recorder_sn": "242E-5824A", "temp_recorder_sn": "242E-5824C",
        "contractor_representative": "Travis Whitehurst",
        "inspector": "Shawn Aguirre", "instrument_sn": None,
        "calibration_date": None, "readings": ramp_and_hold(),
    }
    payload.update(over)
    return payload


def apply(db, pid, payload, page_no=3):
    """Read a page and fold it in, as a real pass does — cache included.

    HYD-07 asks how much of each package was *read*, not how many tests came
    out of it, so a test that writes the record without recording the reading
    is not testing the same thing the pass does.
    """
    for page in range(TARGET.pages):
        db.ocr_put("fp1:hydrotest:2000", page, "test-model",
                   payload if page == page_no else {"page_type": "chart"})
    return _apply_hydrotest(db, pid, TARGET, payload, page_no)


def fire(db, pid, rule):
    return rule(db, pid, "run")


def only_test(db, pid):
    return db.one("SELECT * FROM hydrotest WHERE project_id=?", (pid,))


# -- classification ---------------------------------------------------------

def test_a_map_filed_in_the_hydro_folder_is_still_a_map():
    # GL 31 keeps its heat and weld maps under HYDRO TEST DOCUMENTS. The
    # filename says what a document is; the folder says where it was put.
    assert kind_for(r"x\HYDRO TEST DOCUMENTS\WELLS MAPS WELD.pdf") == "weld_map"
    assert kind_for(r"x\HYDRO TEST DOCUMENTS\WELLS MAPS Heat.pdf") == "weld_map"
    assert kind_for(r"x\17-HYDRO\Seg. D Hydro-Test Package.pdf") == "hydrotest"


def test_the_folder_still_decides_when_the_filename_says_nothing():
    assert kind_for(r"x\11 NDE\Reader Sheets\AFB-005.pdf") == "nde_reader_sheet"
    # A mill certificate's name is a heat number, which matches no kind rule,
    # so it keeps the hydro folder's kind. Recognising it by shape was tried
    # and reclassified 342 Bluewater reader sheets as certificates: the parser
    # that reads MTR filenames is lenient by design and makes a bad classifier.
    assert kind_for(r"x\HYDRO TEST DOCUMENTS\132044 2 S160 CS Pipe.pdf") == "hydrotest"


def test_misfiled_certificates_are_not_sent_to_the_vision_model(db, monkeypatch):
    # Which is where a heuristic is affordable: guessing wrong only skips a
    # page rather than mislabelling a document. Bluewater's real packages do not
    # say "hydro" in the filename at all, so length is what separates them
    # from GL 31's one-page fitting certificates.
    database, pid = db
    pages = {"014378 6in 45.pdf": 1, "454M06.pdf": 1, "11-16-25 6IN FG A PAD .pdf": 16,
             "Seg D Hydro-Test.pdf": 14,
             "MARDEN PRESSURE TEST PLAN 4IN FG.pdf": 6,
             "GPPB-0130 Rev 2 Pressure Testing Guidance.pdf": 40}
    with database.tx() as c:
        for i, name in enumerate(n for n in pages if n != "Seg D Hydro-Test.pdf"):
            c.execute(
                """INSERT INTO document(project_id, path, filename, ext, kind,
                                        segment, fingerprint)
                   VALUES(?, ?, ?, '.pdf', 'hydrotest', '16IN LP', ?)""",
                (pid, name, name, f"fp{i + 2}"),
            )
    monkeypatch.setattr("weldaudit.extract.vision_pass.page_count",
                        lambda path: pages.get(Path(path).name, 1))

    chosen = {t.filename: t.reason for t in hydrotest_targets(database, pid)}
    assert "014378 6in 45.pdf" not in chosen and "454M06.pdf" not in chosen
    assert "GPPB-0130 Rev 2 Pressure Testing Guidance.pdf" not in chosen
    assert chosen["Seg D Hydro-Test.pdf"] == "pressure test package"
    assert chosen["11-16-25 6IN FG A PAD .pdf"] == "filed under the hydro test section"
    assert chosen["MARDEN PRESSURE TEST PLAN 4IN FG.pdf"] == "test plan only, no record"


# -- merging a package's pages ----------------------------------------------

def test_requirements_and_record_land_on_one_row(db):
    database, pid = db
    apply(database, pid, {
        "page_type": "test_requirements", "required_min_pressure": 428,
        "required_max_pressure": 444, "required_duration_hours": 8,
        "code": "B31.8", "service": "LP SEG.D", "readings": [],
    }, page_no=2)
    apply(database, pid, record(required_min_pressure=None,
                                required_duration_hours=None))

    rows = database.q("SELECT * FROM hydrotest WHERE project_id=?", (pid,))
    assert len(rows) == 1
    assert rows[0]["req_min_press"] == 428 and rows[0]["req_hours"] == 8
    assert rows[0]["stated_hours"] == 8 and rows[0]["result"] == "ACCEPTABLE"


def test_replaying_the_same_pages_does_not_duplicate_readings(db):
    database, pid = db
    for _ in range(3):
        apply(database, pid, record())
    assert len(database.q("SELECT * FROM hydrotest WHERE project_id=?", (pid,))) == 1
    readings = database.q(
        "SELECT * FROM hydrotest_reading WHERE hydrotest_id=?", (only_test(database, pid)["id"],))
    assert len(readings) == len(ramp_and_hold())


def test_charts_and_boilerplate_are_skipped(db):
    database, pid = db
    assert apply(database, pid, {"page_type": "chart", "readings": []}) == 0
    assert apply(database, pid, {"page_type": "other", "readings": []}) == 0
    assert database.q("SELECT * FROM hydrotest WHERE project_id=?", (pid,)) == []


def test_start_and_finish_become_datetimes(db):
    database, pid = db
    apply(database, pid, record())
    row = only_test(database, pid)
    assert row["started_at"] == "2025-08-18 07:00:00"
    assert row["completed_at"] == "2025-08-18 16:10:00"
    assert row["started_raw"] == "8/18/25 7:00am"


@pytest.mark.parametrize("written,expected", [
    ("8/18/25 7:00am", "2025-08-18 07:00:00"),
    ("8/18/25 12:30am", "2025-08-18 00:30:00"),
    ("8/18/25 12:30pm", "2025-08-18 12:30:00"),
    ("8/18/25", "2025-08-18"),
    ("", ""),
])
def test_datetime_parsing(written, expected):
    assert _parse_datetime(written) == expected


# -- HYD-01 pressure --------------------------------------------------------

def test_a_hold_at_pressure_is_not_a_finding(db):
    database, pid = db
    apply(database, pid, record())
    assert fire(database, pid, rules.pressure_below_minimum) == []


def test_the_pressurisation_ramp_is_not_a_pressure_drop(db):
    # The log opens at 109 psig against a 428 psig minimum. Reading that as a
    # failed hold would make the rule fire on every test ever filed.
    database, pid = db
    apply(database, pid, record())
    row = only_test(database, pid)
    readings = database.q(
        "SELECT * FROM hydrotest_reading WHERE hydrotest_id=? ORDER BY seq", (row["id"],))
    window = rules.hold_window(readings, 428)
    assert window is not None and window[0] == 3


def test_a_drop_during_the_hold_is_critical(db):
    database, pid = db
    readings = ramp_and_hold()
    readings[8]["pressure_psig"] = 402
    apply(database, pid, record(readings=readings))
    found = fire(database, pid, rules.pressure_below_minimum)
    assert len(found) == 1 and found[0]["severity"] == "critical"
    assert "402" in found[0]["message"] and "428" in found[0]["message"]


def test_an_overshoot_past_the_maximum_is_reported(db):
    database, pid = db
    readings = ramp_and_hold()
    readings[6]["pressure_psig"] = 471
    apply(database, pid, record(readings=readings))
    found = fire(database, pid, rules.pressure_above_maximum)
    assert len(found) == 1 and "471" in found[0]["message"]


def test_no_maximum_stated_means_no_overshoot_rule(db):
    database, pid = db
    apply(database, pid, record(required_max_pressure=None,
                                readings=[{"time": "9:00AM", "pressure_psig": 900,
                                           "ambient_temp": None}]))
    assert fire(database, pid, rules.pressure_above_maximum) == []


# -- HYD-03 duration --------------------------------------------------------

def test_a_full_hold_passes(db):
    database, pid = db
    apply(database, pid, record())
    assert fire(database, pid, rules.duration_short) == []


def test_a_short_hold_is_critical(db):
    database, pid = db
    apply(database, pid, record(completed_at="8/18/25 11:00am"))
    found = fire(database, pid, rules.duration_short)
    assert len(found) == 1 and found[0]["severity"] == "critical"
    assert "4.0" in found[0]["message"]


def test_rounding_on_the_form_is_not_a_short_hold(db):
    # 7:00am to 2:55pm is 7.92 hours against a required 8. Firing on five
    # minutes would make the rule noise.
    database, pid = db
    apply(database, pid, record(completed_at="8/18/25 2:55pm"))
    assert fire(database, pid, rules.duration_short) == []


def test_duration_falls_back_to_the_stated_hours(db):
    database, pid = db
    apply(database, pid, record(started_at=None, completed_at=None,
                                stated_duration_hours=2))
    found = fire(database, pid, rules.duration_short)
    assert len(found) == 1 and "the record states" in found[0]["detail"]


# -- HYD-04 result ----------------------------------------------------------

def test_an_unmarked_result_box_is_reported(db):
    # The real Kestrel 8 record: complete, signed, and it never says it passed.
    database, pid = db
    apply(database, pid, record(result=None))
    found = fire(database, pid, rules.result_not_recorded)
    assert len(found) == 1 and found[0]["severity"] == "major"
    assert "neither" in found[0]["message"]


def test_an_unacceptable_result_is_critical(db):
    database, pid = db
    apply(database, pid, record(result="UNACCEPTABLE"))
    found = fire(database, pid, rules.result_not_recorded)
    assert len(found) == 1 and found[0]["severity"] == "critical"


def test_a_requirements_page_alone_says_nothing_about_the_result(db):
    # Reading the requirements sheet but not the record must not be reported
    # as a missing result: no one has looked at the box yet.
    database, pid = db
    apply(database, pid, {"page_type": "test_requirements",
                          "required_min_pressure": 428, "readings": []}, page_no=2)
    assert fire(database, pid, rules.result_not_recorded) == []


# -- HYD-05 calibration -----------------------------------------------------

def cal(db, pid, serial, date, page_no=9):
    _apply_hydrotest(db, pid, TARGET, {
        "page_type": "calibration_certificate", "instrument_sn": serial,
        "calibration_date": date, "readings": [],
    }, page_no)


def test_no_certificates_read_means_no_calibration_findings(db):
    database, pid = db
    apply(database, pid, record())
    assert fire(database, pid, rules.instrument_calibration) == []


def test_in_date_certificates_pass(db):
    database, pid = db
    apply(database, pid, record())
    for i, sn in enumerate(("1715013650", "242E-5824A", "242E-5824C")):
        cal(database, pid, sn, "6/1/25", page_no=9 + i)
    assert fire(database, pid, rules.instrument_calibration) == []


def test_a_stale_certificate_is_reported(db):
    database, pid = db
    apply(database, pid, record())
    cal(database, pid, "1715013650", "1/2/24")
    cal(database, pid, "242E-5824A", "6/1/25", page_no=10)
    cal(database, pid, "242E-5824C", "6/1/25", page_no=11)
    found = fire(database, pid, rules.instrument_calibration)
    assert len(found) == 1 and "deadweight" in found[0]["message"]


def test_an_instrument_with_no_certificate_is_reported(db):
    database, pid = db
    apply(database, pid, record())
    cal(database, pid, "1715013650", "6/1/25")
    found = fire(database, pid, rules.instrument_calibration)
    assert {f["subject"] for f in found} == {"242E-5824A", "242E-5824C"}


def test_serials_match_across_punctuation():
    assert serial_key("242E-5824A") == serial_key("242e 5824 a")


# -- HYD-06 welds after the test --------------------------------------------

def add_weld(db, pid, weld_no, date, segment="16IN LP"):
    with db.tx() as c:
        c.execute(
            """INSERT INTO weld(project_id, segment, line, weld_no, date_welded,
                                source)
               VALUES(?, ?, '16 LP', ?, ?, 'weld_map_vision')""",
            (pid, segment, weld_no, date),
        )


def test_welds_before_the_test_are_fine(db):
    database, pid = db
    apply(database, pid, record())
    add_weld(database, pid, "AFB-16", "2025-08-08")
    assert fire(database, pid, rules.weld_after_test) == []


def test_a_weld_after_the_test_is_critical(db):
    database, pid = db
    apply(database, pid, record())
    add_weld(database, pid, "AFB-16", "2025-08-08")
    add_weld(database, pid, "AFB-22", "2025-08-25")
    found = fire(database, pid, rules.weld_after_test)
    assert len(found) == 1 and found[0]["severity"] == "critical"
    assert "AFB-22" in found[0]["message"] and "AFB-16" not in found[0]["message"]


def test_a_weld_on_another_segment_is_not_this_test_s_problem(db):
    database, pid = db
    apply(database, pid, record())
    add_weld(database, pid, "GFB-04", "2025-09-01", segment="4IN FG")
    assert fire(database, pid, rules.weld_after_test) == []


def test_a_weld_with_no_date_cannot_be_placed(db):
    database, pid = db
    apply(database, pid, record())
    add_weld(database, pid, "AFB-30", "")
    assert fire(database, pid, rules.weld_after_test) == []


# -- HYD-07 untested segments -----------------------------------------------

def test_a_welded_segment_with_no_test_is_reported(db):
    database, pid = db
    apply(database, pid, record())
    add_weld(database, pid, "GFB-04", "2025-07-01", segment="4IN FG")
    found = fire(database, pid, rules.segment_untested)
    assert len(found) == 1 and found[0]["segment"] == "4IN FG"


def test_a_partly_read_package_claims_nothing(db):
    # PLU files twelve packages of 14 to 37 pages, and the record can be
    # anywhere in one. Seeding a single page made six segments look untested.
    database, pid = db
    database.ocr_put("fp1:hydrotest:2000", 3, "test-model", record())
    _apply_hydrotest(database, pid, TARGET, record(), 3)
    add_weld(database, pid, "GFB-04", "2025-07-01", segment="4IN FG")
    assert fire(database, pid, rules.segment_untested) == []


def test_a_chart_page_still_counts_as_read(db):
    # Charts and calibration certificates are most of a package. Counting
    # tests rather than pages would make one permanently unread.
    database, pid = db
    apply(database, pid, record())          # page 4 is the record, rest charts
    add_weld(database, pid, "GFB-04", "2025-07-01", segment="4IN FG")
    assert len(fire(database, pid, rules.segment_untested)) == 1


def test_the_other_hydro_rules_do_not_wait(db):
    # Only HYD-07 reasons from absence; the rest read the record in front of
    # them, so a single package still reports its own result.
    database, pid = db
    database.ocr_put("fp1:hydrotest:2000", 3, "test-model", record(result=None))
    _apply_hydrotest(database, pid, TARGET, record(result=None), 3)
    assert len(fire(database, pid, rules.result_not_recorded)) == 1


def test_no_tests_at_all_means_no_untested_findings(db):
    # A project whose hydro packages have never been read must not report
    # every segment as untested.
    database, pid = db
    add_weld(database, pid, "GFB-04", "2025-07-01", segment="4IN FG")
    assert fire(database, pid, rules.segment_untested) == []


# -- the summary table ------------------------------------------------------

def test_the_summary_reports_the_hold_not_the_ramp(db):
    database, pid = db
    apply(database, pid, record())
    row = rules.test_summary(database, pid)[0]
    assert (row["required_min"], row["required_max"]) == (428, 444)
    assert (row["held_low"], row["held_high"]) == (429, 434)
    assert row["actual_hours"] == 9.2 and row["required_hours"] == 8
    assert row["readings"] == 11


def test_an_unmarked_result_is_not_a_blank_in_the_summary(db):
    # A test nobody has read and a test whose box is empty are different
    # facts, and must not render the same.
    database, pid = db
    apply(database, pid, record(result=None))
    assert rules.test_summary(database, pid)[0]["result"] == "(unmarked)"

    apply(database, pid, {"page_type": "test_requirements",
                          "required_min_pressure": 428, "readings": []}, page_no=2)
    with database.tx() as c:
        c.execute("UPDATE hydrotest SET page_no=NULL, result='' WHERE project_id=?", (pid,))
    assert rules.test_summary(database, pid)[0]["result"] == ""


# -- HYD-08 readings stopping early -----------------------------------------

def test_readings_running_to_the_end_are_fine(db):
    database, pid = db
    apply(database, pid, record(completed_at="8/18/25 3:10pm"))
    assert fire(database, pid, rules.readings_stop_early) == []


def test_readings_stopping_an_hour_early_is_a_note(db):
    # The real Kestrel 8 log ends at 3:10pm against a 4:10pm completion.
    database, pid = db
    apply(database, pid, record())
    found = fire(database, pid, rules.readings_stop_early)
    assert len(found) == 1 and found[0]["severity"] == "minor"
    assert "1.2 hours" in found[0]["message"]
