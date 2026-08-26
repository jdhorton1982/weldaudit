"""Agreeing to the terms before the program audits anybody's package.

A beta build reaches people the author never met, on machines holding a
customer's turnover package. What is recorded here is evidence of acceptance,
not a signature - it lives on the tester's own machine - and the thing it
reaches that a countersigned agreement does not is the second engineer who got
a copy from the person who signed.

Which is why the tests that matter are the ones about *not* being able to get
past it: an audit refused, an edited document asked about again, and a stale
page unable to record agreement to wording that has changed.

The real documents are gitignored, so nothing here reads them. Every test
points the loader at its own folder.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from weldaudit import agreements  # noqa: E402
from weldaudit.api import create_app  # noqa: E402
from weldaudit.db import Database  # noqa: E402

PRIVACY = "Privacy and security notice\n\nNothing leaves your machine.\n"
NDA = "Non-disclosure agreement\n\nDo not pass it on.\n"
PILOT = "Pilot agreement\n\nThe pilot runs for ninety days.\n"


@pytest.fixture
def papers(tmp_path, monkeypatch):
    """A build carrying three documents, and a database to record against."""
    folder = tmp_path / "agreements"
    folder.mkdir()
    (folder / "privacy-and-security.txt").write_text(PRIVACY, encoding="utf-8")
    (folder / "nda.txt").write_text(NDA, encoding="utf-8")
    (folder / "pilot-agreement.txt").write_text(PILOT, encoding="utf-8")
    monkeypatch.setattr(agreements, "folder", lambda: folder)

    db_path = tmp_path / "a.db"
    db = Database(db_path)
    return db, db_path, folder


@pytest.fixture
def client(papers):
    _db, db_path, _folder = papers
    return TestClient(create_app(db_path))


# -- what the build carries -------------------------------------------------

def test_the_documents_are_read_in_the_order_they_are_shown(papers):
    keys = [d.key for d in agreements.documents()]
    assert keys == ["privacy", "nda", "pilot"]
    assert agreements.gate_is_armed()


def test_the_first_line_is_the_title_and_the_rest_is_the_text(papers):
    privacy = agreements.documents()[0]
    assert privacy.title == "Privacy and security notice"
    assert privacy.body.startswith("Nothing leaves")
    assert "Privacy and security notice" not in privacy.body


def test_a_build_with_no_documents_does_not_gate(tmp_path, monkeypatch):
    # The same choice the approved list makes: a build the author did not put
    # the data folder into is a valid build. Refusing to start would be worse.
    monkeypatch.setattr(agreements, "folder", lambda: tmp_path / "nothing")
    assert agreements.documents() == []
    assert agreements.gate_is_armed() is False


# -- recording --------------------------------------------------------------

def test_an_acceptance_records_who_when_and_which_wording(papers):
    db, _p, _f = papers
    nda = agreements.documents()[1]
    agreements.record(db, nda, "A Welder", "Contractor Ltd", "a@example.invalid")

    row = agreements.accepted(db)[0]
    assert row["document_key"] == "nda"
    assert row["name"] == "A Welder"
    assert row["company"] == "Contractor Ltd"
    assert row["sha256"] == nda.sha256
    assert row["accepted_at"].endswith("+00:00")     # UTC, explicitly
    assert row["app_version"]


def test_an_acceptance_needs_a_name(papers):
    db, _p, _f = papers
    with pytest.raises(ValueError):
        agreements.record(db, agreements.documents()[0], "   ", "Co")


def test_nothing_is_outstanding_once_all_three_are_accepted(papers):
    db, _p, _f = papers
    assert len(agreements.outstanding(db)) == 3
    for doc in agreements.documents():
        agreements.record(db, doc, "A Welder", "Contractor Ltd")
    assert agreements.outstanding(db) == []


def test_editing_a_document_makes_it_outstanding_again(papers):
    # The reason identity is the hash rather than a version number somebody
    # remembers to bump: changed wording is wording nobody has agreed to.
    db, _p, folder = papers
    for doc in agreements.documents():
        agreements.record(db, doc, "A Welder", "Contractor Ltd")
    assert agreements.outstanding(db) == []

    (folder / "nda.txt").write_text(NDA + "\nAnd do not decompile it.\n",
                                    encoding="utf-8")
    waiting = agreements.outstanding(db)
    assert [d.key for d in waiting] == ["nda"]
    # The old acceptance is kept - it is a record of what was agreed then.
    assert len(agreements.accepted(db, "nda")) == 1


def test_a_changed_title_counts_as_a_changed_document(papers):
    db, _p, folder = papers
    for doc in agreements.documents():
        agreements.record(db, doc, "A Welder", "Co")
    (folder / "privacy-and-security.txt").write_text(
        "Privacy notice\n\nNothing leaves your machine.\n", encoding="utf-8")
    assert [d.key for d in agreements.outstanding(db)] == ["privacy"]


# -- the gate ---------------------------------------------------------------

def test_an_audit_is_refused_until_the_terms_are_answered(client, tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    r = client.post("/api/audit", json={"root": str(job)})
    assert r.status_code == 409
    # This app reports an HTTPException detail under "error", not "detail".
    assert r.json()["error"]["reason"] == "agreement"
    assert set(r.json()["error"]["documents"]) == {"privacy", "nda", "pilot"}


def test_an_audit_runs_once_they_are(client, papers, tmp_path):
    db, _p, _f = papers
    for doc in agreements.documents():
        agreements.record(db, doc, "A Welder", "Contractor Ltd")

    job = tmp_path / "job"
    job.mkdir()
    r = client.post("/api/audit", json={"root": str(job)})
    assert r.status_code != 409, r.text


def test_accepting_two_of_three_still_refuses(client, papers, tmp_path):
    db, _p, _f = papers
    for doc in agreements.documents()[:2]:
        agreements.record(db, doc, "A Welder", "Contractor Ltd")
    job = tmp_path / "job"
    job.mkdir()
    r = client.post("/api/audit", json={"root": str(job)})
    assert r.status_code == 409
    assert r.json()["error"]["documents"] == ["pilot"]


# -- through the API --------------------------------------------------------

def test_the_page_is_told_what_to_show(client):
    said = client.get("/api/agreements").json()
    assert said["armed"] is True
    assert [d["key"] for d in said["documents"]] == ["privacy", "nda", "pilot"]
    assert all(d["accepted"] is False for d in said["documents"])
    assert said["documents"][0]["body"]           # the text travels to the page


def test_accepting_through_the_api_records_it(client):
    doc = client.get("/api/agreements").json()["documents"][0]
    r = client.post("/api/agreements/accept", json={
        "document_key": doc["key"], "sha256": doc["sha256"],
        "name": "A Welder", "company": "Contractor Ltd"})
    assert r.status_code == 200
    assert r.json()["recorded"] == "privacy"
    assert "privacy" not in r.json()["outstanding"]


def test_a_stale_page_cannot_record_the_wrong_wording(client, papers):
    # A window left open across an update is showing yesterday's text. The
    # checksum is checked rather than trusted, so it cannot record agreement
    # to wording nobody is looking at.
    _db, _p, folder = papers
    doc = client.get("/api/agreements").json()["documents"][1]
    (folder / "nda.txt").write_text(NDA + "\nAnd do not decompile it.\n",
                                    encoding="utf-8")
    r = client.post("/api/agreements/accept", json={
        "document_key": "nda", "sha256": doc["sha256"], "name": "A Welder"})
    assert r.status_code == 409
    assert "changed" in str(r.json()["error"])


def test_an_acceptance_with_no_name_is_refused(client):
    doc = client.get("/api/agreements").json()["documents"][0]
    r = client.post("/api/agreements/accept", json={
        "document_key": doc["key"], "sha256": doc["sha256"], "name": "  "})
    assert r.status_code == 400


def test_an_unknown_document_is_a_404(client):
    r = client.post("/api/agreements/accept", json={
        "document_key": "invented", "sha256": "0" * 64, "name": "A Welder"})
    assert r.status_code == 404


# -- reading it again, afterwards -------------------------------------------
#
# The gate shows a document once and then never again, which left a tester who
# had agreed to an NDA unable to see what they agreed to. That is the opposite
# of what recording it was for: the first move in any argument about an
# agreement is "show me what I signed". The wording, and who accepted it and
# when, come back with every listing so the page can offer it.


def test_an_accepted_document_says_who_accepted_it_and_when(client):
    doc = client.get("/api/agreements").json()["documents"][0]
    client.post("/api/agreements/accept", json={
        "document_key": doc["key"], "sha256": doc["sha256"],
        "name": "A Welder", "company": "Contractor Ltd"})

    again = client.get("/api/agreements").json()["documents"][0]
    assert again["accepted"] is True
    assert again["accepted_by"] == "A Welder"
    assert again["accepted_company"] == "Contractor Ltd"
    assert again["accepted_at"]


def test_the_wording_comes_back_with_it(client):
    """Without the body there is nothing to re-read."""
    doc = client.get("/api/agreements").json()["documents"][0]
    client.post("/api/agreements/accept", json={
        "document_key": doc["key"], "sha256": doc["sha256"], "name": "A Welder"})
    again = client.get("/api/agreements").json()["documents"][0]
    assert again["body"] == doc["body"]
    assert again["sha256"] == doc["sha256"]


def test_a_document_nobody_accepted_carries_no_acceptance(client):
    doc = client.get("/api/agreements").json()["documents"][0]
    assert "accepted_by" not in doc
    assert "accepted_at" not in doc


def test_the_acceptance_returned_is_of_the_wording_on_offer(client, papers):
    """An edited document is a document nobody has accepted.

    Matching on the key alone would attach yesterday's acceptance to today's
    text and show a tester a name against wording that person never saw.
    """
    _db, _p, folder = papers
    doc = client.get("/api/agreements").json()["documents"][1]
    client.post("/api/agreements/accept", json={
        "document_key": "nda", "sha256": doc["sha256"], "name": "A Welder"})

    (folder / "nda.txt").write_text(NDA + "\nAnd do not decompile it.\n",
                                    encoding="utf-8")
    after = client.get("/api/agreements").json()["documents"][1]
    assert after["accepted"] is False
    assert "accepted_by" not in after


def test_the_helper_finds_the_acceptance_of_that_exact_wording(papers):
    db, _p, _folder = papers
    privacy, nda, _pilot = agreements.documents()
    agreements.record(db, privacy, "A Welder", "Contractor Ltd")

    got = agreements.acceptance_of(db, privacy)
    assert got is not None and got["name"] == "A Welder"
    assert agreements.acceptance_of(db, nda) is None


# -- the record leaves the machine ------------------------------------------

def test_the_record_reads_as_something_a_person_can_send(client, papers):
    db, _p, _f = papers
    for doc in agreements.documents():
        agreements.record(db, doc, "A Welder", "Contractor Ltd", "a@example.invalid")

    text = client.get("/api/agreements/record").text
    assert "Non-disclosure agreement" in text
    assert "A Welder (Contractor Ltd) <a@example.invalid>" in text
    assert "sha256" in text
    # A local row is only worth what the machine is worth, so it has to be
    # able to leave.
    assert client.get("/api/agreements/record").headers["content-disposition"] \
        .startswith("attachment")


def test_an_empty_record_says_so_rather_than_being_blank(client):
    assert "No agreement has been accepted" in client.get("/api/agreements/record").text
