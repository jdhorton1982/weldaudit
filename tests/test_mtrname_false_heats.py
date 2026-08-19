"""Filenames that name no heat must yield no heat.

A heat invented out of a filename is worse than none. It becomes a certificate
on file for a melt that does not exist, matches nothing on any as-built, and
then reports itself as a gap for somebody to go and read — sending an auditor
to open a bill of lading in search of a heat number that was never there.

Measured on one real job: 17 of 159 certificates were filed under a heat that
was actually a date, an XTO tag, a catalogue figure or a valve model code.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.mtrname import parse  # noqa: E402


# -- dates -------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "8-13-25 PIPE.pdf",
    "9-4-25 PIPE.pdf",
    "9-5-25 PIPE.pdf",
    "6-24-25 FITTINGS & VALVES.pdf",
    "6-25-25 Backing rings, Couplings' Valves.pdf",
    "2-26-25 Valves.pdf",
    "12-1-2025 pipe.pdf",
    "1/7/25 fittings.pdf",
])
def test_a_delivery_date_is_not_a_heat(name):
    """Bills of lading are filed by the day they arrived, and the digits sit
    exactly where a heat usually does. The trimmer made it worse: 13 and 25
    both read as grades, so 8-13-25 was peeled back to heat '8'."""
    assert parse(name).heat == ""


# -- internal tags and catalogue numbers -------------------------------------

@pytest.mark.parametrize("name", [
    "2IN 150 CGI FLEX GASKET XTO-421.pdf",
    "4IN 150 CGI FLEX GASKET XTO-464.pdf",
    "6IN 150 CGI FLEX GASKET XTO-484.pdf",
])
def test_an_xto_tag_is_not_a_heat(name):
    """The tag and its number are one thing. Split apart, the number stood
    alone at the end of the name where the trailing-heat rule adopted it."""
    assert parse(name).heat == ""


def test_a_catalogue_figure_is_not_a_heat():
    assert parse("F-AE 2IN 600 YARROW CAP FIG 500 F-AF HUB XH .5IN TAP XTO-946.pdf").heat == ""


@pytest.mark.parametrize("name", [
    "8F-T63SN-RF.pdf",
    "2F-F63N-RF BAYLOR 2IN 600 FP BV.pdf",
    "1F-F03N-SE 1IN BAYLOR BV THRD 3000.pdf",
    "5F-F03N-SE BAYLOR 3000 THRD BALL VALVE XTO-1276 (1).pdf",
    "6F-F13N-RF15.5 6IN 150 BAYLOR BV FP LONG PATTERN XTO-358.pdf",
    "2F-F13N-RF 2IN 150 FP BAYLOR BV XTO-408.pdf",
])
def test_a_valve_model_code_is_not_a_heat(name):
    """Size, maker's figure, facing — and every piece of it in turn looks
    heat-shaped. These were filed under 3000, then F03N, then RF15.5."""
    assert parse(name).heat == ""


def test_a_threaded_pressure_class_is_not_a_heat():
    """API 602 classes read as bare four-digit numbers."""
    assert parse("BAYLOR BV THRD 3000 2IN.pdf").heat == ""
    assert parse("6000 PSI THRD COUPLING 1IN.pdf").heat == ""


def test_the_word_mtr_is_not_a_heat():
    assert parse("MTR 2 Inch Pipe.pdf").heat == ""


# -- a label wins ------------------------------------------------------------

def test_a_labelled_heat_is_taken_over_the_position():
    """The real heat sat behind an explicit label while 'MTR' led the name."""
    assert parse("MTR - 2 Inch Pipe - HT 318652.pdf").heat == "318652"


@pytest.mark.parametrize("name,expect", [
    ("2 Inch Pipe HEAT 24913.pdf", "24913"),
    ("pipe HEAT# 071B33.pdf", "071B33"),
    ("pipe HT: C48207361.pdf", "C48207361"),
    ("pipe HEAT NO 34L682W.pdf", "34L682W"),
])
def test_the_ways_a_heat_gets_labelled(name, expect):
    assert parse(name).heat == expect


def test_the_ht_in_a_description_is_not_a_label():
    """'1IN TAP' follows 'HT' in a flange description; the label has to be a
    whole word standing before the number, not any HT in the name."""
    got = parse("3Q142 - 2IN 600 RF BLIND 1IN TAP A105N.pdf").heat
    assert got == "3Q142"


# -- nothing that worked before may stop working -----------------------------

@pytest.mark.parametrize("name,expect", [
    ("071B33 - 16IN 150 RFWN FLANGE STD A105N XTO-414.pdf", "071B33"),
    ("377314022-003 ~ 16IN 150 TRUNNION BV CS NACE.pdf", "377314022-003"),
    ("4F214-FLG-WN-2IN-CL600-SCH160-A105N.pdf", "4F214"),
    ("FLANGE 3 INCH SCH 80- 4F318P.pdf", "4F318P"),
    ("45° 8 INCH 3R SEGM-5J17DK.pdf", "5J17DK"),
    ("4x0.337 PIPE Gr.B LL0731.pdf", "LL0731"),
    ("31082664 - 6IN PIPE FBE STD CS X52.pdf", "31082664"),
])
def test_the_conventions_that_already_worked(name, expect):
    assert parse(name).heat == expect


def test_a_rolling_still_yields_every_heat():
    assert parse("3651447 3653602 3754167 3756253.pdf").heats == [
        "3651447", "3653602", "3754167", "3756253"]
    assert parse("F37B6 F45B6.pdf").heats == ["F37B6", "F45B6"]


def test_a_name_with_no_heat_still_yields_none():
    assert parse("2IN_600_FLG_WN_SCH160_A105N_CS.pdf").heat == ""


# -- the trimmer must not manufacture a stub ---------------------------------

def test_trimming_never_leaves_a_fragment():
    """Peeling a token down to one or two characters means it was never a
    heat with a description attached — it was something else, and the stub is
    noise that then counts as a certified melt."""
    for name in ("8-13-25 PIPE.pdf", "6-30-25 Fittings.pdf"):
        assert len(parse(name).heat) != 1


def test_a_short_but_real_heat_survives():
    """Three characters is the floor, and EL8 really is a heat."""
    assert parse("FLANGE 16 INCH STD EL8.pdf").heat == "EL8"


# -- no control characters in any pattern ------------------------------------

def test_no_pattern_holds_a_control_character():
    """A backslash-b written through a shell heredoc becomes a literal
    backspace, and the pattern then silently matches nothing. It happened
    twice in this codebase before the check existed."""
    import re as _re

    import weldaudit.mtrname as m

    for name in dir(m):
        value = getattr(m, name)
        if isinstance(value, _re.Pattern):
            assert not any(ord(c) < 32 for c in value.pattern), name
