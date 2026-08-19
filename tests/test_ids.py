"""Identifier parsing, against the naming conventions actually found on disk.

Every case here is a real filename or NOTES value from the corpus.  The two
site conventions differ enough that a change which fixes one has a habit of
breaking the other, so both are pinned.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from weldaudit.ids import (  # noqa: E402
    NdeId, cutout_series, gaps, parse_ids, parse_one, sequences,
)
from weldaudit.extract.readersheets import ids_from_filename, sheet_date  # noqa: E402


def ids(text: str) -> list[str]:
    return [str(i) for i in parse_ids(text)]


# -- compact convention (Bluewater / Bluewater 14) ---------------------------------

def test_compact_range():
    assert ids("GFB-037-040") == ["GFB-037", "GFB-038", "GFB-039", "GFB-040"]


def test_range_keeps_suffix_only_on_opening_shot():
    assert ids("FXR-001P-006") == [
        "FXR-001P", "FXR-002", "FXR-003", "FXR-004", "FXR-005", "FXR-006"
    ]


def test_both_endpoints_suffixed_is_a_list_not_a_range():
    # "GFB-4P-15P-19" is shots 4P, 15P and 19 - not shots 4 through 15.
    assert ids("GFB-4P-15P-19") == ["GFB-004P", "GFB-015P", "GFB-019"]


def test_comma_continuation_inherits_prefix():
    assert ids("GFB-58-62,24CO") == [
        "GFB-058", "GFB-059", "GFB-060", "GFB-061", "GFB-062", "GFB-024CO"
    ]


def test_an_unknown_suffix_still_parses():
    # 'AFB-16C' appears on the Kestrel 8 weld maps. C is not P/R/CO/RR, but
    # refusing the whole id drops a real weld's NDE reference — and then the
    # weld looks like one that was never inspected.
    assert ids("AFB-16C") == ["AFB-016C"]


def test_a_following_word_is_not_swallowed_as_a_suffix():
    # Accepting a lone trailing letter must not eat the next word's initial.
    assert ids("CTI-001 North CS LATERAL") == ["CTI-001"]


def test_known_suffixes_still_win_over_a_bare_letter():
    assert ids("GFB-008CO") == ["GFB-008CO"]
    assert ids("FXR-045R") == ["FXR-045R"]


def test_multiple_prefixes_on_one_sheet():
    assert ids("GTI-038-039 GDTI-002CO") == ["GTI-038", "GTI-039", "GDTI-002CO"]


def test_repair_shot_alongside_range():
    got = ids("FXR-089-090 FXR-090R")
    assert got == ["FXR-089", "FXR-090", "FXR-090R"]


# -- spelt-out convention (GL 31) -------------------------------------------

def test_to_range_with_repeated_prefix():
    assert ids("AXR-03 to AXR-15") == [f"AXR-{n:03d}" for n in range(3, 16)]


def test_to_range_without_a_space():
    assert ids("AFB-01P toAFB-05")[:5] == [
        "AFB-001P", "AFB-002", "AFB-003", "AFB-004", "AFB-005"
    ]


def test_several_ranges_and_a_single_in_one_name():
    got = ids_from_filename("AFB-01P toAFB-05, AFB-08P to AFB-17, AFB-19 -  17600142 - 06-03-26.pdf")
    assert "AFB-001P" in got_str(got) and "AFB-003" in got_str(got)
    assert "AFB-012" in got_str(got) and "AFB-019" in got_str(got)
    # 06 and 07 are on a different sheet and must NOT be invented here.
    assert "AFB-006" not in got_str(got)


def got_str(items) -> set[str]:
    return {str(i) for i in items}


def test_ticket_number_is_not_read_as_a_range_end():
    # "AXR-95 -  17600194" must not become a 17-million-shot range.
    assert ids_from_filename("AXR-95 -  17600194 - 06-29-26.pdf") == [NdeId("AXR", 95, "")]


# -- weld report NOTES ------------------------------------------------------

def test_notes_normalise_to_padded_form():
    assert str(parse_one("DTI-5")) == "DTI-005"
    assert str(parse_one("DXR-85")) == "DXR-085"


def test_free_text_note_yields_nothing():
    assert parse_one("BORE 2") is None


# -- filename handling ------------------------------------------------------

def test_line_prefix_and_date_are_stripped():
    got = ids_from_filename("20IN LP 09.09.25 GFB-037-040.pdf")
    assert got_str(got) == {"GFB-037", "GFB-038", "GFB-039", "GFB-040"}


def test_windows_copy_marker_does_not_drop_the_sheet():
    # The corpus contains sheets that exist *only* in "(1)" form; ignoring them
    # invents missing shots.
    got = ids_from_filename("20IN LP 09.25.25 FFB-005-006 (1).pdf")
    assert got_str(got) == {"FFB-005", "FFB-006"}


def test_non_sheet_filenames_yield_nothing():
    assert ids_from_filename("20IN LP 09.26.25 INFO.pdf") == []


def test_sheet_dates():
    assert sheet_date("20IN LP 09.09.25 GFB-037-040.pdf") == "2025-09-09"
    assert sheet_date("AXR-03 to AXR-15 -  17600167 - 06-11-26.pdf") == "2026-06-11"
    # Month 0 is a typo in the corpus; refuse to guess rather than invent a date.
    assert sheet_date("20IN LP 0.16.25 DBR-001P-005.pdf") is None


# -- sequence gaps ----------------------------------------------------------

def test_gaps_finds_the_hole():
    present = [NdeId("FXR", n) for n in list(range(1, 81)) + list(range(85, 93))]
    assert [str(g) for g in gaps(present)] == ["FXR-081", "FXR-082", "FXR-083", "FXR-084"]


def test_repairs_and_cutouts_do_not_create_gaps():
    present = [NdeId("GFB", 1), NdeId("GFB", 2), NdeId("GFB", 45, "R"), NdeId("GFB", 3)]
    assert gaps(present) == []


def test_no_gaps_reported_for_a_single_shot():
    assert gaps([NdeId("GPT", 7)]) == []


# -- cut-outs ---------------------------------------------------------------
#
# A cut-out is named for the weld it removed, so its number is borrowed from
# the line's run rather than taken from one of its own. Getting that wrong cost
# a hundred and fifty-three findings on the Flexsteel spread.

def test_a_cut_out_series_has_no_run_to_have_a_gap_in():
    # GCFB holds exactly four sheets - the four welds removed from the line -
    # numbered 31, 37, 39 and 114. Measured 31 to 114 it read as eighty
    # missing sheets.
    co = [NdeId("GCFB", n, "CO") for n in (31, 37, 39, 114)]
    assert gaps(co) == []
    assert cutout_series(co) == {"GCFB"}


def test_a_bare_sighting_does_not_re_anchor_a_cut_out():
    # The same shot arrives twice and only one source says CO: the filename is
    # `GCFB-31 CO ,GCFB-37 CO PO FLEX STEEL.pdf`, while the sheet's own table
    # prints a bare `GCFB-31`. Judging per shot rather than per number let the
    # bare copy put the whole span back.
    both = [NdeId("GCFB", 31, "CO"), NdeId("GCFB", 31),
            NdeId("GCFB", 114, "CO"), NdeId("GCFB", 114)]
    assert gaps(both) == []


def test_a_cut_out_inside_a_full_run_is_not_a_hole():
    # The other direction. `GFB-64CO` is the only sheet bearing 64 in a series
    # that runs 1 to 129; dropping cut-outs from what is on file invents a gap
    # in the middle of a complete run.
    run = [NdeId("GFB", n) for n in range(1, 130) if n != 64]
    run.append(NdeId("GFB", 64, "CO"))
    assert gaps(run) == []


def test_a_line_with_ordinary_shots_is_not_a_cut_out_series():
    run = [NdeId("GFB", n) for n in range(1, 130)] + [NdeId("GFB", 45, "CO")]
    assert cutout_series(run) == set()


def test_a_trailing_cut_out_does_not_extend_the_run():
    # Nothing was shot after 5; the sheet at 40 removed a weld numbered 40 on
    # a run that is filed elsewhere. Anchoring on it claims thirty-four
    # missing sheets.
    ids = [NdeId("FTI", n) for n in range(1, 6)] + [NdeId("FTI", 40, "CO")]
    assert gaps(ids) == []
    assert sequences(ids)["FTI"] == (1, 5, 6)


def test_a_compound_suffix_is_read():
    # `GFFB-001PCO` - the procedure shot for a weld that was later cut out.
    # Read as a bare `P` it anchored the series and kept GFFB out of the
    # cut-out classification.
    one = parse_ids("GFFB-001PCO")[0]
    assert (one.suffix, one.is_procedure, one.is_cutout) == ("PCO", True, True)
    assert str(one) == "GFFB-001PCO"


def test_the_real_gffb_sheets_are_all_cut_outs():
    ids = (ids_from_filename("GFFB-001PCO,GFFB-002CO,GFFB-078CO,GFFB-079CO  9-24-25.pdf")
           + ids_from_filename("GFFB-34CO, GFFB-72CO  9-22-25.pdf"))
    assert [str(i) for i in ids] == [
        "GFFB-001PCO", "GFFB-002CO", "GFFB-078CO", "GFFB-079CO",
        "GFFB-034CO", "GFFB-072CO",
    ]
    assert cutout_series(ids) == {"GFFB"}
    assert gaps(ids) == []


def test_a_plain_c_suffix_is_still_not_a_cut_out():
    # AFB-16C on the Kestrel 8 weld maps is a crew designator, not a removal.
    assert parse_ids("AFB-16C")[0].is_cutout is False
