"""Release for backfill: the last hold point before the ditch closes.

The form makes three assertions in one printed sentence — all weld and heat
map data captured, all NDE cleared, AC mitigation installed — and two of them
are checkable against records the audit already holds. These tests pin that,
and pin the two judgement calls underneath it.

**The release date is the earliest signature**, not the latest: the ditch
could be closed from the moment the first party signed, and Bluewater has a
release the contractor counter-signed six weeks after the inspector released
it. **The join is segment and date, not station** — the form states its extent
in survey stations and nothing else in the corpus places a weld against one.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402
from weldaudit.extract.vision_pass import Target, _apply_backfill  # noqa: E402
from weldaudit.rules import backfill as rules  # noqa: E402
from weldaudit.taxonomy import kind_for  # noqa: E402


# -- classification ---------------------------------------------------------

def test_release_forms_classify_as_backfill():
    assert kind_for(r"x/20 Backfill/Backfill Release Forms.pdf") == "backfill"
    assert kind_for(r"x/20 Backfill/Release for Backfill Forms.PDF") == "backfill"


# -- the rules --------------------------------------------------------------

@pytest.fixture(autouse=True)
def one_page_bundles(monkeypatch):
    """Treat each test's release document as a single-page bundle.

    The date rules skip a segment whose bundle has not been read through, and
    the fixtures below seed one page. Tests that care about the guard itself
    override this.
    """
    monkeypatch.setattr("weldaudit.vision.page_count", lambda path: 1)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "b.db")
    pid = database.upsert_project("B", str(tmp_path))
    with database.tx() as c:
        c.execute(
            """INSERT INTO document(id, project_id, path, filename, ext, kind,
                                    segment, fingerprint)
               VALUES(1, ?, 'p', 'Backfill Release Forms.pdf', '.pdf',
                      'backfill', '16IN LP', 'fp1')""",
            (pid,),
        )
    return database, pid


TARGET = Target(1, "p", "Backfill Release Forms.pdf", "fp1", 3, "test", "16IN LP")

FORM = {
    "page_is_release": True, "line_size": '16"', "wall": ".375",
    "material": "STEEL", "yield_grade": "X52M", "service": "LP",
    "from_station": "0+00", "to_station": "1+65",
    "inspector_signed": True, "inspector_date": "08-07-25",
    "contractor_signed": True, "contractor_date": "8-7-25",
    "survey_signed": False, "survey_date": None,
}


def release(**over):
    payload = dict(FORM)
    payload.update(over)
    return payload


def apply(db, pid, payload, page_no=0):
    """Read a page and fold it in, as a real pass does — cache included.

    The guard asks how much of the bundle was *read*, not how many releases
    came out of it, so a test that writes the release without recording the
    reading is not testing the same thing the pass does.
    """
    db.ocr_put(f"fp1:backfill:2000", page_no, "test-model", payload)
    return _apply_backfill(db, pid, TARGET, payload, page_no)


def weld(db, pid, weld_no, when, *, segment="16IN LP", status="", repair=""):
    with db.tx() as c:
        c.execute(
            """INSERT INTO weld(project_id, segment, line, weld_no, nde_id,
                                date_welded, nde_status, repair_nde_id, source)
               VALUES(?, ?, '16 LP', ?, ?, ?, ?, ?, 'weld_map_text')""",
            (pid, segment, weld_no, weld_no, when, status, repair),
        )


def shot(db, pid, nde_id, when, *, segment="16IN LP"):
    with db.tx() as c:
        c.execute(
            """INSERT INTO nde_shot(project_id, segment, nde_id, sheet_date,
                                    evidence)
               VALUES(?, ?, ?, ?, 'filename')""",
            (pid, segment, nde_id, when),
        )


def fire(db, pid, rule):
    return rule(db, pid, "run")


# -- writing the release back -----------------------------------------------

def test_a_page_becomes_a_release(db):
    database, pid = db
    assert apply(database, pid, release()) == 1
    row = database.one("SELECT * FROM backfill_release WHERE project_id=?", (pid,))
    assert row["from_station"] == "0+00" and row["to_station"] == "1+65"
    assert row["released_on"] == "2025-08-07"
    assert row["inspector_signed"] == 1 and row["survey_signed"] == 0


def test_the_release_date_is_the_earliest_signature(db):
    # Bluewater: inspector and survey on 7-25-25, contractor on 9-6-25. Taking
    # the latest would let a late counter-signature excuse a weld made in
    # between.
    database, pid = db
    apply(database, pid, release(inspector_date="7-25-25", contractor_date="9-6-25",
                                 survey_signed=True, survey_date="7-25-25"))
    assert database.one("SELECT released_on FROM backfill_release")["released_on"] \
        == "2025-07-25"


def test_each_page_of_a_bundle_is_its_own_release(db):
    database, pid = db
    apply(database, pid, release(from_station="0+00", to_station="1+65"), page_no=0)
    apply(database, pid, release(from_station="1+65", to_station="3+20"), page_no=1)
    rows = database.q("SELECT to_station FROM backfill_release ORDER BY page_no")
    assert [r["to_station"] for r in rows] == ["1+65", "3+20"]


def test_replaying_a_page_does_not_duplicate_it(db):
    database, pid = db
    for _ in range(3):
        apply(database, pid, release())
    assert len(database.q("SELECT * FROM backfill_release")) == 1


def test_a_page_that_is_not_a_release_is_skipped(db):
    database, pid = db
    assert apply(database, pid, release(page_is_release=False)) == 0
    assert database.q("SELECT * FROM backfill_release") == []


# -- BF-01 welds after the release ------------------------------------------

def test_welds_before_the_release_pass(db):
    database, pid = db
    apply(database, pid, release())
    weld(database, pid, "AFB-01", "2025-08-01")
    assert fire(database, pid, rules.weld_after_release) == []


def test_a_weld_after_the_release_is_critical(db):
    database, pid = db
    apply(database, pid, release())
    weld(database, pid, "AFB-01", "2025-08-01")
    weld(database, pid, "AFB-02", "2025-09-14")
    found = fire(database, pid, rules.weld_after_release)
    assert len(found) == 1 and found[0]["severity"] == "critical"
    assert "AFB-02" in found[0]["message"] and "AFB-01" not in found[0]["message"]


def test_the_last_release_of_a_segment_is_the_one_that_counts(db):
    # A segment is released in lengths, one form per stretch of ditch.
    database, pid = db
    apply(database, pid, release(inspector_date="08-07-25", contractor_date="08-07-25"),
          page_no=0)
    apply(database, pid, release(inspector_date="09-20-25", contractor_date="09-20-25"),
          page_no=1)
    weld(database, pid, "AFB-02", "2025-09-14")
    assert fire(database, pid, rules.weld_after_release) == []


def test_a_weld_on_another_segment_is_not_covered(db):
    database, pid = db
    apply(database, pid, release())
    weld(database, pid, "GFB-01", "2025-09-14", segment="4IN FG")
    assert fire(database, pid, rules.weld_after_release) == []


def test_an_undated_weld_cannot_be_placed(db):
    database, pid = db
    apply(database, pid, release())
    weld(database, pid, "AFB-02", "")
    assert fire(database, pid, rules.weld_after_release) == []


def test_an_unsigned_release_releases_nothing(db):
    # With no signature there is no date, so nothing is measured against it.
    database, pid = db
    apply(database, pid, release(inspector_signed=False, inspector_date=None,
                                 contractor_signed=False, contractor_date=None))
    weld(database, pid, "AFB-02", "2025-09-14")
    assert fire(database, pid, rules.weld_after_release) == []


def test_a_partly_read_bundle_makes_no_claim(db, monkeypatch):
    # Bluewater files 27 releases in one PDF. Reading page 1 alone would make
    # July look like the end of the job and report every weld after it as
    # buried without a hold point.
    database, pid = db
    monkeypatch.setattr("weldaudit.vision.page_count", lambda path: 27)
    apply(database, pid, release())
    weld(database, pid, "AFB-02", "2025-09-14")
    assert fire(database, pid, rules.weld_after_release) == []
    assert fire(database, pid, rules.nde_after_release) == []


def test_a_fully_read_bundle_is_judged(db, monkeypatch):
    database, pid = db
    monkeypatch.setattr("weldaudit.vision.page_count", lambda path: 2)
    apply(database, pid, release(), page_no=0)
    apply(database, pid, release(), page_no=1)
    weld(database, pid, "AFB-02", "2025-09-14")
    assert len(fire(database, pid, rules.weld_after_release)) == 1


def test_a_page_that_is_not_a_release_still_counts_as_read(db, monkeypatch):
    # The guard used to count releases rather than pages, so a bundle holding
    # anything that is not a release — a cover sheet, a divider, a page the
    # model declined — could never satisfy it, and these rules stayed silent
    # after a complete and correct pass with nothing to say why.
    database, pid = db
    monkeypatch.setattr("weldaudit.vision.page_count", lambda path: 2)
    apply(database, pid, release(), page_no=0)
    apply(database, pid, {"page_is_release": False}, page_no=1)   # a cover
    weld(database, pid, "AFB-02", "2025-09-14")
    assert len(fire(database, pid, rules.weld_after_release)) == 1


def test_a_page_that_errored_is_not_read(db, monkeypatch):
    # A refusal or an unparsable reply is not a reading, and those are the
    # pages most likely to hold something the rest of the bundle does not.
    database, pid = db
    monkeypatch.setattr("weldaudit.vision.page_count", lambda path: 2)
    apply(database, pid, release(), page_no=0)
    database.ocr_put("fp1:backfill:2000", 1, "test-model", {"_error": "refused"})
    weld(database, pid, "AFB-02", "2025-09-14")
    assert fire(database, pid, rules.weld_after_release) == []


# -- BF-02 NDE after the release --------------------------------------------

def test_nde_before_the_release_passes(db):
    database, pid = db
    apply(database, pid, release())
    shot(database, pid, "AFB-01", "2025-08-01")
    assert fire(database, pid, rules.nde_after_release) == []


def test_nde_after_the_release_is_reported(db):
    database, pid = db
    apply(database, pid, release())
    shot(database, pid, "AFB-05", "2025-09-02")
    found = fire(database, pid, rules.nde_after_release)
    assert len(found) == 1 and found[0]["severity"] == "major"
    assert "All NDE is cleared" in found[0]["message"]


# -- BF-03 released over an open reject -------------------------------------

def test_a_clean_segment_passes(db):
    database, pid = db
    apply(database, pid, release())
    weld(database, pid, "AFB-01", "2025-08-01")
    assert fire(database, pid, rules.released_with_open_reject) == []


def test_a_repaired_reject_is_not_open(db):
    database, pid = db
    apply(database, pid, release())
    weld(database, pid, "AFB-01", "2025-08-01", status="REJECT", repair="AFB-01R")
    assert fire(database, pid, rules.released_with_open_reject) == []


def test_an_unrepaired_reject_under_a_closed_ditch_is_critical(db):
    database, pid = db
    apply(database, pid, release())
    weld(database, pid, "AFB-01", "2025-08-01", status="REJECT")
    found = fire(database, pid, rules.released_with_open_reject)
    assert len(found) == 1 and found[0]["severity"] == "critical"
    assert "ditch is now closed over the defect" in found[0]["message"]


# -- BF-04 signatures -------------------------------------------------------

def test_a_fully_signed_release_passes(db):
    database, pid = db
    apply(database, pid, release())
    assert fire(database, pid, rules.release_unsigned) == []


def test_a_missing_contractor_signature_is_reported(db):
    database, pid = db
    apply(database, pid, release(contractor_signed=False, contractor_date=None))
    found = fire(database, pid, rules.release_unsigned)
    assert len(found) == 1 and "contractor's signature" in found[0]["message"]


def test_a_signature_with_no_date_is_reported(db):
    database, pid = db
    apply(database, pid, release(inspector_date=None))
    found = fire(database, pid, rules.release_unsigned)
    assert len(found) == 1 and "inspector's date" in found[0]["message"]


def test_a_form_revision_without_a_survey_line_is_not_missing_one(db):
    # Older forms have two signature lines; later ones add a third.
    database, pid = db
    apply(database, pid, release(survey_signed=False, survey_date=None))
    assert fire(database, pid, rules.release_unsigned) == []


# -- BF-05 signature drift --------------------------------------------------

def test_signatures_on_the_same_day_pass(db):
    database, pid = db
    apply(database, pid, release())
    assert fire(database, pid, rules.signature_drift) == []


def test_signatures_a_day_apart_pass(db):
    database, pid = db
    apply(database, pid, release(inspector_date="08-07-25", contractor_date="08-08-25"))
    assert fire(database, pid, rules.signature_drift) == []


def test_signatures_six_weeks_apart_are_reported(db):
    # The real Bluewater release: inspector and survey 7-25-25, contractor 9-6-25.
    database, pid = db
    apply(database, pid, release(inspector_date="7-25-25", contractor_date="9-6-25",
                                 survey_signed=True, survey_date="7-25-25"))
    found = fire(database, pid, rules.signature_drift)
    assert len(found) == 1 and found[0]["subject"] == "43 days"
    assert "inspector, survey rep signed on 2025-07-25" in found[0]["message"]
    assert "contractor on 2025-09-06" in found[0]["message"]


def test_one_date_alone_cannot_drift(db):
    database, pid = db
    apply(database, pid, release(contractor_signed=False, contractor_date=None))
    assert fire(database, pid, rules.signature_drift) == []


# -- BF-06 unreleased segments ----------------------------------------------

def test_a_welded_segment_with_no_release_is_reported(db):
    database, pid = db
    apply(database, pid, release())
    weld(database, pid, "GFB-01", "2025-07-01", segment="4IN FG")
    found = fire(database, pid, rules.segment_unreleased)
    assert len(found) == 1 and found[0]["segment"] == "4IN FG"


def test_no_releases_read_means_no_claim(db):
    database, pid = db
    weld(database, pid, "GFB-01", "2025-07-01", segment="4IN FG")
    assert fire(database, pid, rules.segment_unreleased) == []


# -- BF-07 extent -----------------------------------------------------------

def test_a_release_stating_its_extent_passes(db):
    database, pid = db
    apply(database, pid, release())
    assert fire(database, pid, rules.release_without_extent) == []


def test_releases_with_no_stations_are_grouped_per_document(db):
    database, pid = db
    apply(database, pid, release(from_station=None), page_no=0)
    apply(database, pid, release(to_station=None), page_no=1)
    found = fire(database, pid, rules.release_without_extent)
    assert len(found) == 1 and found[0]["subject"] == "2 releases"
    assert "pages 1, 2" in found[0]["message"]


# -- the summary ------------------------------------------------------------

def test_the_summary_lists_who_signed(db):
    database, pid = db
    apply(database, pid, release(survey_signed=True, survey_date="8-7-25"))
    row = rules.release_summary(database, pid)[0]
    assert row["signed_by"] == "inspector, contractor, survey"
    assert row["from_station"] == "0+00" and row["released_on"] == "2025-08-07"
