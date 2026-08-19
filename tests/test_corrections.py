"""What a person read off the page, and why it has to outrank everything.

Some values cannot be recovered by machine at any resolution. Tex-Tubo's
letterhead is a logotype, and a vision model reads it as TECKCUBO, TEKSUMEO or
Tekube depending on the page while OCR returns nothing — seven certificates,
seven critical findings, all against approved material. VIS-02 told the
auditor to read the letterhead and enter the company, and there was nowhere to
enter it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402
from weldaudit.extract.corrections import (  # noqa: E402
    CORRECTABLE, apply_corrections, listing, record,
)


@pytest.fixture
def job(tmp_path):
    db = Database(tmp_path / "t.db")
    pid = db.upsert_project("T", str(tmp_path))
    with db.tx() as c:
        # One certificate, filed into two segment books — the usual shape.
        for doc_id, segment in ((1, "6 FG"), (2, "20 LP")):
            c.execute(
                """INSERT INTO document(id, project_id, path, filename, ext,
                                        fingerprint, segment, kind)
                   VALUES(?,?,?,'24015852 6 280 X52.pdf','.pdf','fpTEX',?,'mtr')""",
                (doc_id, pid, f"c{doc_id}.pdf", segment))
            c.execute(
                """INSERT INTO material(project_id, document_id, segment, heat,
                                        heat_key, source, manufacturer, confidence)
                   VALUES(?,?,?,'24015852','24015852','mtr_file','TECKCUBO','vision')""",
                (pid, doc_id, segment))
    return db, pid


def test_a_corrected_value_overrides_every_reader(job):
    db, pid = job
    record(db, pid, "fpTEX", "manufacturer", "Tex Tubo")
    assert apply_corrections(db, pid) == 2
    for r in db.q("SELECT manufacturer, confidence FROM material"):
        assert r["manufacturer"] == "Tex Tubo"
        assert r["confidence"] == "human"


def test_one_correction_covers_every_filing_copy(job):
    """The same certificate is filed into several segment books. Keying on the
    fingerprint means the auditor reads the letterhead once, not once a book."""
    db, pid = job
    record(db, pid, "fpTEX", "manufacturer", "Tex Tubo")
    apply_corrections(db, pid)
    assert {r["segment"] for r in db.q(
        "SELECT segment FROM material WHERE manufacturer='Tex Tubo'")} == {"6 FG", "20 LP"}


def test_a_correction_survives_re_indexing(job):
    """Re-indexing wipes every project table. A value a person typed cannot be
    rebuilt by reading the documents again, so it must not be in that wipe."""
    db, pid = job
    record(db, pid, "fpTEX", "manufacturer", "Tex Tubo")
    with db.tx() as c:                     # what index.py does to a project
        c.execute("DELETE FROM material WHERE project_id=?", (pid,))
        c.execute("DELETE FROM finding WHERE project_id=?", (pid,))
    assert db.one("SELECT COUNT(*) n FROM correction")["n"] == 1

    with db.tx() as c:                     # extraction rebuilds the row
        c.execute(
            """INSERT INTO material(project_id, document_id, segment, heat,
                                    heat_key, source, manufacturer, confidence)
               VALUES(?,1,'6 FG','24015852','24015852','mtr_file','TECKCUBO','vision')""",
            (pid,))
    assert apply_corrections(db, pid) == 1
    assert db.one("SELECT manufacturer FROM material")["manufacturer"] == "Tex Tubo"


def test_correcting_twice_replaces_rather_than_duplicates(job):
    db, pid = job
    record(db, pid, "fpTEX", "manufacturer", "Tex Tub")      # a typo
    record(db, pid, "fpTEX", "manufacturer", "Tex Tubo")     # fixed
    assert len(listing(db, pid)) == 1
    apply_corrections(db, pid)
    assert db.one("SELECT manufacturer FROM material")["manufacturer"] == "Tex Tubo"


def test_a_correction_can_be_withdrawn(job):
    db, pid = job
    record(db, pid, "fpTEX", "manufacturer", "Tex Tubo")
    record(db, pid, "fpTEX", "manufacturer", None)
    assert listing(db, pid) == []
    assert apply_corrections(db, pid) == 0


def test_only_named_fields_can_be_corrected(job):
    """This overwrites every automated reader, so each correctable field is a
    field where a typo silently becomes the truth."""
    db, pid = job
    with pytest.raises(ValueError):
        record(db, pid, "fpTEX", "severity", "minor")
    assert "manufacturer" in CORRECTABLE


def test_the_note_is_kept_for_whoever_reads_it_later(job):
    db, pid = job
    record(db, pid, "fpTEX", "manufacturer", "Tex Tubo",
           note="letterhead is a logotype; read by eye")
    entry = listing(db, pid)[0]
    assert "logotype" in entry["note"]
    assert entry["filename"] == "24015852 6 280 X52.pdf"


def test_a_corrected_name_is_exempt_from_the_doubts_about_readers(job):
    """VIS-03 second-guesses names a model read; the supplier demotion and the
    disputed-name deferral both look for confidence='vision'. A value somebody
    read off the page has already answered the doubt those encode."""
    from weldaudit.extract.vision_pass import demote_known_suppliers

    db, pid = job
    record(db, pid, "fpTEX", "manufacturer", "Big River Steel")
    apply_corrections(db, pid)
    # Even a name the corpus calls a steel supplier stands, once a person
    # has said it is the maker of this item.
    demote_known_suppliers(db, pid)
    assert db.one("SELECT manufacturer FROM material")["manufacturer"] == "Big River Steel"


def test_nothing_happens_when_no_one_has_corrected_anything(job):
    db, pid = job
    assert apply_corrections(db, pid) == 0
    assert db.one("SELECT manufacturer FROM material")["manufacturer"] == "TECKCUBO"
