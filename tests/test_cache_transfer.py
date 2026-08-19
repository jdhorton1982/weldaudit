"""Carrying the page readings to another machine.

The cache lives in the user's profile, not in the exe, and an audit does not
run OCR. So a colleague handed the program and pointed at the same folder
starts with nothing read: the same job came back with 2 manufacturers named
out of 495 material rows instead of 28 of 37, and therefore with none of the
approved-manufacturer findings -- three of them critical. A shorter report
that reads like a cleaner package.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402


def _put(db, sha1, model, payload='{"heat":"1"}', page=0):
    with db.tx() as c:
        c.execute("""INSERT OR REPLACE INTO ocr_cache(sha1, page_no, model, payload)
                     VALUES(?,?,?,?)""", (sha1, page, model, payload))


@pytest.fixture
def mine(tmp_path):
    db = Database(tmp_path / "mine.db")
    _put(db, "aaa", "claude-haiku-4-5")
    _put(db, "bbb", "claude-haiku-4-5")
    _put(db, "ccc", "local:ocr")
    return db


@pytest.fixture
def theirs(tmp_path):
    return Database(tmp_path / "theirs.db")


def test_the_readings_arrive(mine, theirs, tmp_path):
    assert mine.export_cache(tmp_path / "c.wacache") == 3
    got = theirs.import_cache(tmp_path / "c.wacache")
    assert got == {"in_the_file": 3, "added": 3, "already_here": 0}
    assert theirs.cached_pages() == 3


def test_importing_twice_changes_nothing(mine, theirs, tmp_path):
    mine.export_cache(tmp_path / "c.wacache")
    theirs.import_cache(tmp_path / "c.wacache")
    again = theirs.import_cache(tmp_path / "c.wacache")
    assert again["added"] == 0 and theirs.cached_pages() == 3


def test_what_is_already_here_is_not_overwritten(mine, theirs, tmp_path):
    """The safe direction. A reading this machine already has wins, and the
    key carries the model, so a paid reading and a free one of the same page
    sit side by side for ocr_any to choose between."""
    _put(theirs, "aaa", "claude-haiku-4-5", payload='{"heat":"KEEP ME"}')
    mine.export_cache(tmp_path / "c.wacache")
    theirs.import_cache(tmp_path / "c.wacache")
    kept = theirs.one("SELECT payload FROM ocr_cache WHERE sha1='aaa'")["payload"]
    assert "KEEP ME" in kept


def test_a_free_and_a_paid_reading_of_one_page_coexist(mine, theirs, tmp_path):
    _put(mine, "ddd", "claude-haiku-4-5")
    _put(mine, "ddd", "local:ocr")
    mine.export_cache(tmp_path / "c.wacache")
    theirs.import_cache(tmp_path / "c.wacache")
    assert theirs.one("SELECT COUNT(*) c FROM ocr_cache WHERE sha1='ddd'")["c"] == 2


def test_the_payloads_survive_the_trip(mine, theirs, tmp_path):
    _put(mine, "eee", "claude-haiku-4-5", payload='{"issuing_company":"Norvale"}')
    mine.export_cache(tmp_path / "c.wacache")
    theirs.import_cache(tmp_path / "c.wacache")
    got = theirs.one("SELECT payload FROM ocr_cache WHERE sha1='eee'")["payload"]
    assert got == '{"issuing_company":"Norvale"}'


def test_exporting_over_an_old_file_replaces_it(mine, tmp_path):
    out = tmp_path / "c.wacache"
    mine.export_cache(out)
    _put(mine, "fff", "local:ocr")
    assert mine.export_cache(out) == 4


def test_a_missing_file_says_so(theirs, tmp_path):
    with pytest.raises(FileNotFoundError):
        theirs.import_cache(tmp_path / "nowhere.wacache")


def test_something_that_is_not_a_cache_says_so(theirs, tmp_path):
    junk = tmp_path / "junk.txt"
    junk.write_text("not a database")
    with pytest.raises(ValueError, match="not a WeldAudit cache"):
        theirs.import_cache(junk)


def test_an_export_is_not_tied_to_a_job(mine, theirs, tmp_path):
    """Keyed by the hash of the page, so it holds wherever the document sits
    — which is what makes handing it over worth anything."""
    mine.export_cache(tmp_path / "c.wacache")
    theirs.import_cache(tmp_path / "c.wacache")
    cols = {c["name"] for c in theirs.q("PRAGMA table_info(ocr_cache)")}
    assert "project_id" not in cols
