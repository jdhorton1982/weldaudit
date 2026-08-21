"""Looking a manufacturer up on the approved list, from the app.

The rule these tests exist to protect: **the search box and the audit must
give the same answer.** A lookup that said "approved" where the report said
"not on the AML" would be worse than no lookup at all, because it would be
used to wave the finding away. So the verdict comes from ``Aml.match`` — the
same call the rules make — and the tests below check the two agree rather
than checking the search in isolation.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from weldaudit.aml import Aml, AmlEntry, SizeLimit, normalise_manufacturer  # noqa: E402
from weldaudit.amlsearch import categories, search  # noqa: E402
from weldaudit.api import create_app  # noqa: E402
from weldaudit.db import Database  # noqa: E402


def entry(mfr, category="1.0 Pipe", location="Houston, TX, USA",
          limits="", min_nps=None, max_nps=None):
    size = SizeLimit(min_nps, max_nps) if (min_nps or max_nps) else None
    return AmlEntry(category=category, manufacturer=mfr, location=location,
                    limits_raw=limits, size_limit=size,
                    key=normalise_manufacturer(mfr))


@pytest.fixture
def aml():
    return Aml([
        entry("Norvale Dalmine", location="Bergamo, ITALY"),
        entry("Norvale Algoma", location="Sault Ste. Marie, Ontario, CANADA"),
        entry("Halden Steelworks", limits="Up to NPS 20", max_nps=20.0),
        entry("Bonney Forge", category="2.0 Pipe Fittings",
              location="Mount Union, PA, USA"),
        entry("Balon Corp", category="7.0 Valves - Ball",
              limits="Up to NPS 12", max_nps=12.0),
    ])


# -- the verdict -------------------------------------------------------------

def test_a_name_on_the_list_comes_back_approved(aml):
    v = search(aml, "Halden Steelworks")["verdict"]
    assert v["status"] == "approved"
    assert v["score"] == 100
    assert v["names"] == ["Halden Steelworks"]


def test_a_name_that_is_nowhere_near_is_not_listed(aml):
    assert search(aml, "Consolidated Widget Co")["verdict"]["status"] == "not_listed"


def test_the_verdict_is_the_matcher_the_audit_uses(aml):
    """Not a second implementation. If these could differ, the box could be
    used to wave away a finding the report was right about."""
    for name in ["Norvale", "norvale dalmine", "Halden", "Nobody At All"]:
        assert search(aml, name)["verdict"]["status"] == aml.match(name).status


def test_the_reason_and_score_come_through(aml):
    """Shown on purpose: 'matched on a leading-word prefix, 95' is how
    somebody understands why a finding said what it said."""
    v = search(aml, "Norvale")["verdict"]
    assert v["reason"]
    assert 0 < v["score"] <= 100


def test_an_empty_query_has_no_verdict_and_lists_everything(aml):
    out = search(aml, "")
    assert out["verdict"] is None
    assert out["total"] == 5


# -- the rows ----------------------------------------------------------------

def test_matched_entries_are_marked_and_come_first(aml):
    rows = search(aml, "Halden Steelworks")["rows"]
    assert rows[0]["matched"] is True
    assert rows[0]["manufacturer"] == "Halden Steelworks"


def test_a_substring_search_finds_what_the_matcher_did_not(aml):
    """The point of showing rows at all: 'not listed' is worth little unless
    you can see what the list does hold near that name."""
    out = search(aml, "Bergamo")
    assert out["verdict"]["status"] == "not_listed"
    assert [r["manufacturer"] for r in out["rows"]] == ["Norvale Dalmine"]


def test_a_search_matches_the_limits_text_too(aml):
    assert {r["manufacturer"] for r in search(aml, "Up to NPS")["rows"]} \
        == {"Halden Steelworks", "Balon Corp"}


def test_a_category_narrows_the_rows(aml):
    out = search(aml, "", category="7.0 Valves - Ball")
    assert [r["manufacturer"] for r in out["rows"]] == ["Balon Corp"]


def test_categories_are_listed_in_the_lists_own_order(aml):
    assert categories(aml) == ["1.0 Pipe", "2.0 Pipe Fittings", "7.0 Valves - Ball"]


def test_identical_rows_are_collapsed():
    """The issued PDF sub-divides a category by a heading the parser drops —
    'Top Entry', then 'Two Piece' — so one manufacturer lands a dozen times
    with every visible field the same. Nothing distinguishes the copies."""
    aml = Aml([entry("AMPO", category="7.0 Valves - Ball", limits="Brand: POYAM")
               for _ in range(12)])
    assert search(aml, "AMPO")["total"] == 1


def test_rows_differing_in_any_shown_field_are_both_kept():
    aml = Aml([entry("Bonney Forge", location="Mount Union, PA, USA"),
               entry("Bonney Forge", location="Shanghai, CHINA")])
    assert search(aml, "Bonney")["total"] == 2


# -- the size half -----------------------------------------------------------

def test_a_size_within_the_limit_is_marked_as_covered(aml):
    row = [r for r in search(aml, "Halden", nps="12")["rows"]
           if r["manufacturer"] == "Halden Steelworks"][0]
    assert row["size"] == "allows"


def test_a_size_beyond_the_limit_is_marked_as_excluded(aml):
    row = [r for r in search(aml, "Halden", nps="24")["rows"]
           if r["manufacturer"] == "Halden Steelworks"][0]
    assert row["size"] == "excludes"


def test_approved_on_the_name_but_not_for_the_size_is_its_own_verdict(aml):
    """A mill cleared 'Up to NPS 20' supplying 24" pipe passes on name and
    fails on size, and the name is the half people check."""
    assert search(aml, "Halden Steelworks", nps="24")["verdict"]["status"] == "size"
    assert search(aml, "Halden Steelworks", nps="20")["verdict"]["status"] == "approved"


def test_an_entry_with_no_size_limit_is_never_marked(aml):
    row = search(aml, "Norvale Dalmine", nps="36")["rows"][0]
    assert row["size"] == ""


def test_a_fraction_is_understood(aml):
    assert search(aml, "Balon", nps='1/2')["rows"][0]["size"] == "allows"


def test_no_size_given_marks_nothing(aml):
    assert all(r["size"] == "" for r in search(aml, "Halden")["rows"])


# -- flags -------------------------------------------------------------------

def test_a_provisional_entry_is_flagged():
    aml = Aml([entry("* Newcomer Steel")])
    assert search(aml, "Newcomer")["rows"][0]["flag"] == "provisional"


def test_an_entry_on_hold_is_flagged():
    aml = Aml([entry("Sanctioned Mill", limits="HOLD")])
    assert search(aml, "Sanctioned")["rows"][0]["flag"] == "hold"


def test_a_superseded_entry_is_flagged():
    aml = Aml([entry("Old Name (deleted)")])
    assert search(aml, "Old Name")["rows"][0]["flag"] == "superseded"


# -- through the API ---------------------------------------------------------

@pytest.fixture
def api(tmp_path):
    db = Database(tmp_path / "t.db")
    root = tmp_path / "Job"
    root.mkdir()
    pid = db.upsert_project("Job", root)
    with db.tx() as c:
        c.execute("""INSERT INTO aml_source(project_id, path, kind, revision,
                                            valid_thru, entries)
                     VALUES(?,?,?,?,?,?)""",
                  (pid, r"C:\lists\Piping AML.pdf", "pdf", "Sept 30, 2026",
                   "2026-09-30", 2))
        c.executemany(
            """INSERT INTO aml_entry(project_id, category, manufacturer, location,
                                     limits_raw, min_nps, max_nps, conditions, norm_name)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            [(pid, "1.0 Pipe", "Halden Steelworks", "Gent, BELGIUM",
              "Up to NPS 20", None, 20.0, "", "halden steelworks"),
             (pid, "1.0 Pipe", "Norvale Dalmine", "Bergamo, ITALY",
              "", None, None, "", "norvale dalmine")])
    return TestClient(create_app(tmp_path / "t.db")), pid


