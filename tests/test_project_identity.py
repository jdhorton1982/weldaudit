"""A folder is what identifies an audit — not the name it was given.

Keying on the name produced two wrong answers, both of which happened in a
real database:

- Pointing at one folder under a second name made a *second* audit of it. The
  same 893 documents were indexed twice and sat side by side in the dropdown
  looking like two different jobs.
- Two different folders sharing a name destroyed one another. The old
  ``ON CONFLICT(name)`` clause repointed the first audit's row at the second
  folder and handed back its id, and ``index_project`` then cleared that
  project and indexed the new folder into it. Auditing a second ``BOOK``
  threw the first one away without a word.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402

BS = chr(92)


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "t.db")


# -- one folder is one audit --------------------------------------------------

def test_the_same_folder_under_a_new_name_reuses_the_audit(tmp_path, db):
    """The duplicate this exists to prevent."""
    root = tmp_path / "Kestrel 8 Lateral to Terminal"
    root.mkdir()
    first = db.upsert_project("Kestrel 8", root)
    second = db.upsert_project("Kestrel 8 Lateral to Terminal", root)
    assert first == second
    assert len(db.projects()) == 1


def test_the_name_somebody_chose_survives_a_re_audit(tmp_path, db):
    """Re-auditing is not a request to rename. The name arriving is usually
    just the folder's own, filled in by the picker."""
    root = tmp_path / "Kestrel 8 Lateral to Terminal"
    root.mkdir()
    db.upsert_project("Kestrel 8", root)
    db.upsert_project("Kestrel 8 Lateral to Terminal", root)
    assert db.projects()[0]["name"] == "Kestrel 8"


@pytest.mark.parametrize("spelling", [
    "{root}", "{root}" + os.sep, "{slashes}", "{parent}/sub/../{leaf}",
])
def test_one_folder_however_the_path_is_spelled(tmp_path, db, spelling):
    """A typed path, a path from the Windows picker and a path read back out
    of the database are rarely byte-identical for the same folder."""
    root = tmp_path / "Job"
    (root / "sub").mkdir(parents=True)
    first = db.upsert_project("Job", root)
    variant = spelling.format(root=str(root), slashes=str(root).replace(BS, "/"),
                              parent=str(root), leaf="")
    again = db.upsert_project("Whatever", variant.rstrip("/"))
    assert again == first
    assert len(db.projects()) == 1


@pytest.mark.skipif(os.name != "nt", reason="only Windows ignores path case")
def test_case_does_not_make_a_second_audit_on_windows(tmp_path, db):
    root = tmp_path / "Job"
    root.mkdir()
    first = db.upsert_project("Job", root)
    assert db.upsert_project("Job", str(root).upper()) == first


# -- two folders are two audits ----------------------------------------------

def test_two_folders_sharing_a_name_do_not_destroy_each_other(tmp_path, db):
    """Turnover packages are not named distinctively: several jobs have a
    BOOK folder, and the picker offers the folder's own name. Returning the
    first one's id here is not a near miss — the caller wipes that project and
    indexes the second folder into it."""
    a = tmp_path / "Bluewater 14" / "BOOK"
    b = tmp_path / "Revision" / "BOOK"
    for r in (a, b):
        r.mkdir(parents=True)
    first = db.upsert_project("BOOK", a)
    second = db.upsert_project("BOOK", b)
    assert first != second
    assert len(db.projects()) == 2
    # and the first audit still points where it did
    kept = db.one("SELECT root FROM project WHERE id=?", (first,))["root"]
    assert Database.path_key(kept) == Database.path_key(a)


def test_the_second_of_two_is_told_apart_by_its_parent(tmp_path, db):
    a = tmp_path / "Bluewater 14" / "BOOK"
    b = tmp_path / "Revision" / "BOOK"
    for r in (a, b):
        r.mkdir(parents=True)
    db.upsert_project("BOOK", a)
    db.upsert_project("BOOK", b)
    assert {p["name"] for p in db.projects()} == {"BOOK", "BOOK (Revision)"}


def test_a_third_clash_falls_back_to_counting(tmp_path, db):
    roots = []
    for parent in ("One", "Two", "Three"):
        r = tmp_path / parent / "BOOK"
        r.mkdir(parents=True)
        roots.append(r)
    # force the parent-name route to be taken as well
    for r in roots:
        db.upsert_project("BOOK", r)
    extra = tmp_path / "Two" / "deeper" / "BOOK"
    extra.mkdir(parents=True)
    db.upsert_project("BOOK", extra)
    names = {p["name"] for p in db.projects()}
    assert len(names) == 4, names
    assert "BOOK" in names


def test_no_audit_ever_loses_its_folder(tmp_path, db):
    """The old failure mode: the row survived but pointed somewhere else."""
    pairs = [("BOOK", tmp_path / p / "BOOK") for p in ("A", "B", "C")]
    ids = []
    for name, root in pairs:
        root.mkdir(parents=True)
        ids.append(db.upsert_project(name, root))
    for (_n, root), pid in zip(pairs, ids):
        stored = db.one("SELECT root FROM project WHERE id=?", (pid,))["root"]
        assert Database.path_key(stored) == Database.path_key(root)


# -- the lookup itself --------------------------------------------------------

def test_project_at_finds_nothing_for_an_unaudited_folder(tmp_path, db):
    (tmp_path / "Never").mkdir()
    assert db.project_at(tmp_path / "Never") is None


def test_project_at_works_on_a_folder_that_has_been_deleted(tmp_path, db):
    """Job folders get archived. Looking one up must not need it to exist."""
    root = tmp_path / "Gone"
    root.mkdir()
    pid = db.upsert_project("Gone", root)
    root.rmdir()
    found = db.project_at(root)
    assert found is not None and found["id"] == pid
