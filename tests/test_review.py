"""VIS-01: values the close-ups could not settle.

This rule reports a hole in the evidence rather than a defect in the work, so
what matters is that it fires only where the hole costs something, and that
the message tells someone which page to open.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit import vision  # noqa: E402
from weldaudit.db import Database  # noqa: E402
from weldaudit.extract import vision_pass  # noqa: E402
from weldaudit.extract.vision_pass import Target, _record_conflicts  # noqa: E402
from weldaudit.rules import review  # noqa: E402


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "t.db")
    pid = database.upsert_project("T", str(tmp_path))
    with database.tx() as c:
        c.execute(
            """INSERT INTO document(id, project_id, path, filename, ext,
                                    fingerprint, segment, kind)
               VALUES(1, ?, 'm.pdf', 'KANDAL.pdf', '.pdf', 'fp1', 'SEG A', 'mtr')""",
            (pid,),
        )
    return database, pid


def _target():
    return Target(1, "m.pdf", "KANDAL.pdf", "fp1", 1, "test", "SEG A")


def _payload(**notes):
    return {"page_is_certificate": True,
            "_tiles": 4,
            "_tiles_disagreed": notes}


# -- what gets filed --------------------------------------------------------

def test_an_unsettled_decisive_field_reaches_a_human(db):
    database, pid = db
    flagged = _record_conflicts(
        database, pid, "mtr", _target(),
        _payload(heat={"readings": ["24913", "13587"], "chose": None}), 0)
    assert flagged == 1

    findings = review.unsettled_readings(database, pid, "run1")
    assert len(findings) == 1
    assert findings[0]["rule"] == "VIS-01"
    assert findings[0]["severity"] == "major"
    # The message has to be actionable on its own: which file, which page,
    # and what the two readings were.
    message = findings[0]["message"]
    assert "KANDAL.pdf" in message and "page 1" in message
    assert "'24913'" in message and "'13587'" in message


def test_a_disagreement_the_majority_settled_is_filed_but_not_raised(db):
    """It is worth knowing later; it is not worth anyone's time now."""
    database, pid = db
    flagged = _record_conflicts(
        database, pid, "mtr", _target(),
        _payload(heat={"readings": ["24913", "13.15", "24913"], "chose": "24913"}), 0)
    assert flagged == 0
    assert review.unsettled_readings(database, pid, "run1") == []
    assert database.one("SELECT COUNT(*) n FROM vision_conflict")["n"] == 1


def test_an_incidental_field_is_filed_but_not_raised(db):
    """Close-ups disagree ~0.8 times per page; only the costly ones are raised."""
    database, pid = db
    flagged = _record_conflicts(
        database, pid, "mtr", _target(),
        _payload(country_of_melt={"readings": ["USA", "US"], "chose": None}), 0)
    assert flagged == 0
    assert review.unsettled_readings(database, pid, "run1") == []
    assert database.one("SELECT COUNT(*) n FROM vision_conflict")["n"] == 1


def test_a_page_that_agreed_files_nothing(db):
    database, pid = db
    assert _record_conflicts(database, pid, "mtr", _target(),
                             {"page_is_certificate": True, "_tiles": 4}, 0) == 0
    assert database.one("SELECT COUNT(*) n FROM vision_conflict")["n"] == 0


# -- how it reads -----------------------------------------------------------

def test_one_finding_per_document_and_field_not_per_page(db):
    """A sixteen-page bundle with an unreadable ticket is one thing to check."""
    database, pid = db
    for page in range(16):
        _record_conflicts(database, pid, "mtr", _target(),
                          _payload(heat={"readings": ["A", "B"], "chose": None}),
                          page)
    findings = review.unsettled_readings(database, pid, "run1")
    assert len(findings) == 1
    assert "and 10 more" in findings[0]["message"]


def test_two_fields_on_one_page_are_two_things_to_check(db):
    database, pid = db
    _record_conflicts(
        database, pid, "mtr", _target(),
        _payload(heat={"readings": ["A", "B"], "chose": None},
                 issuing_company={"readings": ["X", "Y"], "chose": None}), 0)
    assert len(review.unsettled_readings(database, pid, "run1")) == 2


