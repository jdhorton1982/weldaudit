"""Renaming a stored audit.

Names became permanent once the folder took over as an audit's identity:
re-running an audit no longer overwrites the name, which is what stops the
picker's folder-derived name replacing one somebody chose. This is the way to
change it deliberately.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from weldaudit.api import create_app  # noqa: E402
from weldaudit.db import Database  # noqa: E402


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    (tmp_path / "A").mkdir()
    (tmp_path / "B").mkdir()
    d.upsert_project("Kestrel 8 Lateral to Terminal", tmp_path / "A")
    d.upsert_project("Bluewater 16", tmp_path / "B")
    return d


@pytest.fixture
def api(db, tmp_path):
    return TestClient(create_app(tmp_path / "t.db"))


def _id(db, name):
    return db.one("SELECT id FROM project WHERE name=?", (name,))["id"]


# -- renaming ----------------------------------------------------------------

def test_a_name_can_be_changed(db):
    pid = _id(db, "Kestrel 8 Lateral to Terminal")
    assert db.rename_project(pid, "Kestrel 8") == "Kestrel 8"
    assert db.one("SELECT name FROM project WHERE id=?", (pid,))["name"] == "Kestrel 8"


def test_the_findings_come_with_it(db, tmp_path):
    """The rename must not disturb what the audit holds."""
    pid = _id(db, "Bluewater 16")
    with db.tx() as c:
        c.execute("""INSERT INTO finding(project_id, run_id, rule, severity, message)
                     VALUES(?, 'r', 'MTR-11', 'critical', 'm')""", (pid,))
    db.rename_project(pid, "Bluewater 16 North")
    assert db.one("SELECT COUNT(*) c FROM finding WHERE project_id=?",
                  (pid,))["c"] == 1


def test_the_folder_it_points_at_is_unchanged(db, tmp_path):
    pid = _id(db, "Bluewater 16")
    before = db.one("SELECT root FROM project WHERE id=?", (pid,))["root"]
    db.rename_project(pid, "Something Else")
    assert db.one("SELECT root FROM project WHERE id=?", (pid,))["root"] == before


def test_surrounding_whitespace_is_tidied(db):
    pid = _id(db, "Bluewater 16")
    assert db.rename_project(pid, "  Bluewater   16 North \n") == "Bluewater 16 North"


def test_renaming_to_the_same_name_is_allowed(db):
    """Otherwise pressing Save without editing is an error, which is absurd."""
    pid = _id(db, "Bluewater 16")
    assert db.rename_project(pid, "Bluewater 16") == "Bluewater 16"


# -- what is refused ---------------------------------------------------------

def test_a_name_another_audit_uses_is_refused(db):
    """free_name invents a suffix because nobody chose that name. Here
    somebody typed it, and quietly storing something else is worse."""
    pid = _id(db, "Bluewater 16")
    with pytest.raises(ValueError, match="already uses that name"):
        db.rename_project(pid, "Kestrel 8 Lateral to Terminal")


def test_a_clash_is_caught_whatever_the_case(db):
    """SQLite calls 'Kestrel 8' and 'kestrel 8' different; a person reading the
    dropdown does not."""
    pid = _id(db, "Bluewater 16")
    with pytest.raises(ValueError, match="already uses that name"):
        db.rename_project(pid, "kestrel 8 LATERAL TO terminal")


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_an_empty_name_is_refused(db, blank):
    pid = _id(db, "Bluewater 16")
    with pytest.raises(ValueError):
        db.rename_project(pid, blank)


def test_an_absurdly_long_name_is_refused(db):
    pid = _id(db, "Bluewater 16")
    with pytest.raises(ValueError, match="120"):
        db.rename_project(pid, "x" * 200)


def test_renaming_an_audit_that_does_not_exist(db):
    with pytest.raises(LookupError):
        db.rename_project(999999, "Anything")


# -- through the API ---------------------------------------------------------

def test_the_endpoint_renames_and_reports_the_stored_name(api, db):
    pid = _id(db, "Kestrel 8 Lateral to Terminal")
    r = api.post(f"/api/projects/{pid}/name", json={"name": "  Kestrel 8  "})
    assert r.status_code == 200 and r.json()["name"] == "Kestrel 8"


def test_the_endpoint_explains_a_clash_rather_than_failing_blankly(api, db):
    pid = _id(db, "Bluewater 16")
    r = api.post(f"/api/projects/{pid}/name",
                 json={"name": "Kestrel 8 Lateral to Terminal"})
    assert r.status_code == 400
    assert "already uses that name" in r.json()["error"]


def test_the_endpoint_refuses_a_blank_name(api, db):
    pid = _id(db, "Bluewater 16")
    assert api.post(f"/api/projects/{pid}/name", json={"name": " "}).status_code == 400


def test_the_endpoint_404s_on_an_unknown_audit(api):
    assert api.post("/api/projects/999999/name",
                    json={"name": "X"}).status_code == 404


# -- and it survives a re-audit ----------------------------------------------

def test_a_renamed_audit_keeps_its_name_when_re_run(db, tmp_path):
    """The whole reason renaming had to become explicit."""
    pid = _id(db, "Kestrel 8 Lateral to Terminal")
    db.rename_project(pid, "Kestrel 8")
    again = db.upsert_project("Kestrel 8 Lateral to Terminal", tmp_path / "A")
    assert again == pid
    assert db.one("SELECT name FROM project WHERE id=?", (pid,))["name"] == "Kestrel 8"
