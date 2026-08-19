"""MTR-08 has to say which certificates, not just how many.

One finding per segment is deliberate - sixty separate notes saying "this one
could not be read" is not a report. But collapsing them threw the identities
away, and "7 heats could not be checked" leaves the reader to open every MTR
in the book and work out which seven were the unread ones.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402
from weldaudit.rules.materials import manufacturer_unknown  # noqa: E402


@pytest.fixture
def job(tmp_path):
    db = Database(tmp_path / "t.db")
    root = tmp_path / "Job"
    root.mkdir()
    pid = db.upsert_project("Job", root)
    with db.tx() as c:
        c.execute("""INSERT INTO aml_entry(project_id, category, manufacturer,
                                           location, norm_name)
                     VALUES(?, '1.0 Pipe', 'Norvale', 'Veracruz', 'norvale')""",
                  (pid,))
    return db, pid


def _cert(db, pid, doc_id, filename, heat, manufacturer=""):
    with db.tx() as c:
        c.execute("""INSERT INTO document(id, project_id, path, filename, ext,
                                          fingerprint, segment, kind)
                     VALUES(?,?,?,?,'.pdf',?,'S','mtr')""",
                  (doc_id, pid, f"C:/Job/S/{filename}", filename, f"fp{doc_id}"))
        c.execute("""INSERT INTO material(project_id, document_id, segment, heat,
                                          heat_key, source, manufacturer)
                     VALUES(?,?,'S',?,?,'mtr_file',?)""",
                  (pid, doc_id, heat, heat, manufacturer))


def test_the_certificates_are_named(job):
    db, pid = job
    _cert(db, pid, 1, "HT-9001 pipe.pdf", "9001")
    _cert(db, pid, 2, "HT-9002 pipe.pdf", "9002")
    found = manufacturer_unknown(db, pid, "r1")
    assert len(found) == 1
    message = found[0]["message"]
    assert "9001 on HT-9001 pipe.pdf" in message
    assert "9002 on HT-9002 pipe.pdf" in message


def test_every_document_reaches_the_report(job):
    """The report turns detail['document_ids'] into full paths. Without it a
    finding covering eight certificates points at one."""
    db, pid = job
    for i in range(1, 4):
        _cert(db, pid, i, f"HT-900{i}.pdf", f"900{i}")
    found = manufacturer_unknown(db, pid, "r1")[0]
    ids = json.loads(found["detail"])["document_ids"]
    assert sorted(ids.split(", ")) == ["1", "2", "3"]
    assert found["document_id"] in (1, 2, 3)


def test_a_heat_somebody_else_names_is_not_listed(job):
    """A certificate with no readable mill is fine when a pipe export names
    it. Sending somebody to read a page that needs no reading is the same
    mistake as not telling them which page."""
    db, pid = job
    _cert(db, pid, 1, "HT-9001.pdf", "9001")
    with db.tx() as c:
        c.execute("""INSERT INTO material(project_id, segment, heat, heat_key,
                                          source, manufacturer)
                     VALUES(?, 'S', '9001', '9001', 'pipes_csv', 'Norvale')""",
                  (pid,))
    assert manufacturer_unknown(db, pid, "r1") == []


def test_a_long_list_is_cut_short_in_the_message_but_not_in_the_detail(job):
    db, pid = job
    for i in range(1, 15):
        _cert(db, pid, i, f"HT-{i:04}.pdf", f"{i:04}")
    found = manufacturer_unknown(db, pid, "r1")[0]
    assert "and 6 more" in found["message"]
    detail = json.loads(found["detail"])
    assert len(detail["unread"].split(", ")) == 14
    assert len(detail["document_ids"].split(", ")) == 14


def test_each_segment_is_reported_separately(job):
    db, pid = job
    _cert(db, pid, 1, "a.pdf", "9001")
    with db.tx() as c:
        c.execute("""INSERT INTO document(id, project_id, path, filename, ext,
                                          fingerprint, segment, kind)
                     VALUES(2,?, 'C:/Job/T/b.pdf', 'b.pdf', '.pdf', 'fp2', 'T', 'mtr')""",
                  (pid,))
        c.execute("""INSERT INTO material(project_id, document_id, segment, heat,
                                          heat_key, source, manufacturer)
                     VALUES(?, 2, 'T', '9002', '9002', 'mtr_file', '')""", (pid,))
    found = manufacturer_unknown(db, pid, "r1")
    assert {f["segment"] for f in found} == {"S", "T"}


def test_no_aml_means_the_question_cannot_be_asked(job):
    db, pid = job
    with db.tx() as c:
        c.execute("DELETE FROM aml_entry WHERE project_id=?", (pid,))
    _cert(db, pid, 1, "HT-9001.pdf", "9001")
    assert manufacturer_unknown(db, pid, "r1") == []
