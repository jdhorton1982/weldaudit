"""Reading scanned certificates with OCR, on this machine, for nothing.

The engine itself is not tested here — it is somebody else's model and it
either recognises characters or it does not. What is tested is everything
around it: which line gets believed, what counts as a heat number, and the
fact that an OCR reading enters the system in exactly the shape a paid vision
reading does, so nothing downstream has to know the difference.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.aml import Aml, AmlEntry, normalise_manufacturer  # noqa: E402
from weldaudit.db import Database  # noqa: E402
from weldaudit.extract import mtrocr  # noqa: E402
from weldaudit.extract.mtrtext import prominent_lines  # noqa: E402


def _aml(*names):
    return Aml([AmlEntry(category="Fittings", manufacturer=n, location="",
                         limits_raw="", conditions="",
                         key=normalise_manufacturer(n)) for n in names])


# -- what counts as a heat number -------------------------------------------

@pytest.mark.parametrize("line, want", [
    ("Heat Number:071B33, Customer PO Number:2414532", "071B33"),
    ("Heat: 032052", "032052"),
    ("Heat No. C48207361", "C48207361"),
    ("COLADA N. 24913", "24913"),
    ("COLADA N.o 021A24", "021A24"),
    ("Schmelze 130740", "130740"),
    # "Heat" labels the treatment and the affected zone as often as a number.
    # A real page recorded heat='Treatment' before the digit rule went in.
    ("Heat Treatment: Normalized", None),
    ("HEAT AFFECTED ZONE", None),
    ("Heat analysis", None),
    ("Heat/Product", None),
])
def test_reading_a_heat_number_off_a_line(line, want):
    assert mtrocr.heat_in(line) == want


# -- choosing the letterhead from OCR boxes ---------------------------------

def test_the_biggest_text_at_the_top_is_the_letterhead():
    """OCR gives box heights where a text layer gives font sizes; the same
    judgement has to work on both, which is why the rule takes neither."""
    lines = [("BARROW FORGE CORPORATION", 40, 30),
             ("Tel. +49 34691 40 0", 90, 8),
             ("14-Nov-2023", 105, 8),
             ("Chemical composition", 600, 9)]
    assert prominent_lines(lines, 1000) == ["BARROW FORGE CORPORATION"]


def test_ocr_garble_still_reaches_the_right_company():
    """'halden mfg.co., I.p.' is what the scanner made of a Halden letterhead.
    Recognising it is the whole reason the match is fuzzy and AML-anchored."""
    from weldaudit.extract.mtrtext import letterhead_from

    got = letterhead_from(["halden mfg.co., I.p."], _aml("Halden (F)", "Norvale"))
    assert got is not None and got[0] == "Halden (F)"


def test_a_customer_read_off_a_scan_is_still_refused():
    from weldaudit.extract.mtrtext import letterhead_from

    assert letterhead_from(["CUSTOMER: MRC GLOBAL (US) INC."],
                           _aml("MRC Global")) is None


# -- the payload it produces -------------------------------------------------

def test_the_payload_is_shaped_like_a_vision_reading(monkeypatch):
    """It goes into the same cache and through the same appliers, so a missing
    key here is a KeyError somewhere that has nothing to do with OCR."""
    from weldaudit.vision import SCHEMAS

    monkeypatch.setattr(mtrocr, "read_page_lines", lambda *a, **k: (
        [("BARROW FORGE CORPORATION", 40, 30), ("Heat No. 24913", 60, 12)], 2600))
    payload = mtrocr.payload_for("x.pdf", _aml("Barrow Forge"))
    # The AML's spelling is recorded, because that string becomes the
    # manufacturer everywhere; what the scanner actually read is kept beside it.
    assert payload["issuing_company"] == "Barrow Forge"
    assert payload["_ocr"]["letterhead"] == "BARROW FORGE CORPORATION"
    assert payload["heat"] == "24913"
    expected = set(SCHEMAS["mtr"]["properties"])
    assert expected <= set(payload), sorted(expected - set(payload))


def test_ocr_never_claims_a_second_producer(monkeypatch):
    """mill_name outranks the letterhead, and it is only earned by reading a
    label. OCR reads no labels, so it must not fill that field."""
    monkeypatch.setattr(mtrocr, "read_page_lines", lambda *a, **k: (
        [("TUBOS REUNIDOS GROUP S.L.U.", 40, 30), ("Heat No. 130740", 60, 12)], 2600))
    payload = mtrocr.payload_for("x.pdf", _aml("Tubos Reunidos SA"))
    assert payload["mill_name"] is None and payload["mill_source"] is None


def test_a_page_that_settles_nothing_is_not_cached(monkeypatch):
    monkeypatch.setattr(mtrocr, "read_page_lines", lambda *a, **k: (
        [("SOME UNREADABLE SMUDGE", 40, 30)], 2600))
    assert mtrocr.payload_for("x.pdf", _aml("Barrow Forge")) is None


# -- entering the system as a free reading -----------------------------------

@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "t.db")
    return database, database.upsert_project("T", str(tmp_path))


def test_a_paid_reading_is_never_replaced_by_a_free_one(db):
    """`ocr_any` prefers hosted over local, and OCR is deliberately labelled
    local — so running it after paying for Haiku cannot downgrade the audit."""
    database, _pid = db
    database.ocr_put("fp1:mtr:2000:2x2:v1", 0, "claude-haiku-4-5",
                     {"issuing_company": "Kandal Pipe USA, Inc"})
    database.ocr_put(f"fp1:mtr:{mtrocr.OCR_MAX_EDGE}:ocr", 0, mtrocr.OCR_MODEL,
                     {"issuing_company": "indat Pipe USA"})
    assert mtrocr.OCR_MODEL.startswith("local:")
    got = database.ocr_any("fp1", "mtr", 0)
    assert got["issuing_company"] == "Kandal Pipe USA, Inc"


def test_ocr_is_used_when_it_is_the_only_reading(db):
    database, _pid = db
    database.ocr_put(f"fp1:mtr:{mtrocr.OCR_MAX_EDGE}:ocr", 0, mtrocr.OCR_MODEL,
                     {"issuing_company": "Tex-Tubo"})
    assert database.ocr_any("fp1", "mtr", 0)["issuing_company"] == "Tex-Tubo"


def test_certificates_with_a_text_layer_are_left_to_the_free_reader(db, tmp_path):
    """mtrtext already reads those exactly; OCR would be slower and worse."""
    import pymupdf

    database, pid = db
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    for i in range(14):
        page.insert_text((40, 60 + i * 12), "a searchable certificate line " * 2,
                         fontsize=9)
    path = tmp_path / "digital.pdf"
    doc.save(str(path))
    with database.tx() as c:
        c.execute(
            """INSERT INTO document(id, project_id, path, filename, ext, kind,
                                    fingerprint)
               VALUES(1, ?, ?, 'digital.pdf', '.pdf', 'mtr', 'fp1')""",
            (pid, str(path)))
        c.execute(
            """INSERT INTO material(project_id, document_id, segment, heat,
                                    heat_key, source, manufacturer)
               VALUES(?, 1, 'SEG A', 'H1', 'H1', 'mtr_file', '')""", (pid,))
    assert mtrocr.scanned_targets(database, pid) == []


def test_the_top_band_is_a_fraction_of_the_page_not_of_the_text(monkeypatch):
    """Derived from the boxes, a page whose only text is a letterhead has no
    top band, and the letterhead falls outside it."""
    monkeypatch.setattr(mtrocr, "read_page_lines", lambda *a, **k: (
        [("TEXTUBO", 40, 30)], 2600))
    payload = mtrocr.payload_for("x.pdf", _aml("Tex Tubo"))
    assert payload is not None and payload["issuing_company"] == "Tex Tubo"


def test_the_scanners_spelling_is_not_what_gets_recorded(monkeypatch):
    """'halden mfg.co., I.p.' is a real OCR reading of a Halden letterhead.
    It should be evidence, not the manufacturer every finding then quotes."""
    monkeypatch.setattr(mtrocr, "read_page_lines", lambda *a, **k: (
        [("halden mfg.co., I.p.", 40, 30)], 2600))
    payload = mtrocr.payload_for("x.pdf", _aml("Halden (F)"))
    assert payload["issuing_company"] == "Halden (F)"
    assert payload["_ocr"]["letterhead"] == "halden mfg.co., I.p."
