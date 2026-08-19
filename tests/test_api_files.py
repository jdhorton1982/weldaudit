"""Opening a job folder from the app, and getting the report back out.

The two things an auditor does that are not reading findings: point the tool
at a package, and hand the result to somebody. Both used to require typing a
Windows path into a text box and then digging the file out of Downloads.
"""

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from weldaudit.api import create_app  # noqa: E402
from weldaudit.db import Database  # noqa: E402
from weldaudit.report import write_csv  # noqa: E402


@pytest.fixture
def job(tmp_path):
    """A project whose root is a real folder we are allowed to write into."""
    root = tmp_path / "Kestrel 8"
    (root / "11 NDE").mkdir(parents=True)
    database = Database(tmp_path / "t.db")
    pid = database.upsert_project("Kestrel 8", str(root))
    with database.tx() as c:
        c.execute(
            """INSERT INTO finding(project_id, run_id, rule, severity, segment,
                                   subject, message)
               VALUES(?, 'r1', 'MTR-02', 'critical', '16 PW',
                      'ORTEGA FORJA, S.COOP.',
                      'Heat 071B33 came from OÑATI — em dash and accents')""",
            (pid,))
    return database, pid, root


@pytest.fixture
def client(job, tmp_path):
    database, pid, root = job
    return TestClient(create_app(tmp_path / "t.db")), pid, root


# -- finding the folder ------------------------------------------------------

def test_browsing_starts_somewhere_useful(client):
    api, _pid, _root = client
    body = api.get("/api/browse").json()
    assert body["folders"], "no starting points offered"
    assert any(Path.home().name in f["path"] for f in body["folders"])


def test_browsing_lists_sub_folders_with_real_paths(client, tmp_path):
    """The picker exists because a browser will not reveal a path; the server
    has to return absolute ones or nothing can be audited from them."""
    api, _pid, root = client
    body = api.get("/api/browse", params={"path": str(root)}).json()
    names = [f["name"] for f in body["folders"]]
    assert "11 NDE" in names
    assert Path(body["folders"][0]["path"]).is_absolute()
    assert body["up"] == str(root.parent)


def test_browsing_something_that_is_not_a_folder_says_so(client, tmp_path):
    api, _pid, _root = client
    missing = tmp_path / "nope"
    assert api.get("/api/browse", params={"path": str(missing)}).status_code == 400


# -- getting the report out --------------------------------------------------

def test_saving_puts_the_report_beside_the_job(client):
    api, pid, root = client
    body = api.get("/api/export",
                   params={"project_id": pid, "fmt": "csv", "to": "job"}).json()
    assert body["saved"] and not body["fell_back"]
    written = Path(body["path"])
    assert written.parent == root / "WeldAudit Reports"
    assert written.is_file()


def test_excel_saves_beside_the_job_too(client):
    api, pid, root = client
    body = api.get("/api/export",
                   params={"project_id": pid, "fmt": "xlsx", "to": "job"}).json()
    assert Path(body["path"]).suffix == ".xlsx"
    assert Path(body["path"]).is_file()


def test_downloading_returns_the_file_rather_than_a_path(client):
    api, pid, _root = client
    r = api.get("/api/export",
                params={"project_id": pid, "fmt": "csv", "to": "download"})
    assert r.status_code == 200
    assert "attachment" in r.headers.get("content-disposition", "")
    assert b"MTR-02" in r.content


def test_an_unwritable_job_folder_falls_back_and_says_where(client, monkeypatch):
    """Job folders live on shares that go read-only. Failing with nothing is
    the wrong answer when the report has already been produced."""
    api, pid, _root = client
    import weldaudit.report as report

    real = report.write_csv
    calls = {"n": 0}

    def refuse_the_first(db, project_id, path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("read-only share")
        return real(db, project_id, path)

    monkeypatch.setattr("weldaudit.api.write_csv", refuse_the_first)
    body = api.get("/api/export",
                   params={"project_id": pid, "fmt": "csv", "to": "job"}).json()
    assert body["saved"] and body["fell_back"]
    assert "read-only share" in body["reason"] or "PermissionError" in body["reason"]
    assert Path(body["path"]).is_file()


def test_an_unknown_format_is_refused(client):
    api, pid, _root = client
    r = api.get("/api/export", params={"project_id": pid, "fmt": "pdf", "to": "job"})
    assert r.status_code == 400


# -- the CSV itself ----------------------------------------------------------

def test_the_csv_opens_correctly_in_excel(job, tmp_path):
    """Without a BOM, Excel on Windows reads UTF-8 as cp1252 and mangles the
    mill names the findings quote."""
    database, pid, _root = job
    out = write_csv(database, pid, tmp_path / "x.csv")
    assert out.read_bytes().startswith(b"\xef\xbb\xbf")

    with out.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0][:2] == ["Severity", "Rule"]
    assert rows[1][1] == "MTR-02"
    assert "OÑATI" in rows[1][4]


