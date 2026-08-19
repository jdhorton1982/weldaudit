"""Removing a stored audit.

One audit is kept per folder ever pointed at — including the same job indexed
twice under two names — and until now the only way to remove one was to delete
the whole database. That also threw away every page reading that had been paid
for, which is why nobody did it and why the list only ever grew.

What must survive is the point of these tests: the folder on disk, and the OCR
cache. What must not survive is any row belonging to the job.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from weldaudit.api import create_app  # noqa: E402
from weldaudit.db import Database  # noqa: E402


@pytest.fixture
def two_jobs(tmp_path):
    """Two audited folders, one of them holding a hand-typed correction."""
    keep_root = tmp_path / "Kestrel 8"
    drop_root = tmp_path / "Old Job"
    for r in (keep_root, drop_root):
        (r / "11 NDE").mkdir(parents=True)
    db = Database(tmp_path / "t.db")
    keep = db.upsert_project("Kestrel 8", str(keep_root))
    drop = db.upsert_project("Old Job", str(drop_root))

    with db.tx() as c:
        for pid, tag in ((keep, "K"), (drop, "D")):
            c.execute(
                """INSERT INTO document(project_id, path, filename, ext,
                                        fingerprint, segment, kind)
                   VALUES(?,?,?,'.pdf',?,'S','mtr')""",
                (pid, f"{tag}.pdf", f"{tag}.pdf", f"fp{tag}"))
            doc = c.execute("SELECT last_insert_rowid() i").fetchone()[0]
            c.execute(
                """INSERT INTO finding(project_id, run_id, rule, severity,
                                       segment, subject, message)
                   VALUES(?,'r1','MTR-02','critical','S',?,'m')""", (pid, tag))
            c.execute(
                """INSERT INTO material(project_id, document_id, segment, heat,
                                        heat_key, source, manufacturer)
                   VALUES(?,?,'S','H1','H1','mtr_file','Norvale')""", (pid, doc))
            c.execute(
                """INSERT INTO hydrotest(project_id, segment) VALUES(?, 'S')""",
                (pid,))
            hid = c.execute("SELECT last_insert_rowid() i").fetchone()[0]
            c.execute(
                """INSERT INTO hydrotest_reading(hydrotest_id, pressure)
                   VALUES(?, 1440)""", (hid,))
            c.execute(
                """INSERT INTO correction(project_id, fingerprint, field, value)
                   VALUES(?,?,'manufacturer','Tex Tubo')""", (pid, f"fp{tag}"))
        # A paid reading, keyed by file hash rather than by job.
        c.execute("""INSERT INTO ocr_cache(sha1, page_no, model, payload)
                     VALUES('fpD', 0, 'claude', '{}')""")
    return db, keep, drop, keep_root, drop_root


@pytest.fixture
def client(two_jobs, tmp_path):
    db, keep, drop, keep_root, drop_root = two_jobs
    return TestClient(create_app(tmp_path / "t.db")), db, keep, drop, drop_root


# -- what goes ---------------------------------------------------------------

def test_removing_an_audit_leaves_no_row_behind(client):
    """Every table is checked, not a chosen few: an orphan row for a job that
    no longer exists is invisible until some later query counts it."""
    api, db, keep, drop, _root = client
    assert api.delete(f"/api/projects/{drop}").status_code == 200

    for table in db.project_tables():
        n = db.one(f"SELECT COUNT(*) c FROM {table} WHERE project_id=?",
                   (drop,))["c"]
        assert n == 0, f"{table} still holds {n} rows for the removed audit"
    assert db.one("SELECT COUNT(*) c FROM project WHERE id=?", (drop,))["c"] == 0


def test_the_grandchildren_go_too(client):
    """hydrotest_reading and the coating tables carry no project_id, so nothing
    that searches for one will ever find them."""
    api, db, _keep, drop, _root = client
    api.delete(f"/api/projects/{drop}")
    left = db.q("""SELECT r.id FROM hydrotest_reading r
                   LEFT JOIN hydrotest h ON h.id = r.hydrotest_id
                   WHERE h.id IS NULL""")
    assert left == []


def test_the_database_is_left_consistent(client):
    api, db, _keep, drop, _root = client
    api.delete(f"/api/projects/{drop}")
    assert db.q("PRAGMA foreign_key_check") == []


# -- what stays --------------------------------------------------------------

def test_the_other_audit_is_untouched(client):
    api, db, keep, drop, _root = client
    api.delete(f"/api/projects/{drop}")
    assert db.one("SELECT COUNT(*) c FROM finding WHERE project_id=?",
                  (keep,))["c"] == 1
    assert db.one("SELECT COUNT(*) c FROM correction WHERE project_id=?",
                  (keep,))["c"] == 1


def test_the_folder_on_disk_is_not_touched(client):
    """The single thing this must never do."""
    api, _db, _keep, drop, drop_root = client
    api.delete(f"/api/projects/{drop}")
    assert drop_root.is_dir() and (drop_root / "11 NDE").is_dir()


def test_pages_already_paid_for_are_kept(client):
    """ocr_cache is keyed by file hash, so re-auditing the same folder costs
    nothing. If a delete wiped it, nobody could afford to use this."""
    api, db, _keep, drop, _root = client
    body = api.delete(f"/api/projects/{drop}").json()
    assert db.one("SELECT COUNT(*) c FROM ocr_cache")["c"] == 1
    assert body["cached_pages_kept"] == 1


# -- what it tells you -------------------------------------------------------

def test_the_reply_says_what_it_removed(client):
    api, _db, _keep, drop, _root = client
    body = api.delete(f"/api/projects/{drop}").json()
    assert body["name"] == "Old Job"
    assert body["findings"] == 1 and body["documents"] == 1
    assert body["typed_by_hand"] == 1


def test_the_listing_says_enough_to_judge_what_is_old(client, tmp_path):
    """A count of hand-typed values, and whether the folder is still there."""
    api, _db, _keep, drop, drop_root = client
    rows = api.get("/api/projects").json()
    old = next(r for r in rows if r["id"] == drop)
    assert old["documents"] == 1
    assert old["typed_by_hand"] == 1
    assert old["folder_here"] is True

    import shutil
    shutil.rmtree(drop_root)
    again = next(r for r in api.get("/api/projects").json() if r["id"] == drop)
    assert again["folder_here"] is False


def test_removing_an_audit_that_does_not_exist(client):
    api, _db, _keep, _drop, _root = client
    assert api.delete("/api/projects/999999").status_code == 404


def test_ocr_cache_is_never_treated_as_project_data(client):
    """The guard behind every test above: the discovery that drives the delete
    must not find the cache, however the schema changes."""
    _api, db, _keep, _drop, _root = client
    assert "ocr_cache" not in db.project_tables()
    assert "correction" in db.project_tables()


# -- the order the deletes run in --------------------------------------------

def test_documents_go_after_everything_that_points_at_them(client):
    """The bug this ordering exists for. Alphabetically `document` is fifth,
    ahead of `finding` and `material`, and the delete dies on a foreign key
    with the audit half removed."""
    _api, db, _keep, _drop, _root = client
    order = db.delete_order(db.project_tables())
    assert order.index("document") > order.index("finding")
    assert order.index("document") > order.index("material")
    assert order[-1] == "document"


def test_every_table_appears_exactly_once(client):
    _api, db, _keep, _drop, _root = client
    tables = db.project_tables()
    order = db.delete_order(tables)
    assert sorted(order) == sorted(tables)
    assert len(order) == len(set(order))
