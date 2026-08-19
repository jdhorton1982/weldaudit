"""A bill of lading is not a certificate filed without a heat.

Their filenames are dates -- `8-13-25 PIPE.pdf` -- and the extractor used to
read `8` out of one and record it as a heat. Correcting that left them with no
heat, which is right, and MTR-06 then reported eight certificates with
unreadable heat numbers. They are delivery notes. Asking one which melt the
steel came from is asking the wrong document.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402
from weldaudit.rules.materials import certificate_without_heat  # noqa: E402


@pytest.fixture
def job(tmp_path):
    db = Database(tmp_path / "t.db")
    root = tmp_path / "Job"
    root.mkdir()
    return db, db.upsert_project("Job", root)


def _doc(db, pid, doc_id, filename, kind, heat=""):
    with db.tx() as c:
        c.execute("""INSERT INTO document(id, project_id, path, filename, ext,
                                          fingerprint, segment, kind)
                     VALUES(?,?,?,?,'.pdf',?,'S',?)""",
                  (doc_id, pid, f"C:/Job/{filename}", filename, f"fp{doc_id}", kind))
        c.execute("""INSERT INTO material(project_id, document_id, segment, heat,
                                          heat_key, source)
                     VALUES(?,?,'S',?,?,'mtr_file')""", (pid, doc_id, heat, heat))


def test_a_delivery_note_is_not_reported(job):
    db, pid = job
    _doc(db, pid, 1, "8-13-25 PIPE.pdf", "bill_of_lading")
    assert certificate_without_heat(db, pid, "r") == []


def test_a_certificate_with_no_heat_still_is(job):
    db, pid = job
    _doc(db, pid, 1, "FLANGE 2IN 300 RF.pdf", "mtr")
    found = certificate_without_heat(db, pid, "r")
    assert len(found) == 1
    assert found[0]["subject"] == "FLANGE 2IN 300 RF.pdf"


def test_a_valve_certificate_is_a_certificate(job):
    db, pid = job
    _doc(db, pid, 1, "8F-T63SN-RF.pdf", "valve_doc")
    assert len(certificate_without_heat(db, pid, "r")) == 1


def test_a_certificate_that_names_its_heat_is_not_reported(job):
    db, pid = job
    _doc(db, pid, 1, "071B33 - 16IN FLANGE.pdf", "mtr", heat="071B33")
    assert certificate_without_heat(db, pid, "r") == []


def test_the_mix_that_prompted_this(job):
    """Five delivery notes and one genuinely unnamed certificate."""
    db, pid = job
    for i, name in enumerate(("8-13-25 PIPE.pdf", "9-4-25 PIPE.pdf",
                              "8IN PO C PAD.pdf", "FITTING.pdf",
                              "6-24-25 FITTINGS & VALVES.pdf"), start=1):
        _doc(db, pid, i, name, "bill_of_lading")
    _doc(db, pid, 9, "PIPE MTR.pdf", "mtr")
    found = certificate_without_heat(db, pid, "r")
    assert [f["subject"] for f in found] == ["PIPE MTR.pdf"]
