"""Reading weld and heat callouts off an isometric, and comparing map to log.

Most of this file guards against inventing welds. A general identifier regex
run over a drawing's text layer produces plenty of them — from the date beside
a callout, from the line number in the title block, from a note printed on the
sheet — and every one becomes a critical finding about a joint that does not
exist. Each case below is text taken from a real drawing.

The grouping tests matter for the opposite reason: the parts of one callout
arrive as separate spans, stacked vertically on GL 31 and side by side on
Kestrel 8 because that drawing is plotted rotated, and a callout whose welders
and date do not reach it is a weld with no crew and no date.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402
from weldaudit.rules import registers as rules  # noqa: E402
from weldaudit.weldmap import (  # noqa: E402
    all_ids, cluster_spans, group_spans, is_concentrated, parse_callout,
    parse_heat_token, parse_id_token,
)


# -- what is and is not an identifier ---------------------------------------

@pytest.mark.parametrize("token,expected", [
    ("AFB-19", ("AFB", 19, "")),
    ("AFB-16C", ("AFB", 16, "C")),
    ("AXR-01P", ("AXR", 1, "P")),
    ("CAFB-093RP", ("CAFB", 93, "RP")),
    ("APT-001", ("APT", 1, "")),
    ("AFB 094P", ("AFB", 94, "P")),   # the separator may be a space
])
def test_identifier_tokens(token, expected):
    assert parse_id_token(token) == expected


@pytest.mark.parametrize("token", [
    "DTD22-LP-16-1A",              # the line number - yields LP-016 unguarded
    "DTD22MP-LP-16-1A",
    "12-Mar-2025",                 # a date - yields MAR-2025 unguarded
    "NI.2024.14306.CAP.01",        # the AFE
    '16"-A1-0-PG-0417',            # the drawing number
    "10'", "8-01-25", "ELBOW-090", "THE-150", "AND-2",
])
def test_things_that_are_not_identifiers(token):
    assert parse_id_token(token) is None


def test_two_letter_prefixes_need_the_projects_own_vocabulary():
    # TI and FB are real series on Bluewater, but accepting every two-letter
    # prefix everywhere lets an elevation mark become a weld.
    assert parse_id_token("TI-04") is None
    assert parse_id_token("TI-04", allow_short=frozenset({"TI"})) == ("TI", 4, "")


# -- whole callouts ---------------------------------------------------------

def test_a_plu_callout_reads_id_welders_and_date():
    callout = parse_callout("AFB-19 ARO/ARV 8-01-25")
    assert (callout.weld_id, callout.welders, callout.date) == (
        "AFB-019", "ARO/ARV", "8-01-25")


def test_the_parts_may_arrive_in_any_order():
    # Both orders occur on the same drawing.
    callout = parse_callout("ARV/AFM AFB-20 7/31/25")
    assert (callout.weld_id, callout.welders, callout.date) == (
        "AFB-020", "ARV/AFM", "7/31/25")


def test_the_date_is_not_read_as_more_welds():
    # `AFB-19 ARO/ARV 8-01-25` yields AFB-019, AFB-001 and AFB-025 to a
    # general identifier regex: the date's -01 and -25 read as bare
    # continuations of the series.
    callout = parse_callout("AFB-19 ARO/ARV 8-01-25")
    assert callout.weld_id == "AFB-019"
    assert all_ids("AFB-19 ARO/ARV 8-01-25".split()) == ["AFB-019"]


def test_an_identifier_split_by_a_space_is_joined():
    assert parse_callout("AFB 094P").weld_id == "AFB-094P"
    assert parse_callout("CAFB 093RP").weld_id == "CAFB-093RP"


def test_a_note_printed_on_the_drawing_is_not_a_callout():
    assert parse_callout("TO BE INCLUDED IN THE 150 SERIES TEST") is None
    assert parse_callout(
        "PLEASE SEE NEXT PAGE FOR EAST AND NORTH RUNS; MOVED DUE TO SPACE") is None


def test_two_welder_groups_mean_the_pairing_is_not_safe():
    # Two balloons whose spans have run together. Guessing which crew belongs
    # to the weld is how the wrong welder ends up on a joint.
    callout = parse_callout("AFB-19 ARO/ARV AFM/ARS")
    assert callout.weld_id == "AFB-019" and callout.welders == ""


# -- grouping spans into balloons -------------------------------------------

def plu_spans():
    """Three spans side by side: this drawing is plotted rotated."""
    return [(222.2, 298.5, 250.0, 305.0, "8-01-25"),
            (234.1, 294.2, 262.0, 301.0, "ARO/ARV"),
            (246.1, 298.7, 274.0, 305.0, "AFB-19")]


def di31_spans():
    """Three spans stacked, the same x, ten points apart."""
    return [(61.0, 214.0, 90.0, 222.0, "AFB-10"),
            (63.0, 224.0, 92.0, 232.0, "6/4/26"),
            (62.0, 233.0, 91.0, 241.0, "EM93")]


@pytest.mark.parametrize("spans,weld_id,welders,date", [
    (plu_spans(), "AFB-019", "ARO/ARV", "8-01-25"),
    (di31_spans(), "AFB-010", "EM93", "6/4/26"),
])
def test_a_balloons_parts_find_each_other_whichever_way_it_is_plotted(
        spans, weld_id, welders, date):
    found = group_spans(spans)
    assert len(found) == 1
    assert (found[0].weld_id, found[0].welders, found[0].date) == (
        weld_id, welders, date)


def test_an_identifier_split_over_two_lines_is_joined():
    # `AFB` above `092`, the same x, ten points apart.
    spans = [(215.0, 485.0, 240.0, 493.0, "AFB"),
             (216.0, 495.0, 236.0, 503.0, "092")]
    found = group_spans(spans)
    assert len(found) == 1 and found[0].weld_id == "AFB-092"


def test_a_distant_date_is_not_adopted():
    # The next balloon's date must not become this weld's.
    spans = [(61.0, 214.0, 90.0, 222.0, "AFB-10"),
             (530.0, 480.0, 560.0, 488.0, "6/4/26")]
    found = {c.weld_id: c for c in group_spans(spans)}
    assert found["AFB-010"].date == ""


def test_touching_balloons_still_yield_every_weld():
    # On a crowded sheet two balloons link. With nothing to pair them to,
    # emitting both identifiers is safe and losing one is not.
    spans = [(215.0, 485.0, 240.0, 493.0, "AFB"),
             (216.0, 495.0, 236.0, 503.0, "215"),
             (217.0, 505.0, 242.0, 513.0, "AFB"),
             (218.0, 515.0, 238.0, 523.0, "216")]
    assert {c.weld_id for c in group_spans(spans)} == {"AFB-215", "AFB-216"}


def test_clustering_keeps_separate_balloons_apart():
    spans = [(10.0, 10.0, 40.0, 18.0, "AFB-01"),
             (400.0, 300.0, 430.0, 308.0, "AFB-02")]
    assert len(cluster_spans(spans)) == 2


# -- deciding a sheet is a weld map at all ----------------------------------

def test_a_map_dominated_by_one_series_is_believed():
    assert is_concentrated(["AFB"] * 30 + ["ATI"] * 5 + ["APT"] * 4 + ["AXR"])
    assert is_concentrated(["CFB"] * 40 + ["AFB"] * 30)


def test_scattered_identifiers_mean_the_text_is_not_a_register():
    # Bluewater's combined map is a scan; its OCR yields sixteen "identifiers"
    # across six prefixes, none of them a real series.
    assert not is_concentrated(
        ["DRG"] * 6 + ["DBG"] * 3 + ["NORTH"] * 2 + ["DRC"] * 2
        + ["OOO"] * 2 + ["GFB"])


def test_nothing_is_not_concentrated():
    assert not is_concentrated([])


# -- heat callouts ----------------------------------------------------------

@pytest.mark.parametrize("text,heat", [
    ("HT: NN0446", "NN0446"),
    ("HT# 946376", "946376"),
    ("HT- 071B33", "071B33"),          # the separator is written every way
    ("HT: 652583/3", "652583/3"),      # one heat with a plate suffix
    ("HT: F37B6", "F37B6"),
    ("AFB-19 ARO/ARV", ""),
])
def test_heat_callouts(text, heat):
    assert parse_heat_token(text) == heat


@pytest.mark.parametrize("text", [
    "HT: 2F-F33N-RF", "HT: 1F-F03N-SE", "HT: 2FN-F13N-RF",
])
def test_a_valve_figure_written_as_a_heat_is_not_one(text):
    # The draughtsmen use the HT callout for a valve's figure number, because
    # that is how a valve is identified. There will never be a mill
    # certificate for it, so treating it as a heat is a permanent false
    # critical against MTR-01.
    assert parse_heat_token(text) == ""


# -- map against log --------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "m.db")
    return database, database.upsert_project("M", str(tmp_path))


def weld(db, pid, nde_id, source, segment="LINE A"):
    with db.tx() as c:
        c.execute(
            """INSERT INTO weld(project_id, segment, line, weld_no, nde_id, source)
               VALUES(?, ?, 'L', ?, ?, ?)""",
            (pid, segment, nde_id, nde_id, source),
        )


def shot(db, pid, nde_id):
    with db.tx() as c:
        c.execute(
            """INSERT INTO nde_shot(project_id, nde_id, prefix, number, evidence)
               VALUES(?, ?, ?, ?, 'filename')""",
            (pid, nde_id, nde_id.split("-")[0], int(nde_id.split("-")[1][:3])),
        )


def fire(db, pid, rule):
    return rule(db, pid, "run")


def test_a_series_the_log_never_records_is_reported(db):
    database, pid = db
    for i in range(1, 21):
        weld(database, pid, f"CFB-{i:03d}", "weld_map_text")
    for i in range(1, 11):
        weld(database, pid, f"AXR-{i:03d}", "weld_log_csv")
    found = fire(database, pid, rules.map_welds_not_logged)
    assert len(found) == 1 and found[0]["severity"] == "critical"
    assert "20 CFB welds" in found[0]["message"]
    assert "none of them has a reader sheet either" in found[0]["message"]


def test_reader_sheets_soften_the_verdict(db):
    database, pid = db
    for i in range(1, 21):
        weld(database, pid, f"CFB-{i:03d}", "weld_map_text")
        shot(database, pid, f"CFB-{i:03d}")
    for i in range(1, 11):
        weld(database, pid, f"AXR-{i:03d}", "weld_log_csv")
    found = fire(database, pid, rules.map_welds_not_logged)
    assert len(found) == 1 and found[0]["severity"] == "major"
    assert "reader sheets cover 20 of them" in found[0]["message"]


def test_a_series_the_log_covers_is_not_reported(db):
    database, pid = db
    for i in range(1, 11):
        weld(database, pid, f"AFB-{i:03d}", "weld_map_text")
        weld(database, pid, f"AFB-{i:03d}", "weld_log_csv")
    assert fire(database, pid, rules.map_welds_not_logged) == []


def test_a_couple_of_joints_ahead_of_the_paperwork_is_not_reported(db):
    database, pid = db
    for i in range(1, 21):
        weld(database, pid, f"AFB-{i:03d}", "weld_map_text")
    for i in range(1, 19):
        weld(database, pid, f"AFB-{i:03d}", "weld_log_csv")
    assert fire(database, pid, rules.map_welds_not_logged) == []


def test_no_work_record_means_no_comparison(db):
    # Kestrel 8: the map is the only register there is, and the coverage table
    # already says so.
    database, pid = db
    for i in range(1, 21):
        weld(database, pid, f"AFB-{i:03d}", "weld_map_text")
    assert fire(database, pid, rules.map_welds_not_logged) == []


def test_a_log_that_barely_numbers_its_welds_cannot_be_the_yardstick(db):
    # Bluewater's daily reports carry an NDE number on seventy of nineteen
    # hundred welds. "The log records none of the CFB series" is true of a
    # register that records almost no series at all.
    database, pid = db
    for i in range(1, 21):
        weld(database, pid, f"CFB-{i:03d}", "weld_map_text")
    weld(database, pid, "GFB-001", "daily_weld_report")
    for i in range(40):
        weld(database, pid, "", "daily_weld_report")
    assert fire(database, pid, rules.map_welds_not_logged) == []


def test_welds_in_the_log_and_on_no_map_are_noted(db):
    database, pid = db
    for i in range(1, 6):
        weld(database, pid, f"AXR-{i:03d}", "weld_map_text")
    for i in range(1, 21):
        weld(database, pid, f"AXR-{i:03d}", "weld_log_csv")
    found = fire(database, pid, rules.logged_welds_not_mapped)
    assert len(found) == 1 and found[0]["severity"] == "info"
    assert "15 AXR welds" in found[0]["message"]


def test_a_series_with_no_map_at_all_says_nothing(db):
    database, pid = db
    weld(database, pid, "AFB-001", "weld_map_text")
    for i in range(1, 21):
        weld(database, pid, f"AXR-{i:03d}", "weld_log_csv")
    assert fire(database, pid, rules.logged_welds_not_mapped) == []