def test_the_readings_survive_a_round_trip_through_the_table(db):
    database, pid = db
    _record_conflicts(database, pid, "mtr", _target(),
                      _payload(heat={"readings": ["24913", "13587"], "chose": None}), 2)
    row = database.one("SELECT * FROM vision_conflict")
    assert json.loads(row["readings"]) == ["24913", "13587"]
    assert row["chosen"] is None and row["page_no"] == 3      # 1-based


# -- the map that decides what counts ---------------------------------------

def test_every_decisive_field_is_a_field_that_exists():
    """A typo here raises findings about a value no rule reads, or none at all."""
    for kind, names in vision.DECISIVE_FIELDS.items():
        assert kind in vision.SCHEMAS, kind
        assert names <= set(vision.SCHEMAS[kind]["properties"]), kind


def test_the_tiled_kinds_all_declare_decisive_fields():
    """Tiling a kind with nothing decisive would record conflicts and raise none."""
    assert vision.TILED_KINDS <= set(vision.DECISIVE_FIELDS)


# -- surviving a re-audit ---------------------------------------------------

def _make_it_replayable(database, pid, monkeypatch):
    """Replay finds MTR pages through the material rows they produced."""
    from weldaudit.extract import vision_pass

    with database.tx() as c:
        c.execute(
            """INSERT INTO material(project_id, document_id, segment, heat,
                                    heat_key, source)
               VALUES(?, 1, 'SEG A', 'H1', 'H1', 'mtr_file')""",
            (pid,),
        )
    monkeypatch.setattr(vision_pass, "page_count", lambda _p: 1)
    monkeypatch.setitem(vision_pass._APPLIERS, "mtr", lambda *a: 0)


def test_conflicts_come_back_after_a_reindex(db, tmp_path, monkeypatch):
    """Re-indexing clears the table; replay must refill it.

    An empty review list reads as "nothing to check", not "the record was
    thrown away", so losing these on the second audit is worse than never
    having recorded them.
    """
    from weldaudit.extract import vision_pass

    database, pid = db
    # The letterhead, not the heat: _make_it_replayable gives the document a
    # heat from its filename, which makes a heat conflict cost nothing.
    payload = _payload(issuing_company={"readings": ["MRC Global", "ORTEGA"],
                                        "chose": None})
    database.ocr_put("fp1:mtr:2000:2x2:v1", 0, "claude-haiku-4-5", payload)
    _record_conflicts(database, pid, "mtr", _target(), payload, 0)
    assert len(review.unsettled_readings(database, pid, "run1")) == 1

    with database.tx() as c:                       # what re-indexing does
        c.execute("DELETE FROM vision_conflict WHERE project_id=?", (pid,))
    assert review.unsettled_readings(database, pid, "run1") == []

    _make_it_replayable(database, pid, monkeypatch)
    vision_pass.replay(database, pid, kinds=("mtr",))
    assert len(review.unsettled_readings(database, pid, "run1")) == 1


def test_replaying_twice_does_not_double_the_review_list(db, monkeypatch):
    from weldaudit.extract import vision_pass

    database, pid = db
    payload = _payload(heat={"readings": ["24913", "13587"], "chose": None})
    database.ocr_put("fp1:mtr:2000:2x2:v1", 0, "claude-haiku-4-5", payload)
    _make_it_replayable(database, pid, monkeypatch)
    vision_pass.replay(database, pid, kinds=("mtr",))
    vision_pass.replay(database, pid, kinds=("mtr",))
    assert database.one("SELECT COUNT(*) n FROM vision_conflict")["n"] == 1


# -- a place must never become the manufacturer -----------------------------

