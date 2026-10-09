"""`NO DATA.pdf` - a section considered, and empty.

Contractors drop a placeholder into a section folder to show they looked and
had nothing to file: 77 of them in one package, 150 spelled `NOT APPLICABLE`
in another. It is a good convention and the audit should read it.

Two things were wrong before. The placeholder was classified `unknown`, so it
sat in the pile of documents nobody could identify. And because a section
holding one of them holds *a document*, the section counted as present -- a
book with `NO DATA.pdf` in thirteen of its twenty-one required sections read
as complete.

So a section is now one of three things, not two: filled, declared empty, or
absent. Declared empty does not count toward the percentage, and it is
reported beside the missing ones rather than among them, because the
distinction is the whole point. A contractor saying "no bore profiles on this
job" is answering; a contractor who never opened the folder is not.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402
from weldaudit.index import completeness  # noqa: E402
from weldaudit.taxonomy import kind_for  # noqa: E402

JOB = r"C:\Jobs\JOB 7\16IN XX ALL FILES"


# -- recognising the placeholder --------------------------------------------

@pytest.mark.parametrize("name", [
    "NO DATA.pdf", "No Data.pdf", "NOT APPLICABLE.pdf", "Not Applicable.pdf",
    "N-A.pdf", "N A.pdf", "NA.pdf", "None.pdf", "NO_DATA.pdf",
])
def test_a_placeholder_is_recognised(name):
    assert kind_for(rf"{JOB}\13-CORROSION REQUIREMENTS\{name}") == "no_data"


@pytest.mark.parametrize("name,kind", [
    # Anchored to the whole filename: a real document that happens to mention
    # the words is still that document.
    ("MTR no data available.pdf", "mtr"),
    ("391K44.pdf", "mtr"),
    ("Data Sheet NA 4in.pdf", "mtr"),
])
def test_a_real_document_is_not_a_placeholder(name, kind):
    assert kind_for(rf"{JOB}\07-MTRs\Pipe\{name}") == kind


# -- what it does to the book -----------------------------------------------

@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "n.db")
    return database, database.upsert_project("N", str(tmp_path))


def filed(db, pid, section_no, kind, *, segment="SEG", name="x.pdf"):
    with db.tx() as c:
        c.execute(
            """INSERT INTO document(project_id, path, filename, ext, kind,
                                    segment, section_no)
               VALUES(?,?,?,'.pdf',?,?,?)""",
            (pid, rf"C:\j\{segment}\{name}", name, kind, segment, section_no),
        )


def only(rows):
    assert len(rows) == 1
    return rows[0]


def test_a_section_with_only_a_placeholder_is_declared_empty(db):
    database, pid = db
    filed(database, pid, 13, "no_data", name="NO DATA.pdf")
    got = only(completeness(database, pid))
    assert any(s.startswith("13 ") for s in got["declared_empty"])
    assert not any(s.startswith("13 ") for s in got["missing_required"])


def test_a_declared_empty_section_does_not_count_as_complete(db):
    """The failure that prompted this: a placeholder reading as a filled
    section, and a book of them reading as a finished book."""
    database, pid = db
    filed(database, pid, 13, "no_data", name="NO DATA.pdf")
    empty = only(completeness(database, pid))["pct_complete"]

    other = Database(Path(database.path).with_name("o.db"))
    pid2 = other.upsert_project("O", "/tmp/o")
    filed(other, pid2, 13, "coating", name="a real document.pdf")
    filled = only(completeness(other, pid2))["pct_complete"]

    assert filled > empty, "a filled section must count for more than a placeholder"


def test_a_section_with_a_placeholder_and_real_content_is_filled(db):
    """One stray NO DATA beside thirty real documents does not empty them."""
    database, pid = db
    filed(database, pid, 19, "no_data", name="NO DATA.pdf")
    filed(database, pid, 19, "coating", name="coating report.pdf")
    got = only(completeness(database, pid))
    assert not any(s.startswith("19 ") for s in got["declared_empty"])


def test_an_absent_section_is_still_missing_not_declared_empty(db):
    database, pid = db
    filed(database, pid, 7, "mtr", name="391K44.pdf")
    got = only(completeness(database, pid))
    assert any(s.startswith("13 ") for s in got["missing_required"])
    assert got["declared_empty"] == []


def test_the_three_states_do_not_overlap(db):
    """Filled, declared empty, absent - a section is exactly one of them."""
    database, pid = db
    filed(database, pid, 7, "mtr", name="391K44.pdf")          # filled
    filed(database, pid, 13, "no_data", name="NO DATA.pdf")    # declared empty
    got = only(completeness(database, pid))                    # 19 absent
    missing = {s.split()[0] for s in got["missing_required"]}
    empty = {s.split()[0] for s in got["declared_empty"]}
    assert "7" not in missing and "7" not in empty
    assert "13" in empty and "13" not in missing
    assert "19" in missing and "19" not in empty
