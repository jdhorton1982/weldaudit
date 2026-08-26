"""Serving a document so the window can draw it, instead of downloading it.

The audit window used to open a finding's document with `target="_blank"`,
which on Windows launches a browser: the audit goes behind whatever opens and
the finding you were reading is two alt-tabs away. The panel that replaced it
can only work if the response says `inline` -- `FileResponse(path,
filename=...)` sends `attachment`, and an attachment in an iframe downloads
rather than displays.

So the header is the whole feature, and it is what these check.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from weldaudit.api import VIEWABLE, create_app  # noqa: E402
from weldaudit.db import Database  # noqa: E402

PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


@pytest.fixture
def app(tmp_path):
    db_path = tmp_path / "v.db"
    db = Database(db_path)
    pid = db.upsert_project("V", str(tmp_path))

    def add(name: str, body: bytes = PDF, stored: str | None = None) -> int:
        """Index a file. `stored` is the name the row carries, if not its own.

        The two differ because `filename` is a database column: Windows will
        not create a file with a quote in its name, but nothing stops that
        value reaching the column from an export, a rename, or another
        machine's filesystem.
        """
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        with db.tx() as c:
            cur = c.execute(
                "INSERT INTO document(project_id, path, filename, ext) VALUES(?,?,?,?)",
                (pid, str(path), stored or name, Path(name).suffix.lower()))
        return cur.lastrowid

    return TestClient(create_app(db_path)), add, tmp_path


def test_a_pdf_comes_back_inline_so_a_panel_can_show_it(app):
    client, add, _ = app
    r = client.get(f"/api/document/{add('70097-MTR.pdf')}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/pdf")
    assert r.headers["content-disposition"].startswith("inline")


def test_the_name_travels_with_it(app):
    # So that saving from the viewer produces a file named after the document
    # rather than after its id.
    client, add, _ = app
    r = client.get(f"/api/document/{add('70097-MTR.pdf')}")
    assert 'filename="70097-MTR.pdf"' in r.headers["content-disposition"]


def test_a_name_that_would_break_the_header_is_cleaned(app):
    # A quote or a newline would end the header early. Contractor filenames
    # are not a controlled vocabulary, and this one is a database value.
    client, add, _ = app
    doc = add("ordinary.pdf", stored='odd "quoted"\nname.pdf')
    disposition = client.get(f"/api/document/{doc}").headers["content-disposition"]
    assert disposition.count('"') == 2, disposition
    assert "\n" not in disposition
    assert disposition.startswith("inline")


def test_a_spreadsheet_still_downloads(app):
    # The window cannot draw one, so it keeps the old behaviour and goes to
    # whichever program is registered for it.
    client, add, _ = app
    r = client.get(f"/api/document/{add('weld log.xlsx', b'PK\\x03\\x04')}")
    assert r.headers["content-disposition"].startswith("attachment")


def test_a_download_can_still_be_asked_for(app):
    client, add, _ = app
    r = client.get(f"/api/document/{add('70097-MTR.pdf')}?download=true")
    assert r.headers["content-disposition"].startswith("attachment")


@pytest.mark.parametrize("name", ["scan.png", "photo.JPG", "sheet.webp"])
def test_images_are_viewable_too(app, name):
    # A weld map photographed rather than plotted is the ordinary case.
    client, add, _ = app
    r = client.get(f"/api/document/{add(name, b'\\x89PNG')}")
    assert r.headers["content-disposition"].startswith("inline")


def test_the_viewable_list_is_lower_case(app):
    # It is matched against `Path(...).suffix.lower()`, so an upper-case entry
    # would silently never match and that document would quietly download.
    assert all(ext == ext.lower() for ext in VIEWABLE)
    assert all(ext.startswith(".") for ext in VIEWABLE)


def test_a_missing_file_is_still_a_404(app):
    client, add, tmp = app
    doc = add("gone.pdf")
    (tmp / "gone.pdf").unlink()
    assert client.get(f"/api/document/{doc}").status_code == 404


def test_where_it_is_says_whether_it_is_still_there(app):
    client, add, tmp = app
    doc = add("70097-MTR.pdf")
    said = client.get(f"/api/document/{doc}/where").json()
    assert said["filename"] == "70097-MTR.pdf"
    assert said["on_disk"] is True

    (tmp / "70097-MTR.pdf").unlink()
    # Still answers, so "Open folder" can say the file moved rather than
    # failing silently.
    assert client.get(f"/api/document/{doc}/where").json()["on_disk"] is False


def test_the_window_no_longer_hands_documents_to_a_browser():
    # The regression this whole change exists to prevent.
    page = (Path(__file__).resolve().parents[1]
            / "weldaudit" / "web" / "index.html").read_text(encoding="utf-8")
    # Only the markup, not the prose: the comment explaining why this changed
    # naturally quotes the thing it is explaining.
    linking = [ln for ln in page.splitlines()
               if "/api/document/" in ln and not ln.strip().startswith(("//", "*", "<!--"))]
    assert linking, "no document link found at all"
    for line in linking:
        assert "target=" not in line, line.strip()
    assert "viewDoc(" in page
    assert 'id="viewer-frame"' in page


# -- nothing the window shows may be cached ---------------------------------

def test_the_page_and_the_polled_endpoints_are_never_cached(app):
    """An update replaces the file behind a fixed address.

    The interface is one page at one URL, and updating swaps the file without
    the URL changing. With no cache directive the window guesses, and a copy
    opened after an update can show the previous interface while running the
    new program — the exe is new, the file is new, the server returns the new
    page, and the screen does not.

    A cached `/api/status` is a progress bar that never moves, and a cached
    `/api/update` is an update that is never offered.
    """
    client, _add, _tmp = app
    for path in ("/", "/api/status", "/api/update"):
        said = client.get(path).headers.get("cache-control", "")
        assert "no-store" in said, f"{path} may be cached: {said!r}"


def test_a_served_document_is_not_cached_either(app):
    # A corrected reading, or a certificate replaced on disk, must not be
    # masked by a copy the window kept.
    client, add, _tmp = app
    r = client.get(f"/api/document/{add('70097-MTR.pdf')}")
    assert "no-store" in r.headers.get("cache-control", "")
    # and it is still inline, so the viewer can draw it
    assert r.headers["content-disposition"].startswith("inline")


# -- the timing outlives the run --------------------------------------------

def test_a_project_never_audited_has_no_timing_and_says_so(app):
    # The deck stays hidden rather than reading zero.
    client, _add, _tmp = app
    said = client.get("/api/timing?project_id=1").json()
    assert said["timing"] == {}


def test_the_timing_is_read_back_from_the_run_record(app, tmp_path):
    """The bug this closes: gauges visible for the eleven seconds of a run.

    Timing lived on the in-memory job, so closing the program threw it away.
    Open WeldAudit the next morning and the deck was hidden — which looks
    exactly like a build where the feature was never added, and was reported
    as one.
    """
    import json

    client, _add, _t = app
    db = Database(tmp_path / "v.db")
    measured = {"elapsed": 5.16, "phases": [
        {"stage": "index", "seconds": 0.08, "steps": [
            {"name": "Scanning", "seconds": 0.08}], "running": False},
        {"stage": "extract", "seconds": 5.02, "steps": [
            {"name": "Loading approved materials list", "seconds": 5.01}],
         "running": False}]}
    with db.tx() as c:
        c.execute("INSERT INTO run(id, project_id, started_at, finished_at, "
                  "summary, timing) VALUES(?,?,?,?,?,?)",
                  ("r1", 1, "2026-08-26T10:00:00+00:00",
                   "2026-08-26T10:00:05+00:00", "{}", json.dumps(measured)))

    said = client.get("/api/timing?project_id=1").json()
    assert said["timing"]["elapsed"] == 5.16
    assert [p["stage"] for p in said["timing"]["phases"]] == ["index", "extract"]
    assert said["when"] == "2026-08-26T10:00:05+00:00"


def test_the_newest_run_wins(app, tmp_path):
    import json

    client, _add, _t = app
    db = Database(tmp_path / "v.db")
    with db.tx() as c:
        for rid, when, elapsed in [("old", "2026-08-01T09:00:00+00:00", 99.0),
                                   ("new", "2026-08-26T10:00:05+00:00", 5.16)]:
            c.execute("INSERT INTO run(id, project_id, started_at, finished_at,"
                      " summary, timing) VALUES(?,?,?,?,?,?)",
                      (rid, 1, when, when, "{}",
                       json.dumps({"elapsed": elapsed, "phases": []})))
    assert client.get("/api/timing?project_id=1").json()["timing"]["elapsed"] == 5.16


def test_unreadable_timing_is_not_fatal(app, tmp_path):
    # A row written by a version that stored something else there.
    client, _add, _t = app
    db = Database(tmp_path / "v.db")
    with db.tx() as c:
        c.execute("INSERT INTO run(id, project_id, started_at, finished_at,"
                  " summary, timing) VALUES(?,?,?,?,?,?)",
                  ("r1", 1, "x", "x", "{}", "not json"))
    assert client.get("/api/timing?project_id=1").json()["timing"] == {}


def test_the_page_paints_the_deck_when_a_project_is_opened():
    page = (Path(__file__).resolve().parents[1]
            / "weldaudit" / "web" / "index.html").read_text(encoding="utf-8")
    assert "/api/timing?project_id=" in page
    assert "paintDeck" in page
