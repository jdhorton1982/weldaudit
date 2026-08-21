"""The approved list carried inside the program.

An audit with no approved list runs every other check and skips the
manufacturer approvals. That is reported as skipped rather than passed — but
skipped all the same, and those are the checks that catch an unapproved mill.

It happened. The list lives beside the *jobs*, not inside the program, so a
colleague handed the exe alone audited a whole package with every approval
unchecked. A copy now travels in the build.

It is a floor, not a default. A list found near the job always wins, because
that one is the auditor's own choice and the built-in one goes stale — and the
report records which was used, because a copy inside the exe is only whatever
was current on the day the program was built.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.extract import materials  # noqa: E402
from weldaudit.extract.materials import bundled_aml, find_aml_workbook  # noqa: E402


@pytest.fixture
def pretend_bundled(monkeypatch, tmp_path):
    """A stand-in for the copy inside the exe, so these tests hold whether or
    not the build they run against happens to carry one."""
    inside = tmp_path / "inside-the-exe"
    inside.mkdir()
    book = inside / "AML built in.xlsx"
    book.write_bytes(b"stub")
    monkeypatch.setattr(materials, "bundled_aml", lambda: book)
    return book


@pytest.fixture
def job(tmp_path):
    """A job folder with nothing above it, inside a fake home."""
    home = tmp_path / "home"
    root = home / "Jobs" / "16 PW"
    root.mkdir(parents=True)
    return root, home


# -- the fallback ------------------------------------------------------------

def test_a_job_with_no_list_anywhere_uses_the_built_in_one(pretend_bundled, job,
                                                           monkeypatch):
    root, home = job
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    assert find_aml_workbook(root) == pretend_bundled


def test_without_a_built_in_one_it_still_returns_nothing(job, monkeypatch):
    """A build made without the folder is valid; it behaves as it always did,
    and the audit says the approval checks were skipped."""
    root, home = job
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setattr(materials, "bundled_aml", lambda: None)
    assert find_aml_workbook(root) is None


# -- a list on disk still wins -----------------------------------------------

def test_a_list_beside_the_job_beats_the_built_in_one(pretend_bundled, job,
                                                      monkeypatch):
    root, home = job
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    theirs = root / "AML Search Spreadsheet.xlsx"
    theirs.write_bytes(b"stub")
    assert find_aml_workbook(root) == theirs


def test_a_list_above_the_job_beats_the_built_in_one(pretend_bundled, job,
                                                     monkeypatch):
    """The usual arrangement: one list over a folder full of jobs."""
    root, home = job
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    theirs = root.parent / "AML Search Spreadsheet.xlsx"
    theirs.write_bytes(b"stub")
    assert find_aml_workbook(root) == theirs


def test_the_built_in_one_does_not_override_an_expired_list_on_disk(
        pretend_bundled, job, monkeypatch):
    """Deliberate. Silently swapping in a different list would hide which one
    an audit ran against; AML-01 reports an expired list instead."""
    root, home = job
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    old = root / "AML 2019.xlsx"
    old.write_bytes(b"stub")
    assert find_aml_workbook(root) == old


# -- what the report records -------------------------------------------------

def test_the_source_is_recorded_as_bundled(tmp_path, monkeypatch):
    """So the auditor can see the list came from the program, not from them."""
    from weldaudit.db import Database
    from weldaudit.extract.materials import load_aml

    real = Path(__file__).resolve().parents[1] / "weldaudit" / "data"
    books = [p for pattern in materials.AML_PATTERNS for p in real.glob(pattern)] \
        if real.is_dir() else []
    if not books:
        pytest.skip("this build carries no built-in approved list")

    db = Database(tmp_path / "t.db")
    root = tmp_path / "Job"
    root.mkdir()
    pid = db.upsert_project("Job", root)
    load_aml(db, pid, root, books[0])

    source = db.one("SELECT kind, entries FROM aml_source WHERE project_id=?", (pid,))
    assert source["kind"] == "bundled"
    assert source["entries"] > 1000


def test_the_same_list_off_disk_is_recorded_as_a_pdf(tmp_path, monkeypatch):
    """The discriminator is where it came from, not what is in it: the very
    same document is 'pdf' when the auditor supplied it and 'bundled' when the
    program did."""
    from weldaudit.db import Database
    from weldaudit.extract.materials import load_aml

    real = Path(__file__).resolve().parents[1] / "weldaudit" / "data"
    books = [p for p in real.glob("*AML*.pdf")] if real.is_dir() else []
    if not books:
        pytest.skip("this build carries no built-in approved list")

    # Same file, but the program is not carrying it any more.
    monkeypatch.setattr(materials, "bundled_aml", lambda: None)
    db = Database(tmp_path / "t.db")
    root = tmp_path / "Job"
    root.mkdir()
    pid = db.upsert_project("Job", root)
    load_aml(db, pid, root, books[0])
    assert db.one("SELECT kind FROM aml_source WHERE project_id=?", (pid,))["kind"] == "pdf"


def test_a_job_with_nothing_anywhere_loads_the_built_in_list(tmp_path, monkeypatch):
    """The colleague's case, end to end: point it at a bare folder and the
    approval checks still have a list to run against."""
    from weldaudit.db import Database
    from weldaudit.extract.materials import load_aml

    if bundled_aml() is None:
        pytest.skip("this build carries no built-in approved list")

    home = tmp_path / "home"
    root = home / "Jobs" / "16 PW"
    root.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))

    db = Database(tmp_path / "t.db")
    pid = db.upsert_project("16 PW", root)
    aml, book = load_aml(db, pid, root)

    assert aml is not None and len(aml.entries) > 1000
    assert db.one("SELECT kind FROM aml_source WHERE project_id=?", (pid,))["kind"] \
        == "bundled"


# -- the copy this build actually carries ------------------------------------

def test_this_build_carries_a_readable_list():
    """Guards the packaging rather than the logic: a bundled file that cannot
    be parsed would be worse than none, because it would look loaded."""
    book = bundled_aml()
    if book is None:
        pytest.skip("this build carries no built-in approved list")
    from weldaudit.amlpdf import entries, revision

    if book.suffix.lower() == ".pdf":
        said, on = revision(book)
        assert on is not None, "a built-in list must state when it stops being valid"
        assert len(entries(book)) > 1000
