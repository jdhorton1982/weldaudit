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
