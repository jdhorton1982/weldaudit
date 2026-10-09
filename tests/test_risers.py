"""Riser checklists: reading the form, and reading its questions back.

GPPB-0105 requires one in the field package, item (h). Until now the audit had
no concept of them, and a package carrying nine went through with none of them
looked at.

The reader is tested against the shape of the real template rather than a
tidied version of it, because the two bugs worth guarding both came from the
layout:

**Labels do not all answer the same way.** "Drain Plugs Verified" is answered
to its right; "Riser Description" heads a column and is answered on the row
below. Reading rightward from the description returned the *next label* -
every riser came back described as "Riser Serial / Pkg #".

**Blank cells carry meaning, so rows cannot be compacted.** Packing the filled
cells of a valve row left-to-right slid every column one place when "Date
Torqued" was empty, and the Blinds answer landed in the torque date: every
valve in the package reported as torqued on "N". Since the empty torque column
is exactly what RSR-01 exists to report, compacting hid the finding and
replaced it with a wrong one.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import openpyxl  # noqa: E402
import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402
from weldaudit.extract.risers import read_sheet  # noqa: E402
from weldaudit.rules.risers import (  # noqa: E402
    box_not_answered, no_coordinates, no_valves, not_signed, valve_not_dated,
)


def a_form(path, *, description="GL SEG B TO B PAD", serial="NA",
           lat=32.210277, lon=-103.870213, plugs="NA", photo="Yes",
           inspector="Dale Thorn", signature="Dale Thorn",
           valves=(("VALVECO", "3F-F88P-SE", "FULLPORT BALL VLV", '2"', "", "N"),)):
    """A workbook laid out like the real template, sparse columns and all."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Riser Cklst"

    def put(row, col, value):
        if value not in (None, ""):
            ws.cell(row=row, column=col, value=value)

    put(1, 1, "Pipe Size:"); put(1, 3, '4"')
    put(1, 5, "Grade:");     put(1, 7, "X42")
    put(1, 9, "Service:");   put(1, 11, "FG")
    put(1, 13, "Today's Date:"); put(1, 15, "2025-09-18")
    # Headed columns, answered on the row below.
    put(9, 2, "Coordinates"); put(9, 8, "Riser Description"); put(9, 24, "Riser Serial / Pkg #")
    put(10, 2, "Latitude"); put(10, 5, "Longitude"); put(10, 8, description); put(10, 24, serial)
    put(11, 2, lat); put(11, 5, lon)
    # Answered to the right.
    put(13, 3, "Drain Plugs Verified"); put(13, 7, plugs)
    put(13, 14, "Picture of riser taken with coordinates stamped"); put(13, 24, photo)
    put(15, 5, "List all valves on riser:")
    for col, label in ((2, "Mfr#"), (7, "Valve Serial #"), (14, "Type"),
                       (19, "Size"), (23, "Date Torqued"), (27, "Blinds (Y/N)")):
        put(16, col, label)
    for n, v in enumerate(valves):
        for col, value in zip((2, 7, 14, 19, 23, 27), v):
            put(17 + n, col, value)
    put(29, 2, "Inspector Printed Name:"); put(29, 7, inspector)
    put(30, 2, "Inspector Signature:");    put(30, 7, signature)
    wb.save(path)
    return str(path)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "r.db")
    pid = database.upsert_project("R", str(tmp_path))
    with database.tx() as c:
        c.execute(
            """INSERT INTO document(id, project_id, path, filename, ext, kind, segment)
               VALUES(1, ?, 'r.xlsx', 'r.xlsx', '.xlsx', 'riser_checklist', 'SEG')""",
            (pid,),
        )
    return database, pid


def stored(db, pid, **head):
    """One riser row, with its valves, as the extractor would write them."""
    valves = head.pop("valves", [])
    with db.tx() as c:
        cur = c.execute(
            """INSERT INTO riser(project_id, document_id, segment, description,
                                 serial, latitude, longitude, drain_plugs, photo,
                                 inspector, signature, source)
               VALUES(?,1,'SEG',?,?,?,?,?,?,?,?,'riser_checklist')""",
            (pid, head.get("description", "A RISER"), head.get("serial", "NA"),
             head.get("latitude", 32.2), head.get("longitude", -103.8),
             head.get("drain_plugs", "NA"), head.get("photo", "Yes"),
             head.get("inspector", "D Thorn"), head.get("signature", "D Thorn")),
        )
        rid = cur.lastrowid
        for n, (serial, torqued) in enumerate(valves, 1):
            c.execute(
                """INSERT INTO riser_valve(project_id, riser_id, document_id, row_no,
                                           manufacturer, serial, serial_key,
                                           valve_type, size, torqued_on, blinds)
                   VALUES(?,?,1,?,'VALVECO',?,?,'BALL','2\"',?,'N')""",
                (pid, rid, n, serial, serial.upper(), torqued),
            )
    return rid