def test_a_town_in_mill_name_does_not_become_the_manufacturer(db):
    """mill outranks the letterhead, so junk there is what the AML gets asked."""
    from weldaudit.extract.vision_pass import _apply_mtr

    database, pid = db
    with database.tx() as c:
        c.execute(
            """INSERT INTO material(project_id, document_id, segment, heat,
                                    heat_key, source)
               VALUES(?, 1, 'SEG A', '24913', '24913', 'mtr_file')""", (pid,))
    _apply_mtr(database, pid, _target(), {
        "page_is_certificate": True,
        "issuing_company": "Kandal Pipe USA, Inc",
        "mill_name": "Mingo Junction, OH",
    }, 0)
    row = database.one("SELECT manufacturer, mill_name FROM material")
    assert row["manufacturer"] == "Kandal Pipe USA, Inc"
    assert row["mill_name"] is None


def test_a_real_second_company_still_outranks_the_letterhead(db):
    """The distinction exists because some certificates do name a separate mill."""
    from weldaudit.extract.vision_pass import _apply_mtr

    database, pid = db
    with database.tx() as c:
        c.execute(
            """INSERT INTO material(project_id, document_id, segment, heat,
                                    heat_key, source)
               VALUES(?, 1, 'SEG A', 'H1', 'H1', 'mtr_file')""", (pid,))
    _apply_mtr(database, pid, _target(), {
        "page_is_certificate": True,
        "issuing_company": "Some Distributor LLC",
        "mill_name": "Norvale Dalmine",
    }, 0)
    assert database.one("SELECT manufacturer FROM material")["manufacturer"] == "Norvale Dalmine"


# -- the model reports where it looked; the policy is applied here ----------

def test_a_mill_name_off_a_supplier_line_is_not_the_manufacturer(db):
    """Valvitalia forged the elbow; Tubos Reunidos supplied the pipe stock.

    Tubos Reunidos is on the AML, so recording it as the maker produces a
    clean 'approved' verdict for a company that did not make the item — the
    worst outcome this tool has, because nothing about it looks wrong.
    """
    from weldaudit.extract.vision_pass import _apply_mtr

    database, pid = db
    with database.tx() as c:
        c.execute(
            """INSERT INTO material(project_id, document_id, segment, heat,
                                    heat_key, source)
               VALUES(?, 1, 'SEG A', '130740', '130740', 'mtr_file')""", (pid,))
    _apply_mtr(database, pid, _target(), {
        "page_is_certificate": True,
        "issuing_company": "VALVITALIA S.p.A. - Tecnoforge Division",
        "mill_name": "TUBOS REUNIDOS GROUP S.L.U.",
        "mill_source": "supplier_line",
    }, 0)
    row = database.one("SELECT manufacturer, mill_name FROM material")
    assert row["manufacturer"] == "VALVITALIA S.p.A. - Tecnoforge Division"
    assert row["mill_name"] is None


def test_a_works_line_still_names_the_producer(db):
    """The distinction is the point: some certificates do name a separate mill."""
    from weldaudit.extract.vision_pass import _apply_mtr

    database, pid = db
    with database.tx() as c:
        c.execute(
            """INSERT INTO material(project_id, document_id, segment, heat,
                                    heat_key, source)
               VALUES(?, 1, 'SEG A', 'H1', 'H1', 'mtr_file')""", (pid,))
    _apply_mtr(database, pid, _target(), {
        "page_is_certificate": True,
        "issuing_company": "Some Distributor LLC",
        "mill_name": "Norvale Dalmine",
        "mill_source": "works_line",
    }, 0)
    assert database.one("SELECT manufacturer FROM material")["manufacturer"] == "Norvale Dalmine"


def test_a_mill_name_that_merely_repeats_the_letterhead_is_not_separate(db):
    from weldaudit.extract.vision_pass import _apply_mtr

    database, pid = db
    with database.tx() as c:
        c.execute(
            """INSERT INTO material(project_id, document_id, segment, heat,
                                    heat_key, source)
               VALUES(?, 1, 'SEG A', 'H1', 'H1', 'mtr_file')""", (pid,))
    _apply_mtr(database, pid, _target(), {
        "page_is_certificate": True,
        "issuing_company": "ORTEGA FORJA, S.COOP.",
        "mill_name": "ORTEGA FORJA, S.COOP.",
        "mill_source": "letterhead",
    }, 0)
    row = database.one("SELECT manufacturer, mill_name FROM material")
    assert row["manufacturer"] == "ORTEGA FORJA, S.COOP." and row["mill_name"] is None


