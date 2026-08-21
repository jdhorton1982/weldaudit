"""The exceptions report as a printable PDF.

A spreadsheet is what somebody works through; a PDF is what gets attached to
an email, walked into a meeting, or filed with the book. The rule that matters
is that it is the *same* report: the same findings under the same filter as
the Excel and the CSV, so the three cannot disagree about one job.

Built on PyMuPDF, which is already here to read the packages, rather than
adding a reporting library to a program that already takes half a minute to
start.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from weldaudit.api import create_app  # noqa: E402
from weldaudit.db import Database  # noqa: E402
from weldaudit.report import write_csv  # noqa: E402
from weldaudit.reportpdf import write_pdf  # noqa: E402


def _finding(db, pid, rule="MTR-02", segment="16 PW", subject="Norvale",
             message="not on the approved list", severity="critical",
             status="open"):
    with db.tx() as c:
        c.execute(
            """INSERT INTO finding(project_id, run_id, rule, severity, segment,
                                   subject, message, status)
               VALUES(?, 'r1', ?, ?, ?, ?, ?, ?)""",
            (pid, rule, severity, segment, subject, message, status))
        return c.execute("SELECT last_insert_rowid() i").fetchone()[0]


@pytest.fixture
def job(tmp_path):
    db = Database(tmp_path / "t.db")
    root = tmp_path / "Job"
    root.mkdir()
    return db, db.upsert_project("Kestrel 8", root)


@pytest.fixture
def api(job, tmp_path):
    return TestClient(create_app(tmp_path / "t.db"))


def text_of(path):
    import pymupdf

    doc = pymupdf.open(path)
    # The ligature MuPDF emits for "fi" would otherwise break every search
    # for a word like "findings" or "certificate".
    return "\n".join(p.get_text() for p in doc).replace("\ufb01", "fi") \
                                                .replace("\ufb02", "fl")


# -- it is a real PDF --------------------------------------------------------

def test_a_readable_pdf_comes_out(api, job, tmp_path):
    db, pid = job
    fid = _finding(db, pid)
    api.post(f"/api/findings/{fid}/status", json={"status": "accepted"})
    out = write_pdf(db, pid, tmp_path / "r.pdf")
    assert out.read_bytes().startswith(b"%PDF")
    assert out.stat().st_size > 1000


def test_the_job_name_heads_the_report(api, job, tmp_path):
    db, pid = job
    fid = _finding(db, pid)
    api.post(f"/api/findings/{fid}/status", json={"status": "accepted"})
    assert "Kestrel 8" in text_of(write_pdf(db, pid, tmp_path / "r.pdf"))


def test_the_finding_and_its_document_are_both_printed(api, job, tmp_path):
    """A finding has to say which document and where, or somebody going back
    to correct the record has nowhere to go."""
    db, pid = job
    with db.tx() as c:
        c.execute("""INSERT INTO document(project_id, path, filename, ext,
                                          size_bytes, segment, kind)
                     VALUES(?,?,?,?,?,?,?)""",
                  (pid, r"C:\Jobs\16 PW\7 MTRS\EA6906.pdf", "EA6906.pdf",
                   ".pdf", 10, "16 PW", "mtr"))
        did = c.execute("SELECT last_insert_rowid() i").fetchone()[0]
        c.execute("UPDATE finding SET document_id=? WHERE project_id=?", (did, pid))
    fid = _finding(db, pid, subject="Kandal")
    with db.tx() as c:
        c.execute("UPDATE finding SET document_id=? WHERE id=?", (did, fid))
    api.post(f"/api/findings/{fid}/status", json={"status": "accepted"})

    printed = text_of(write_pdf(db, pid, tmp_path / "r.pdf"))
    assert "Kandal" in printed
    assert "EA6906.pdf" in printed


def test_a_comment_is_printed(api, job, tmp_path):
    db, pid = job
    fid = _finding(db, pid)
    api.post(f"/api/findings/{fid}/comment", json={"text": "cert on order"})
    api.post(f"/api/findings/{fid}/status", json={"status": "accepted"})
    assert "cert on order" in text_of(write_pdf(db, pid, tmp_path / "r.pdf"))


def test_the_pages_are_numbered(api, job, tmp_path):
    """A printed exception report gets separated, and a page with no number
    cannot be put back."""
    db, pid = job
    for n in range(40):
        fid = _finding(db, pid, rule=f"MTR-{n:02d}", subject=f"Heat {n}",
                       message="a reasonably long finding " * 6)
        api.post(f"/api/findings/{fid}/status", json={"status": "accepted"})
    out = write_pdf(db, pid, tmp_path / "r.pdf")

    import pymupdf

    doc = pymupdf.open(out)
    assert doc.page_count > 1
    assert f"page 1 of {doc.page_count}" in text_of(out)
    assert f"page {doc.page_count} of {doc.page_count}" in text_of(out)


# -- the same report as the other two ----------------------------------------

def test_it_carries_the_same_findings_as_the_csv(api, job, tmp_path):
    """The rule that matters. Three formats that disagree about one job would
    be worse than having only one."""
    db, pid = job
    keep = _finding(db, pid, rule="MTR-02", subject="Kandal")
    drop = _finding(db, pid, rule="AB-07", subject="StationTypo")
    _finding(db, pid, rule="NDE-01", subject="Unreviewed")
    api.post(f"/api/findings/{keep}/status", json={"status": "accepted"})
    api.post(f"/api/findings/{drop}/status", json={"status": "dismissed"})

    printed = text_of(write_pdf(db, pid, tmp_path / "r.pdf"))
    import csv
    with write_csv(db, pid, tmp_path / "r.csv").open(
            encoding="utf-8-sig", newline="") as fh:
        subjects = [r[3] for r in list(csv.reader(fh))[1:]]

    assert subjects == ["Kandal"]
    assert "Kandal" in printed
    assert "StationTypo" not in printed
    assert "Unreviewed" not in printed


def test_it_says_these_are_the_findings_marked_as_an_issue(api, job, tmp_path):
    """Said once at the top rather than on every item. Everything in this
    report is an Issue — that is the filter — so a per-item label would be a
    column of the same word, and the reader needs to know the *scope* of the
    document, not the status of each line."""
    db, pid = job
    for n in range(3):
        fid = _finding(db, pid, rule=f"MTR-0{n}", subject=f"s{n}")
        api.post(f"/api/findings/{fid}/status", json={"status": "accepted"})
    printed = text_of(write_pdf(db, pid, tmp_path / "r.pdf"))
    assert "3 findings marked as an issue" in printed


def test_one_finding_reads_in_the_singular(api, job, tmp_path):
    db, pid = job
    fid = _finding(db, pid)
    api.post(f"/api/findings/{fid}/status", json={"status": "accepted"})
    assert "1 finding marked as an issue" in text_of(write_pdf(db, pid, tmp_path / "r.pdf"))


def test_the_items_are_numbered(api, job, tmp_path):
    """So they can be referred to — "item 4 is the one we cleared"."""
    db, pid = job
    for n in range(3):
        fid = _finding(db, pid, rule=f"MTR-0{n}", subject=f"subject{n}")
        api.post(f"/api/findings/{fid}/status", json={"status": "accepted"})
    printed = text_of(write_pdf(db, pid, tmp_path / "r.pdf"))
    for n in ("1", "2", "3"):
        assert f"\n{n} " in printed or printed.startswith(f"{n} ")


def test_the_severity_is_printed_for_each(api, job, tmp_path):
    db, pid = job
    fid = _finding(db, pid, severity="critical")
    api.post(f"/api/findings/{fid}/status", json={"status": "accepted"})
    assert "CRITICAL" in text_of(write_pdf(db, pid, tmp_path / "r.pdf"))


def test_an_empty_report_says_why_it_is_empty(job, tmp_path):
    """The blank-report trap again: on paper there is no window to explain it,
    so the page has to say it itself."""
    db, pid = job
    _finding(db, pid)                       # left unreviewed, so held back
    printed = text_of(write_pdf(db, pid, tmp_path / "r.pdf"))
    assert "not the same as nothing being wrong" in printed


# -- through the API ---------------------------------------------------------

def test_the_endpoint_serves_a_pdf(api, job):
    db, pid = job
    fid = _finding(db, pid)
    api.post(f"/api/findings/{fid}/status", json={"status": "accepted"})
    r = api.get("/api/export", params={"project_id": pid, "fmt": "pdf",
                                       "to": "download"})
    assert r.status_code == 200
    assert r.content.startswith(b"%PDF")
    assert ".pdf" in r.headers.get("content-disposition", "")


def test_saving_to_the_job_folder_writes_a_pdf(api, job, tmp_path):
    db, pid = job
    fid = _finding(db, pid)
    api.post(f"/api/findings/{fid}/status", json={"status": "accepted"})
    body = api.get("/api/export", params={"project_id": pid, "fmt": "pdf",
                                          "to": "job"}).json()
    written = Path(body["path"])
    assert written.suffix == ".pdf"
    assert written.read_bytes().startswith(b"%PDF")


def test_an_unknown_format_is_still_refused(api, job):
    _db, pid = job
    r = api.get("/api/export", params={"project_id": pid, "fmt": "docx",
                                       "to": "job"})
    assert r.status_code == 400