# -- entering what you read off the page ------------------------------------

def test_a_correction_is_saved_and_applied_at_once(client, job):
    """Making somebody re-run a whole job to see their own correction take
    effect is a good way to stop them entering any."""
    api, pid, _root = client
    database, _pid, _r = job
    with database.tx() as c:
        c.execute(
            """INSERT INTO document(id, project_id, path, filename, ext,
                                    fingerprint, segment, kind)
               VALUES(9, ?, 'c.pdf', 'TEX.pdf', '.pdf', 'fpTEX', 'S', 'mtr')""",
            (pid,))
        c.execute(
            """INSERT INTO material(project_id, document_id, segment, heat,
                                    heat_key, source, manufacturer, confidence)
               VALUES(?, 9, 'S', 'H1', 'H1', 'mtr_file', 'TECKCUBO', 'vision')""",
            (pid,))
    r = api.post("/api/correct", json={"project_id": pid, "document_id": 9,
                                       "field": "manufacturer", "value": "Tex Tubo"})
    assert r.status_code == 200 and r.json()["applied"] == 1
    row = database.one("SELECT manufacturer, confidence FROM material")
    assert row["manufacturer"] == "Tex Tubo" and row["confidence"] == "human"


def test_a_field_nobody_may_correct_is_refused(client):
    api, pid, _root = client
    r = api.post("/api/correct", json={"project_id": pid, "document_id": 1,
                                       "field": "severity", "value": "minor"})
    assert r.status_code == 400


def test_correcting_a_document_that_does_not_exist(client):
    api, pid, _root = client
    r = api.post("/api/correct", json={"project_id": pid, "document_id": 999,
                                       "field": "manufacturer", "value": "X"})
    assert r.status_code == 404


# -- taking a decision back ---------------------------------------------------
#
# Accept sits one button away from dismiss, and the row leaves the default view
# the moment either is pressed. The finding that just vanished may have been
# the critical one, so the way back has to be as cheap as the way in.

def _status(db, fid):
    return db.one("SELECT status, note FROM finding WHERE id=?", (fid,))


@pytest.fixture
def finding_id(job):
    database, _pid, _root = job
    return database.one("SELECT id FROM finding")["id"], database


def test_a_finding_can_be_put_back_on_the_list(client, finding_id):
    api, _pid, _root = client
    fid, db = finding_id
    api.post(f"/api/findings/{fid}/status", json={"status": "accepted"})
    assert _status(db, fid)["status"] == "accepted"

    r = api.post(f"/api/findings/{fid}/status", json={"status": "open"})
    assert r.status_code == 200
    assert _status(db, fid)["status"] == "open"


def test_the_reply_says_what_it_replaced(client, finding_id):
    """An undo button has to know where to go back to, and the browser's copy
    of the row may be stale or belong to another window."""
    api, _pid, _root = client
    fid, _db = finding_id
    first = api.post(f"/api/findings/{fid}/status", json={"status": "accepted"})
    assert first.json()["was"] == "open"
    second = api.post(f"/api/findings/{fid}/status", json={"status": "dismissed"})
    assert second.json()["was"] == "accepted"


def test_a_status_change_without_a_note_keeps_the_note(client, finding_id):
    """This wrote NULL unconditionally, so every undo threw away the reason
    somebody had typed for the decision being undone."""
    api, _pid, _root = client
    fid, db = finding_id
    api.post(f"/api/findings/{fid}/status",
             json={"status": "accepted", "note": "cleared with the mill"})
    api.post(f"/api/findings/{fid}/status", json={"status": "dismissed"})
    assert _status(db, fid)["note"] == "cleared with the mill"


def test_reopening_keeps_the_comment(client, finding_id):
    """This once cleared the note, on the reasoning that a justification for an
    acceptance should not hang on a finding that is open again. That column has
    since become the Comments column an auditor types into, so clearing it now
    would delete their own work every time they changed their mind."""
    api, _pid, _root = client
    fid, db = finding_id
    api.post(f"/api/findings/{fid}/status",
             json={"status": "accepted", "note": "cleared with the mill"})
    api.post(f"/api/findings/{fid}/status", json={"status": "open"})
    row = _status(db, fid)
    assert row["status"] == "open"
    assert row["note"] == "cleared with the mill"


def test_an_unknown_status_is_refused(client, finding_id):
    api, _pid, _root = client
    fid, _db = finding_id
    r = api.post(f"/api/findings/{fid}/status", json={"status": "resolved"})
    assert r.status_code == 400


def test_marking_a_finding_that_does_not_exist(client):
    api, _pid, _root = client
    r = api.post("/api/findings/999999/status", json={"status": "accepted"})
    assert r.status_code == 404
