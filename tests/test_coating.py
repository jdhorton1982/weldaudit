"""Coating: the instrument filename grammar, and the reconciliation rules.

Two things here are worth more attention than the rest.

The instrument grammar has to read two conventions off the same folder — PLU
writes `Positector 6000 SN 1073795 4.24.25.pdf`, Bluewater writes `HOLIDAY
DETECTOR - 12594.pdf` — and every case below is a real filename from the
corpus.

The rules have to tell "the value is wrong" apart from "there is no value",
because on these reports the second is far more common than the first. The two
end-to-end cases at the bottom are the two real forms, transcribed by eye, and
they pin what an auditor should be told about each.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database, declared_columns  # noqa: E402
from weldaudit.extract.vision_pass import Target, _apply_coating  # noqa: E402
from weldaudit.instruments import (  # noqa: E402
    nearest_serials, parse, serial_key,
)
from weldaudit.rules import coating as rules  # noqa: E402
from weldaudit.taxonomy import kind_for  # noqa: E402


# -- instrument certificate filenames ---------------------------------------

@pytest.mark.parametrize("filename,kind,serial,calibrated", [
    # Kestrel 8: instrument, SN, serial, calibration date.
    ("Positector 6000 SN 1073795 4.24.25.pdf", "dft_gauge", "1073795", "2025-04-24"),
    ("Positector SPG SN 1075827 4.24.25.pdf", "profile_gauge", "1075827", "2025-04-24"),
    ("Spy Holiday Detector SN 12737 4.22.25.pdf", "holiday_detector", "12737", "2025-04-22"),
    ("Pocket Jeep Meter SN 9241 4.22.25.pdf", "holiday_detector", "9241", "2025-04-22"),
    ("Testex Profile Gauge SN BUWN38 4.22.25.pdf", "profile_gauge", "BUWN38", "2025-04-22"),
    # Bluewater: serial first or after a dash, and never a date.
    ("1090338 Coating Thickness Gauge.pdf", "dft_gauge", "1090338", ""),
    ("COATING THICKNESS INSTRUMENT - 1122124.pdf", "dft_gauge", "1122124", ""),
    ("HOLIDAY DETECTOR - 12594.pdf", "holiday_detector", "12594", ""),
    ("PULSE JEEP METER 20KV - PJM-7292.pdf", "holiday_detector", "PJM-7292", ""),
    ("DIAL THICKNESS GAGE - BTYG12.pdf", "profile_gauge", "BTYG12", ""),
    ("DKFP76 TESTEX MICROMETER.pdf", "profile_gauge", "DKFP76", ""),
    ("382161 DPM.pdf", "dpm", "382161", ""),
    ("POSI TECTOR GAUGE SN.894206.pdf", "dft_gauge", "894206", ""),
])
def test_certificate_filenames(filename, kind, serial, calibrated):
    identity = parse(filename)
    assert (identity.kind, identity.serial, identity.calibrated) == (
        kind, serial, calibrated)


def test_the_instrument_name_is_not_mistaken_for_the_serial():
    # `Environmental Gauge  1061323` puts the instrument's own name exactly
    # where a serial-first filename puts the serial. Every real serial has a
    # digit, which is what separates them.
    assert parse("Environmental Gauge  1061323.pdf").serial == "1061323"
    assert parse("Environmental Gauge 435060.pdf").serial == "435060"


def test_a_model_number_is_not_a_serial():
    # "Positector 6000" and "PULSE JEEP METER 20KV" both dangle a plausible
    # token where a serial would sit.
    assert parse("Positector 6000.pdf").serial == ""
    assert parse("PULSE JEEP METER 20KV.pdf").serial == ""


def test_a_slip_in_the_date_still_parses():
    # `5.19.254` is on disk; the year is 25.
    assert parse("Positector DPM SN 1141501 5.19.254.pdf").calibrated == "2025-05-19"


@pytest.mark.parametrize("filename", [
    "4 FG 7-11-25.pdf", "8-21-25 Coating Reports.pdf", "PDS Macropoxy 646.pdf",
    "SDS Denso Protal 7200 Part A.pdf", "NO DATA.pdf",
    "GPPB-0140 Protective Coatings and Insulation Rev 3.pdf",
])
def test_other_coating_documents_are_not_certificates(filename):
    assert parse(filename).serial == ""


def test_serials_match_across_punctuation_and_case():
    assert serial_key("PJM-7292") == serial_key("pjm 7292")


def test_a_one_character_slip_is_found():
    # BTYG12 is on file; a report writes BTYGL2.
    assert nearest_serials("BTYGL2", {"BTYG12", "DKFP76"}) == ["BTYG12"]
    assert nearest_serials("7545", {"12594", "7961", "6902"}) == []


# -- classification ---------------------------------------------------------

def test_the_coating_folder_is_separated_into_its_document_kinds():
    at = "x/19 Coating/"
    assert kind_for(at + "HOLIDAY DETECTOR - 12594.pdf") == "instrument_cal"
    assert kind_for(at + "PDS Macropoxy 646.pdf") == "product_data_sheet"
    assert kind_for(at + "SHER-WILLIAMS HI-SOLIDS_POLYURETHANE PDS.PDF") == "product_data_sheet"
    assert kind_for(at + "SDS Denso Protal 7200 Part A.pdf") == "safety_data_sheet"
    assert kind_for(at + "8-21-25 Coating Reports.pdf") == "coating"
    assert kind_for(at + "4 FG 7-11-25.pdf") == "coating"


# -- schema migration -------------------------------------------------------

def test_an_older_database_gains_new_columns(tmp_path):
    # Auditors keep their own database and update on their own schedule, so a
    # release that adds a column has to migrate rather than fail on insert.
    path = tmp_path / "old.db"
    database = Database(path)
    with database.tx() as c:
        c.execute("ALTER TABLE instrument_cal DROP COLUMN description")
    database.conn.close()

    reopened = Database(path)
    columns = {r["name"] for r in
               reopened.conn.execute("PRAGMA table_info(instrument_cal)")}
    assert "description" in columns
    assert reopened._add_new_columns() == []      # and is idempotent


def test_the_schema_parser_sees_every_table():
    declared = declared_columns()
    assert "coating_report" in declared and "weld" in declared
    # A trailing `-- comment` must not travel into the column definition.
    assert declared["instrument_cal"]["serial_key"] == "TEXT"
    assert declared["weld"]["project_id"].startswith("INTEGER NOT NULL")


# -- the rules --------------------------------------------------------------

@pytest.fixture(autouse=True)
def one_page_documents(monkeypatch):
    """Treat each test's coating document as a single page.

    COAT-10 reasons from absence and so waits until every page of every
    coating document has been read; the fixtures below seed one.
    """
    monkeypatch.setattr("weldaudit.vision.page_count", lambda path: 1)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "c.db")
    pid = database.upsert_project("C", str(tmp_path))
    with database.tx() as c:
        c.execute(
            """INSERT INTO document(id, project_id, path, filename, ext, kind,
                                    segment, fingerprint)
               VALUES(1, ?, 'p', '8-21-25 Coating Reports.pdf', '.pdf',
                      'coating', '20IN LP', 'fp1')""",
            (pid,),
        )
    return database, pid


TARGET = Target(1, "p", "8-21-25 Coating Reports.pdf", "fp1", 7, "test", "20IN LP")

BLANK = {
    "page_is_coating_report": True, "report_date": "8-21-25", "line_size": '20"',
    "material": "CS", "service": "Low Pressure", "job_name": "BLUEWATER PIPELINES",
    "contractor": "GENESIS", "inspector": "MIKE HOWLE", "starting_station": None,
    "ending_station": None, "blast_media": "Garnet",
    "cleanliness_standard": "NACE#2", "profile_required": 2.5,
    "profile_readings": [3.6, 3.7, 3.6, 3.0],
    "environmental": [{"time": "3:43pm", "air_temp_f": 104.4,
                       "relative_humidity": 17.4, "steel_temp_f": 117.6,
                       "dew_point_f": 49.1}],
    "coats": [{"nde_weld_no": "GXR 048", "manufacturer": "VALSPAR PIPECLAD",
               "product": "2000 SLOW GEL", "color": "Green",
               "batch_a": "CU0585BB", "batch_b": None,
               "application_method": "Flocking", "wft_mils": None,
               "dft_mils": 32.5, "dft_layer": "top"}],
    "total_welds_coated": None, "jeeped_from_station": "0+00",
    "jeeped_to_station": "12+50",
    "instruments": [{"role": "dft_gauge", "serial": "1122124"}],
    "comments": None,
}


def report(**over):
    payload = dict(BLANK)
    payload.update(over)
    return payload


def apply(db, pid, payload, page_no=0):
    """Read a page and fold it in, as a real pass does — cache included.

    COAT-10 asks how much of each document was *read*, not how many reports
    came out of it, so a test that writes the report without recording the
    reading is not testing the same thing the pass does.
    """
    db.ocr_put("fp1:coating:2000", page_no, "test-model", payload)
    return _apply_coating(db, pid, TARGET, payload, page_no)


def fire(db, pid, rule):
    return rule(db, pid, "run")


def cert(db, pid, serial, kind="dft_gauge", calibrated=""):
    from weldaudit.instruments import serial_key as key
    with db.tx() as c:
        c.execute(
            """INSERT INTO instrument_cal
               (project_id, kind, serial, serial_key, calibrated, evidence, source)
               VALUES (?,?,?,?,?,'filename','instrument_filename')""",
            (pid, kind, serial, key(serial), calibrated),
        )


def add_weld(db, pid, weld_no, segment="20IN LP"):
    with db.tx() as c:
        c.execute(
            """INSERT INTO weld(project_id, segment, line, weld_no, source)
               VALUES(?, ?, '20 LP', ?, 'weld_map_vision')""",
            (pid, segment, weld_no),
        )


# -- writing the report back ------------------------------------------------

def test_a_page_becomes_a_report_with_its_children(db):
    database, pid = db
    assert apply(database, pid, report()) == 1
    row = database.one("SELECT * FROM coating_report WHERE project_id=?", (pid,))
    assert row["line_nps"] == 20 and row["profile_reqd"] == 2.5
    assert row["cleanliness"] == "NACE#2" and row["jeep_from"] == "0+00"
    assert len(database.q("SELECT * FROM coating_profile WHERE report_id=?",
                          (row["id"],))) == 4
    assert len(database.q("SELECT * FROM coating_coat WHERE report_id=?",
                          (row["id"],))) == 1


def test_each_page_of_a_bundle_is_its_own_report(db):
    # "8-22 & 8-23 Coating Reports.pdf" holds one form per page; unlike a
    # pressure test package these must not merge onto one row.
    database, pid = db
    apply(database, pid, report(report_date="8-22-25"), page_no=0)
    apply(database, pid, report(report_date="8-23-25"), page_no=1)
    rows = database.q("SELECT report_date FROM coating_report WHERE project_id=?", (pid,))
    assert sorted(r["report_date"] for r in rows) == ["2025-08-22", "2025-08-23"]


def test_replaying_a_page_does_not_duplicate_it(db):
    database, pid = db
    for _ in range(3):
        apply(database, pid, report())
    assert len(database.q("SELECT * FROM coating_report WHERE project_id=?", (pid,))) == 1
    assert len(database.q("SELECT * FROM coating_profile")) == 4


def test_a_page_that_is_not_a_report_is_skipped(db):
    database, pid = db
    assert apply(database, pid, report(page_is_coating_report=False)) == 0
    assert database.q("SELECT * FROM coating_report WHERE project_id=?", (pid,)) == []


# -- COAT-01 conditions -----------------------------------------------------

def test_good_conditions_pass(db):
    database, pid = db
    apply(database, pid, report())
    assert fire(database, pid, rules.conditions_out_of_range) == []


def test_humidity_over_the_limit_is_critical(db):
    database, pid = db
    apply(database, pid, report(environmental=[
        {"time": "9:00am", "air_temp_f": 71.0, "relative_humidity": 91.0,
         "steel_temp_f": 68.0, "dew_point_f": 55.0}]))
    found = fire(database, pid, rules.conditions_out_of_range)
    assert len(found) == 1 and found[0]["severity"] == "critical"
    assert "91" in found[0]["message"] and "85" in found[0]["message"]


def test_steel_too_close_to_the_dew_point_is_critical(db):
    database, pid = db
    apply(database, pid, report(environmental=[
        {"time": "6:30am", "air_temp_f": 60.0, "relative_humidity": 70.0,
         "steel_temp_f": 57.0, "dew_point_f": 54.0}]))
    found = fire(database, pid, rules.conditions_out_of_range)
    assert len(found) == 1 and "3°F" in found[0]["message"]


def test_exactly_five_degrees_above_the_dew_point_is_allowed(db):
    database, pid = db
    apply(database, pid, report(environmental=[
        {"time": "7:00am", "air_temp_f": 60.0, "relative_humidity": 70.0,
         "steel_temp_f": 59.0, "dew_point_f": 54.0}]))
    assert fire(database, pid, rules.conditions_out_of_range) == []


def test_both_faults_on_one_reading_are_one_finding(db):
    database, pid = db
    apply(database, pid, report(environmental=[
        {"time": "6:00am", "air_temp_f": 58.0, "relative_humidity": 96.0,
         "steel_temp_f": 56.0, "dew_point_f": 55.0}]))
    found = fire(database, pid, rules.conditions_out_of_range)
    assert len(found) == 1
    assert "humidity" in found[0]["message"] and "dew point" in found[0]["message"]


def test_a_missing_reading_cannot_fail(db):
    database, pid = db
    apply(database, pid, report(environmental=[
        {"time": "9:00am", "air_temp_f": None, "relative_humidity": None,
         "steel_temp_f": None, "dew_point_f": None}]))
    assert fire(database, pid, rules.conditions_out_of_range) == []


# -- COAT-02 epoxy in the cold ----------------------------------------------

def test_cold_epoxy_is_critical(db):
    database, pid = db
    apply(database, pid, report(
        coats=[{"nde_weld_no": None, "manufacturer": "Sherwin-Williams",
                "product": "Macropoxy 646", "color": "White", "batch_a": "X",
                "batch_b": None, "application_method": "Spray",
                "wft_mils": 8.0, "dft_mils": 5.0, "dft_layer": "primer"}],
        environmental=[{"time": "7:00am", "air_temp_f": 34.0,
                        "relative_humidity": 60.0, "steel_temp_f": 36.0,
                        "dew_point_f": 20.0}]))
    found = fire(database, pid, rules.epoxy_too_cold)
    assert len(found) == 1 and "36" in found[0]["message"]


def test_the_cold_limit_is_not_applied_to_other_coatings(db):
    # 40°F is epoxy's floor in GPPB-0140 §4.C; flocked FBE has its own.
    database, pid = db
    apply(database, pid, report(environmental=[
        {"time": "7:00am", "air_temp_f": 34.0, "relative_humidity": 60.0,
         "steel_temp_f": 36.0, "dew_point_f": 20.0}]))
    assert fire(database, pid, rules.epoxy_too_cold) == []


# -- COAT-03 blast media ----------------------------------------------------

@pytest.mark.parametrize("media", ["Black Beauty", "coal slag", "Dolen Sand #3"])
def test_prohibited_media_is_reported(db, media):
    database, pid = db
    apply(database, pid, report(blast_media=media))
    found = fire(database, pid, rules.prohibited_media)
    assert len(found) == 1 and media in found[0]["message"]


def test_dolen_sand_names_the_contradiction_in_the_form(db):
    # The older form prints Dolen Sand #3 as acceptable; Table I footnote 4
    # prohibits it. A crew following the form was not being careless.
    database, pid = db
    apply(database, pid, report(blast_media="Dolen Sand #3"))
    assert "form is out of date" in fire(database, pid, rules.prohibited_media)[0]["message"]


def test_garnet_is_fine(db):
    database, pid = db
    apply(database, pid, report())
    assert fire(database, pid, rules.prohibited_media) == []


# -- COAT-04 thickness ------------------------------------------------------

def test_flocked_fbe_above_twelve_mils_passes(db):
    database, pid = db
    apply(database, pid, report())
    assert fire(database, pid, rules.thickness_below_minimum) == []


def test_thin_flocked_fbe_is_reported(db):
    database, pid = db
    apply(database, pid, report(coats=[{**BLANK["coats"][0], "dft_mils": 9.0}]))
    found = fire(database, pid, rules.thickness_below_minimum)
    assert len(found) == 1 and "12 mils" in found[0]["message"]
    assert "GXR 048" in found[0]["message"]


def test_thin_epoxy_is_measured_against_table_one(db):
    database, pid = db
    apply(database, pid, report(coats=[
        {"nde_weld_no": None, "manufacturer": "Sherwin-Williams",
         "product": "Macropoxy 646", "color": "White", "batch_a": "X",
         "batch_b": None, "application_method": "Spray", "wft_mils": 6.0,
         "dft_mils": 2.5, "dft_layer": "primer"}]))
    found = fire(database, pid, rules.thickness_below_minimum)
    assert len(found) == 1 and "4 mils" in found[0]["message"]


def test_an_unrecognised_product_is_left_alone(db):
    # Measuring an unfamiliar trade name against a guessed limit would produce
    # confident findings an auditor cannot defend.
    database, pid = db
    apply(database, pid, report(coats=[
        {"nde_weld_no": None, "manufacturer": "Someone", "product": "Widget 9",
         "color": None, "batch_a": None, "batch_b": None,
         "application_method": "Brush", "wft_mils": None, "dft_mils": 0.5,
         "dft_layer": "top"}]))
    assert fire(database, pid, rules.thickness_below_minimum) == []


def test_a_taped_field_joint_is_not_held_to_the_fbe_minimum(db):
    # A 20" line's body is flocked and its field joints are taped; the method
    # decides the limit, not the diameter.
    database, pid = db
    apply(database, pid, report(coats=[
        {"nde_weld_no": "GXR 049", "manufacturer": "Polyguard",
         "product": "RD-6 tape", "color": None, "batch_a": None,
         "batch_b": None, "application_method": "RD-6 tape", "wft_mils": None,
         "dft_mils": 8.0, "dft_layer": "top"}]))
    assert fire(database, pid, rules.thickness_below_minimum) == []


# -- COAT-05 profile --------------------------------------------------------

def test_profiles_above_the_requirement_pass(db):
    database, pid = db
    apply(database, pid, report())
    assert fire(database, pid, rules.profile_out_of_range) == []


def test_a_low_profile_is_reported(db):
    database, pid = db
    apply(database, pid, report(profile_readings=[3.6, 1.9, 3.0, 2.1]))
    found = fire(database, pid, rules.profile_out_of_range)
    assert len(found) == 1 and "2 readings below" in found[0]["message"]


def test_no_stated_requirement_means_no_comparison(db):
    database, pid = db
    apply(database, pid, report(profile_required=None, profile_readings=[0.4]))
    assert fire(database, pid, rules.profile_out_of_range) == []


# -- COAT-06 jeeping --------------------------------------------------------

def test_a_jeeped_report_passes(db):
    database, pid = db
    apply(database, pid, report())
    assert fire(database, pid, rules.not_jeeped) == []


def test_coating_with_no_jeep_stations_is_reported(db):
    database, pid = db
    apply(database, pid, report(jeeped_from_station=None, jeeped_to_station=None))
    found = fire(database, pid, rules.not_jeeped)
    assert len(found) == 1 and "backfilled" in found[0]["message"]


def test_a_blast_only_day_has_nothing_to_jeep(db):
    database, pid = db
    apply(database, pid, report(coats=[], jeeped_from_station=None,
                                jeeped_to_station=None))
    assert fire(database, pid, rules.not_jeeped) == []


# -- COAT-07 completeness ---------------------------------------------------

def test_a_complete_report_is_not_reported(db):
    database, pid = db
    apply(database, pid, report())
    assert fire(database, pid, rules.incomplete_report) == []


def test_missing_fields_are_one_finding_not_five(db):
    database, pid = db
    apply(database, pid, report(blast_media=None, cleanliness_standard=None,
                                profile_required=None,
                                coats=[{**BLANK["coats"][0], "dft_mils": None}]))
    found = fire(database, pid, rules.incomplete_report)
    assert len(found) == 1 and found[0]["severity"] == "major"
    for label in ("blast media", "cleanliness standard", "required profile",
                  "dry film thickness"):
        assert label in found[0]["message"]


def test_a_preparation_only_report_is_only_a_note(db):
    database, pid = db
    apply(database, pid, report(coats=[], blast_media=None))
    found = fire(database, pid, rules.incomplete_report)
    assert len(found) == 1 and found[0]["severity"] == "minor"
    assert "surface preparation only" in found[0]["message"]


# -- COAT-08 instruments ----------------------------------------------------

def test_no_certificates_read_means_no_instrument_findings(db):
    database, pid = db
    apply(database, pid, report(instruments=[{"role": "dft_gauge", "serial": "9999"}]))
    assert fire(database, pid, rules.instrument_uncalibrated) == []


def test_a_certificated_instrument_passes(db):
    database, pid = db
    apply(database, pid, report())
    cert(database, pid, "1122124")
    assert fire(database, pid, rules.instrument_uncalibrated) == []


def test_an_instrument_with_no_certificate_is_major(db):
    database, pid = db
    apply(database, pid, report(instruments=[
        {"role": "holiday_detector", "serial": "6539"}]))
    cert(database, pid, "12594", kind="holiday_detector")
    found = fire(database, pid, rules.instrument_uncalibrated)
    assert len(found) == 1 and found[0]["severity"] == "major"
    assert "holiday detector" in found[0]["message"]


def test_a_one_character_slip_is_reported_as_a_slip(db):
    # BTYG12 is filed and a report writes BTYGL2. Calling that instrument
    # uncalibrated would be a false major over a 1 read as an L.
    database, pid = db
    apply(database, pid, report(instruments=[
        {"role": "profile_gauge", "serial": "BTYGL2"}]))
    cert(database, pid, "BTYG12", kind="profile_gauge")
    found = fire(database, pid, rules.instrument_uncalibrated)
    assert len(found) == 1 and found[0]["severity"] == "minor"
    assert "BTYG12" in found[0]["message"]


# -- COAT-09 coating system -------------------------------------------------

def test_a_large_line_coated_by_tape_is_reported(db):
    database, pid = db
    apply(database, pid, report(coats=[
        {**BLANK["coats"][0], "application_method": "RD-6 tape"}]))
    found = fire(database, pid, rules.wrong_coating_system)
    assert len(found) == 1 and "flocked" in found[0]["message"]


def test_a_small_line_may_be_taped(db):
    database, pid = db
    apply(database, pid, report(line_size='4"', coats=[
        {**BLANK["coats"][0], "application_method": "RD-6 tape"}]))
    assert fire(database, pid, rules.wrong_coating_system) == []


def test_flocking_a_large_line_is_correct(db):
    database, pid = db
    apply(database, pid, report())
    assert fire(database, pid, rules.wrong_coating_system) == []


# -- COAT-10 uncoated segments ----------------------------------------------

def test_a_welded_segment_with_no_coating_report_is_reported(db):
    database, pid = db
    apply(database, pid, report())
    add_weld(database, pid, "GXR-100", segment="6IN FG")
    found = fire(database, pid, rules.segment_uncoated)
    assert len(found) == 1 and found[0]["segment"] == "6IN FG"


def test_a_partly_read_project_claims_nothing(db, monkeypatch):
    # Bluewater files nineteen coating documents. Seeding a single page of one of
    # them makes sixteen segments look uncoated — every one an artefact of
    # stopping early rather than a gap in the package.
    database, pid = db
    monkeypatch.setattr("weldaudit.vision.page_count", lambda path: 7)
    apply(database, pid, report())
    add_weld(database, pid, "GXR-100", segment="6IN FG")
    assert fire(database, pid, rules.segment_uncoated) == []


def test_a_page_that_is_not_a_report_still_counts_as_read(db, monkeypatch):
    # Counted by pages read, not reports produced: a product data sheet bound
    # into the same PDF must not make the document unreadable for ever.
    database, pid = db
    monkeypatch.setattr("weldaudit.vision.page_count", lambda path: 2)
    apply(database, pid, report(), page_no=0)
    apply(database, pid, {"page_is_coating_report": False}, page_no=1)
    add_weld(database, pid, "GXR-100", segment="6IN FG")
    assert len(fire(database, pid, rules.segment_uncoated)) == 1


def test_the_other_coating_rules_do_not_wait(db, monkeypatch):
    # Only COAT-10 reasons from absence. A single report still supports every
    # check that reads what is on the page in front of it.
    database, pid = db
    monkeypatch.setattr("weldaudit.vision.page_count", lambda path: 7)
    apply(database, pid, report(jeeped_from_station=None, jeeped_to_station=None))
    assert len(fire(database, pid, rules.not_jeeped)) == 1


def test_no_coating_read_at_all_means_no_findings(db):
    database, pid = db
    add_weld(database, pid, "GXR-100", segment="6IN FG")
    assert fire(database, pid, rules.segment_uncoated) == []


# -- COAT-11..15: tying the coating to individual welds ---------------------

def known_weld(db, pid, nde_id, *, welded="", examined="", segment="20 LP"):
    """A weld the project knows: on the register, on a reader sheet, or both."""
    with db.tx() as c:
        if welded:
            c.execute(
                """INSERT INTO weld(project_id, segment, line, weld_no, nde_id,
                                    date_welded, source)
                   VALUES(?, ?, '20 LP', ?, ?, ?, 'weld_map_vision')""",
                (pid, segment, nde_id, nde_id, welded),
            )
        if examined or not welded:
            c.execute(
                """INSERT INTO nde_shot(project_id, segment, nde_id, sheet_date,
                                        evidence)
                   VALUES(?, ?, ?, ?, 'filename')""",
                (pid, segment, nde_id, examined or None),
            )


def coats(*ids):
    """Coating rows naming these welds, as the form writes them."""
    return [{**BLANK["coats"][0], "nde_weld_no": i} for i in ids]


def test_the_form_s_spacing_is_normalised_for_joining(db):
    # The coating form writes `GXR 048`; everything else writes `GXR-048`.
    # Without this the column joins to nothing at all.
    database, pid = db
    apply(database, pid, report())
    row = database.one("SELECT nde_weld_no, nde_id FROM coating_coat")
    assert (row["nde_weld_no"], row["nde_id"]) == ("GXR 048", "GXR-048")


def test_a_column_holding_something_else_is_not_a_weld_id(db):
    database, pid = db
    apply(database, pid, report(coats=coats("N/A")))
    assert database.one("SELECT nde_id FROM coating_coat")["nde_id"] == ""


# -- COAT-11 unknown welds --------------------------------------------------

def test_a_coated_weld_the_project_knows_passes(db):
    database, pid = db
    known_weld(database, pid, "GXR-048")
    apply(database, pid, report())
    assert fire(database, pid, rules.coated_weld_unknown) == []


def test_a_reader_sheet_alone_is_enough_to_know_a_weld(db):
    # Bluewater links only 80 of 2,004 welds to an NDE id, but has 2,141 reader
    # sheets. Requiring the weld register would report the whole job.
    database, pid = db
    known_weld(database, pid, "GXR-048", examined="2025-08-20")
    apply(database, pid, report())
    assert fire(database, pid, rules.coated_weld_unknown) == []


def test_a_coated_weld_nothing_else_knows_is_reported(db):
    database, pid = db
    known_weld(database, pid, "GXR-001")
    apply(database, pid, report(coats=coats("GXR 900")))
    found = fire(database, pid, rules.coated_weld_unknown)
    assert len(found) == 1 and found[0]["subject"] == "GXR-900"
    assert "never examined" in found[0]["message"]


def test_with_no_registers_at_all_nothing_is_claimed(db):
    database, pid = db
    apply(database, pid, report())
    assert fire(database, pid, rules.coated_weld_unknown) == []


# -- COAT-12 / COAT-13 sequence ---------------------------------------------

def test_examined_then_coated_passes(db):
    database, pid = db
    known_weld(database, pid, "GXR-048", examined="2025-08-20")
    apply(database, pid, report())
    assert fire(database, pid, rules.coated_before_nde) == []


def test_examined_and_coated_the_same_day_passes(db):
    # The real pairing: the 8-21-25 report and a reader sheet dated 2025-08-21.
    # Shot in the morning, coated in the afternoon is ordinary sequencing.
    database, pid = db
    known_weld(database, pid, "GXR-048", examined="2025-08-21")
    apply(database, pid, report())
    assert fire(database, pid, rules.coated_before_nde) == []


def test_coated_before_the_shot_is_reported(db):
    database, pid = db
    known_weld(database, pid, "GXR-048", examined="2025-09-04")
    apply(database, pid, report())
    found = fire(database, pid, rules.coated_before_nde)
    assert len(found) == 1 and "cannot be radiographed" in found[0]["message"]
    assert "2025-08-21" in found[0]["message"] and "2025-09-04" in found[0]["message"]


def test_coated_before_welded_is_critical(db):
    database, pid = db
    known_weld(database, pid, "GXR-048", welded="2025-09-01")
    apply(database, pid, report())
    found = fire(database, pid, rules.coated_before_welded)
    assert len(found) == 1 and found[0]["severity"] == "critical"
    assert "before it existed" in found[0]["message"]


def test_a_weld_with_no_dates_cannot_be_sequenced(db):
    database, pid = db
    known_weld(database, pid, "GXR-048")
    apply(database, pid, report())
    assert fire(database, pid, rules.coated_before_nde) == []
    assert fire(database, pid, rules.coated_before_welded) == []


# -- COAT-14 welds skipped inside a coated run ------------------------------

def test_a_weld_skipped_between_two_coated_ones_is_reported(db):
    database, pid = db
    for n in range(40, 50):
        known_weld(database, pid, f"GXR-{n:03d}")
    apply(database, pid, report(coats=coats("GXR 040", "GXR 048")))
    found = fire(database, pid, rules.uncoated_weld)
    assert len(found) == 1 and found[0]["subject"] == "7 welds"
    assert "GXR-041" in found[0]["message"] and "GXR-047" in found[0]["message"]


def test_welds_outside_the_coated_run_are_not_claimed(db):
    # The reports have not all been read. Reporting GXR-001 and GXR-089 as
    # uncoated would turn "we have not looked yet" into a finding about pipe.
    database, pid = db
    for n in (1, 40, 41, 48, 89):
        known_weld(database, pid, f"GXR-{n:03d}")
    apply(database, pid, report(coats=coats("GXR 040", "GXR 048")))
    found = fire(database, pid, rules.uncoated_weld)
    assert len(found) == 1
    assert "GXR-041" in found[0]["message"]
    for outside in ("GXR-001", "GXR-089"):
        assert outside not in found[0]["message"]


def test_a_mistyped_weld_number_cannot_stretch_the_run(db):
    # Seeded against Bluewater's real 247-weld GXR series, a single bogus
    # `GXR 900` in the weld column pushed the covered run to GXR-044..GXR-900
    # and reported 122 uncoated welds instead of three. The id nothing else
    # recognises is the one most likely to be a slip of the pen, so it cannot
    # be a boundary — COAT-11 reports it on its own account.
    database, pid = db
    for n in (44, 45, 46, 47, 48):
        known_weld(database, pid, f"GXR-{n:03d}")
    apply(database, pid, report(coats=coats("GXR 044", "GXR 048", "GXR 900")))
    found = fire(database, pid, rules.uncoated_weld)
    assert len(found) == 1 and found[0]["subject"] == "3 welds"
    assert "GXR-044 to GXR-048" in found[0]["message"]


def test_coverage_needs_a_coated_weld_the_project_knows(db):
    database, pid = db
    known_weld(database, pid, "GXR-044")
    apply(database, pid, report(coats=coats("GXR 900", "GXR 901")))
    assert fire(database, pid, rules.uncoated_weld) == []


def test_a_fully_coated_run_reports_nothing(db):
    database, pid = db
    for n in (40, 41, 42):
        known_weld(database, pid, f"GXR-{n:03d}")
    apply(database, pid, report(coats=coats("GXR 040", "GXR 041", "GXR 042")))
    assert fire(database, pid, rules.uncoated_weld) == []


def test_one_coated_weld_defines_no_run(db):
    database, pid = db
    for n in (40, 41, 42):
        known_weld(database, pid, f"GXR-{n:03d}")
    apply(database, pid, report())
    assert fire(database, pid, rules.uncoated_weld) == []


def test_another_series_is_not_swept_in(db):
    database, pid = db
    for n in (40, 44, 48):
        known_weld(database, pid, f"GXR-{n:03d}")
    known_weld(database, pid, "AFB-044")
    apply(database, pid, report(coats=coats("GXR 040", "GXR 048")))
    found = fire(database, pid, rules.uncoated_weld)
    assert len(found) == 1 and "AFB-044" not in found[0]["message"]


def test_no_coating_read_means_no_coverage_claim(db):
    database, pid = db
    for n in (40, 41, 42):
        known_weld(database, pid, f"GXR-{n:03d}")
    assert fire(database, pid, rules.uncoated_weld) == []


# -- COAT-15 the count box --------------------------------------------------

def test_the_count_box_agreeing_passes(db):
    database, pid = db
    apply(database, pid, report(total_welds_coated=2,
                                coats=coats("GXR 048", "GXR 049")))
    assert fire(database, pid, rules.welds_coated_mismatch) == []


def test_the_count_box_disagreeing_is_reported(db):
    database, pid = db
    apply(database, pid, report(total_welds_coated=9,
                                coats=coats("GXR 048", "GXR 049")))
    found = fire(database, pid, rules.welds_coated_mismatch)
    assert len(found) == 1 and "says 9 welds" in found[0]["message"]
    assert "lists 2" in found[0]["message"]


def test_a_blank_count_box_is_not_a_disagreement(db):
    # The real Bluewater form leaves it blank; that is COAT-07's business.
    database, pid = db
    apply(database, pid, report(total_welds_coated=None))
    assert fire(database, pid, rules.welds_coated_mismatch) == []


def test_an_older_form_with_no_weld_column_is_left_alone(db):
    # PLU's revision has no NDE Weld # column at all, so none of these rules
    # have anything to say about it.
    database, pid = db
    known_weld(database, pid, "GXR-048")
    apply(database, pid, report(total_welds_coated=4, coats=coats(None)))
    for rule in (rules.coated_weld_unknown, rules.coated_before_nde,
                 rules.coated_before_welded, rules.uncoated_weld,
                 rules.welds_coated_mismatch):
        assert fire(database, pid, rule) == []


# -- the two real forms, end to end -----------------------------------------

def test_the_bluewater_form_reports_its_three_real_problems(db):
    """8-21-25, 20" LP: the form is well filled, and three things are wrong.

    Both holiday detectors it names are uncertificated, and the profile gauge
    serial is one character off one that is.
    """
    database, pid = db
    apply(database, pid, report(
        total_welds_coated=None, jeeped_from_station=None, jeeped_to_station=None,
        instruments=[{"role": "holiday_detector", "serial": "7545"},
                     {"role": "dft_gauge", "serial": "1122124"},
                     {"role": "dpm", "serial": "1061323"},
                     {"role": "profile_gauge", "serial": "BTYGL2"},
                     {"role": "holiday_detector", "serial": "6539"}]))
    for serial, kind in (("12594", "holiday_detector"), ("7961", "holiday_detector"),
                         ("1122124", "dft_gauge"), ("1061323", "dpm"),
                         ("BTYG12", "profile_gauge")):
        cert(database, pid, serial, kind=kind)

    found = {f["rule"]: f for f in
             [*fire(database, pid, rules.conditions_out_of_range),
              *fire(database, pid, rules.thickness_below_minimum),
              *fire(database, pid, rules.profile_out_of_range),
              *fire(database, pid, rules.incomplete_report),
              *fire(database, pid, rules.wrong_coating_system)]}
    assert found == {}          # everything it recorded is compliant

    assert len(fire(database, pid, rules.not_jeeped)) == 1
    instruments = fire(database, pid, rules.instrument_uncalibrated)
    assert {f["subject"]: f["severity"] for f in instruments} == {
        "7545": "major", "6539": "major", "BTYGL2": "minor"}


def test_the_plu_form_is_mostly_blank(db):
    """4 FG 7-11-25: signed by both parties, and it records almost nothing.

    Blast media, cleanliness standard, required profile and dry film thickness
    are all empty, and so are the jeeped stations — on a form that has a
    product, five profile readings and two signatures on it.
    """
    database, pid = db
    apply(database, pid, report(
        report_date="7-11-25", line_size='4"', service="GL",
        blast_media=None, cleanliness_standard=None, profile_required=None,
        profile_readings=[2.9, 3.1, 2.5, 3.2, 2.6],
        environmental=[{"time": "1:00", "air_temp_f": 97.0,
                        "relative_humidity": 18.0, "steel_temp_f": 107.0,
                        "dew_point_f": 62.0}],
        coats=[{"nde_weld_no": None, "manufacturer": "Macropoxy 646",
                "product": None, "color": "White", "batch_a": "XM4425BLM",
                "batch_b": "XM098SA2M", "application_method": "Gun 2100",
                "wft_mils": 10.0, "dft_mils": None, "dft_layer": "primer"}],
        total_welds_coated=None, jeeped_from_station=None,
        jeeped_to_station=None, instruments=[]))

    incomplete = fire(database, pid, rules.incomplete_report)
    assert len(incomplete) == 1 and incomplete[0]["severity"] == "major"
    assert "dry film thickness" in incomplete[0]["message"]
    assert len(fire(database, pid, rules.not_jeeped)) == 1

    # Nothing else can fire: the report gives no figure to compare against.
    assert fire(database, pid, rules.profile_out_of_range) == []
    assert fire(database, pid, rules.thickness_below_minimum) == []
    assert fire(database, pid, rules.conditions_out_of_range) == []
