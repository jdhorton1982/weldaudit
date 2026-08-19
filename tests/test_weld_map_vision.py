"""Reading a piping isometric: the weld register and the material installed.

The payloads mirror the real Kestrel 8 drawings for line DTD22MP-LP-16-1A — the
weld map with five balloon callouts (id / welders / date) and the heat map with
five boxed heats, one of which is boxed twice.

The line these tests hold is that a heat map says *a heat is in this line*, not
*this heat is on that end of that weld*. Pairing a boxed callout to a joint
needs spatial reasoning about leader lines, and a wrong pairing attaches
material to the wrong weld with nothing to show for it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402
from weldaudit.extract import welders as welder_extract  # noqa: E402
from weldaudit.extract.vision_pass import (  # noqa: E402
    WELD_MAP_SOURCE, Target, replay, run,
)
from weldaudit.rules import materials as mrules  # noqa: E402

WELD_MAP_PAGE = {
    "page_is_isometric": True,
    "line_no": "DTD22MP-LP-16-1A", "drawing_no": "DN-PL22M-PL-LD-MP-ISO-0015-001",
    "sheet": "1 of 3", "revision": "0", "service": "LP GAS",
    "afe": "NI.2024.14306.CAP.01",
    "weld_callouts": [
        {"weld_id": "AFB-18", "welders": "AFM/ARV", "date": "06/01/25",
         "signed_off": True},
        {"weld_id": "AFB-17", "welders": "AFM/ARV", "date": "06/01/25",
         "signed_off": True},
        {"weld_id": "AFB-16C", "welders": "ARO/ARS", "date": "08/08/25",
         "signed_off": True},
        {"weld_id": "AFB-19", "welders": "ARO/ARV", "date": "8-01-25",
         "signed_off": True},
        {"weld_id": "AFB-20", "welders": "ARO/ARS", "date": "8-06-25",
         "signed_off": True},
    ],
    "heat_callouts": [],
    "bill_of_material": [
        {"mark": "1", "size": "16", "quantity": "4'-4\"",
         "description": "PIPE, ERW, HFW, API 5L PSL-2 GR B, NACE MR0175, STD"},
    ],
}

HEAT_MAP_PAGE = {
    "page_is_isometric": True,
    "line_no": "DTD22MP-LP-16-1A", "drawing_no": "DN-PL22M-PL-LD-MP-ISO-0015-001",
    "sheet": "1 of 1", "revision": "0", "service": "LP GAS",
    "afe": "NI.2024.14306.CAP.01",
    "weld_callouts": [],
    "heat_callouts": [
        {"heat": "NN0446", "note": None},
        {"heat": "652580", "note": None},
        {"heat": "453M66", "note": None},
        {"heat": "3756253", "note": None},
        # The same heat is boxed twice on the real drawing; the repeat is real.
        {"heat": "453M66", "note": None},
    ],
    "bill_of_material": [],
}


class StubReader:
    def __init__(self, pages):
        self.pages = pages

    def cached(self, fingerprint, page_no, kind):
        return None

    def read_page(self, path, page_no, kind, fingerprint):
        return self.pages[page_no]


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "m.db")
    pid = database.upsert_project("M", str(tmp_path))
    with database.tx() as c:
        for doc_id, name, fp in ((1, "16in LP Seg. D Weld Map.pdf", "fpWM"),
                                 (2, "16in LP Seg. D Heat Map.pdf", "fpHM")):
            c.execute(
                """INSERT INTO document(id, project_id, path, filename, ext,
                                        fingerprint, segment, kind)
                   VALUES(?, ?, ?, ?, '.pdf', ?, '16IN LP', 'weld_map')""",
                (doc_id, pid, name, name, fp),
            )
    return database, pid


def _wm():
    return Target(1, "wm.pdf", "16in LP Seg. D Weld Map.pdf", "fpWM", 1, "t", "16IN LP")


def _hm():
    return Target(2, "hm.pdf", "16in LP Seg. D Heat Map.pdf", "fpHM", 1, "t", "16IN LP")


# -- the weld register ------------------------------------------------------

def test_weld_callouts_become_welds(db):
    database, pid = db
    result = run(database, pid, "weld_map", StubReader([WELD_MAP_PAGE]), [_wm()])
    assert result.updated == 5
    assert database.one("SELECT COUNT(*) n FROM weld WHERE project_id=? AND source=?",
                        (pid, WELD_MAP_SOURCE))["n"] == 5


def test_a_callout_supplies_the_nde_id_the_weld_reports_lack(db):
    database, pid = db
    run(database, pid, "weld_map", StubReader([WELD_MAP_PAGE]), [_wm()])
    row = database.one(
        "SELECT * FROM weld WHERE project_id=? AND weld_no='AFB-18'", (pid,))
    assert row["nde_id"] == "AFB-018"          # normalised for joining
    assert row["welder_root"] == "AFM/ARV"
    assert row["date_welded"] == "2025-06-01"
    assert row["line"] == "DTD22MP-LP-16-1A"


def test_a_dashed_american_date_is_parsed_too(db):
    database, pid = db
    run(database, pid, "weld_map", StubReader([WELD_MAP_PAGE]), [_wm()])
    assert database.one("SELECT date_welded FROM weld WHERE project_id=? AND weld_no='AFB-19'",
                        (pid,))["date_welded"] == "2025-08-01"


def test_an_id_with_an_unrecognised_suffix_is_still_kept_verbatim(db):
    # 'AFB-16C' carries a suffix the id grammar does not know. The weld must
    # still be recorded — dropping it would lose a real joint.
    database, pid = db
    run(database, pid, "weld_map", StubReader([WELD_MAP_PAGE]), [_wm()])
    row = database.one("SELECT * FROM weld WHERE project_id=? AND weld_no='AFB-16C'",
                       (pid,))
    assert row is not None and row["welder_root"] == "ARO/ARS"


def test_weld_map_welds_feed_the_welder_extractor(db):
    database, pid = db
    run(database, pid, "weld_map", StubReader([WELD_MAP_PAGE]), [_wm()])
    _rows, stencils = welder_extract.extract_passes(database, pid)
    assert {r["stencil"] for r in database.q(
        "SELECT DISTINCT stencil FROM welder_pass WHERE project_id=?", (pid,))} == {
        "AFM", "ARV", "ARO", "ARS"}
    assert stencils == 4


# -- the material installed -------------------------------------------------

def test_heat_callouts_become_installed_heats_not_welds(db):
    database, pid = db
    run(database, pid, "weld_map", StubReader([HEAT_MAP_PAGE]), [_hm()])
    assert database.one("SELECT COUNT(*) n FROM installed_heat WHERE project_id=?",
                        (pid,))["n"] == 5
    # Crucially: no weld rows. A heat map does not tell you where the joints are.
    assert database.one("SELECT COUNT(*) n FROM weld WHERE project_id=?", (pid,))["n"] == 0


def test_a_heat_boxed_twice_is_recorded_twice(db):
    database, pid = db
    run(database, pid, "weld_map", StubReader([HEAT_MAP_PAGE]), [_hm()])
    assert database.one(
        "SELECT COUNT(*) n FROM installed_heat WHERE project_id=? AND heat='453M66'",
        (pid,))["n"] == 2


def test_installed_heats_carry_their_line_and_drawing(db):
    database, pid = db
    run(database, pid, "weld_map", StubReader([HEAT_MAP_PAGE]), [_hm()])
    row = database.one(
        "SELECT * FROM installed_heat WHERE project_id=? AND heat='NN0446'", (pid,))
    assert row["line"] == "DTD22MP-LP-16-1A"
    assert row["heat_key"] == "NN0446"


def test_a_page_that_is_not_an_isometric_creates_nothing(db):
    database, pid = db
    page = {"page_is_isometric": False, "weld_callouts": [], "heat_callouts": []}
    assert run(database, pid, "weld_map", StubReader([page]), [_hm()]).updated == 0


def test_rereading_does_not_duplicate(db):
    database, pid = db
    for _ in range(3):
        run(database, pid, "weld_map", StubReader([HEAT_MAP_PAGE]), [_hm()])
        run(database, pid, "weld_map", StubReader([WELD_MAP_PAGE]), [_wm()])
    assert database.one("SELECT COUNT(*) n FROM installed_heat WHERE project_id=?",
                        (pid,))["n"] == 5
    assert database.one("SELECT COUNT(*) n FROM weld WHERE project_id=?", (pid,))["n"] == 5


# -- what it unlocks in the material chain ----------------------------------

def test_a_heat_map_heat_with_no_certificate_is_reported(db):
    database, pid = db
    run(database, pid, "weld_map", StubReader([HEAT_MAP_PAGE]), [_hm()])
    # One certificate on file, for a heat that is not on the drawing.
    with database.tx() as c:
        c.execute(
            """INSERT INTO material(project_id, document_id, segment, heat, heat_key,
                                    source) VALUES(?, 1, '16IN LP', 'ZZ1', 'ZZ1',
                                    'mtr_file')""",
            (pid,),
        )
    findings = mrules.heat_without_certificate(database, pid, "r1")
    subjects = {f["subject"] for f in findings}
    assert "Heat NN0446" in subjects


def test_the_finding_says_heat_map_not_welded_into(db):
    # The two kinds of evidence are not equally strong and must not read alike.
    database, pid = db
    run(database, pid, "weld_map", StubReader([HEAT_MAP_PAGE]), [_hm()])
    with database.tx() as c:
        c.execute(
            """INSERT INTO material(project_id, document_id, segment, heat, heat_key,
                                    source) VALUES(?, 1, '16IN LP', 'ZZ1', 'ZZ1',
                                    'mtr_file')""",
            (pid,),
        )
    msg = next(f for f in mrules.heat_without_certificate(database, pid, "r1")
               if f["subject"] == "Heat 453M66")["message"]
    assert "shown on the heat map" in msg and "welded into" not in msg
    assert "2 callouts" in msg


# -- durability -------------------------------------------------------------

def test_replay_restores_both_welds_and_heats(db):
    database, pid = db
    database.ocr_put("fpWM:weld_map:2000", 0, "claude-opus-5", WELD_MAP_PAGE)
    database.ocr_put("fpHM:weld_map:2000", 0, "claude-opus-5", HEAT_MAP_PAGE)

    counts = replay(database, pid, ("weld_map",))
    assert counts["weld_map"] == 10          # 5 welds + 5 heat callouts
    assert database.one("SELECT COUNT(*) n FROM weld WHERE project_id=?", (pid,))["n"] == 5
    assert database.one("SELECT COUNT(*) n FROM installed_heat WHERE project_id=?",
                        (pid,))["n"] == 5


def test_replay_is_idempotent(db):
    database, pid = db
    database.ocr_put("fpWM:weld_map:2000", 0, "claude-opus-5", WELD_MAP_PAGE)
    for _ in range(3):
        replay(database, pid, ("weld_map",))
    assert database.one("SELECT COUNT(*) n FROM weld WHERE project_id=?", (pid,))["n"] == 5
