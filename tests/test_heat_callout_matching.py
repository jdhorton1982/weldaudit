"""Reading a heat callout, and recognising the certificate that covers it.

Both halves of this were found by running one real turnover package: eight
segments, seven heat maps, a hundred and thirty-six certificates. Fifty-six of
its hundred and thirteen heats matched no certificate, and twenty-one of those
had their certificate sitting in the book under a name a character or two away.

**Recognising a heat is not the same as certifying it.** A near match here only
changes what the finding says - the heat is still reported, and the message
names the certificate and asks which heat was actually installed. That is why
this can afford to be generous where the exact join cannot: a wrong guess costs
a question, where a wrong *match* would let a joint pass as certified against
material nobody ever checked. Every test below that widens the net also asserts
the finding is still made.

The other half is callouts that were never heats. A drafter labels the whole
bill of material with `HT:`, so the line's bore and the fitting's catalogue
code arrive wearing the same marker as the melt. Taken at face value each one
becomes a heat with no certificate, and an auditor is sent looking for an MTR
that was never going to exist.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402
from weldaudit.mtrname import normalise_heat, parse  # noqa: E402
from weldaudit.rules.materials import (  # noqa: E402
    callout_is_not_a_heat, heat_without_certificate,
)
from weldaudit.weldmap import (  # noqa: E402
    NOT_A_HEAT_NOTE, parse_heat_token, why_not_a_heat,
)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "c.db")
    pid = database.upsert_project("C", str(tmp_path))
    with database.tx() as c:
        c.execute(
            """INSERT INTO document(id, project_id, path, filename, ext, kind)
               VALUES(1, ?, 'm.pdf', 'm.pdf', '.pdf', 'mtr')""",
            (pid,),
        )
    return database, pid


def cert(db, pid, heat):
    with db.tx() as c:
        c.execute(
            """INSERT INTO material(project_id, document_id, segment, heat,
                                    heat_key, source)
               VALUES(?, 1, 'SEG', ?, ?, 'mtr_file')""",
            (pid, heat, normalise_heat(heat)),
        )


def on_the_map(db, pid, heat, *, note=""):
    with db.tx() as c:
        c.execute(
            """INSERT INTO installed_heat(project_id, document_id, segment, line,
                                          drawing_no, heat, heat_key, note, source)
               VALUES(?, 1, 'SEG', 'L-1', 'D-1', ?, ?, ?, 'heat_map_text')""",
            (pid, heat, "" if note else normalise_heat(heat), note),
        )


def only(found):
    assert len(found) == 1, [f["message"] for f in found]
    return found[0]


# -- callouts that are not heat numbers -------------------------------------

@pytest.mark.parametrize("callout", [
    "DN40", "DN 50",                       # the bore of the line
    "3F-F77P-SE", "3F-F8TP-SE",            # flange catalogue codes
    "IF-F08P", "1F-F08P-SE", "1F--F08P",   # the same, mis-typed
    "0.5-F08P-SE", "7-8F-F08P-SE",        # fractions written with a dash
])
def test_a_callout_that_is_not_a_heat_is_recognised(callout):
    assert why_not_a_heat(callout)
    assert parse_heat_token(f"HT: {callout}") == ""


@pytest.mark.parametrize("heat", [
    "391K44", "418390", "3356551", "T59894", "NN0446", "6X4L18", "F621",
    "W10910", "HA802", "51711", "3427435", "M204362-3", "575886/2",
    "35198853", "B2717", "4Y3L24-2Y", "082C44", "418393/3",
])
def test_a_real_heat_is_not_mistaken_for_one(heat):
    """The expensive half: rejecting a genuine heat loses a certificate."""
    assert why_not_a_heat(heat) == ""
    assert parse_heat_token(f"HT: {heat}") == heat


def test_a_rejected_callout_is_kept_with_its_reason(db):
    """Dropping it silently would make a drawing error nobody can find."""
    database, pid = db
    on_the_map(database, pid, "DN40", note=NOT_A_HEAT_NOTE + "a nominal bore, not a heat")
    row = database.one("SELECT heat, heat_key, note FROM installed_heat "
                       "WHERE project_id=?", (pid,))
    assert row["heat"] == "DN40"
    assert row["heat_key"] == "", "an empty key keeps it out of every join"
    assert row["note"].startswith(NOT_A_HEAT_NOTE)


def test_the_rejected_callouts_are_reported(db):
    database, pid = db
    on_the_map(database, pid, "DN40", note=NOT_A_HEAT_NOTE + "a nominal bore, not a heat")
    on_the_map(database, pid, "3F-F77P-SE",
               note=NOT_A_HEAT_NOTE + "a fitting catalogue code, not a heat")
    got = only(callout_is_not_a_heat(database, pid, "r"))
    assert got["rule"] == "MTR-13" and got["severity"] == "minor"
    assert "DN40" in got["message"] and "3F-F77P-SE" in got["message"]


def test_a_rejected_callout_is_not_a_missing_certificate(db):
    """The whole point: no phantom critical for a document that cannot exist."""
    database, pid = db
    cert(database, pid, "391K44")
    on_the_map(database, pid, "DN40", note=NOT_A_HEAT_NOTE + "a nominal bore, not a heat")
    assert heat_without_certificate(database, pid, "r") == []


def test_a_filename_that_is_a_part_number_yields_no_heat():
    """`3F-F77P-SE.pdf` already yielded nothing; `0.5F-F08P-SE.pdf` yielded
    `0.75F`, a fragment that passes the heat test on its own."""
    for name in ("0.5F-F08P-SE.pdf", "7-8F-F08P-SE.pdf", "3F-F77P-SE.pdf"):
        assert parse(name).heat == "", name


@pytest.mark.parametrize("name,heat", [
    ("418390-3.pdf", "418390-3"), ("391K44.pdf", "391K44"),
    ("M204362-3.pdf", "M204362-3"), ("575886-2.pdf", "575886-2"),
    ("6X4L18.pdf", "6X4L18"), ("F621.pdf", "F621"),
])
def test_a_real_certificate_filename_still_parses(name, heat):
    assert parse(name).heat == heat


# -- recognising the certificate that is already in the book ----------------

@pytest.mark.parametrize("on_map,on_file,why", [
    ("418390", "418390-3", "the melt against the piece"),
    ("P7739", "P77391", "a trailing character lost"),
    ("F621N", "F621", "a trailing character gained"),
    ("70055WX", "70B55WX", "0 read as D, same length"),
    ("RTOO74", "RT0074", "O read as 0, twice"),
    ("20628", "K20628", "the leading letter lost"),
    ("7748819", "77488-9", "a separator read as a digit"),
    ("391L144", "391K44", "M read as N, and a digit doubled"),
])
def test_a_heat_written_two_ways_finds_its_certificate(db, on_map, on_file, why):
    database, pid = db
    cert(database, pid, on_file)
    on_the_map(database, pid, on_map)
    got = only(heat_without_certificate(database, pid, "r"))
    assert got["severity"] == "major", why
    assert on_file in got["message"], why
    assert "confirm which heat was actually installed" in got["message"]


def test_a_near_match_never_counts_as_certified(db):
    """The rule this whole widening rests on.

    A pairing changes what the finding says, never whether it is made. If this
    ever starts returning nothing, a joint can pass as certified against
    material nobody looked at.
    """
    database, pid = db
    cert(database, pid, "70B55WX")
    on_the_map(database, pid, "70055WX")
    found = heat_without_certificate(database, pid, "r")
    assert len(found) == 1, "the heat is still reported, not swallowed"


def test_an_exact_match_is_not_reported_at_all(db):
    database, pid = db
    cert(database, pid, "391K44")
    on_the_map(database, pid, "391K44")
    assert heat_without_certificate(database, pid, "r") == []


def test_a_genuinely_different_heat_is_still_critical(db):
    """Two characters out of five is a different heat, not a typo."""
    database, pid = db
    cert(database, pid, "61783")
    on_the_map(database, pid, "61888")
    got = only(heat_without_certificate(database, pid, "r"))
    assert got["severity"] == "critical"
    assert "no material certificate" in got["message"]


def test_a_short_heat_is_not_paired_on_a_thin_resemblance(db):
    database, pid = db
    cert(database, pid, "F621")
    on_the_map(database, pid, "D914")
    assert only(heat_without_certificate(database, pid, "r"))["severity"] == "critical"


def test_a_shared_stem_has_to_be_long_enough(db):
    """`A1` starting `B2717` is not a heat and its piece."""
    database, pid = db
    cert(database, pid, "B2717")
    on_the_map(database, pid, "A1")
    assert only(heat_without_certificate(database, pid, "r"))["severity"] == "critical"


def test_the_message_says_when_the_package_names_it_correctly_elsewhere(db):
    """A drawing contradicting itself is not a missing document.

    The 16IN XX heat map calls one heat `391K44` on page 1 and `391L144` on
    page 4. Sending somebody to look for the second certificate wastes the
    search; saying the book already has it under the other spelling does not.
    """
    database, pid = db
    cert(database, pid, "391K44")
    on_the_map(database, pid, "391K44")        # the spelling that matches
    on_the_map(database, pid, "391L144")       # and the one that does not
    got = only(heat_without_certificate(database, pid, "r"))
    assert "also calls it 391K44" in got["message"]
    assert "which does have its certificate" in got["message"]