def test_a_letterhead_in_mill_name_is_moved_not_discarded(db):
    """BQN's certificate came back with the only company name in mill_name."""
    from weldaudit.extract.vision_pass import _apply_mtr

    database, pid = db
    with database.tx() as c:
        c.execute(
            """INSERT INTO material(project_id, document_id, segment, heat,
                                    heat_key, source)
               VALUES(?, 1, 'SEG A', 'H1', 'H1', 'mtr_file')""", (pid,))
    _apply_mtr(database, pid, _target(), {
        "page_is_certificate": True,
        "issuing_company": None,
        "mill_name": "BQN Forgings Private Limited",
        "mill_source": "letterhead",
    }, 0)
    row = database.one("SELECT manufacturer, issuing_company FROM material")
    assert row["manufacturer"] == "BQN Forgings Private Limited"
    assert row["issuing_company"] == "BQN Forgings Private Limited"


def test_an_unlabelled_mill_name_is_still_taken(db):
    """A model that fills mill_name but not mill_source must not lose the mill."""
    from weldaudit.extract.vision_pass import _apply_mtr

    database, pid = db
    with database.tx() as c:
        c.execute(
            """INSERT INTO material(project_id, document_id, segment, heat,
                                    heat_key, source)
               VALUES(?, 1, 'SEG A', 'H1', 'H1', 'mtr_file')""", (pid,))
    _apply_mtr(database, pid, _target(), {
        "page_is_certificate": True,
        "issuing_company": "Some Distributor LLC",
        "mill_name": "Norvale Dalmine",
        "mill_source": None,
    }, 0)
    assert database.one("SELECT manufacturer FROM material")["manufacturer"] == "Norvale Dalmine"


# -- VIS-02/03: a manufacturer name is only as good as the scan it came from --

def _aml(database, pid, *names):
    # norm_name through the real normaliser, because that is the key the
    # matcher looks names up by — a plain lower() builds an AML nothing hits.
    from weldaudit.aml import normalise_manufacturer

    with database.tx() as c:
        for n in names:
            c.execute(
                """INSERT INTO aml_entry(project_id, category, manufacturer,
                                         location, limits_raw, conditions, norm_name)
                   VALUES(?, 'Fittings', ?, '', '', '', ?)""",
                (pid, n, normalise_manufacturer(n)))


def _material(database, pid, manufacturer, confidence="vision", heat="H1"):
    with database.tx() as c:
        c.execute(
            """INSERT INTO material(project_id, document_id, segment, heat,
                                    heat_key, source, manufacturer, confidence)
               VALUES(?, 1, 'SEG A', ?, ?, 'mtr_file', ?, ?)""",
            (pid, heat, heat, manufacturer, confidence))


def test_a_settled_but_disputed_letterhead_is_still_reported(db):
    """VIS-01 covers names that were lost; this covers names that were guessed."""
    database, pid = db
    _material(database, pid, "Besteel Pipe USA, Inc")
    _record_conflicts(database, pid, "mtr", _target(), _payload(
        issuing_company={"readings": ["Besteel Pipe USA, Inc", "Kandal Pipe USA, Inc"],
                         "chose": "Besteel Pipe USA, Inc"}), 0)
    findings = review.disputed_manufacturer_name(database, pid, "run1")
    assert len(findings) == 1
    assert "Besteel" in findings[0]["message"] and "Kandal" in findings[0]["message"]


def test_a_letterhead_every_close_up_agreed_on_is_not_reported(db):
    database, pid = db
    _material(database, pid, "ORTEGA FORJA, S.COOP.")
    assert review.disputed_manufacturer_name(database, pid, "run1") == []


