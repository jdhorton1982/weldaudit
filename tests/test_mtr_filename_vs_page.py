"""The heat in the filename against the heat on the certificate.

Every heat in this corpus arrives from a filename, because that is the only
exact text there is — a heat on a scan has been through OCR. The filename is
also typed by whoever filed the document, and nothing was checking it.

A page heat was in fact being read and then **thrown away** whenever the
filename had already supplied one, so a certificate filed under the wrong heat
looked exactly like a correct one. One typo gives three wrong answers: the
wrong mill is checked against the approved list, whatever was really installed
shows as having no certificate, and the heat in the filename shows as
certified when nothing certifies it.

The hard part is not detecting a difference. It is not reporting the three
ways two readings legitimately differ — an OCR slip, a punctuation
difference, and a filename that carries a piece suffix the page does not.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402
from weldaudit.rules.materials import filename_heat_disagrees  # noqa: E402


@pytest.fixture
def job(tmp_path):
    db = Database(tmp_path / "t.db")
    root = tmp_path / "Job"
    root.mkdir()
    return db, db.upsert_project("Job", root)


def certificate(db, pid, filename, heat, page_heat, source="mtr_file"):
    """One MTR: a heat from its filename, and a heat read off the page."""
    from weldaudit.mtrname import normalise_heat

    with db.tx() as c:
        c.execute(
            """INSERT INTO document(project_id, path, filename, ext, size_bytes,
                                    segment, section_no, kind)
               VALUES(?,?,?,'.pdf',10,'16 PW',7,'mtr')""",
            (pid, f"C:/Job/7 MTRS/{filename}", filename))
        did = c.execute("SELECT last_insert_rowid() i").fetchone()[0]
        c.execute(
            """INSERT INTO material(project_id, document_id, segment, heat,
                                    heat_key, page_heat, source, confidence)
               VALUES(?,?,'16 PW',?,?,?,?, 'vision')""",
            (pid, did, heat, normalise_heat(heat) if heat else "",
             page_heat, source))
    return did


def rules(db, pid):
    return filename_heat_disagrees(db, pid, "r1")


# -- it catches a misfiled certificate ---------------------------------------

def test_a_filename_naming_a_different_heat_is_reported(job):
    db, pid = job
    certificate(db, pid, "A11484 - 4IN FLANGE.pdf", "A11484", "B72219")
    found = rules(db, pid)
    assert len(found) == 1
    assert found[0]["rule"] == "MTR-12"
    assert found[0]["severity"] == "major"


def test_it_names_the_document_and_both_readings(job):
    """A finding has to say which document, and what the disagreement is."""
    db, pid = job
    certificate(db, pid, "A11484 - 4IN FLANGE.pdf", "A11484", "B72219")
    message = rules(db, pid)[0]["message"]
    assert "A11484 - 4IN FLANGE.pdf" in message
    assert "A11484" in message and "B72219" in message


def test_it_explains_what_the_mistake_costs(job):
    db, pid = job
    certificate(db, pid, "A11484 - 4IN FLANGE.pdf", "A11484", "B72219")
    assert "has no certificate" in rules(db, pid)[0]["message"]


def test_it_links_to_the_certificate(job):
    db, pid = job
    did = certificate(db, pid, "A11484 - 4IN FLANGE.pdf", "A11484", "B72219")
    assert rules(db, pid)[0]["document_id"] == did


# -- and stays quiet where the two agree -------------------------------------

def test_the_same_heat_is_not_reported(job):
    db, pid = job
    certificate(db, pid, "A11484 - 4IN FLANGE.pdf", "A11484", "A11484")
    assert rules(db, pid) == []


def test_punctuation_is_not_a_disagreement(job):
    db, pid = job
    certificate(db, pid, "A-11484 - 4IN FLANGE.pdf", "A-11484", "A11484")
    assert rules(db, pid) == []


def test_one_character_apart_on_a_scan_is_not_a_disagreement(job):
    """The tolerance the rest of the audit uses. A scanned heat that comes
    back a character out is a statement about the scan, not the steel — and
    reporting every one would bury the real misfilings."""
    db, pid = job
    certificate(db, pid, "867985 - 4IN PIPE.pdf", "867985", "367985")
    assert rules(db, pid) == []


def test_a_piece_suffix_in_the_filename_is_not_a_disagreement(job):
    """The certificate prints the melt, the filename carries the piece it was
    cut for. That is a naming convention, not a finding."""
    db, pid = job
    certificate(db, pid, "A11484-24 ~ 4IN FLANGE.pdf", "A11484-24", "A11484")
    assert rules(db, pid) == []


def test_a_short_shared_start_is_still_reported(job):
    """Two heats beginning "24" are not the same heat, and treating a prefix
    as agreement would swallow real misfilings."""
    db, pid = job
    certificate(db, pid, "24 - 4IN PIPE.pdf", "24", "2477310")
    assert len(rules(db, pid)) == 1


# -- absence guards ----------------------------------------------------------

def test_a_certificate_nobody_has_read_is_not_reported(job):
    """No page heat means unchecked, not agreed. Reporting silence as
    agreement is how an audit passes what it never looked at."""
    db, pid = job
    certificate(db, pid, "A11484 - 4IN FLANGE.pdf", "A11484", None)
    assert rules(db, pid) == []


def test_a_certificate_with_no_filename_heat_is_not_reported(job):
    """MTR-06 already reports that, and there is nothing to disagree with."""
    db, pid = job
    certificate(db, pid, "scan001.pdf", "", "A11484")
    assert rules(db, pid) == []


def test_a_row_that_did_not_come_from_a_certificate_is_not_reported(job):
    """Pipe/heat exports carry a heat but no page to read."""
    db, pid = job
    certificate(db, pid, "pipe list.csv", "A11484", "B72219", source="pipes_csv")
    assert rules(db, pid) == []


def test_an_empty_job_reports_nothing(job):
    db, pid = job
    assert rules(db, pid) == []


# -- what the reader picks up that is not a heat -----------------------------

def test_a_specification_read_into_the_heat_field_is_ignored(job):
    """The reader put "A/SA105-N" in the heat field on a real certificate. A
    specification compared against a heat is a finding about nothing."""
    db, pid = job
    certificate(db, pid, "1N040N 2IN 300 RF BL.PDF", "1N040N", "A/SA105-N")
    assert rules(db, pid) == []


@pytest.mark.parametrize("page_heat", ["AJ3550", "181532", "1P732", "CXK0297",
                                       "SR95772-2-1819-EP", "4598-08-02"])
def test_a_real_heat_is_not_mistaken_for_a_specification(job, page_heat):
    db, pid = job
    certificate(db, pid, "X - 4IN PIPE.pdf", "ZZZ999", page_heat)
    assert len(rules(db, pid)) == 1


# -- one certificate, many pieces --------------------------------------------
#
# A works certifies a whole heat on one sheet, and it is then filed once per
# piece under each piece's own number. Five swing check valves on the real job
# gave five copies of one certificate, all reading heat AJ3550, filed under
# five serial numbers. Per file that is five findings about one page.


def test_copies_of_one_certificate_are_reported_once(job):
    db, pid = job
    for serial in ["23874010001", "23874010003", "23874010007", "23874010008"]:
        certificate(db, pid, f"{serial} - 4IN 300 SWING CHECK VALVE.pdf",
                    serial, "AJ3550")
    found = rules(db, pid)
    assert len(found) == 1
    assert "4 certificates" in found[0]["message"]


def test_the_grouped_finding_names_every_number_they_were_filed_under(job):
    db, pid = job
    for serial in ["23874010001", "23874010003"]:
        certificate(db, pid, f"{serial} - VALVE.pdf", serial, "AJ3550")
    found = rules(db, pid)[0]
    assert "23874010001" in found["message"] and "23874010003" in found["message"]
    assert "AJ3550" in found["message"]


def test_it_offers_both_readings_of_what_went_wrong(job):
    """Either the filenames carry a serial, or a certificate is misfiled. The
    finding must not assert the one it cannot know."""
    db, pid = job
    certificate(db, pid, "23874010001 - VALVE.pdf", "23874010001", "AJ3550")
    message = rules(db, pid)[0]["message"]
    assert "serial number" in message
    assert "wrong heat" in message


def test_different_page_heats_stay_separate(job):
    db, pid = job
    certificate(db, pid, "a - PIPE.pdf", "AAA111", "BBB222")
    certificate(db, pid, "b - PIPE.pdf", "CCC333", "DDD444")
    assert len(rules(db, pid)) == 2
