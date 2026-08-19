"""Identifying a letterhead once instead of once per certificate.

Tex-Tubo prints its name as a logotype, and one job spelled it nine ways
across twenty-four certificates. Correcting that page by page is twenty-four
separate acts of judgement about one company.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402
from weldaudit.extract.readings import (  # noqa: E402
    apply_readings, forget, listing, propose, record,
)


@pytest.fixture
def job(tmp_path):
    db = Database(tmp_path / "t.db")
    pid = db.upsert_project("T", str(tmp_path))
    return db, pid


def _cert(db, pid, doc_id, manufacturer, confidence="vision"):
    with db.tx() as c:
        c.execute("""INSERT INTO document(id, project_id, path, filename, ext,
                                          fingerprint, segment, kind)
                     VALUES(?,?,?,?,'.pdf',?, 'S','mtr')""",
                  (doc_id, pid, f"c{doc_id}.pdf", f"c{doc_id}.pdf", f"fp{doc_id}"))
        c.execute("""INSERT INTO material(project_id, document_id, segment, heat,
                                          heat_key, source, manufacturer, confidence)
                     VALUES(?,?, 'S', ?, ?, 'mtr_file', ?, ?)""",
                  (pid, doc_id, f"H{doc_id}", f"H{doc_id}", manufacturer, confidence))


# -- the point of it ---------------------------------------------------------

def test_one_entry_covers_every_certificate_with_that_reading(job):
    db, pid = job
    for i, name in enumerate(("TEXTUBOO", "TEXTUBOO", "TEXTUBOO"), start=1):
        _cert(db, pid, i, name)
    record(db, pid, "TEXTUBOO", "Tex Tubo")
    assert apply_readings(db, pid) == 3
    assert {r["manufacturer"] for r in db.q("SELECT manufacturer FROM material")} \
        == {"Tex Tubo"}


def test_punctuation_is_not_a_separate_entry(job):
    """'tex-tubo', 'TEX TUBO' and 'tex tubo.' are one letterhead."""
    db, pid = job
    _cert(db, pid, 1, "tex-tubo")
    _cert(db, pid, 2, "TEX TUBO")
    record(db, pid, "Tex Tubo", "Tex Tubo Inc")
    assert apply_readings(db, pid) == 2


def test_it_never_overwrites_a_value_typed_against_a_page(job):
    """A person looked at that specific certificate; a rule about a name did
    not. The page wins."""
    db, pid = job
    _cert(db, pid, 1, "TEXTUBOO", confidence="human")
    record(db, pid, "TEXTUBOO", "Tex Tubo")
    assert apply_readings(db, pid) == 0
    assert db.one("SELECT manufacturer FROM material")["manufacturer"] == "TEXTUBOO"


def test_an_entry_can_be_withdrawn(job):
    db, pid = job
    record(db, pid, "TEXTUBOO", "Tex Tubo")
    forget(db, pid, "TEXTUBOO")
    assert listing(db, pid) == []


def test_recording_it_twice_replaces_rather_than_duplicates(job):
    db, pid = job
    record(db, pid, "TEXTUBOO", "Tex Tub")
    record(db, pid, "TEXTUBOO", "Tex Tubo")
    entries = listing(db, pid)
    assert len(entries) == 1 and entries[0]["manufacturer"] == "Tex Tubo"


def test_it_is_scoped_to_one_job(job, tmp_path):
    """A misread is a fact about this contractor's scanner, not about the world.
    The global alias file is for real names; this must not leak between jobs."""
    db, pid = job
    other = db.upsert_project("Other", str(tmp_path / "o"))
    _cert(db, pid, 1, "TEXTUBOO")
    record(db, other, "TEXTUBOO", "Something Else")
    assert apply_readings(db, pid) == 0


# -- proposing is not recording ---------------------------------------------

def test_similar_names_are_offered(job):
    db, pid = job
    for i, name in enumerate(("TEKCUBEO", "TEXQUBEO", "tex-tubo.com"), start=1):
        _cert(db, pid, i, name)
    offered = {name for name, _n, _s in propose(db, pid, "TEXTUBOO")}
    assert offered == {"TEKCUBEO", "TEXQUBEO", "tex-tubo.com"}


def test_a_different_mill_in_the_same_family_is_not_offered(job):
    """The filename family holding the Tex-Tubo certificates also holds a
    Borusan Mannesmann one. Clustering by filename would have relabelled it;
    clustering by name does not."""
    db, pid = job
    _cert(db, pid, 1, "TEKCUBEO")
    _cert(db, pid, 2, "BORUSAN MANNESMANN")
    offered = {name for name, _n, _s in propose(db, pid, "TEXTUBOO")}
    assert offered == {"TEKCUBEO"}


def test_proposing_records_nothing(job):
    db, pid = job
    _cert(db, pid, 1, "TEKCUBEO")
    propose(db, pid, "TEXTUBOO")
    assert listing(db, pid) == []
    assert apply_readings(db, pid) == 0


def test_a_name_already_settled_is_not_offered_again(job):
    db, pid = job
    _cert(db, pid, 1, "TEKCUBEO")
    record(db, pid, "TEKCUBEO", "Tex Tubo")
    assert propose(db, pid, "TEXTUBOO") == []