def test_an_ocr_name_approved_on_a_fuzzy_match_is_flagged(db):
    """'BQM Forgings' is one letter from 'BQN Forgings' and approves at 91."""
    database, pid = db
    _aml(database, pid, "BQN Forgings Pvt Ltd")
    _material(database, pid, "BQM Forgings Pvt Ltd")
    findings = review.scanned_name_approved_loosely(database, pid, "run1")
    assert len(findings) == 1
    assert "BQM Forgings Pvt Ltd" in findings[0]["subject"]


def test_an_exact_match_is_not_flagged(db):
    database, pid = db
    _aml(database, pid, "Rivermark")
    _material(database, pid, "Rivermark")
    assert review.scanned_name_approved_loosely(database, pid, "run1") == []


def test_a_name_from_a_spreadsheet_is_not_second_guessed(db):
    """This guard is about scans; an exported name did not go through a model."""
    database, pid = db
    _aml(database, pid, "BQN Forgings Pvt Ltd")
    _material(database, pid, "BQN Forgings Pvt Ltd", confidence="filename")
    assert review.scanned_name_approved_loosely(database, pid, "run1") == []


def test_a_name_the_aml_does_not_approve_is_left_to_the_mtr_rules(db):
    """MTR-02 and MTR-03 already report those; this rule is about silent passes."""
    database, pid = db
    _aml(database, pid, "Norvale Dalmine")
    _material(database, pid, "Completely Different Pipe Co")
    assert review.scanned_name_approved_loosely(database, pid, "run1") == []


# -- not every decisive field is decisive on every document ------------------

def test_a_heat_the_filename_already_gave_is_not_worth_a_human(db):
    """_apply_mtr discards a scanned heat when the filename supplied one."""
    database, pid = db
    with database.tx() as c:
        c.execute(
            """INSERT INTO material(project_id, document_id, segment, heat,
                                    heat_key, source)
               VALUES(?, 1, 'SEG A', '24179651', '24179651', 'mtr_file')""", (pid,))
    flagged = _record_conflicts(database, pid, "mtr", _target(),
                                _payload(heat={"readings": ["24179651", "825680246"],
                                               "chose": None}), 0)
    assert flagged == 0
    assert review.unsettled_readings(database, pid, "run1") == []
    # Still filed: it is the first thing worth seeing if the heat turns out wrong.
    assert database.one("SELECT COUNT(*) n FROM vision_conflict")["n"] == 1


def test_a_heat_nothing_else_supplies_is_still_raised(db):
    """MTR-06 exists because certificates do arrive with no heat in the name."""
    database, pid = db
    with database.tx() as c:
        c.execute(
            """INSERT INTO material(project_id, document_id, segment, heat,
                                    heat_key, source)
               VALUES(?, 1, 'SEG A', '', '', 'mtr_file')""", (pid,))
    flagged = _record_conflicts(database, pid, "mtr", _target(),
                                _payload(heat={"readings": ["A", "B"], "chose": None}), 0)
    assert flagged == 1
    assert len(review.unsettled_readings(database, pid, "run1")) == 1


def test_the_letterhead_is_always_worth_a_human(db):
    """Nothing but the certificate supplies the manufacturer."""
    database, pid = db
    with database.tx() as c:
        c.execute(
            """INSERT INTO material(project_id, document_id, segment, heat,
                                    heat_key, source)
               VALUES(?, 1, 'SEG A', 'H1', 'H1', 'mtr_file')""", (pid,))
    flagged = _record_conflicts(
        database, pid, "mtr", _target(),
        _payload(issuing_company={"readings": ["MRC Global", "ORTEGA"], "chose": None}), 0)
    assert flagged == 1


# -- the corpus knows who supplies steel and who makes things ----------------

def _cert_payload(database, pid, fingerprint, mill, source, doc_id=1):
    with database.tx() as c:
        c.execute(
            """INSERT OR REPLACE INTO document(id, project_id, path, filename,
                                               ext, fingerprint, kind)
               VALUES(?, ?, 'c.pdf', 'c.pdf', '.pdf', ?, 'mtr')""",
            (doc_id, pid, fingerprint))
    database.ocr_put(f"{fingerprint}:mtr:2000:2x2:v1", 0, "claude-haiku-4-5",
                     {"page_is_certificate": True, "mill_name": mill,
                      "mill_source": source})


