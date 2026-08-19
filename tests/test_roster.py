"""The project welder log: reading it, and what it can say that certificates cannot.

The roster's value is that it dates things the certificates do not — when a
welder arrived, when they left, when the contractor says their ticket falls
due. Most of these tests are about not over-claiming on that: a welder appears
on several segment logs with different windows, the same log is filed into
many folders, and a name is typed sixteen times with sixteen chances to be
spelled differently.

The cases below are the real corpus shapes. Bluewater's `Welder Log.xlsx` exists
as eleven files with eight distinct fingerprints; `MICHAEL MUNOZ` and
`MICHEAL MUNOZ` are one man; and `ABF` is recorded against two different
welders on two different logs at the same time.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402
from weldaudit.extract.roster import _iso, _locate  # noqa: E402
from weldaudit.rules import roster as rules  # noqa: E402
from weldaudit.taxonomy import kind_for  # noqa: E402


# -- classification ---------------------------------------------------------

def test_a_welder_log_is_not_a_weld_log():
    # One letter apart, entirely different documents: the roster of people
    # against the register of joints.
    assert kind_for(r"x/10 Welding/Welder Log 20 LP.xlsx") == "welder_roster"
    assert kind_for(r"x/10 Welding/Kestrel 8 DTD 4in FG SEG. B WELDER LOG.xlsx") \
        == "welder_roster"
    assert kind_for(r"x/10 Welding/Master_Weld_Log_Summary.csv") == "weld_log_csv"
    assert kind_for(r"x/10 Welding/Weld Log Summary.xlsx") == "weld_log_csv"


def test_certificates_are_still_certificates():
    assert kind_for(r"x/10 Welding/Welder Certs/ABF.pdf") == "welder_cert"


# -- reading the sheet ------------------------------------------------------

def test_the_header_is_found_by_label():
    rows = [(None,) * 3, ("AFE#:", None), (None,),
            ("Welder Name", None, None, None, None, None, "Welder Stencil",
             None, "Cert for  CS or SS?", None, "Cert Test Date", None, None,
             "Requal Date", None, None, "Next Requal / Annual Required", None,
             None, "Date Arrived on Job", None, None, "Date Left Job")]
    located = _locate(rows)
    assert located is not None
    index, columns = located
    assert index == 3
    assert columns["stencil"] == 6 and columns["next_requal"] == 16
    assert columns["arrived"] == 19 and columns["left_job"] == 22


def test_a_sheet_with_no_welder_name_column_is_not_a_roster():
    assert _locate([("Flange #", "Size"), ("1", "12")]) is None


@pytest.mark.parametrize("cell,expected", [
    ("2025-04-24", "2025-04-24"),
    ("19/9/25", "2025-09-19"),          # one cell typed as text, day first
    ("9/19/25", "2025-09-19"),          # and the other way round
    ("5-13-25", "2025-05-13"),
    ("", ""),
    ("REQUAL", ""),
])
def test_date_cells(cell, expected):
    assert _iso(cell) == expected


# -- the rules --------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "r.db")
    pid = database.upsert_project("R", str(tmp_path))
    with database.tx() as c:
        c.execute(
            """INSERT INTO document(id, project_id, path, filename, ext, kind,
                                    segment, fingerprint)
               VALUES(1, ?, 'p', 'Welder Log 20 LP.xlsx', '.xlsx',
                      'welder_roster', '20 LP', 'fp1')""",
            (pid,),
        )
    return database, pid


def enrol(db, pid, stencil, name="ALTON MORGAN", *, segment="20 LP",
          cert="2024-11-14", requal="2025-05-13", nxt="2025-11-09",
          arrived="2025-06-25", left="2025-08-01", material="CS", reason=""):
    with db.tx() as c:
        c.execute(
            """INSERT INTO welder_roster
               (project_id, document_id, fingerprint, segment, row_no, name,
                stencil, material, cert_date, requal_date, next_requal,
                arrived, left_job, reason, source)
               VALUES(?, 1, 'fp1', ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      'welder_roster_xlsx')""",
            (pid, segment, name, stencil, material, cert, requal, nxt,
             arrived, left, reason),
        )


def welded(db, pid, stencil, *dates, segment="20 LP"):
    with db.tx() as c:
        for i, when in enumerate(dates):
            c.execute(
                """INSERT INTO welder_pass
                   (project_id, segment, line, weld_no, stencil, date_welded)
                   VALUES(?, ?, '20 LP', ?, ?, ?)""",
                (pid, segment, f"W{i}", stencil, when),
            )


def certify(db, pid, stencil):
    with db.tx() as c:
        c.execute(
            "INSERT INTO welder_cert(project_id, stencil, evidence) VALUES(?,?,'filename')",
            (pid, stencil),
        )


def fire(db, pid, rule):
    return rule(db, pid, "run")


# -- merging the logs -------------------------------------------------------

def test_a_welder_on_several_logs_gets_the_widest_window(db):
    # A welder who moves between spreads appears on several logs. Taking one
    # log's dates alone would report the other spread's welds as out of window.
    database, pid = db
    enrol(database, pid, "AMG", arrived="2025-08-01", left="2025-09-01",
          segment="20 LP")
    enrol(database, pid, "AMG", arrived="2025-06-25", left="2025-12-03",
          segment="4 FLEXSTEEL")
    entry = rules.roster(database, pid)["AMG"]
    assert entry["arrived"] == "2025-06-25" and entry["left_job"] == "2025-12-03"


def test_a_blank_leaving_date_means_still_on_the_job(db):
    database, pid = db
    enrol(database, pid, "AMG", left="2025-09-01", segment="20 LP")
    enrol(database, pid, "AMG", left="", segment="4 FLEXSTEEL")
    assert rules.roster(database, pid)["AMG"]["still_on_job"] is True


# -- ROS-01 dates on the job ------------------------------------------------

def test_welds_inside_the_window_pass(db):
    database, pid = db
    enrol(database, pid, "AMG", arrived="2025-06-25", left="2025-08-01")
    welded(database, pid, "AMG", "2025-07-01", "2025-07-20")
    assert fire(database, pid, rules.welded_off_roster_dates) == []


def test_a_weld_after_leaving_is_reported(db):
    # ABJ (Andrew Morgan) on the real Bluewater job.
    database, pid = db
    enrol(database, pid, "ABJ", name="ANDREW MORGAN", left="2025-08-28")
    welded(database, pid, "ABJ", "2025-07-01", "2025-09-15")
    found = fire(database, pid, rules.welded_off_roster_dates)
    assert len(found) == 1 and found[0]["severity"] == "major"
    assert "ANDREW MORGAN" in found[0]["message"]
    assert "18 days after they left" in found[0]["message"]


def test_a_weld_before_arriving_is_reported(db):
    database, pid = db
    enrol(database, pid, "ADW", name="JOHN LOPEZ", arrived="2025-08-18", left="")
    welded(database, pid, "ADW", "2025-07-14")
    found = fire(database, pid, rules.welded_off_roster_dates)
    assert len(found) == 1 and "35 days before they arrived" in found[0]["message"]


def test_both_ends_wrong_is_one_finding(db):
    database, pid = db
    enrol(database, pid, "ADW", arrived="2025-08-18", left="2025-08-28")
    welded(database, pid, "ADW", "2025-07-14", "2025-09-03")
    found = fire(database, pid, rules.welded_off_roster_dates)
    assert len(found) == 1
    assert "before they arrived" in found[0]["message"]
    assert "after they left" in found[0]["message"]


def test_a_few_days_out_is_only_minor(db):
    # A mobilisation date written down loosely is not the same as a month.
    database, pid = db
    enrol(database, pid, "ANR", name="MARCOS YANEZ", arrived="2025-07-24", left="")
    welded(database, pid, "ANR", "2025-07-18")
    found = fire(database, pid, rules.welded_off_roster_dates)
    assert len(found) == 1 and found[0]["severity"] == "minor"


def test_a_welder_still_on_the_job_cannot_weld_after_leaving(db):
    database, pid = db
    enrol(database, pid, "AMG", left="", arrived="2025-06-25")
    welded(database, pid, "AMG", "2026-03-01")
    assert fire(database, pid, rules.welded_off_roster_dates) == []


def test_a_stencil_not_on_the_roster_is_not_this_rule_s_business(db):
    database, pid = db
    enrol(database, pid, "AMG")
    welded(database, pid, "ZZZ", "2025-07-01")
    assert fire(database, pid, rules.welded_off_roster_dates) == []


# -- ROS-02 requalification -------------------------------------------------

def test_welding_before_the_requal_due_date_passes(db):
    database, pid = db
    enrol(database, pid, "AMG", nxt="2025-11-09", left="")
    welded(database, pid, "AMG", "2025-10-01")
    assert fire(database, pid, rules.welded_after_requal_due) == []


def test_welding_after_the_requal_due_date_is_reported(db):
    # The contractor wrote the expiry down; WLD-03 can only infer one from a
    # 183-day gap between welds.
    database, pid = db
    enrol(database, pid, "AMG", nxt="2025-11-09", left="")
    welded(database, pid, "AMG", "2025-10-01", "2025-12-20")
    found = fire(database, pid, rules.welded_after_requal_due)
    assert len(found) == 1 and "2025-11-09" in found[0]["message"]
    assert "1 pass" in found[0]["message"]


def test_no_stated_due_date_means_no_comparison(db):
    database, pid = db
    enrol(database, pid, "AMG", nxt="", left="")
    welded(database, pid, "AMG", "2027-01-01")
    assert fire(database, pid, rules.welded_after_requal_due) == []


# -- ROS-03 impossible dates ------------------------------------------------

def test_consistent_dates_pass(db):
    database, pid = db
    enrol(database, pid, "AMG")
    assert fire(database, pid, rules.roster_dates_impossible) == []


def test_qualifying_after_leaving_is_reported(db):
    # CLINT WILSON on the real Bluewater log: cert 2025-11-14, left 2025-09-11.
    database, pid = db
    enrol(database, pid, "AEA", name="CLINT WILSON", cert="2025-11-14",
          requal="2026-05-13", nxt="2026-11-09", left="2025-09-11")
    found = fire(database, pid, rules.roster_dates_impossible)
    assert len(found) == 1 and "after leaving the job" in found[0]["message"]


def test_requalifying_before_qualifying_is_reported(db):
    database, pid = db
    enrol(database, pid, "AMG", cert="2025-06-01", requal="2025-01-01",
          nxt="2025-12-01", left="")
    found = fire(database, pid, rules.roster_dates_impossible)
    assert len(found) == 1 and "before qualifying" in found[0]["message"]


# -- ROS-04 missing from the log --------------------------------------------

def test_a_certified_welder_absent_from_the_log_is_reported(db):
    database, pid = db
    enrol(database, pid, "AMG")
    certify(database, pid, "AQR")
    welded(database, pid, "AQR", "2025-07-01")
    found = fire(database, pid, rules.missing_from_roster)
    assert len(found) == 1 and found[0]["subject"] == "AQR"


def test_an_uncertified_stencil_is_left_to_the_welder_rules(db):
    # A stencil with neither certificate nor roster entry is already WLD-01's
    # critical; raising it twice under two headings helps nobody.
    database, pid = db
    enrol(database, pid, "AMG")
    certify(database, pid, "AMG")
    welded(database, pid, "ZZZ", "2025-07-01")
    assert fire(database, pid, rules.missing_from_roster) == []


def test_a_near_miss_names_the_roster_entry(db):
    database, pid = db
    enrol(database, pid, "ABF")
    certify(database, pid, "AFB")
    welded(database, pid, "AFB", "2025-07-01")
    found = fire(database, pid, rules.missing_from_roster)
    assert len(found) == 1 and "ABF" in found[0]["message"]


# -- ROS-05 shared stencils -------------------------------------------------

def test_one_welder_spelled_two_ways_is_one_welder(db):
    # MICHAEL / MICHEAL MUNOZ, typed into sixteen spreadsheets.
    database, pid = db
    enrol(database, pid, "AQR", name="MICHAEL MUNOZ", segment="20 LP")
    enrol(database, pid, "AQR", name="MICHEAL MUNOZ", segment="6 FG")
    assert fire(database, pid, rules.stencil_shared) == []


def test_two_welders_holding_one_stencil_at_once_is_reported(db):
    database, pid = db
    enrol(database, pid, "ABF", name="JORGE MUNOZ", arrived="2025-06-25",
          left="2025-10-25", segment="6 FG PAD B")
    enrol(database, pid, "ABF", name="TAYLOR PHILLIPS", arrived="2025-06-25",
          left="2025-08-26", segment="20 LP")
    found = fire(database, pid, rules.stencil_shared)
    majors = [f for f in found if f["severity"] == "major"]
    assert len(majors) == 1 and majors[0]["subject"] == "ABF"
    assert "JORGE MUNOZ" in majors[0]["message"]
    assert "the 20 LP log" in majors[0]["message"]


def test_a_stencil_handed_on_is_context_not_a_defect(db):
    database, pid = db
    enrol(database, pid, "ABF", name="FIRST WELDER", arrived="2025-01-01",
          left="2025-03-01")
    enrol(database, pid, "ABF", name="SECOND WELDER", arrived="2025-04-01",
          left="2025-06-01")
    found = fire(database, pid, rules.stencil_shared)
    assert len(found) == 1 and found[0]["severity"] == "info"
    assert "passed from one welder to another" in found[0]["message"]
    assert "FIRST WELDER then SECOND WELDER" in found[0]["message"]


def test_one_welder_one_stencil_reports_nothing(db):
    database, pid = db
    enrol(database, pid, "AMG")
    enrol(database, pid, "ABF", name="SOMEONE ELSE")
    assert fire(database, pid, rules.stencil_shared) == []


# -- ROS-06 no roster -------------------------------------------------------

def test_no_roster_on_a_job_with_welders_is_reported(db):
    database, pid = db
    welded(database, pid, "AAA", "2025-07-01")
    welded(database, pid, "BBB", "2025-07-02")
    found = fire(database, pid, rules.no_roster)
    assert len(found) == 1 and "2 stencils" in found[0]["message"]


def test_a_job_with_a_roster_reports_nothing(db):
    database, pid = db
    enrol(database, pid, "AMG")
    welded(database, pid, "AMG", "2025-07-01")
    assert fire(database, pid, rules.no_roster) == []


def test_a_job_with_no_welding_needs_no_roster(db):
    database, pid = db
    assert fire(database, pid, rules.no_roster) == []


# -- the summary ------------------------------------------------------------

def test_the_summary_pairs_the_log_with_what_was_welded(db):
    database, pid = db
    enrol(database, pid, "AMG", name="ALTON MORGAN")
    certify(database, pid, "AMG")
    welded(database, pid, "AMG", "2025-07-01", "2025-07-20")
    row = rules.roster_summary(database, pid)[0]
    assert row["name"] == "ALTON MORGAN" and row["passes"] == 2
    assert (row["first_weld"], row["last_weld"]) == ("2025-07-01", "2025-07-20")
    assert row["certificate"] is True


def test_a_welder_still_on_site_shows_no_leaving_date(db):
    database, pid = db
    enrol(database, pid, "AMG", left="")
    assert rules.roster_summary(database, pid)[0]["left_job"] == ""
