"""Which certificates the vision pass reads first.

The MTR pass is by far the largest — 744 documents and 1,286 pages across the
corpus, against 72 for coating and 69 for backfill — so what a `--limit` buys
is decided entirely by the ordering. Material that is actually in the ground
comes first, because an unapproved mill there is a real non-conformance.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402
from weldaudit.extract.vision_pass import mtr_targets  # noqa: E402


@pytest.fixture(autouse=True)
def one_page_certs(monkeypatch):
    monkeypatch.setattr("weldaudit.extract.vision_pass.page_count", lambda path: 1)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "m.db")
    return database, database.upsert_project("M", str(tmp_path))


def cert(db, pid, doc_id, heat, *, filename=None, segment="20 LP"):
    from weldaudit.mtrname import normalise_heat
    with db.tx() as c:
        c.execute(
            """INSERT INTO document(id, project_id, path, filename, ext, kind,
                                    segment, fingerprint)
               VALUES(?, ?, ?, ?, '.pdf', 'mtr', ?, ?)""",
            (doc_id, pid, f"p{doc_id}", filename or f"{heat}.pdf", segment,
             f"fp{doc_id}"),
        )
        c.execute(
            """INSERT INTO material(project_id, document_id, segment, heat,
                                    heat_key, source)
               VALUES(?, ?, ?, ?, ?, 'mtr_file')""",
            (pid, doc_id, segment, heat, normalise_heat(heat)),
        )


def on_asbuilt(db, pid, heat, segment="20 LP"):
    from weldaudit.mtrname import normalise_heat
    with db.tx() as c:
        c.execute(
            """INSERT INTO asbuilt_joint(project_id, segment, station, heat,
                                         heat_key, description, source)
               VALUES(?, ?, '1+00', ?, ?, 'ML', 'asbuilt_xlsx')""",
            (pid, segment, heat, normalise_heat(heat)),
        )


def on_heatmap(db, pid, heat, segment="20 LP"):
    from weldaudit.mtrname import normalise_heat
    with db.tx() as c:
        c.execute(
            """INSERT INTO installed_heat(project_id, segment, line, heat,
                                          heat_key, source)
               VALUES(?, ?, '20 LP', ?, ?, 'heat_map_text')""",
            (pid, segment, heat, normalise_heat(heat)),
        )


def welded(db, pid, heat, segment="20 LP"):
    with db.tx() as c:
        c.execute(
            """INSERT INTO weld(project_id, segment, line, weld_no, heat_us,
                                source)
               VALUES(?, ?, '20 LP', 'W1', ?, 'weld_map_text')""",
            (pid, segment, heat),
        )


def reasons(db, pid):
    return [t.reason for t in mtr_targets(db, pid)]


# -- what counts as installed ------------------------------------------------

def test_a_heat_in_the_weld_log_comes_first(db):
    database, pid = db
    cert(database, pid, 1, "111111")
    cert(database, pid, 2, "222222")
    welded(database, pid, "222222")
    assert "222222" in reasons(database, pid)[0]


def test_the_as_built_also_places_a_heat_in_the_ground(db):
    # Neither PLU's nor Bluewater's weld register records a heat at all, so
    # asking only the weld log left this rank empty on two of three jobs and
    # 545 Bluewater certificates came out in no useful order.
    database, pid = db
    cert(database, pid, 1, "111111")
    cert(database, pid, 2, "222222")
    on_asbuilt(database, pid, "222222")
    first = reasons(database, pid)[0]
    assert "222222" in first and "on the as-built" in first


def test_the_heat_map_also_places_a_heat_in_the_ground(db):
    database, pid = db
    cert(database, pid, 1, "111111")
    cert(database, pid, 2, "222222")
    on_heatmap(database, pid, "222222")
    first = reasons(database, pid)[0]
    assert "222222" in first and "shown on the heat map" in first


def test_the_evidence_is_named_not_assumed(db):
    # A heat map says a heat is somewhere on a line; a weld log says which
    # joint. The reason should not claim the stronger of the two.
    database, pid = db
    cert(database, pid, 1, "111111")
    on_heatmap(database, pid, "111111")
    assert "welded into the line" not in reasons(database, pid)[0]


# -- the rest of the order ---------------------------------------------------

def test_a_certificate_with_no_readable_heat_comes_next(db):
    database, pid = db
    cert(database, pid, 1, "", filename="2in Valve Certs.pdf")
    cert(database, pid, 2, "333333")
    cert(database, pid, 3, "222222")
    on_asbuilt(database, pid, "222222")
    got = reasons(database, pid)
    assert "222222" in got[0]
    assert got[1] == "no heat number readable from the filename"
    assert "333333" in got[2]


def test_a_certified_manufacturer_is_not_worth_reading(db):
    database, pid = db
    cert(database, pid, 1, "111111")
    with database.tx() as c:
        c.execute(
            """INSERT INTO material(project_id, document_id, segment, heat,
                                    heat_key, manufacturer, source)
               VALUES(?, 1, '20 LP', '111111', '111111', 'NORVALE', 'pipes_csv')""",
            (pid,),
        )
    assert mtr_targets(database, pid) == []


def test_filing_copies_are_read_once(db):
    database, pid = db
    cert(database, pid, 1, "111111")
    with database.tx() as c:
        c.execute("UPDATE document SET fingerprint='same' WHERE id=1")
        c.execute(
            """INSERT INTO document(id, project_id, path, filename, ext, kind,
                                    segment, fingerprint)
               VALUES(2, ?, 'p2', '111111.pdf', '.pdf', 'mtr', '6 FG', 'same')""",
            (pid,),
        )
        c.execute(
            """INSERT INTO material(project_id, document_id, segment, heat,
                                    heat_key, source)
               VALUES(?, 2, '6 FG', '111111', '111111', 'mtr_file')""",
            (pid,),
        )
    assert len(mtr_targets(database, pid)) == 1


# -- shipping paperwork filed with the certificates --------------------------
#
# A material folder is otherwise taken at its word, so bills of lading became
# material rows with no manufacturer — which is precisely the set the vision
# pass targets. On Bluewater 14 that was 136 BOLs and 12 copies of a
# compliments slip, read at five model calls each, all returning "not a
# certificate": a quarter of everything that pass paid for.

from weldaudit.extract.materials import NOT_A_CERTIFICATE, _is_certificate  # noqa: E402


@pytest.mark.parametrize("filename", [
    "BOL 10284 BEY 7-25-16 Pipe.pdf",
    "BOL 10560 BEY 9-18-25 Nipples.pdf",
    "8-2-25 6IN FG BOL.pdf",
    "Bill of Lading 44321.pdf",
    "Packing List 8891.pdf",
    "SEE SEG A FOR ALL IRIGINAL DOCUMENT.pdf",
])
def test_shipping_paperwork_is_not_a_certificate(filename):
    assert NOT_A_CERTIFICATE.search(filename)
    assert not _is_certificate(r"C:\job\FITTINGS\x.pdf", filename, set())


@pytest.mark.parametrize("filename", [
    # Halden is a flange maker; the rule matches BOL only as a whole word.
    "HALDEN 51908 - 2IN X .5IN TOL 3M CS A105N XTO-613.pdf",
    "1B452N 8IN 300 RF BLIND NORMAILIZED.PDF",
    "032052 - 6IN ELL 90 LR XH CS A420 WPL6 XTO-784.pdf",
    "2316324 - 2IN 150 RFWN FLANGE XS XTO-1026.pdf",
])
def test_real_certificates_are_untouched(filename):
    assert not NOT_A_CERTIFICATE.search(filename)
    assert _is_certificate(r"C:\job\FITTINGS\x.pdf", filename, set())
