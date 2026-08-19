"""Reading the approved manufacturer list out of the PDF the operator issues.

The AML decides whether material passes, so the failure that matters here is
not a crash. It is a parse that quietly returns most of the list: every
manufacturer it failed to read comes back "not on the approved list", which in
the report is indistinguishable from a manufacturer that genuinely is not on
it. Most of these tests are about refusing rather than reading.
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit import amlpdf  # noqa: E402
from weldaudit.db import Database  # noqa: E402

#: The real document, when this machine has it. Nothing synthetic exercises
#: the layout quirks that actually broke the parser.
def _an_aml_pdf_on_this_machine():
    """An issued AML on this machine, if there is one. Discovered rather than
    written down: a customer's path does not belong in a public test."""
    for start in (Path.cwd(), *Path.cwd().parents, Path.home()):
        for found in sorted(start.glob("*AML*.pdf")):
            return found
        if start == Path.home():
            break
    return None


REAL = _an_aml_pdf_on_this_machine()
needs_real = pytest.mark.skipif(REAL is None, reason="no issued AML on this machine")


def _pdf(tmp_path, text, name="fake.pdf"):
    """A one-page PDF holding this text."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), text, fontsize=11)
    out = tmp_path / name
    doc.save(out)
    doc.close()
    return out


# -- the revision date -------------------------------------------------------

@pytest.mark.parametrize("printed,expect", [
    ("Valid Thru Sept 30, 2026", date(2026, 9, 30)),
    ("Valid Thru Sep 30, 2026", date(2026, 9, 30)),
    ("Valid Thru Mar 31, 2026", date(2026, 3, 31)),
    ("Valid Thru March 31, 2026", date(2026, 3, 31)),
    ("Valid Thru Dec 1, 2027", date(2027, 12, 1)),
])
def test_the_validity_date_is_read(tmp_path, printed, expect):
    """'Sept' is not a Python month abbreviation, and this document uses it."""
    _said, on = amlpdf.revision(_pdf(tmp_path, printed))
    assert on == expect


def test_an_unreadable_date_keeps_what_was_printed(tmp_path):
    said, on = amlpdf.revision(_pdf(tmp_path, "Valid Thru whenever"))
    assert on is None and "whenever" in said


def test_a_pdf_with_no_validity_line(tmp_path):
    assert amlpdf.revision(_pdf(tmp_path, "nothing here")) == ("", None)


# -- expiry ------------------------------------------------------------------

def test_expired_counts_days_past():
    assert amlpdf.expired(date(2026, 3, 31), date(2026, 8, 19)) == 141


def test_still_in_date_is_not_expired():
    assert amlpdf.expired(date(2026, 9, 30), date(2026, 8, 19)) is None
    assert amlpdf.expired(None) is None


def test_the_last_valid_day_is_not_expired():
    assert amlpdf.expired(date(2026, 8, 19), date(2026, 8, 19)) is None


# -- recognising the document ------------------------------------------------

def test_something_that_is_not_an_aml_is_not_treated_as_one(tmp_path):
    assert not amlpdf.looks_like_an_aml(_pdf(tmp_path, "Daily Weld Report"))


def test_an_unopenable_file_is_not_an_aml(tmp_path):
    bad = tmp_path / "torn.pdf"
    bad.write_bytes(b"not a pdf at all")
    assert not amlpdf.looks_like_an_aml(bad)


# -- refusing a bad parse ----------------------------------------------------

def test_a_missing_section_is_refused():
    """The failure this check exists for: section 11 headings are a single
    text span where section 1 uses two, and requiring two silently dropped
    every approved fastener supplier."""
    rows = [["1.0 Pipe", "Norvale", "Veracruz, MEXICO", "", "1.1", ""]]
    complaints, _per = amlpdf.check(rows)
    assert any("11.0 Fasteners" in c for c in complaints)


def test_an_entry_with_no_location_is_refused():
    rows = [["1.0 Pipe", "Norvale", "", "", "1.1", ""]]
    complaints, _ = amlpdf.check(rows)
    assert any("no location" in c for c in complaints)


def test_a_row_that_swallowed_its_neighbour_is_refused():
    """A one-line cell is centred against its multi-line neighbour, so a
    wrapped location sits above its own manufacturer. Getting that wrong
    merged two companies into one name and deleted the second."""
    rows = [["1.0 Pipe", "Saalfeld Mannesmann Rohr Sachsen (MRS) " * 3,
             "Zeithaim, GERMANY", "", "1.1", ""]]
    complaints, _ = amlpdf.check(rows)
    assert any("suspiciously long" in c for c in complaints)


def test_entries_raises_rather_than_returning_a_partial_list(tmp_path):
    with pytest.raises(ValueError):
        amlpdf.entries(_pdf(tmp_path, "Valid Thru Sept 30, 2026"))


# -- comparing revisions -----------------------------------------------------

def test_a_moved_marker_is_not_a_change():
    """(F) marks in-house forging. Between two revisions it moved from the
    front of the name to the back, and compared literally that reported the
    same company as both added and removed, on every flange page."""
    rows = [["3.0 Flanges", "Halden (F)", "Houston, TX, USA", "", "3.1", ""]]
    added, removed = amlpdf.compare(rows, [("(F) Halden", "Houston, TX, USA")])
    assert added == [] and removed == []


def test_a_real_change_is_reported():
    rows = [["3.0 Flanges", "Halden", "Houston, TX, USA", "", "3.1", ""]]
    added, removed = amlpdf.compare(rows, [("Kerkau", "Bay City, MI, USA")])
    assert [a[0] for a in added] == ["Halden"]
    assert [r[0] for r in removed] == ["Kerkau"]


# -- against the real document -----------------------------------------------

@needs_real
def test_the_real_list_parses_without_complaint():
    rows = amlpdf.parse(REAL)
    complaints, per = amlpdf.check(rows)
    assert not complaints, complaints
    assert len(rows) > 500
    # Every section the checker expects returned something, which is the
    # property that matters: a section read as empty refuses every mill in it.
    for section in amlpdf.EXPECTED:
        assert per[section] > 0, section


@needs_real
def test_it_loads_through_the_programs_own_matcher():
    from weldaudit.aml import Aml

    aml = Aml(amlpdf.entries(REAL))
    # Names out of the list itself: whatever this AML holds must match it.
    for entry in aml.entries[:20]:
        assert aml.match(entry.manufacturer).status == "approved", entry.manufacturer
    # And something that is certainly not on any approved list.
    assert aml.match("Wexford Fabrication Co").status == "not_listed"


@needs_real
def test_a_workbook_written_from_it_reads_back_the_same(tmp_path):
    from weldaudit.aml import Aml

    out = amlpdf.write_workbook(REAL, tmp_path / "AML.xlsx")
    assert len(Aml.from_workbook(out).entries) == len(amlpdf.entries(REAL))


# -- which list an audit picks ----------------------------------------------

def test_the_newest_revision_wins_over_the_first_name(tmp_path):
    """The bug this ordering exists for: a folder held a workbook transcribed
    from a list that expired in March and the current PDF beside it, and every
    audit used the expired one because it sorted first."""
    from weldaudit.extract.materials import find_aml_workbook

    job = tmp_path / "Job"
    job.mkdir()
    (tmp_path / "AML Search Spreadsheet.xlsx").write_bytes(b"")
    _pdf(tmp_path, "Valid Thru Mar 31, 2026", "Piping AML.pdf")
    _pdf(tmp_path, "Valid Thru Sept 30, 2026", "Piping AML new.pdf")
    assert find_aml_workbook(job).name == "Piping AML new.pdf"


def test_a_workbook_is_used_when_nothing_is_dated(tmp_path):
    from weldaudit.extract.materials import find_aml_workbook

    job = tmp_path / "Job"
    job.mkdir()
    (tmp_path / "AML Search Spreadsheet.xlsx").write_bytes(b"")
    assert find_aml_workbook(job).name == "AML Search Spreadsheet.xlsx"


# -- the finding -------------------------------------------------------------

@pytest.fixture
def job(tmp_path):
    db = Database(tmp_path / "t.db")
    root = tmp_path / "Job"
    root.mkdir()
    return db, db.upsert_project("Job", root)


def _source(db, pid, **kw):
    with db.tx() as c:
        c.execute("""INSERT INTO aml_source(project_id, path, kind, revision,
                                            valid_thru, entries)
                     VALUES(?,?,?,?,?,?)""",
                  (pid, kw.get("path", "C:/AML.pdf"), kw.get("kind", "pdf"),
                   kw.get("revision", "Mar 31, 2026"), kw.get("valid_thru"),
                   kw.get("entries", 1300)))


def _run(db, pid):
    from weldaudit.rules.materials import aml_out_of_date

    return aml_out_of_date(db, pid, "r1")


def test_an_expired_list_is_critical(job):
    db, pid = job
    _source(db, pid, valid_thru="2020-01-01")
    found = _run(db, pid)
    assert len(found) == 1
    assert found[0]["severity"] == "critical"
    assert "expired" in found[0]["message"]


def test_a_current_list_says_nothing(job):
    db, pid = job
    _source(db, pid, valid_thru="2099-01-01")
    assert _run(db, pid) == []


def test_a_list_about_to_lapse_is_a_minor_note(job):
    from datetime import datetime, timedelta

    db, pid = job
    soon = (datetime.now().date() + timedelta(days=10)).isoformat()
    _source(db, pid, valid_thru=soon)
    found = _run(db, pid)
    assert len(found) == 1 and found[0]["severity"] == "minor"


def test_a_workbook_gets_a_note_that_it_cannot_be_dated(job):
    db, pid = job
    _source(db, pid, kind="workbook", valid_thru=None, revision="")
    found = _run(db, pid)
    assert len(found) == 1 and found[0]["severity"] == "info"
    assert "no validity date" in found[0]["message"]


def test_no_aml_at_all_is_not_this_rules_business(job):
    db, pid = job
    assert _run(db, pid) == []
