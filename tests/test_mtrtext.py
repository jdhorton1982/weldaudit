"""Reading the letterhead off certificates that were never scanned.

The risk here is not missing a name, it is recording the wrong one for free.
A certificate names four or five companies and only one made the item, so
every test below is really about what this refuses.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.aml import Aml, AmlEntry, normalise_manufacturer  # noqa: E402
from weldaudit.db import Database  # noqa: E402
from weldaudit.extract import mtrtext  # noqa: E402


def _aml(*names):
    return Aml([AmlEntry(category="Fittings", manufacturer=n, location="",
                         limits_raw="", conditions="",
                         key=normalise_manufacturer(n)) for n in names])


def _page(lines, width=612, height=792):
    """A one-page PDF from (text, y, size) triples."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=width, height=height)
    for text, y, size in lines:
        page.insert_text((40, y), text, fontsize=size)
    # Reopened from bytes so the text layer is parsed, not remembered.
    return pymupdf.open("pdf", doc.tobytes())[0]


# -- what a candidate line has to be ----------------------------------------

@pytest.mark.parametrize("line", [
    "CUSTOMER: DODSON GLOBAL, INC.",       # the buyer
    "CLIENTE / Customer: ALLIED FITTING",  # the buyer, in two languages
    "Supplier: TUBOS REUNIDOS GROUP",      # the steel, not the fitting
    "STEEL ROLLING : Mingo Junction, OH",  # where the coil was rolled
    "Going To: MRW DI BLUEWATER MEGAPAD",     # a destination
    "MATERIAL TEST CERTIFICATE",           # the title of the form
    "Inspection Certificate 3.1",          # the title again
    "AS-ROLLED",                           # a heat treatment
    "NORMALIZED",
])
def test_lines_that_are_not_a_letterhead(line):
    assert not mtrtext._plausible_company(line)


@pytest.mark.parametrize("line", [
    "ORTEGA FORJA, S.COOP.",
    "DAEKWANG BEND CO., LTD.(DKB)",
    "Barrow Forge",
    "VALVITALIA S.p.A. - Tecnoforge Division",
])
def test_lines_that_are(line):
    assert mtrtext._plausible_company(line)


def test_a_generic_word_names_nobody():
    """'Component' reached 'Forged Components Inc' on a real page."""
    assert not mtrtext._identifying("Component")
    assert not mtrtext._identifying("Steel")
    assert mtrtext._identifying("Barrow Forge")


# -- choosing between the lines on a page -----------------------------------

def test_the_letterhead_is_read_when_the_aml_knows_it():
    page = _page([("BARROW FORGE CORPORATION", 60, 18),
                  ("CUSTOMER: MRC GLOBAL", 110, 9),
                  ("MATERIAL TEST REPORT", 140, 9)])
    got = mtrtext.letterhead_of(page, _aml("Barrow Forge", "Norvale Dalmine"))
    assert got is not None
    assert got[0] == "Barrow Forge"                 # the AML's own spelling
    assert got[1] == "BARROW FORGE CORPORATION"     # what the page prints


def test_small_print_at_the_top_is_not_a_letterhead():
    """Phone numbers and dates reached Bebitz and NOV on real pages."""
    page = _page([("BARROW FORGE CORPORATION", 60, 18),
                  ("Tel. +49 34691 40 0", 90, 6),
                  ("14-Nov-2023", 105, 6)])
    got = mtrtext.letterhead_of(page, _aml("Bebitz", "NOV (National Oilwell Varco)"))
    assert got is None, "matched something set in 6pt"


def test_the_customer_is_not_taken_even_when_the_aml_lists_it():
    """Distributors are on the AML too; the label is what rules them out."""
    page = _page([("CUSTOMER: MRC GLOBAL (US) INC.", 60, 18)])
    assert mtrtext.letterhead_of(page, _aml("MRC Global")) is None


def test_two_different_companies_at_the_top_is_not_an_answer():
    page = _page([("VALVITALIA S.p.A.", 60, 16),
                  ("TUBOS REUNIDOS GROUP S.L.U.", 85, 16)])
    # Both spellings score well past AML_CERTAIN, so this really is two
    # recognised companies rather than one plus a near miss.
    aml = _aml("Valvitalia S.p.A.", "Tubos Reunidos Group S.L.U.")
    assert all(aml.nearest(ln)[0] >= mtrtext.AML_CERTAIN
               for ln in mtrtext._lines_near_the_top(page))
    assert mtrtext.letterhead_of(page, aml) is None


def test_a_name_no_aml_recognises_is_left_for_the_paid_pass():
    """A structural rule was tried here and produced 'bolfex mfg.co.lo.'."""
    page = _page([("SOMEBODY ENTIRELY NEW, INC.", 60, 18)])
    assert mtrtext.letterhead_of(page, _aml("Barrow Forge")) is None