def test_a_company_called_a_supplier_and_never_a_works_is_a_supplier(db):
    database, pid = db
    _cert_payload(database, pid, "fpA", "Big River Steel", "supplier_line")
    assert "big river steel" in vision_pass.supplier_roles(database, pid)


def test_a_company_some_certificate_calls_a_works_is_left_alone(db):
    """Calderon supplies steel on a fitting cert and makes its own pipe."""
    database, pid = db
    _cert_payload(database, pid, "fpA", "Calderon", "supplier_line", doc_id=1)
    _cert_payload(database, pid, "fpB", "Calderon", "works_line", doc_id=2)
    assert "calderon" not in vision_pass.supplier_roles(database, pid)


def test_an_unlabelled_supplier_loses_the_manufacturer_to_the_letterhead(db):
    database, pid = db
    _cert_payload(database, pid, "fpA", "Big River Steel", "supplier_line")
    with database.tx() as c:
        c.execute(
            """INSERT INTO material(project_id, document_id, segment, heat,
                                    heat_key, source, manufacturer,
                                    issuing_company, mill_name, confidence)
               VALUES(?, 1, 'SEG A', 'H1', 'H1', 'mtr_file', 'Big River Steel',
                      'Kandal Pipe USA, Inc', 'Big River Steel', 'vision')""",
            (pid,))
    assert vision_pass.demote_known_suppliers(database, pid) == 1
    row = database.one("SELECT manufacturer, mill_name FROM material")
    assert row["manufacturer"] == "Kandal Pipe USA, Inc" and row["mill_name"] is None


def test_with_no_letterhead_the_manufacturer_is_cleared_not_kept(db):
    """Blank reports as MTR-08. A supplier's name reports as approved."""
    database, pid = db
    _cert_payload(database, pid, "fpA", "TUBOS REUNIDOS GROUP S.L.U.", "supplier_line")
    with database.tx() as c:
        c.execute(
            """INSERT INTO material(project_id, document_id, segment, heat,
                                    heat_key, source, manufacturer,
                                    issuing_company, mill_name, confidence)
               VALUES(?, 1, 'SEG A', 'H1', 'H1', 'mtr_file',
                      'TUBOS REUNIDOS GROUP S.L.U.', NULL,
                      'TUBOS REUNIDOS GROUP S.L.U.', 'vision')""",
            (pid,))
    assert vision_pass.demote_known_suppliers(database, pid) == 1
    assert database.one("SELECT manufacturer FROM material")["manufacturer"] == ""


def test_a_letterhead_that_already_won_is_not_disturbed(db):
    database, pid = db
    _cert_payload(database, pid, "fpA", "Big River Steel", "supplier_line")
    with database.tx() as c:
        c.execute(
            """INSERT INTO material(project_id, document_id, segment, heat,
                                    heat_key, source, manufacturer,
                                    issuing_company, mill_name, confidence)
               VALUES(?, 1, 'SEG A', 'H1', 'H1', 'mtr_file', 'Tex-Tubo',
                      'Tex-Tubo', 'Big River Steel', 'vision')""", (pid,))
    assert vision_pass.demote_known_suppliers(database, pid) == 0
    assert database.one("SELECT manufacturer FROM material")["manufacturer"] == "Tex-Tubo"


def test_a_name_from_a_text_layer_is_not_demoted(db):
    """Those are AML-verified letterheads, not a model's pick between blocks."""
    database, pid = db
    _cert_payload(database, pid, "fpA", "Big River Steel", "supplier_line")
    with database.tx() as c:
        c.execute(
            """INSERT INTO material(project_id, document_id, segment, heat,
                                    heat_key, source, manufacturer,
                                    issuing_company, mill_name, confidence)
               VALUES(?, 1, 'SEG A', 'H1', 'H1', 'mtr_file', 'Big River Steel',
                      'X', 'Big River Steel', 'text')""", (pid,))
    assert vision_pass.demote_known_suppliers(database, pid) == 0


# -- not asserting on evidence we have already doubted ----------------------

