"""Offering the page readings that came with the program.

The readings do not travel with the exe -- they live in each user's profile --
and an audit does not run OCR. A colleague handed the program and pointed at
the same folder therefore reads nothing off the scanned certificates: 2
manufacturers named out of 37 instead of 28, and none of the
approved-manufacturer findings, three of them critical.

A transfer file fixed that in principle from the moment it existed. It did not
fix it in practice, because loading one was a command-line step on machines
where nobody opens a command line. This is the difference between the two.
"""

import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from weldaudit.api import create_app  # noqa: E402
from weldaudit.db import Database  # noqa: E402


@pytest.fixture
def a_cache(tmp_path):
    """A transfer file holding three readings."""
    source = Database(tmp_path / "source.db")
    with source.tx() as c:
        for i in range(3):
            c.execute("""INSERT INTO ocr_cache(sha1, page_no, model, payload)
                         VALUES(?,0,'claude-haiku-4-5','{}')""", (f"h{i}",))
    out = tmp_path / "beside" / "page-readings.wacache"
    out.parent.mkdir()
    source.export_cache(out)
    return out


@pytest.fixture(autouse=True)
def only_look_here(tmp_path, monkeypatch):
    """Confine the search to this test's own directory.

    The real search walks the Desktop, Downloads and every drive's WeldAudit
    folder, which is what makes it useful and what makes it untestable: with a
    USB stick plugged in, six of these tests failed because the program
    correctly found the readings on it.
    """
    import weldaudit.api as api

    monkeypatch.setattr(api, "_cache_places", lambda: [Path.cwd()])


@pytest.fixture
def fresh(tmp_path, a_cache, monkeypatch):
    """A machine that has never read a page, with the file sitting beside it."""
    monkeypatch.chdir(a_cache.parent)
    return TestClient(create_app(tmp_path / "fresh.db"))


def test_a_machine_with_nothing_is_offered_what_is_beside_it(fresh, a_cache):
    body = fresh.get("/api/readings").json()
    assert body["cached"] == 0
    assert Path(body["offer"]).name == "page-readings.wacache"
    assert body["offered"] == 3


def test_accepting_the_offer_loads_them(fresh):
    offer = fresh.get("/api/readings").json()["offer"]
    got = fresh.post("/api/readings/import", json={"path": offer}).json()
    assert got == {"in_the_file": 3, "added": 3, "already_here": 0}
    assert fresh.get("/api/readings").json()["cached"] == 3


def test_a_machine_that_already_has_readings_is_not_pestered(fresh):
    """The offer is for the machine that has read nothing. Once it has, the
    question is answered and asking again is noise."""
    offer = fresh.get("/api/readings").json()["offer"]
    fresh.post("/api/readings/import", json={"path": offer})
    assert fresh.get("/api/readings").json()["cached"] == 3
    # the page only offers when cached is 0; that is the whole condition
    assert fresh.get("/api/readings").json()["offered"] == 3


def test_nothing_beside_the_program_means_no_offer(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    api = TestClient(create_app(tmp_path / "t.db"))
    assert api.get("/api/readings").json()["offer"] is None


def test_a_file_that_is_not_a_cache_is_not_offered(tmp_path, monkeypatch):
    here = tmp_path / "junk"
    here.mkdir()
    (here / "notes.wacache").write_text("not a database")
    monkeypatch.chdir(here)
    api = TestClient(create_app(tmp_path / "t.db"))
    assert api.get("/api/readings").json()["offer"] is None


def test_an_empty_cache_is_not_offered(tmp_path, monkeypatch):
    """Nothing to gain, and an offer of nothing is worse than silence."""
    here = tmp_path / "hollow"
    here.mkdir()
    Database(tmp_path / "src.db").export_cache(here / "page-readings.wacache")
    monkeypatch.chdir(here)
    api = TestClient(create_app(tmp_path / "t.db"))
    assert api.get("/api/readings").json()["offer"] is None


def test_a_missing_file_is_refused(fresh):
    assert fresh.post("/api/readings/import",
                      json={"path": "nowhere.wacache"}).status_code == 404


def test_junk_is_refused_with_a_reason(fresh, tmp_path):
    junk = tmp_path / "junk.bin"
    junk.write_text("not a database")
    r = fresh.post("/api/readings/import", json={"path": str(junk)})
    assert r.status_code == 400 and "not a WeldAudit cache" in r.json()["error"]