def test_nothing_is_claimed_without_an_aml():
    page = _page([("BARROW FORGE CORPORATION", 60, 18)])
    assert mtrtext.letterhead_of(page, None) is None


# -- the strict lookup this depends on --------------------------------------

def test_nearest_does_not_inherit_the_prefix_rule():
    """`match` is generous so a trade name finds its mill; `nearest` is not."""
    aml = _aml("MEGA", "Forged Components Inc.")
    assert aml.match("MEGAPAD TAKEAWAY").score >= 95          # the generous path
    assert aml.nearest("MEGAPAD TAKEAWAY")[0] < mtrtext.AML_CERTAIN
    assert aml.nearest("Component")[0] < mtrtext.AML_CERTAIN


def test_nearest_still_finds_a_real_name():
    aml = _aml("Barrow Forge")
    score, entry = aml.nearest("BARROW FORGE CORPORATION")
    assert entry == "Barrow Forge" and score >= mtrtext.AML_CERTAIN


# -- writing back ------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "t.db")
    pid = database.upsert_project("T", str(tmp_path))
    return database, pid


def _certificate(database, pid, tmp_path, manufacturer=""):
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((40, 60), "BARROW FORGE CORPORATION", fontsize=18)
    # Past MIN_CHARS, in lines short enough not to be clipped at the margin.
    for i in range(14):
        page.insert_text((40, 220 + i * 12), "chemical composition row " + "n" * 12,
                         fontsize=8)
    path = tmp_path / "cert.pdf"
    doc.save(str(path))
    with database.tx() as c:
        c.execute(
            """INSERT INTO document(id, project_id, path, filename, ext, kind)
               VALUES(1, ?, ?, 'cert.pdf', '.pdf', 'mtr')""", (pid, str(path)))
        c.execute(
            """INSERT INTO material(project_id, document_id, segment, heat,
                                    heat_key, source, manufacturer)
               VALUES(?, 1, 'SEG A', 'H1', 'H1', 'mtr_file', ?)""",
            (pid, manufacturer))


def test_a_certificate_with_a_text_layer_is_read_for_nothing(db, tmp_path):
    database, pid = db
    _certificate(database, pid, tmp_path)
    assert mtrtext.extract_letterheads(database, pid, _aml("Barrow Forge")) == 1
    row = database.one("SELECT manufacturer, confidence, evidence FROM material")
    assert row["manufacturer"] == "Barrow Forge"
    assert row["confidence"] == "text"
    assert "BARROW FORGE CORPORATION" in row["evidence"]


def test_a_manufacturer_from_an_export_is_not_overwritten(db, tmp_path):
    """An export did not come through a parser guessing which block is which."""
    database, pid = db
    _certificate(database, pid, tmp_path, manufacturer="Norvale Dalmine")
    assert mtrtext.extract_letterheads(database, pid, _aml("Barrow Forge")) == 0
    assert database.one("SELECT manufacturer FROM material")["manufacturer"] \
        == "Norvale Dalmine"


def test_a_scan_is_left_alone(db, tmp_path):
    """No text layer worth the name; the paid pass exists for these."""
    import pymupdf

    database, pid = db
    doc = pymupdf.open()
    doc.new_page(width=612, height=792).insert_text((40, 60), "XTO-417", fontsize=9)
    path = tmp_path / "scan.pdf"
    doc.save(str(path))
    with database.tx() as c:
        c.execute(
            """INSERT INTO document(id, project_id, path, filename, ext, kind)
               VALUES(1, ?, ?, 'scan.pdf', '.pdf', 'mtr')""", (pid, str(path)))
        c.execute(
            """INSERT INTO material(project_id, document_id, segment, heat,
                                    heat_key, source, manufacturer)
               VALUES(?, 1, 'SEG A', 'H1', 'H1', 'mtr_file', '')""", (pid,))
    assert mtrtext.extract_letterheads(database, pid, _aml("Barrow Forge")) == 0


# -- the mistake that cost an afternoon --------------------------------------

def test_no_pattern_carries_a_stray_control_character():
    r"""A `\b` written through a layer of escaping becomes a backspace.

    _CONDITION spent a run compiled as '\x08(as[...)\x08', matched nothing,
    and AS-ROLLED was recorded as the manufacturer Ajax Rolled Rings.
    """
    for name in ("_NOT_THE_MAKER", "_TITLE_WORDS", "_CONDITION"):
        pattern = getattr(mtrtext, name).pattern
        assert not [c for c in pattern if ord(c) < 32], name