def test_no_critical_non_approval_on_a_name_read_two_ways(db):
    """MTR-02 calls material unapproved. VIS-02 says the name is not reliably
    legible. Both at once is a contradiction, and it was the shape of
    seventeen of the sixty-six findings hand-checked."""
    from weldaudit.rules import materials

    database, pid = db
    _material(database, pid, "Tekkubeo")          # a misread of Tex-Tubo
    _aml(database, pid, "Tex Tubo")
    # Without a recorded dispute, the finding stands.
    assert len(materials.manufacturer_not_approved(database, pid, "r1")) == 1

    _record_conflicts(database, pid, "mtr", _target(), _payload(
        issuing_company={"readings": ["Tekkubeo", "Tex-Tubo"],
                         "chose": "Tekkubeo"}), 0)
    assert materials.manufacturer_not_approved(database, pid, "r1") == []
    # And the page is still reported, by the rule that can act on it.
    assert len(review.disputed_manufacturer_name(database, pid, "r1")) == 1


def test_a_name_every_close_up_agreed_on_is_still_checked(db):
    """Deferring must not become a blanket amnesty for vision readings."""
    from weldaudit.rules import materials

    database, pid = db
    _aml(database, pid, "Norvale Dalmine")
    _material(database, pid, "Completely Different Pipe Co")
    assert len(materials.manufacturer_not_approved(database, pid, "r1")) == 1


# -- two readers, one page, different companies -----------------------------

def _cached(database, pid, fingerprint, model, company, doc_id=1, filename="c.pdf"):
    with database.tx() as c:
        c.execute(
            """INSERT OR REPLACE INTO document(id, project_id, path, filename,
                                               ext, fingerprint, segment, kind)
               VALUES(?,?,'c.pdf',?,'.pdf',?,'S','mtr')""",
            (doc_id, pid, filename, fingerprint))
    database.ocr_put(f"{fingerprint}:mtr:2000:x", 0, model,
                     {"page_is_certificate": True, "issuing_company": company})


def test_readers_that_name_different_companies_are_recorded(db):
    """A confident misread is invisible to every other guard: the close-ups
    agree, so VIS-02 sees nothing. A second reader is the only witness."""
    database, pid = db
    _cached(database, pid, "fpA", "claude-haiku-4-5", "TECKCUBO")
    _cached(database, pid, "fpA", "local:ocr", "Tex Tubo")
    with database.tx() as c:               # VIS-02 reports credited material
        c.execute(
            """INSERT INTO material(project_id, document_id, segment, heat,
                                    heat_key, source, manufacturer, confidence)
               VALUES(?, 1, 'S', 'H1', 'H1', 'mtr_file', 'TECKCUBO', 'vision')""",
            (pid,))
    assert vision_pass.note_reader_disagreements(database, pid) == 1
    assert len(review.disputed_manufacturer_name(database, pid, "r1")) == 1


def test_the_same_company_spelled_two_ways_is_not_a_disagreement(db):
    """The AML lookup they feed is fuzzy and resolves both alike."""
    database, pid = db
    _cached(database, pid, "fpA", "claude-haiku-4-5", "DELMAR FLOW CONTROLS PVT LTD.")
    _cached(database, pid, "fpA", "local:ocr", "Delmar Flow Controls")
    assert vision_pass.note_reader_disagreements(database, pid) == 0


def test_one_reader_alone_is_not_a_disagreement(db):
    database, pid = db
    _cached(database, pid, "fpA", "claude-haiku-4-5", "TECKCUBO")
    assert vision_pass.note_reader_disagreements(database, pid) == 0


def test_recording_it_twice_does_not_double_the_review_list(db):
    database, pid = db
    _cached(database, pid, "fpA", "claude-haiku-4-5", "TECKCUBO")
    _cached(database, pid, "fpA", "local:ocr", "Tex Tubo")
    vision_pass.note_reader_disagreements(database, pid)
    vision_pass.note_reader_disagreements(database, pid)
    assert database.one("SELECT COUNT(*) n FROM vision_conflict")["n"] == 1
