"""Welder stencils, certification filenames, continuity, and rig letters.

Every value here is real: taken from a weld report cell, a certification
filename, or a reader-sheet filename in the corpus.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.rules.ndetech import rig_letter_for  # noqa: E402
from weldaudit.welders import (  # noqa: E402
    continuity_gaps, nearest_stencils, parse_cert_filename, parse_field, stencils_of,
)

NDE_PREFIXES = {"GXR", "CXR", "GFB", "DTI", "FXR", "TI", "FB"}


# -- welder cells -----------------------------------------------------------

@pytest.mark.parametrize("cell,expected", [
    ("AEA", ["AEA"]),
    ("ARB/AMG", ["ARB", "AMG"]),          # two welders sharing a pass
    ("ANR-AMG", ["ANR", "AMG"]),          # same, dash separated
    ("AM53, OM64", ["AM53", "OM64"]),     # letter-digit stencils on DI projects
    ("ADW/AOI", ["ADW", "AOI"]),
])
def test_welder_cells_split_into_stencils(cell, expected):
    assert parse_field(cell, NDE_PREFIXES).stencils == expected


@pytest.mark.parametrize("cell", ["", "N/A", "-", "NONE"])
def test_blank_cells_yield_nothing(cell):
    field = parse_field(cell, NDE_PREFIXES)
    assert not field.stencils and not field.unparsed


def test_nde_id_in_a_welder_column_is_not_a_welder():
    # Weld reports with no NOTES column let the NDE id drift into the cap
    # column; counting GXR-89 as a welder invents an uncertified stencil.
    field = parse_field("GXR-89", NDE_PREFIXES)
    assert field.stencils == [] and field.nde_ids == ["GXR-89"]


def test_a_suffixed_stencil_is_not_mistaken_for_an_nde_id():
    # ADP-1 and GXR-89 are the same shape; only the project's own NDE series
    # can tell them apart.
    field = parse_field("ADP-1", NDE_PREFIXES)
    assert field.stencils == ["ADP-1"] and field.nde_ids == []


def test_stencils_are_deduplicated_across_passes():
    field = stencils_of("ARB/AMG", "ARB/AMG", "ARB", "AMG", nde_prefixes=NDE_PREFIXES)
    assert field.stencils == ["ARB", "AMG"]


# -- certification filenames ------------------------------------------------

KNOWN = {"ABF", "ABJ", "ADL", "KG", "AEA", "AMG", "ARG"}


def test_bare_stencil_filename():
    cert = parse_cert_filename("ABF.pdf", KNOWN)
    assert cert.stencil == "ABF"


def test_descriptive_filename():
    cert = parse_cert_filename("4-8-25 ANDREW MORGAN SS GTAW ABJ.pdf", KNOWN)
    assert cert.stencil == "ABJ"
    assert cert.name == "Andrew Morgan"
    assert cert.process == "GTAW" and cert.material == "SS"
    assert cert.cert_date == "2025-04-08"


def test_underscore_delimited_filename_with_requal():
    # Underscores are word characters, so \b-anchored patterns miss both the
    # date and the REQUAL marker here.
    cert = parse_cert_filename("Craig Lunsford_XTO-SS_050224_ADL_REQUAL.pdf", KNOWN)
    assert cert.stencil == "ADL"
    assert cert.cert_date == "2024-05-02"
    assert cert.requalification is True


def test_a_surname_is_not_read_as_a_stencil():
    # "Babb" is shaped exactly like a stencil; only the stencils the weld
    # reports actually use are accepted.
    cert = parse_cert_filename("Kody Babb XTO-SS-SEC IX_020223.pdf", KNOWN)
    assert cert.stencil == ""
    assert cert.name == "Kody Babb"


def test_expiry_is_read_when_present():
    cert = parse_cert_filename("Juan Rodriguez IIAFS Certs..Exp.10.22.26.pdf", KNOWN)
    assert cert.expiry == "2026-10-22"


# -- near-miss stencils -----------------------------------------------------

CERTIFIED = {"ABF", "AEA", "AMG", "ARG", "AQR", "ANR", "AOI"}


@pytest.mark.parametrize("typo,expected", [
    ("AFB", "ABF"),     # transposition - plain Levenshtein would score this 2
    ("AGR", "ARG"),     # transposition
    ("AREA", "AEA"),    # doubled keystroke
    ("AMF", "AMG"),     # substitution
])
def test_one_keystroke_typos_find_the_certified_stencil(typo, expected):
    assert expected in nearest_stencils(typo, CERTIFIED)


def test_a_genuinely_different_stencil_has_no_near_match():
    assert nearest_stencils("ADP-1", CERTIFIED) == []


def test_a_certified_stencil_is_not_its_own_near_match():
    assert "AEA" not in nearest_stencils("AEA", CERTIFIED)


# -- continuity -------------------------------------------------------------

def test_continuity_gap_is_found():
    gaps = continuity_gaps(["2025-01-10", "2025-02-01", "2025-11-20"])
    assert len(gaps) == 1
    assert gaps[0][0] == "2025-02-01" and gaps[0][1] == "2025-11-20"


def test_regular_welding_has_no_gap():
    assert continuity_gaps(["2025-01-10", "2025-03-01", "2025-05-20"]) == []


def test_a_single_weld_cannot_have_a_gap():
    assert continuity_gaps(["2025-01-10"]) == []


# -- rig letters ------------------------------------------------------------

def test_rig_letter_is_the_first_letter_of_the_prefix():
    assert rig_letter_for("GFB", "20IN LP 09.09.25 GFB-037-040.pdf") == "G"
    assert rig_letter_for("CXR", "20IN LP 08.22.25 CXR-001P-023.pdf") == "C"


def test_an_explicit_rig_in_the_filename_wins():
    # "TI" is a method code, not rig T - the rig is written out.
    assert rig_letter_for("TI", "9-12-25 RIG C TI-1.pdf") == "C"
    assert rig_letter_for("FB", "9-12-25 RIG C FB-8-9.pdf") == "C"


def test_a_loose_rig_letter_before_the_report_id():
    assert rig_letter_for("FB", "10-8-25 D FB-1-2.pdf") == "D"


def test_a_method_only_prefix_with_no_rig_named_is_unknown():
    # Better to report nothing than to invent a rig called T.
    assert rig_letter_for("TI", "some sheet TI-1.pdf") == ""
