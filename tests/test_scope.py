"""Is the audit pointed at a turnover package at all?

SCOPE-02 exists because of a single sentence from the field: a colleague
pointed the program at a folder holding one PDF, and "it is not showing up".

Nothing was broken. The document indexed correctly; every rule then found
nothing to check, and the findings list came back empty. **An empty findings
list is what a clean package looks like.** The program had quietly produced
the one output it must never produce -- a blank report that reads as a pass.

Worse, it already knew: ``completeness`` had worked out that all eighteen
required sections were missing and the book was 0% complete. That summary
feeds its own tab and never becomes a finding, so it had nowhere to say so.

These tests hold both halves: it must speak up when there is nothing to
audit, and it must stay silent whenever there is.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402
from weldaudit.rules.scope import not_a_package  # noqa: E402


@pytest.fixture
def job(tmp_path):
    db = Database(tmp_path / "t.db")
    root = tmp_path / "Kestrel 8"
    root.mkdir()
    return db, db.upsert_project("Kestrel 8", str(root)), root


def add(db, pid, filename, section_no=None, section=None):
    with db.tx() as c:
        c.execute(
            """INSERT INTO document(project_id, path, filename, ext, size_bytes,
                                    segment, section_no, section, kind)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (pid, f"C:/Kestrel 8/{filename}", filename, Path(filename).suffix,
             1024, "(unassigned)" if section_no is None else "16 PW",
             section_no, section, "unknown"))
        return c.execute("SELECT last_insert_rowid() i").fetchone()[0]


# -- it speaks up ------------------------------------------------------------

def test_one_loose_pdf_is_reported(job):
    """The case as it happened."""
    db, pid, _ = job
    add(db, pid, "A2401000 - 16IN EL 90 SCH40 SS XTO-242.pdf")
    found = not_a_package(db, pid, "r1")
    assert len(found) == 1
    assert found[0]["rule"] == "SCOPE-02"
    assert found[0]["severity"] == "critical"


def test_it_names_the_file_it_found(job):
    """A count is not a finding: the auditor has to see what it did read."""
    db, pid, _ = job
    add(db, pid, "A2401000.pdf")
    assert "A2401000.pdf" in not_a_package(db, pid, "r1")[0]["message"]


def test_it_says_the_empty_report_is_not_a_pass(job):
    """The whole point of the rule, so it is asserted rather than assumed."""
    db, pid, _ = job
    add(db, pid, "A2401000.pdf")
    message = not_a_package(db, pid, "r1")[0]["message"]
    assert "not the same as nothing being wrong" in message


def test_it_says_where_to_point_the_program_instead(job):
    db, pid, _ = job
    add(db, pid, "A2401000.pdf")
    assert "7 MTRS" in not_a_package(db, pid, "r1")[0]["message"]


def test_a_single_loose_file_is_linked_to_its_document(job):
    """One file has an obvious source; the Source column should open it."""
    db, pid, _ = job
    did = add(db, pid, "A2401000.pdf")
    assert not_a_package(db, pid, "r1")[0]["document_id"] == did


def test_a_pile_of_loose_files_links_to_none_of_them(job):
    db, pid, _ = job
    for n in range(5):
        add(db, pid, f"scan{n}.pdf")
    found = not_a_package(db, pid, "r1")[0]
    assert found["document_id"] is None
    assert "5 documents were found" in found["message"]
    assert "and 2 more" in found["message"]


def test_an_empty_folder_is_reported(job):
    db, pid, root = job
    found = not_a_package(db, pid, "r1")
    assert len(found) == 1
    assert found[0]["subject"] == "empty folder"
    assert str(root) in found[0]["message"]


def test_the_singular_reads_properly(job):
    db, pid, _ = job
    add(db, pid, "one.pdf")
    message = not_a_package(db, pid, "r1")[0]["message"]
    assert "One document was found" in message
    assert "none of them" not in message


# -- it stays silent ---------------------------------------------------------

def test_a_real_package_says_nothing(job):
    db, pid, _ = job
    add(db, pid, "weld map.xlsx", 1, "Weld Maps")
    add(db, pid, "cert.pdf", 7, "MTRS")
    add(db, pid, "rt.pdf", 11, "NDE")
    assert not_a_package(db, pid, "r1") == []


def test_auditing_one_section_on_purpose_says_nothing(job):
    """"Just the MTRs" is a real thing to do. The Book completeness tab is
    where a partial package is judged, not a critical finding."""
    db, pid, _ = job
    for n in range(22):
        add(db, pid, f"mtr{n}.pdf", 7, "MTRS")
    assert not_a_package(db, pid, "r1") == []


def test_one_recognised_document_among_loose_ones_is_enough(job):
    """Deliberately binary. A stray invoice at the root of a real job -- which
    happens -- must not turn the report critical."""
    db, pid, _ = job
    add(db, pid, "1012792.pdf")                 # a vendor invoice, no section
    add(db, pid, "weld map.xlsx", 1, "Weld Maps")
    assert not_a_package(db, pid, "r1") == []


def test_a_project_that_does_not_exist_does_not_raise(job):
    db, _pid, _ = job
    assert not_a_package(db, 999, "r1") == []
