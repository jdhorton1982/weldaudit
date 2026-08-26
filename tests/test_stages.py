"""Auditing a job that is still being built.

A weld in no test pack is a hole in a hand-over and a normal Tuesday on a job
still under construction. The rule cannot tell which without being told how far
along the job is, and told wrongly in the safe direction: a project that says
nothing is read as turnover, the stricter of the two.

The thing these tests guard hardest is that **nothing is suppressed**. A rule
that goes quiet because of a setting somebody forgot to change is a worse
failure than a noisy report -- the noise is visible and the silence is not. A
softened finding is still made, still stored, still exported, and says in its
own message why it is not being counted yet.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from weldaudit import stages  # noqa: E402
from weldaudit.api import create_app  # noqa: E402
from weldaudit.db import Database  # noqa: E402


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "s.db")
    return database, database.upsert_project("J", str(tmp_path))


@pytest.fixture
def client(tmp_path):
    path = tmp_path / "s.db"
    database = Database(path)
    pid = database.upsert_project("J", str(tmp_path))
    return TestClient(create_app(path)), pid


def finding(rule, severity="critical", message="Something is wrong."):
    return {"rule": rule, "severity": severity, "message": message}


# -- reading the stage off a project ----------------------------------------

def test_a_project_is_turnover_until_it_says_otherwise(db):
    """The stricter reading is the default, so an old project cannot quietly
    become lenient when this feature arrives under it."""
    database, pid = db
    row = database.one("SELECT * FROM project WHERE id=?", (pid,))
    assert stages.stage_of(row) == stages.TURNOVER


def test_the_column_is_added_to_a_database_that_predates_it(db):
    database, pid = db
    cols = [r[1] for r in database.q("PRAGMA table_info(project)")]
    assert "stage" in cols


def test_an_unknown_stage_is_read_as_turnover(db):
    """A value nothing recognises must not soften anything."""
    database, pid = db
    with database.tx() as c:
        c.execute("UPDATE project SET stage='whenever' WHERE id=?", (pid,))
    row = database.one("SELECT * FROM project WHERE id=?", (pid,))
    assert stages.stage_of(row) == stages.TURNOVER


# -- what softening does, and does not, do ----------------------------------

def test_an_assembly_finding_drops_to_info_on_a_preliminary_job():
    got = stages.soften([finding("WT-21")], stages.PRELIMINARY)
    assert got[0]["severity"] == "info"


def test_the_finding_is_kept_and_says_why_it_is_quiet():
    got = stages.soften([finding("WT-21", message="W-37 is in no test pack.")],
                        stages.PRELIMINARY)
    assert len(got) == 1, "a softened finding is never dropped"
    assert "W-37 is in no test pack." in got[0]["message"]
    assert "preliminary" in got[0]["message"]
    assert "turnover" in got[0]["message"]


def test_a_qualification_finding_keeps_its_severity():
    """What gates a hydrotest is not late paperwork before one."""
    for rule in ("WPS-01", "WLD-06", "MTR-10", "WT-17", "WT-12", "AML-01"):
        got = stages.soften([finding(rule)], stages.PRELIMINARY)
        assert got[0]["severity"] == "critical", rule
        assert "preliminary" not in got[0]["message"], rule


def test_a_turnover_job_softens_nothing():
    got = stages.soften([finding("WT-21"), finding("WPS-01")], stages.TURNOVER)
    assert [f["severity"] for f in got] == ["critical", "critical"]
    assert all("preliminary" not in f["message"] for f in got)


def test_softening_never_raises_a_severity():
    got = stages.soften([finding("WT-21", severity="info")], stages.PRELIMINARY)
    assert got[0]["severity"] == "info"


def test_a_mistyped_report_number_is_not_an_assembly_finding():
    """Wrong on the day it was typed, and no amount of construction left to
    run makes it right."""
    assert "WT-17" not in stages.ASSEMBLED_LAST


# -- setting it through the API ---------------------------------------------

def test_the_stage_can_be_set_and_comes_back_on_the_project(client):
    api, pid = client
    r = api.post(f"/api/project/{pid}/stage", json={"stage": "preliminary"})
    assert r.status_code == 200
    assert r.json()["stage"] == "preliminary"
    assert r.json()["rerun_needed"] is True
    assert api.get("/api/projects").json()[0]["stage"] == "preliminary"


def test_an_unknown_stage_is_refused_rather_than_stored(client):
    api, pid = client
    r = api.post(f"/api/project/{pid}/stage", json={"stage": "nearly done"})
    assert r.status_code == 400
    assert api.get("/api/projects").json()[0]["stage"] == "turnover"


def test_setting_the_stage_of_a_project_that_is_not_there(client):
    api, _pid = client
    assert api.post("/api/project/9999/stage",
                    json={"stage": "preliminary"}).status_code == 404


def test_the_options_are_offered_to_the_page(client):
    api, _pid = client
    said = api.get("/api/stages").json()
    assert [s["key"] for s in said["stages"]] == list(stages.STAGES)
    assert all(s["label"] for s in said["stages"])
    assert "WT-21" in said["softened"]
