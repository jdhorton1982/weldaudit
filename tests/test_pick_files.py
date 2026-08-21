"""Choosing documents instead of a folder.

The Windows folder browser shows no files at all. You cannot look inside a
folder before choosing it, and somebody holding a single document has nothing
to point at — which is how a colleague came to try "uploading" one PDF and
conclude it had not shown up.

The property that makes this safe is that a file dialog hands back **full
paths**. A certificate picked out of ``BOOK\\7 MTRS`` is still under section 7,
because ``section_for`` reads the path it is given. So selecting every file in
a package must give the same audit as selecting the package, and the test
below asserts exactly that rather than trusting it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from weldaudit.api import create_app  # noqa: E402
from weldaudit.db import Database  # noqa: E402
from weldaudit.index import chosen_files, common_parent, index_project  # noqa: E402


@pytest.fixture
def package(tmp_path):
    """A folder shaped like a turnover book."""
    root = tmp_path / "16 PW"
    for section, names in [
        ("1 Weld Maps", ["weld map.xlsx"]),
        ("7 MTRS", ["A2401000.pdf", "HK673P.pdf"]),
        ("11 NDE", ["NDE Log.xlsx"]),
    ]:
        (root / section).mkdir(parents=True)
        for n in names:
            (root / section / n).write_bytes(b"%PDF-1.4 stub")
    return root


def every_file(root):
    return [str(p) for p in sorted(root.rglob("*")) if p.is_file()]


# -- the property that makes it safe -----------------------------------------

def test_picking_every_file_matches_picking_the_folder(package, tmp_path):
    """Because the paths come back in full, the sections are unchanged."""
    def index(only):
        db = Database(tmp_path / f"{'files' if only else 'folder'}.db")
        pid, _ = index_project(db, "16 PW", package, only=only)
        return sorted(
            (r["filename"], r["section_no"], r["segment"])
            for r in db.q("SELECT filename, section_no, segment FROM document "
                          "WHERE project_id=?", (pid,)))

    assert index(every_file(package)) == index(None)


def test_a_file_picked_out_of_a_section_keeps_that_section(package, tmp_path):
    db = Database(tmp_path / "t.db")
    one = str(package / "7 MTRS" / "A2401000.pdf")
    pid, _ = index_project(db, "one", package, only=[one])
    row = db.one("SELECT filename, section_no FROM document WHERE project_id=?", (pid,))
    assert row["filename"] == "A2401000.pdf"
    assert row["section_no"] == 7


def test_only_the_chosen_files_are_indexed(package, tmp_path):
    db = Database(tmp_path / "t.db")
    two = [str(package / "7 MTRS" / "A2401000.pdf"),
           str(package / "11 NDE" / "NDE Log.xlsx")]
    pid, stats = index_project(db, "two", package, only=two)
    assert stats.files_indexed == 2
    assert {r["filename"] for r in
            db.q("SELECT filename FROM document WHERE project_id=?", (pid,))} \
        == {"A2401000.pdf", "NDE Log.xlsx"}


# -- filtering what was picked -----------------------------------------------

def test_junk_and_unreadable_extensions_are_dropped(tmp_path):
    """The same two filters the folder walk applies, so the two agree."""
    for n in ["real.pdf", "Thumbs.db", "._real.pdf", "photo.jpg", "sheet.xlsx"]:
        (tmp_path / n).write_bytes(b"x")
    kept = [p.name for p in chosen_files(str(tmp_path / n)
                                         for n in ["real.pdf", "Thumbs.db",
                                                   "._real.pdf", "photo.jpg",
                                                   "sheet.xlsx"])]
    assert kept == ["real.pdf", "sheet.xlsx"]


def test_a_folder_handed_in_by_mistake_is_dropped(tmp_path):
    (tmp_path / "a folder.pdf").mkdir()          # a directory named like a file
    assert chosen_files([str(tmp_path / "a folder.pdf")]) == []


def test_a_file_that_has_gone_is_dropped(tmp_path):
    assert chosen_files([str(tmp_path / "never-existed.pdf")]) == []


# -- where the audit gets rooted ---------------------------------------------

def test_the_root_is_the_folder_the_files_came_from(package):
    picked = chosen_files(every_file(package / "7 MTRS"))
    assert common_parent(picked) == package / "7 MTRS"


def test_files_from_several_sections_root_at_the_package(package):
    picked = chosen_files([str(package / "7 MTRS" / "A2401000.pdf"),
                           str(package / "11 NDE" / "NDE Log.xlsx")])
    assert common_parent(picked) == package


def test_one_file_roots_at_its_own_folder(package):
    picked = chosen_files([str(package / "7 MTRS" / "A2401000.pdf")])
    assert common_parent(picked) == package / "7 MTRS"


# -- through the API ---------------------------------------------------------

@pytest.fixture
def api(tmp_path):
    return TestClient(create_app(tmp_path / "t.db"))


def test_the_endpoint_accepts_a_list_of_files(api, package):
    r = api.post("/api/audit", json={"root": str(package),
                                     "paths": every_file(package)})
    assert r.status_code == 200
    assert r.json()["started"] is True


def test_a_selection_of_nothing_auditable_is_refused_with_a_reason(api, tmp_path):
    """Rather than starting an audit that finds nothing and says nothing."""
    (tmp_path / "holiday.jpg").write_bytes(b"x")
    r = api.post("/api/audit", json={"root": str(tmp_path),
                                     "paths": [str(tmp_path / "holiday.jpg")]})
    assert r.status_code == 400
    assert "PDF" in r.json()["error"]


def test_a_folder_that_is_not_there_is_still_refused(api, tmp_path):
    r = api.post("/api/audit", json={"root": str(tmp_path / "nope")})
    assert r.status_code == 400
    assert "Not a folder" in r.json()["error"]


def test_files_are_audited_even_when_the_root_given_is_wrong(api, package, tmp_path):
    """The files decide the root, so a stale box does not misdirect the audit."""
    r = api.post("/api/audit", json={"root": str(tmp_path / "somewhere else"),
                                     "paths": [str(package / "7 MTRS" / "A2401000.pdf")]})
    assert r.status_code == 200
