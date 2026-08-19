"""Reading an NDE examination report out of its text layer.

The filename convention is not universal. Bluewater and GL 31 name a sheet for
the welds it covers; Kestrel 8 names it for the day — `DTD22 NDE 5.28.25 FG
SEG.A RT RIG.A.pdf` — so the filename pass finds nothing in any of its
sixty-five sheets and every NDE rule on that job stays dark.

Two things in here are worth more than the rest. **Accept and reject need
coordinates**: both render as a bare `✔` in flattened text, so the tick is
assigned to whichever column header it sits nearer. And **not every
identifier on the page is a shot** — the penetrant form lists its consumables
at the foot, and a batch number like `VP-31A` is shaped exactly like a weld
id.

The geometry below is the real layout from the IIA Field Services form.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.readersheet import is_precision_group, parse_page  # noqa: E402

#: (x0, y0, x1, y1, text) is all the parser reads of a word tuple.
def w(text, x, y, width=None):
    return (x, y, x + (width if width is not None else len(str(text)) * 5),
            y + 8, str(text), 0, 0, 0)


#: Column headers, at the x positions the real form uses. The first data row
#: sits 21 points below `ACC` on every sheet in the corpus, which is what
#: separates a stacked header from the rows under it — an earlier version of
#: this fixture put it 12 below and made a correct header band look broken.
HEADER = [
    w("#", 36, 115), w("WELD", 60, 114), w("LOCATION", 109, 114),
    w("ACC", 158, 114, 10), w("REJ", 178, 114, 8), w("DIAM.", 196, 114, 15),
    w("THICK.", 218, 114, 19), w("STENCIL(S)", 306, 114, 26),
]

TOP = [
    w("Ticket", 495, 19), w("No:", 517, 19), w("17200864", 539, 22),
    w("Customer:", 34, 53), w("Date:", 461, 53),
    w("XTO", 75, 56), w("Energy", 94, 56), w("05/28/2025", 486, 56),
    w("Technician:", 34, 81), w("Level:", 230, 81), w("Assistant:", 366, 81),
    w("Juan", 75, 85), w("Rodriguez", 95, 85), w("II", 252, 85),
    w("Gabriel", 399, 85), w("Gonzales", 427, 85),
]


def shot(nde_id, y, *, tick_x=159, diameter="4.500", wall="0.337",
         welders="ARV/AFM"):
    row = [w(nde_id, 51, y)]
    if tick_x is not None:
        row.append(w("✔", tick_x, y, 6))
    if diameter:
        row.append(w(diameter, 197, y, 14))
    if wall:
        row.append(w(wall, 220, y, 14))
    if welders:
        row.append(w(welders, 307, y, 24))
    return row


def page(*rows):
    out = list(TOP) + list(HEADER)
    for r in rows:
        out += r
    return out


# -- the header -------------------------------------------------------------

def test_header_fields_are_read():
    sheet = parse_page(page(shot("AXR-01P.", 135)))
    assert sheet.ticket == "17200864"
    assert sheet.sheet_date == "05/28/2025"
    assert sheet.customer == "XTO Energy"


def test_a_two_word_label_does_not_bound_its_own_value():
    # `Ticket No:` — the second word also ends in a colon, and using it as the
    # right-hand bound left the ticket number, work order and job name blank.
    assert parse_page(page(shot("AXR-01", 135))).ticket == "17200864"


def test_a_value_on_the_labels_own_baseline_is_read():
    # `DBR 1P-8 7-16-25.pdf` prints `Ticket No:` at y 31.0 and `18700011` at
    # 30.6 — four tenths of a point *above*. A window that began below the
    # label read the ticket on 294 pages and missed it on 38, and the ticket
    # is the only thing tying a report's pages together.
    words = [w("Ticket", 467, 31, 22), w("No:", 490, 31, 15),
             w("18700011", 508, 30.6, 37)]
    assert parse_page(words + list(HEADER) + shot("AXR-01", 135)).ticket \
        == "18700011"


def test_the_label_does_not_land_in_its_own_value():
    # The other side of that widening: with the value on the same baseline,
    # the label's words sit in the window too.
    words = [w("Ticket", 467, 31, 22), w("No:", 490, 31, 15),
             w("18700011", 508, 30.6, 37)]
    assert "Ticket" not in parse_page(
        words + list(HEADER) + shot("AXR-01", 135)).ticket


def test_a_label_two_words_long_stops_the_value_before_it():
    # `Per Diem:` follows the job name. Only `Diem:` ends in a colon, so
    # bounding on colon-words alone left a trailing `Per` on every job name,
    # `Work` on every contractor and `Job` on every location.
    words = [w("Job", 223, 104, 12), w("Name:", 237, 104, 20),
             w("BLUEWATER", 262, 104, 30), w("PIPELINE", 294, 104, 34),
             w("Per", 402, 104, 12), w("Diem:", 416, 104, 20)]
    sheet = parse_page(words + list(HEADER) + shot("AXR-01", 135))
    assert sheet.job_name == "BLUEWATER PIPELINE"


def test_a_distant_label_does_not_claim_the_word_before_it():
    # ...but "the word before a colon-word" only holds when the two are
    # adjacent. `Level:` sits ninety points past `Rodriguez` on the same row,
    # and treating that as one label truncated the technician to "Juan".
    sheet = parse_page(page(shot("AXR-01", 135)))
    assert sheet.technician == "Juan Rodriguez"


def test_the_next_label_bounds_the_value():
    # `Technician:` is followed by `Level:`, which is not a field this module
    # collects; without it as a boundary the level lands in the name.
    sheet = parse_page(page(shot("AXR-01", 135)))
    assert sheet.technician == "Juan Rodriguez"
    assert sheet.assistant == "Gabriel Gonzales"


# -- the shot table ---------------------------------------------------------

def test_rows_are_read_with_their_values():
    sheet = parse_page(page(shot("AXR-01P.", 135), shot("AXR-02", 147)))
    assert [r.nde_id for r in sheet.rows] == ["AXR-01P", "AXR-02"]
    first = sheet.rows[0]
    assert first.diameter == "4.500" and first.wall == "0.337"
    assert first.welders == "ARV/AFM"


def test_the_trailing_full_stop_is_the_form_s_not_the_id_s():
    assert parse_page(page(shot("AXR-01P.", 135))).rows[0].nde_id == "AXR-01P"


def test_a_tick_in_the_accept_column_is_an_accept():
    assert parse_page(page(shot("AXR-01", 135, tick_x=159))).rows[0].result == "ACC"


def test_a_tick_in_the_reject_column_is_a_reject():
    # Flattened text renders both columns as the same bare tick; only the x
    # position distinguishes them.
    assert parse_page(page(shot("AXR-01", 135, tick_x=180))).rows[0].result == "REJ"


def test_no_tick_is_no_result_rather_than_an_accept():
    # `DTD22 NDE 6.01.25 LP SEG.D RT.pdf` lists eighteen welds and marks
    # neither box on any of them. Defaulting to accept would bury that.
    sheet = parse_page(page(shot("AFB-03", 135, tick_x=None)))
    assert sheet.rows[0].result == ""


def test_diameter_and_wall_do_not_read_each_other():
    # The two columns are twenty-four points apart, so a fixed window wide
    # enough for one swallows the other.
    row = parse_page(page(shot("AXR-01", 135))).rows[0]
    assert row.diameter == "4.500" and row.wall == "0.337"


# -- what is not a shot -----------------------------------------------------

#: The consumables panel at the foot of every penetrant sheet, at the
#: coordinates all 141 of them use.
CONSUMABLES = [
    w("Penetrant", 60, 561), w("Remover", 150, 561), w("Developer", 238, 561),
    w("VP-31A", 48, 613), w("E-59A", 133, 613), w("D-70", 222, 613),
    w("Light", 307, 613),
]


def test_a_consumable_batch_number_is_not_a_shot():
    # `VP-31A` penetrant, `E-59A` emulsifier and `D-70` developer. Five PT
    # sheets each yielded a phantom VP-031A, which invented a series for the
    # gap rule to find holes in.
    sheet = parse_page(page(shot("GPT-3", 135)) + CONSUMABLES)
    assert [r.nde_id for r in sheet.rows] == ["GPT-3"]


def test_the_consumables_heading_is_what_bounds_the_table():
    # Bounded by where the panel is, not by guessing at the shape of what is
    # in it. Without the heading row there is nothing to bound against.
    codes = [x for x in CONSUMABLES if x[4] not in ("Penetrant", "Remover",
                                                    "Developer")]
    assert [r.nde_id for r in parse_page(page(shot("GPT-3", 135)) + codes).rows] \
        == ["GPT-3", "VP-31A"]
    assert len(parse_page(page(shot("GPT-3", 135)) + CONSUMABLES).rows) == 1


def test_one_mention_of_penetrant_is_the_title_not_the_panel():
    # `LIQUID PENETRANT EXAMINATION REPORT` runs across the top of the form.
    # Bounding on it would empty the table.
    title = [w("LIQUID", 190, 60), w("PENETRANT", 224, 60),
             w("EXAMINATION", 290, 60), w("REPORT", 360, 60)]
    sheet = parse_page(title + page(shot("GPT-3", 135)))
    assert [r.nde_id for r in sheet.rows] == ["GPT-3"]


# -- the penetrant form -----------------------------------------------------
#
# IIA issues two forms. The radiographic one is above; this is the liquid
# penetrant one, at the coordinates `DTD22 NDE 6.01.25 LP SEG.D PT.pdf` uses.
# It labels three columns differently, and its single row carries no tick and
# no measurement — so the earlier row test emptied it, and two PLU sheets with
# a full text layer were counted as blank scans.

PT_HEADER = [
    w("#", 42, 204), w("WELD", 58, 204), w("OR", 80, 204), w("PART", 96, 204),
    w("ID", 117, 204), w("LOCATION", 148, 204),
    w("ACC", 217, 204, 11), w("REJ", 243, 204, 10),
    w("SIZE", 266, 208, 16), w("THICK", 289, 208, 22),
    w("MAT'L", 316, 208, 20), w("STENCIL", 353, 208, 25),
]

PT_ROW = [
    w("1", 42, 225), w("APT-01", 76, 225), w("360", 146, 225),
    w("Degrees", 164, 225), w("2”", 270, 225, 9),
    w("4xsX2”", 288, 225, 25), w("CS", 322, 225), w("ARO", 358, 225),
]

#: Rows 2 to 25 are printed but never filled in.
PT_BLANKS = [w(str(n), 42, 237 + 12 * i) for i, n in enumerate(range(2, 26))]


def test_the_penetrant_form_is_read():
    sheet = parse_page(PT_HEADER + PT_ROW + PT_BLANKS + CONSUMABLES)
    assert [r.nde_id for r in sheet.rows] == ["APT-01"]
    row = sheet.rows[0]
    assert row.welders == "ARO"
    # Neither box is ticked on this sheet, which is the finding.
    assert row.result == ""


def test_its_differently_labelled_columns_are_recognised():
    # OBJECT SIZE and THICK where the radiographic form says DIAM. and THICK.,
    # WELDER STENCIL where it says STENCIL(S).
    row = parse_page(PT_HEADER + PT_ROW + CONSUMABLES).rows[0]
    assert (row.diameter, row.wall) == ("2”", "4xsX2”")


def test_the_technique_block_does_not_move_the_header():
    # The radiographic form's footer records the film exposure, and labels two
    # of its cells `Thick` and `Size` — the same words the table uses. Taking
    # column headers from anywhere on the page put the table's header six
    # hundred points down, below every row, and emptied 868 sheets.
    technique = [w("Tech", 40, 616), w("Mat’l", 54, 616), w("PlpeOD", 66, 616),
                 w("Thick", 88, 620), w("Thick", 106, 620), w("Size", 238, 620),
                 w("Size", 331, 620)]
    sheet = parse_page(page(shot("AXR-01", 135)) + technique)
    assert [r.nde_id for r in sheet.rows] == ["AXR-01"]


def test_size_names_the_diameter_only_when_there_is_no_diam():
    # The radiographic header stacks PIPE over DIAM for the diameter and SIZE
    # over THICK for the wall. Reading SIZE as the diameter there puts the two
    # columns three points apart and neither reads anything.
    rt = [w("ACC", 160, 219, 11), w("REJ", 184, 219, 10),
          w("SIZE", 231, 215, 14), w("DIAM", 205, 226, 17),
          w("THICK", 232, 226, 18), w("STENCIL", 320, 227, 25)]
    row = [w("GFB-97", 58, 240), w("6", 212, 240), w(".432", 234, 240),
           w("3", 265, 240), w("1", 292, 240), w("APO", 325, 240),
           w("N/A", 378, 240)]
    sheet = parse_page(rt + row)
    assert [r.nde_id for r in sheet.rows] == ["GFB-97"]
    assert (sheet.rows[0].diameter, sheet.rows[0].wall) == ("6", ".432")


def test_the_welder_column_does_not_reach_across_unlabelled_ones():
    # `# OF EXPOS` and `TECH ID` sit between the wall and the stencil and carry
    # no label this module knows, so half-the-gap-to-the-neighbour lets the
    # stencil column swallow them and read "1 APO".
    rt = [w("ACC", 160, 219, 11), w("REJ", 184, 219, 10),
          w("DIAM", 205, 226, 17), w("THICK", 232, 226, 18),
          w("STENCIL", 320, 227, 25)]
    row = [w("GFB-97", 58, 240), w("6", 212, 240), w(".432", 234, 240),
           w("3", 265, 240), w("1", 292, 240), w("APO", 325, 240)]
    assert parse_page(rt + row).rows[0].welders == "APO"


def test_the_blank_numbered_rows_are_not_shots():
    sheet = parse_page(PT_HEADER + PT_ROW + PT_BLANKS + CONSUMABLES)
    assert len(sheet.rows) == 1


def test_a_row_with_a_measurement_but_no_tick_is_still_a_shot():
    sheet = parse_page(page(shot("AFB-03", 135, tick_x=None, welders="")))
    assert [r.nde_id for r in sheet.rows] == ["AFB-03"]


def test_a_page_without_the_result_columns_is_not_this_form():
    words = [x for x in page(shot("AXR-01", 135))
             if x[4] not in ("ACC", "REJ")]
    assert parse_page(words).rows == []


def test_an_empty_page_yields_nothing():
    # Nine of PLU's sixty-five sheets are scans with no text at all; those are
    # the vision pass's job, not a parse failure.
    assert parse_page([]).rows == []
    assert parse_page([]).is_report is False


def test_text_above_the_table_is_not_a_shot():
    stray = [w("ABC-12", 51, 60)]
    sheet = parse_page(page(shot("AXR-01", 135)) + stray)
    assert [r.nde_id for r in sheet.rows] == ["AXR-01"]


# -- the fourth vendor's form -----------------------------------------------
#
# D Precision Group, trading as PNDT. Six documents on Bluewater, in two exam
# types. These are scans carrying an OCR layer, and the OCR is wrong exactly
# where it matters, so the module refuses them rather than reading them.

def test_the_precision_group_form_is_refused():
    words = [w("PRECISION", 100, 36), w("GROUP", 100, 48),
             w("PNDT", 90, 116), w("Office:", 118, 116),
             w("Part", 100, 289), w("ID", 114, 289), w("Status", 410, 291),
             w("FPT-015", 96, 307), w("Weldolet", 170, 307), w("AEA", 310, 308)]
    sheet = parse_page(words)
    assert sheet.rows == []
    assert sheet.needs_vision == "precision_group"


def test_it_is_refused_even_where_the_letterhead_is_cropped():
    # Continuation pages lose the address block but keep both marker words.
    assert is_precision_group([w("Precision", 100, 20), w("PNDT", 90, 40)])


def test_an_iia_sheet_is_not_mistaken_for_it():
    sheet = parse_page(page(shot("AXR-01", 135)))
    assert sheet.needs_vision == ""
    assert [r.nde_id for r in sheet.rows] == ["AXR-01"]


def test_one_marker_alone_is_not_the_form():
    # "Precision" turns up in instrument descriptions; PNDT does not appear
    # on any other form in the corpus. Requiring both keeps a stray word from
    # silently sending a readable sheet to the vision pass.
    assert is_precision_group([w("Precision", 100, 20)]) is False
    assert is_precision_group([w("PNDT", 90, 40)]) is False


# -- writing it back --------------------------------------------------------

def test_ids_are_normalised_on_the_way_into_the_database(tmp_path):
    # The sheet writes `CXR-34` where the filename pass writes `CXR-034`;
    # unnormalised they would never join.
    from weldaudit.ids import parse_ids
    assert str(parse_ids("CXR-34")[0]) == "CXR-034"
    assert str(parse_ids("GTI-1")[0]) == "GTI-001"


def test_the_sheet_date_beats_the_filename():
    from weldaudit.extract.readersheets import _iso_date
    assert _iso_date("05/28/2025") == "2025-05-28"
    assert _iso_date("5/28/25") == "2025-05-28"
    assert _iso_date("") is None
    assert _iso_date("not a date") is None


@pytest.mark.parametrize("filename", [
    "DTD22 NDE 5.28.25 FG SEG.A RT RIG.A.pdf",
    "DTD22 NDE 08.22.25 LP SEG.D PT.pdf",
    "DTD 22 -4in FG. Seg. D ..RT.XTO.05.31.25.pdf",
])
def test_plu_filenames_carry_no_shot_numbers(filename):
    # The reason the text pass exists at all.
    from weldaudit.extract.readersheets import ids_from_filename
    assert ids_from_filename(filename) == []
