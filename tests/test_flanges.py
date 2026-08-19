"""Flange logs: reading two forms of the same template, and judging them.

The rules here are unusual in the audit because there is nothing to judge
them against. No document on any of these jobs states a target torque, so
every rule is an internal-consistency rule, and the tests below exist mostly
to pin the line between "the log contradicts itself" and "the log varies in a
way that means nothing" — the second being where a torque rule turns into
noise.

The reading half is pinned too, because the same template arrives as a
workbook on one job and as a printed PDF on the other, and the printed one has
to be rebuilt from word coordinates: its text layer comes out grouped by
column, not by row.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402
from weldaudit.extract.flanges import balloon_count, split_signoff  # noqa: E402
from weldaudit.instruments import LABELS, parse, parse_bare_serial  # noqa: E402
from weldaudit.rules import flanges as rules  # noqa: E402
from weldaudit.taxonomy import kind_for  # noqa: E402


# -- torque wrench certificates ---------------------------------------------

@pytest.mark.parametrize("filename,serial,calibrated", [
    ("Torque Wrench 250 SN 0323120003 5.13.25.PDF", "0323120003", "2025-05-13"),
    ("Torque Wrench 600 SN 0222600459 5.13.25.PDF", "0222600459", "2025-05-13"),
    (".5IN TORQUE WRENCH - 1223113766.pdf", "1223113766", ""),
    ("0824601189 Torque Wrench.pdf", "0824601189", ""),
])
def test_named_wrench_certificates(filename, serial, calibrated):
    identity = parse(filename)
    assert identity.kind == "torque_wrench"
    assert (identity.serial, identity.calibrated) == (serial, calibrated)


@pytest.mark.parametrize("filename,serial", [
    ("0322600192.pdf", "0322600192"),
    ("0918602082 (1).pdf", "0918602082"),      # Windows' duplicate marker
    ("1122600880.pdf", "1122600880"),
])
def test_wrench_certificates_named_only_for_their_serial(filename, serial):
    # Bluewater files most of its wrench certificates this way. Only safe to read
    # because the document is already known to be in the flange section.
    identity = parse_bare_serial(filename)
    assert identity.kind == "torque_wrench" and identity.serial == serial


@pytest.mark.parametrize("filename", [
    "6 FG TO PAD C TORQUE LOG.xlsx", "4 INCH CS - PAD C.pdf",
    "MRW BLUEWATER - LONG HORN  INTERCONNECT.xlsx", "Flange Map.pdf",
])
def test_logs_and_maps_are_not_wrench_certificates(filename):
    assert parse_bare_serial(filename).serial == ""


def test_the_torque_wrench_has_a_label():
    assert LABELS["torque_wrench"] == "torque wrench"


def test_named_wrench_certificates_classify_as_certificates():
    assert kind_for(r"x/18 Flange Map/Torque Wrench 250 SN 0323120003 5.13.25.PDF") \
        == "instrument_cal"
    assert kind_for(r"x/18 Flange Map/6 FG TO PAD C TORQUE LOG.xlsx") == "flange_map"


# -- reading the forms ------------------------------------------------------

@pytest.mark.parametrize("cell,initials,when", [
    ("WL 9/26/25", "WL", "2025-09-26"),
    ("JL", "JL", ""),
    ("2025-09-09 00:00:00", "", "2025-09-09"),
    ("", "", ""),
    ("  ", "", ""),
])
def test_the_signoff_column_splits(cell, initials, when):
    assert split_signoff(cell) == (initials, when)


@pytest.mark.parametrize("numbers,expected", [
    ([1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 10, 12, 13, 14], 14),
    # 45 and 90 are elbow angles written on the drawing, not balloons; taking
    # the maximum would claim a 23-flange map has ninety.
    ([45, 45, 90] + list(range(1, 24)), 23),
    ([2, 3, 4], 0),
    ([], 0),
])
def test_balloon_counting_takes_the_run_from_one(numbers, expected):
    assert balloon_count(numbers) == expected


# -- the rules --------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "f.db")
    pid = database.upsert_project("F", str(tmp_path))
    with database.tx() as c:
        c.execute(
            """INSERT INTO document(id, project_id, path, filename, ext, kind,
                                    segment, fingerprint)
               VALUES(1, ?, 'p', '16 IN PW FLANGE LOG.xlsx', '.xlsx',
                      'flange_map', '16 PW', 'fp1')""",
            (pid,),
        )
    return database, pid


DEFAULTS = dict(sheet="Flange Log", nps=16.0, pressure_class=300.0, gasket="CGI",
                bolts=20.0, lubricant="KK", round1=150.0, round2=300.0,
                round3=500.0, round4=500.0, pattern="CLOCKWISE",
                wrench="0322600192", cert_checked="YES", inspector="GP",
                bolted_on="2025-07-01", drawing_no="16-B60-PW-0401-01",
                notes="16-B60-PW-0401-01", line_size="16", service="PW",
                job_start="2025-06-11")


def joint(db, pid, flange_no, **over):
    values = {**DEFAULTS, **over, "flange_no": str(flange_no)}
    values["wrench_key"] = (values["wrench"] or "").upper()
    row = db.one("SELECT COUNT(*) n FROM flange WHERE project_id=?", (pid,))
    columns = ", ".join(values)
    marks = ", ".join("?" * len(values))
    with db.tx() as c:
        c.execute(
            f"""INSERT INTO flange(project_id, document_id, segment, row_no,
                                   source, {columns})
                VALUES(?, 1, '16 PW', ?, 'flange_log_xlsx', {marks})""",
            (pid, row["n"] + 1, *values.values()),
        )


def cert(db, pid, serial):
    with db.tx() as c:
        c.execute(
            """INSERT INTO instrument_cal
               (project_id, kind, serial, serial_key, evidence, source)
               VALUES(?, 'torque_wrench', ?, ?, 'filename', 'flange_wrench_filename')""",
            (pid, serial, serial.upper()),
        )


def add_map(db, pid, segment, balloons=13):
    with db.tx() as c:
        c.execute(
            """INSERT INTO flange_map(project_id, document_id, segment, drawings,
                                      balloons, source)
               VALUES(?, 1, ?, '', ?, 'flange_map_pdf')""",
            (pid, segment, balloons),
        )


def fire(db, pid, rule):
    return rule(db, pid, "run")


# -- FLG-01 torque disagreement ---------------------------------------------

def test_one_torque_for_a_configuration_is_not_a_finding(db):
    database, pid = db
    for i in range(5):
        joint(database, pid, i + 1)
    assert fire(database, pid, rules.torque_disagreement) == []


def test_a_lone_outlier_is_named_against_the_common_figure(db):
    # The real 16 PW shape: one 2" joint at 320 among 136 at 90.
    database, pid = db
    for i in range(8):
        joint(database, pid, i + 1, nps=2.0, bolts=8.0, round3=90.0)
    joint(database, pid, 9, nps=2.0, bolts=8.0, round3=320.0)
    found = fire(database, pid, rules.torque_disagreement)
    assert len(found) == 1 and found[0]["severity"] == "major"
    assert "90 ft-lb on 8 joints" in found[0]["message"]
    assert "320 is the exception" in found[0]["message"]


def test_a_scattered_configuration_says_there_is_no_target(db):
    database, pid = db
    for i, torque in enumerate((460.0, 440.0, 380.0, 700.0, 290.0, 310.0)):
        joint(database, pid, i + 1, nps=6.0, pressure_class=600.0, bolts=12.0,
              round3=torque)
    found = fire(database, pid, rules.torque_disagreement)
    assert len(found) == 1 and "No single target was used" in found[0]["message"]
    assert "6 different final torques" in found[0]["message"]


def test_rounding_between_crews_is_not_a_disagreement(db):
    # 165 against 168 ft-lb is how differently two people round, not two
    # different targets. Firing on it would make the rule noise.
    database, pid = db
    for i in range(4):
        joint(database, pid, i + 1, nps=4.0, bolts=8.0, round3=168.0)
    joint(database, pid, 5, nps=4.0, bolts=8.0, round3=165.0)
    assert fire(database, pid, rules.torque_disagreement) == []


def test_different_configurations_are_compared_separately(db):
    database, pid = db
    joint(database, pid, 1, nps=2.0, bolts=8.0, round3=90.0)
    joint(database, pid, 2, nps=16.0, bolts=20.0, round3=500.0)
    assert fire(database, pid, rules.torque_disagreement) == []


def test_a_different_bolt_count_is_a_different_configuration(db):
    database, pid = db
    joint(database, pid, 1, bolts=20.0, round3=500.0)
    joint(database, pid, 2, bolts=16.0, round3=350.0)
    assert fire(database, pid, rules.torque_disagreement) == []


def test_a_joint_with_no_final_torque_is_not_compared(db):
    database, pid = db
    joint(database, pid, 1, round3=500.0)
    joint(database, pid, 2, round3=None)
    assert fire(database, pid, rules.torque_disagreement) == []


# -- FLG-02 rounds ----------------------------------------------------------

def test_rising_rounds_pass(db):
    database, pid = db
    joint(database, pid, 1)
    assert fire(database, pid, rules.rounds_inconsistent) == []


def test_rounds_that_do_not_rise_are_reported(db):
    database, pid = db
    joint(database, pid, 17, round1=30.0, round2=100.0, round3=90.0, round4=90.0)
    found = fire(database, pid, rules.rounds_inconsistent)
    assert len(found) == 1 and "30, 100, 90" in found[0]["message"]


def test_a_final_pass_below_the_target_is_reported(db):
    database, pid = db
    joint(database, pid, 2, round1=147.0, round2=294.0, round3=490.0, round4=470.0)
    found = fire(database, pid, rules.rounds_inconsistent)
    assert len(found) == 1 and "470 ft-lb against a 100% round of 490" in found[0]["message"]


def test_a_final_pass_above_the_target_is_not_a_defect(db):
    # Coming back round at a little over the target is a check pass. Requiring
    # equality reported 168 against 165 as a joint pulled up out of sequence.
    database, pid = db
    joint(database, pid, 8, round1=50.0, round2=100.0, round3=165.0, round4=168.0)
    assert fire(database, pid, rules.rounds_inconsistent) == []


def test_the_printed_form_has_no_fourth_round_to_check(db):
    # PLU's logs are the same template printed, and it has no "around the
    # world" column at all.
    database, pid = db
    joint(database, pid, 1, round4=None)
    assert fire(database, pid, rules.rounds_inconsistent) == []


def test_the_thirty_sixty_labels_are_not_enforced(db):
    # The form labels its columns 30% / 60% / 100%, but crews work in thirds
    # as often as not and both are accepted practice.
    database, pid = db
    joint(database, pid, 1, round1=165.0, round2=330.0, round3=500.0, round4=500.0)
    assert fire(database, pid, rules.rounds_inconsistent) == []


# -- FLG-03 wrench calibration ----------------------------------------------

def test_no_certificates_read_means_no_wrench_findings(db):
    database, pid = db
    joint(database, pid, 1, wrench="9999999")
    assert fire(database, pid, rules.wrench_uncalibrated) == []


def test_a_certified_wrench_passes(db):
    database, pid = db
    joint(database, pid, 1)
    cert(database, pid, "0322600192")
    assert fire(database, pid, rules.wrench_uncalibrated) == []


def test_an_uncertified_wrench_is_reported_once_for_all_its_joints(db):
    database, pid = db
    for i in range(6):
        joint(database, pid, i + 1, wrench="1024601314")
    cert(database, pid, "0322600192")
    found = fire(database, pid, rules.wrench_uncalibrated)
    assert len(found) == 1 and found[0]["severity"] == "major"
    assert "6 joints" in found[0]["message"]


def test_a_one_character_slip_names_the_real_wrench(db):
    # The logs write 0323600192 on seven joints and 0322600192 on forty-five;
    # only one wrench exists.
    database, pid = db
    joint(database, pid, 1, wrench="0323600192")
    cert(database, pid, "0322600192")
    found = fire(database, pid, rules.wrench_uncalibrated)
    assert len(found) == 1 and found[0]["severity"] == "minor"
    assert "0322600192 is certified" in found[0]["message"]


# -- FLG-04..06 blank columns -----------------------------------------------

def test_a_fully_filled_log_reports_nothing(db):
    database, pid = db
    for i in range(3):
        joint(database, pid, i + 1)
    for rule in (rules.no_wrench, rules.calibration_not_verified, rules.no_signoff):
        assert fire(database, pid, rule) == []


def test_missing_wrenches_are_one_finding_per_log(db):
    database, pid = db
    joint(database, pid, 1)
    joint(database, pid, 2, wrench="")
    joint(database, pid, 3, wrench="")
    found = fire(database, pid, rules.no_wrench)
    assert len(found) == 1 and "2 of 3 joints record no torque wrench" in found[0]["message"]


def test_the_verification_box_left_empty_is_reported(db):
    database, pid = db
    for i in range(4):
        joint(database, pid, i + 1, cert_checked="")
    found = fire(database, pid, rules.calibration_not_verified)
    assert len(found) == 1 and "4 of 4 joints leave" in found[0]["message"]


def test_a_single_blank_reads_as_singular(db):
    database, pid = db
    joint(database, pid, 1)
    joint(database, pid, 2, cert_checked="")
    found = fire(database, pid, rules.calibration_not_verified)
    assert "1 of 2 joints leaves" in found[0]["message"]
    assert "on this row nobody did" in found[0]["message"]


def test_missing_initials_are_reported(db):
    database, pid = db
    for i in range(21):
        joint(database, pid, i + 1, inspector="")
    found = fire(database, pid, rules.no_signoff)
    assert len(found) == 1 and "21 of 21 joints carry no inspector" in found[0]["message"]


def test_a_row_with_no_torque_is_not_counted_as_missing(db):
    # Blank trailing rows on a part-used log are not unsigned joints.
    database, pid = db
    joint(database, pid, 1)
    joint(database, pid, 2, round3=None, inspector="", cert_checked="", wrench="")
    for rule in (rules.no_wrench, rules.calibration_not_verified, rules.no_signoff):
        assert fire(database, pid, rule) == []


# -- FLG-07 dates -----------------------------------------------------------

def test_a_bolt_up_before_the_job_started_is_reported(db):
    database, pid = db
    joint(database, pid, 1)
    joint(database, pid, 2, bolted_on="2006-01-12")
    found = fire(database, pid, rules.impossible_date)
    assert len(found) == 1 and "2006-01-12" in found[0]["message"]
    assert "2025-06-11" in found[0]["message"]


def test_dates_after_the_job_started_are_fine(db):
    database, pid = db
    joint(database, pid, 1, bolted_on="2025-12-18")
    assert fire(database, pid, rules.impossible_date) == []


def test_a_few_days_before_mobilisation_is_within_grace(db):
    database, pid = db
    joint(database, pid, 1, bolted_on="2025-06-01")
    assert fire(database, pid, rules.impossible_date) == []


def test_no_job_start_means_no_comparison(db):
    database, pid = db
    joint(database, pid, 1, bolted_on="2006-01-12", job_start="")
    assert fire(database, pid, rules.impossible_date) == []


# -- FLG-08 maps without logs -----------------------------------------------

def test_a_segment_with_maps_and_no_log_is_reported(db):
    database, pid = db
    joint(database, pid, 1)
    add_map(database, pid, "6 IN LP North", balloons=13)
    found = fire(database, pid, rules.map_without_log)
    assert len(found) == 1 and found[0]["segment"] == "6 IN LP North"
    assert "13 flanges" in found[0]["message"]


def test_a_segment_with_both_is_not_reported(db):
    database, pid = db
    joint(database, pid, 1)
    add_map(database, pid, "16 PW", balloons=13)
    assert fire(database, pid, rules.map_without_log) == []


def test_no_logs_at_all_means_no_findings(db):
    database, pid = db
    add_map(database, pid, "6 IN LP North")
    assert fire(database, pid, rules.map_without_log) == []


# -- the summary ------------------------------------------------------------

def test_the_summary_counts_what_each_log_left_blank(db):
    database, pid = db
    joint(database, pid, 1)
    joint(database, pid, 2, inspector="", cert_checked="")
    joint(database, pid, 3, wrench="", nps=2.0, bolts=8.0, round3=90.0)
    row = rules.flange_summary(database, pid)[0]
    assert row["joints"] == 3 and row["torqued"] == 3
    assert row["no_signoff"] == 1 and row["not_verified"] == 1
    assert row["no_wrench"] == 1
    assert row["sizes"] == '2", 16"'