# -- reading the form -------------------------------------------------------

def test_the_header_fields_are_read(tmp_path):
    head, _ = read_sheet(a_form(tmp_path / "a.xlsx"))
    assert head["description"] == "GL SEG B TO B PAD"
    assert head["drain_plugs"] == "NA"
    assert head["photo"] == "Yes"
    assert head["signature"] == "Dale Thorn"


def test_a_headed_column_is_read_from_below_not_from_the_right(tmp_path):
    """Reading rightward returned the next label: "Riser Serial / Pkg #"."""
    head, _ = read_sheet(a_form(tmp_path / "b.xlsx", description="XX SEG D TO MSO"))
    assert head["description"] == "XX SEG D TO MSO"
    assert "Serial" not in head["description"]


def test_the_coordinates_are_read(tmp_path):
    head, _ = read_sheet(a_form(tmp_path / "c.xlsx", lat=32.207085, lon=-103.876641))
    assert head["latitude"] == pytest.approx(32.207085)
    assert head["longitude"] == pytest.approx(-103.876641)


def test_an_empty_torque_column_does_not_slide_the_next_one_into_it(tmp_path):
    """The bug this file exists for. Blinds is 'N'; the torque date is blank,
    and blank is the answer RSR-01 is looking for."""
    _, valves = read_sheet(a_form(tmp_path / "d.xlsx"))
    assert len(valves) == 1
    assert valves[0]["torqued_on"] == ""
    assert valves[0]["blinds"] == "N"
    assert valves[0]["serial"] == "3F-F88P-SE"


def test_a_torque_date_that_is_filled_in_is_read(tmp_path):
    path = a_form(tmp_path / "e.xlsx",
                  valves=(("VALVECO", "QRTAB", "BALL", '4"', "2025-09-20", "N"),))
    _, valves = read_sheet(path)
    assert valves[0]["torqued_on"] == "2025-09-20"


def test_every_valve_row_is_read(tmp_path):
    path = a_form(tmp_path / "f.xlsx", valves=tuple(
        ("VALVECO", f"ONV{n}", "BALL", '2"', "", "N") for n in range(5)))
    _, valves = read_sheet(path)
    assert [v["serial"] for v in valves] == [f"ONV{n}" for n in range(5)]


# -- the rules --------------------------------------------------------------

def test_a_valve_with_no_torque_date_is_reported(db):
    database, pid = db
    stored(database, pid, valves=[("3F-F88P-SE", ""), ("QRTAB", "")])
    found = valve_not_dated(database, pid, "r")
    assert len(found) == 1, "one finding per riser, not per valve"
    assert found[0]["rule"] == "RSR-01"
    assert "2 valves" in found[0]["message"]
    assert "3F-F88P-SE" in found[0]["message"]


def test_a_dated_valve_is_quiet(db):
    database, pid = db
    stored(database, pid, valves=[("QRTAB", "2025-09-20")])
    assert valve_not_dated(database, pid, "r") == []


def test_a_well_filled_checklist_raises_nothing(db):
    """Every one of the nine real forms passes all of these but RSR-01."""
    database, pid = db
    stored(database, pid, valves=[("QRTAB", "2025-09-20")])
    for rule in (valve_not_dated, not_signed, no_coordinates, no_valves,
                 box_not_answered):
        assert rule(database, pid, "r") == [], rule.__name__


def test_an_unsigned_checklist_is_reported(db):
    database, pid = db
    stored(database, pid, inspector="", signature="", valves=[("QRTAB", "2025-09-20")])
    assert len(not_signed(database, pid, "r")) == 1


def test_a_checklist_with_no_coordinates_is_reported(db):
    database, pid = db
    stored(database, pid, latitude=None, longitude=None, valves=[("QRTAB", "2025-09-20")])
    assert len(no_coordinates(database, pid, "r")) == 1


def test_a_checklist_with_no_valves_is_reported(db):
    database, pid = db
    stored(database, pid, valves=[])
    assert len(no_valves(database, pid, "r")) == 1


def test_na_is_an_answer_and_blank_is_not(db):
    """A riser with no drain plug answers NA. Reporting that would teach an
    inspector to write Yes instead, which is worse than the gap."""
    database, pid = db
    stored(database, pid, drain_plugs="NA", photo="Yes", valves=[("A", "2025-09-20")])
    assert box_not_answered(database, pid, "r") == []

    stored(database, pid, description="SECOND", drain_plugs="", photo="Yes",
           valves=[("B", "2025-09-20")])
    found = box_not_answered(database, pid, "r")
    assert len(found) == 1
    assert "drain plugs verified" in found[0]["message"]