def test_the_endpoint_answers_a_lookup(api):
    client, pid = api
    r = client.get("/api/aml", params={"project_id": pid, "q": "Halden Steelworks"}).json()
    assert r["loaded"] is True
    assert r["verdict"]["status"] == "approved"
    assert r["rows"][0]["location"] == "Gent, BELGIUM"


def test_the_endpoint_names_the_list_it_searched(api):
    """So a report and a lookup can be shown to be about the same revision."""
    client, pid = api
    r = client.get("/api/aml", params={"project_id": pid}).json()
    assert r["source"]["revision"] == "Sept 30, 2026"
    assert r["entries"] == 2
    assert r["expired"] is False


def test_an_expired_list_says_so(tmp_path):
    db = Database(tmp_path / "t.db")
    root = tmp_path / "Job"
    root.mkdir()
    pid = db.upsert_project("Job", root)
    with db.tx() as c:
        c.execute("""INSERT INTO aml_source(project_id, path, kind, revision,
                                            valid_thru, entries) VALUES(?,?,?,?,?,?)""",
                  (pid, "x.pdf", "pdf", "Sept 30, 2019", "2019-09-30", 1))
        c.execute("""INSERT INTO aml_entry(project_id, category, manufacturer,
                                           location, limits_raw, norm_name)
                     VALUES(?,?,?,?,?,?)""",
                  (pid, "1.0 Pipe", "Somebody", "Houston", "", "somebody"))
    r = TestClient(create_app(tmp_path / "t.db")).get(
        "/api/aml", params={"project_id": pid}).json()
    assert r["expired"] is True


def test_a_job_with_no_list_says_so_rather_than_looking_empty(tmp_path):
    """An empty table and 'nothing matched' look identical on screen, and one
    of them means every approval check on this job was skipped."""
    db = Database(tmp_path / "t.db")
    root = tmp_path / "Job"
    root.mkdir()
    pid = db.upsert_project("Job", root)
    r = TestClient(create_app(tmp_path / "t.db")).get(
        "/api/aml", params={"project_id": pid}).json()
    assert r["loaded"] is False
    assert r["rows"] == []


def test_the_list_searched_is_the_one_the_job_was_audited_against(tmp_path):
    """Per project on purpose: a job run in June stays searchable against the
    June list after a new one is issued, which is the only honest way to
    explain a June finding."""
    db = Database(tmp_path / "t.db")
    june, july = tmp_path / "June", tmp_path / "July"
    june.mkdir()
    july.mkdir()
    a, b = db.upsert_project("June", june), db.upsert_project("July", july)
    with db.tx() as c:
        c.executemany("""INSERT INTO aml_entry(project_id, category, manufacturer,
                                               location, limits_raw, norm_name)
                         VALUES(?,?,?,?,?,?)""",
                      [(a, "1.0 Pipe", "Was Approved In June", "Houston", "", "was approved in june"),
                       (b, "1.0 Pipe", "Only On The July List", "Houston", "", "only on the july list")])
    client = TestClient(create_app(tmp_path / "t.db"))
    assert client.get("/api/aml", params={"project_id": a, "q": "June"}).json()[
        "verdict"]["status"] == "approved"
    assert client.get("/api/aml", params={"project_id": b, "q": "Was Approved In June"}).json()[
        "verdict"]["status"] == "not_listed"
