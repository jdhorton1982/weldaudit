"""A melt line names where the steel came from, not who made the item.

Ryeburn International certified a flexolet under heat 8410BB and named NORTH
AMERICAN STAINLESS on its MILL/COUNTRY OF ORIGIN line under heat CN1G. The
model labelled that a works_line — twice, on two separate readings of the same
page — so nothing downstream could tell it from a genuine second producer, and
the fitting was credited to the steel supplier. Who is not on the approved
list. While Ryeburn, who is, was thrown away.

The label cannot be trusted, but the heat can: a works line describes the item
in hand and carries the item's heat or none at all. Only a supply line brings
a heat of its own.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402
from weldaudit.extract.vision_pass import Target, _apply_mtr  # noqa: E402


@pytest.fixture
def job(tmp_path):
    db = Database(tmp_path / "t.db")
    root = tmp_path / "Job"
    root.mkdir()
    pid = db.upsert_project("Job", root)
    with db.tx() as c:
        c.execute("""INSERT INTO document(id, project_id, path, filename, ext,
                                          fingerprint, segment, kind)
                     VALUES(1,?, 'c.pdf', 'c.pdf', '.pdf', 'fp1', 'S', 'mtr')""",
                  (pid,))
        c.execute("""INSERT INTO material(project_id, document_id, segment, heat,
                                          heat_key, source)
                     VALUES(?,1,'S','8410BB','8410BB','mtr_file')""", (pid,))
    return db, pid


def _read(db, pid, **payload):
    base = {"page_is_certificate": True, "heat": "8410BB"}
    base.update(payload)
    _apply_mtr(db, pid, Target(1, "c.pdf", "c.pdf", "fp1", 1, "test", "S"), base, 0)
    return db.one("SELECT manufacturer, mill_name FROM material")


def test_a_melt_line_with_its_own_heat_is_not_the_maker(job):
    """The real case, exactly as it was read."""
    db, pid = job
    got = _read(db, pid,
                issuing_company="Ryeburn International",
                mill_name="Northfield Stainless",
                mill_source="works_line",        # what the model actually said
                mill_heat="CN1G")
    assert got["manufacturer"] == "Ryeburn International"


def test_a_works_line_carrying_the_same_heat_is_still_the_maker(job):
    """A second works of the same certificate's own material must survive —
    this is the case the mill-over-letterhead rule exists for."""
    db, pid = job
    got = _read(db, pid,
                issuing_company="Norvale",
                mill_name="Norvale Tamsa",
                mill_source="works_line",
                mill_heat="8410BB")
    assert got["manufacturer"] == "Norvale Tamsa"


def test_a_works_line_stating_no_heat_is_still_the_maker(job):
    db, pid = job
    got = _read(db, pid,
                issuing_company="TA CHEN INTERNATIONAL",
                mill_name="BQN Forgings Private Limited",
                mill_source="works_line",
                mill_heat=None)
    assert got["manufacturer"] == "BQN Forgings Private Limited"


def test_punctuation_does_not_make_a_heat_a_different_one(job):
    """Heats are compared the way they are compared everywhere else."""
    db, pid = job
    got = _read(db, pid,
                issuing_company="Norvale",
                mill_name="Norvale Tamsa",
                mill_source="works_line",
                mill_heat="8410-bb")
    assert got["manufacturer"] == "Norvale Tamsa"


def test_a_supplier_label_still_settles_it_without_a_heat(job):
    db, pid = job
    got = _read(db, pid,
                issuing_company="Rivermark",
                mill_name="Big River Steel",
                mill_source="supplier_line")
    assert got["manufacturer"] == "Rivermark"


def test_the_certificate_with_no_heat_of_its_own_is_left_alone(job):
    """Nothing to compare against, so the older rules decide as before."""
    db, pid = job
    with db.tx() as c:
        c.execute("UPDATE material SET heat='', heat_key=''")
    got = _read(db, pid, heat=None,
                issuing_company="Ryeburn International",
                mill_name="Northfield Stainless",
                mill_source="works_line",
                mill_heat="CN1G")
    assert got["manufacturer"] == "Northfield Stainless"
