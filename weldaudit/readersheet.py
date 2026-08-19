"""Reading an NDE examination report out of its text layer.

Two filing conventions put the shot numbers in different places.  Bluewater and
GL 31 name the sheet for the welds it covers — ``20IN LP 09.09.25
GFB-037-040.pdf`` — so the filename alone establishes what exists.  Kestrel 8
names its sheets for the *day* instead — ``DTD22 NDE 5.28.25 FG SEG.A RT
RIG.A.pdf`` — and the shot numbers are only inside.  Sixty-five sheets, and
the filename pass finds nothing in any of them.

They are not scans.  The IIA Field Services form is generated, so its text
layer is complete: weld id, result, pipe diameter and wall, welder stencils,
technician, procedure and date.  That is more than the Bluewater filenames give,
and it costs nothing to read.

**The result needs coordinates.**  Accept and reject are adjacent tick-box
columns, and flattened text renders both as a bare ``✔`` with nothing to say
which column it fell in.  So the header words ``ACC`` and ``REJ`` are located
first and each tick is assigned to whichever it is nearer — the same approach
the printed flange logs need, for the same reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: A tick, in the several characters the form's fonts use for one.
TICKS = frozenset("✔✓☑√x")

#: How far from a header label's baseline its value is printed. Measured
#: across the corpus the drop runs from -0.4 to +3.6 — the entry sits on the
#: label's own line, typeset a little low.
_VALUE_DROP = (-2.0, 7.0)

#: Rows are about eleven points apart; a value belongs to a row if its centre
#: is within half of that.
_ROW_TOLERANCE = 5.0

#: Header labels worth reading, mapped to field names.
HEADER_LABELS: dict[str, str] = {
    "ticket no:": "ticket",
    "customer:": "customer",
    "afe:": "afe",
    "date:": "sheet_date",
    "job name:": "job_name",
    "contractor:": "contractor",
    "work order:": "work_order",
    "technician:": "technician",
    "assistant:": "assistant",
    "location:": "location",
}

#: Header words naming each column, best first.  IIA issues a radiographic
#: form and a liquid-penetrant one and they label the same columns
#: differently, so each field accepts several spellings.
#:
#: ``size`` is a *fallback* for the diameter rather than an equal.  The
#: radiographic form's header is stacked three lines deep and pairs ``PIPE``
#: over ``DIAM`` for the diameter with ``SIZE`` over ``THICK`` for the wall -
#: so on that sheet ``SIZE`` sits three points from ``THICK`` and reading it
#: as the diameter collapses both columns onto the same place.  The penetrant
#: form has no ``DIAM`` at all and means ``OBJECT SIZE``.
_COLUMN_WORDS: dict[str, tuple[str, ...]] = {
    "accept": ("acc",),
    "reject": ("rej",),
    "diameter": ("diam.", "diam", "size"),
    "wall": ("thick.", "thick"),
    "welders": ("stencil(s)", "stencil"),
}

_COLUMN_LABELS = {word: field_name
                  for field_name, words in _COLUMN_WORDS.items()
                  for word in words}

#: How far from the ``ACC`` row a word may sit and still be a column header.
#: The radiographic header is stacked over four lines spanning twelve points;
#: the first data row is twenty below.
_HEADER_BAND = 14.0

#: Furthest a value may sit from its column's centre.  Half the gap to the
#: nearest labelled neighbour is the usual bound, but several columns carry no
#: label this module knows - ``# OF EXPOS``, ``TECH ID`` - and without a cap
#: the welder column reaches across them and reads "1 APO".
_MAX_REACH = 26.0

#: The penetrant form ends with a panel of consumables - a Penetrant, a
#: Remover and a Developer, each with a manufacturer, a product and a batch
#: number.  Two of those three words on one line is the panel's header.
_CONSUMABLE_HEADINGS = frozenset({"penetrant", "remover", "developer"})

#: `AXR-01P.`, `GFB-037`, `TI-1`. The trailing full stop is the form's, not
#: part of the identifier.
_WELD_ID = re.compile(r"^([A-Z]{2,5}-\d{1,4}[A-Z]{0,2})\.?$", re.IGNORECASE)

#: A fourth NDE vendor, D Precision Group, trading as PNDT. Both words survive
#: on every page of every copy in the corpus, including the continuation pages
#: where the letterhead is cropped away.
_PRECISION_MARKERS = ("pndt", "precision")


#: `Page: 3 of 4`. The IIA form prints the labels on every sheet and the crew
#: fills the numbers in on almost none: of 208 text-bearing Bluewater sheets, 200
#: carry the words and the text comes out as a bare `Page:\nof\n`. Only the
#: filled-in ones say anything, so a blank is silence rather than a claim.
_PAGE_OF = re.compile(r"page\s*:?\s*(\d{1,3})\s*of\s*(\d{1,3})\b", re.IGNORECASE)

#: `Ticket No: 18700172` on the IIA forms, `REF#: RT-1061-0794` on the
#: Precision Group one. This is what ties a report's pages together when they
#: are filed as separate PDFs, which is how the crew files them.
_TICKET = re.compile(r"ticket\s*(?:no|#)?\s*:?\s*(\d{5,12})\b", re.IGNORECASE)
_REF = re.compile(r"ref\s*#?\s*:?\s*([A-Z]{2}-\d{3,5}-\d{3,5})", re.IGNORECASE)

#: `Weld Count: 8` at the foot of a Precision Group report.
_WELD_COUNT = re.compile(r"weld\s*count\s*:?\s*(\d{1,3})\b", re.IGNORECASE)


def stated_pagination(text: str) -> tuple[int, int] | None:
    """``(page, of)`` where a report numbers itself, else None.

    The label is nearly universal and the numbers are nearly always blank, so
    this reads as `Ticket No: Page: 3 of 4` on the sheets that fill it in and
    as `Page: of` on the ones that do not.
    """
    match = _PAGE_OF.search(text or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def stated_ticket(text: str) -> str:
    """The report's ticket or reference number, blank if the box is empty.

    A blank one is not unusual and is worth knowing about: it is the only
    thing that links a report's pages once they are filed as separate PDFs, so
    without it a sheet claiming to be page 3 of 4 cannot be chased.
    """
    match = _TICKET.search(text or "") or _REF.search(text or "")
    return match.group(1) if match else ""


def stated_weld_count(text: str) -> int | None:
    """The number of welds a report says it examined, if it says.

    Read from the page's flowed text rather than through :func:`parse_page`,
    and deliberately so: the only form in this corpus that states a count is
    the Precision Group one, whose *table* is refused. The label and its digits
    are plain black text at the foot of the page and survive the OCR that the
    highlighted result column does not — of the eight Precision Group reports,
    every one states its count and eight are legible.
    """
    match = _WELD_COUNT.search(text or "")
    return int(match.group(1)) if match else None


def is_precision_group(words: list[Word]) -> bool:
    """Whether this page is a Precision Group examination report.

    Worth naming because its text layer must **not** be parsed. The IIA forms
    are generated and their text is exact; these are scans carrying an OCR
    layer, and the OCR is wrong in the places that decide an audit:

    * the result column is highlighted — green for Accept, red for Rejected —
      and the highlight defeats recognition. Four penetrant sheets show 8, 6,
      4 and 6 assessed welds between them and their text layers hold 0, 0, 3
      and 6 of the words. On the radiography sheets 31 `Accept`s survive and
      **one of the two `Rejected`s does not**.
    * identifiers are misread: `FPT-G18` for FPT-018, `FP1-022` for FPT-022,
      and `FTI-039R` — the repair shot — is absent altogether.
    * stencils are misread: `AMR` where the page says `ANR`, which is a
      perfectly plausible stencil and would be checked against the roster.

    A missed reject and an invented welder are the two worst things this tool
    can produce, so these pages go to the vision pass instead.
    """
    found = {word for w in words
             for word in _PRECISION_MARKERS
             if word in _text(w).lower()}
    return len(found) == len(_PRECISION_MARKERS)


@dataclass
class Row:
    nde_id: str = ""
    result: str = ""            # 'ACC' | 'REJ' | '' when neither box is ticked
    diameter: str = ""
    wall: str = ""
    welders: str = ""
    remarks: str = ""
    y: float = 0.0


@dataclass
class Sheet:
    ticket: str = ""
    customer: str = ""
    afe: str = ""
    sheet_date: str = ""
    job_name: str = ""
    contractor: str = ""
    work_order: str = ""
    technician: str = ""
    assistant: str = ""
    location: str = ""
    procedure: str = ""
    rows: list[Row] = field(default_factory=list)
    #: Set when the page is a form this module deliberately declines to read,
    #: as opposed to one it simply found nothing on.
    needs_vision: str = ""

    @property
    def is_report(self) -> bool:
        return bool(self.rows)


Word = tuple  # (x0, y0, x1, y1, text, block, line, word_no)


def _text(word: Word) -> str:
    return str(word[4]).strip()


def _centre(word: Word) -> float:
    return (word[0] + word[2]) / 2


def _mid_y(word: Word) -> float:
    return (word[1] + word[3]) / 2


def _labels(words: list[Word]) -> dict[str, tuple[Word, float]]:
    """Header labels as ``{field: (first word, x where the label ends)}``.

    ``Ticket No:`` and ``Work Order:`` are two words; ``Date:`` is one. The
    end x matters as much as the start: a two-word label's own second word
    also ends in a colon, and would otherwise bound its own value — leaving
    the ticket number, the work order and the job name all blank.
    """
    found: dict[str, tuple[Word, float]] = {}
    ordered = sorted(words, key=lambda w: (round(w[1]), w[0]))
    for i, word in enumerate(ordered):
        one = _text(word).lower()
        if one in HEADER_LABELS and HEADER_LABELS[one] not in found:
            found[HEADER_LABELS[one]] = (word, word[2])
        if i + 1 < len(ordered):
            nxt = ordered[i + 1]
            if abs(nxt[1] - word[1]) < 2:
                pair = f"{one} {_text(nxt).lower()}"
                if pair in HEADER_LABELS and HEADER_LABELS[pair] not in found:
                    found[HEADER_LABELS[pair]] = (word, nxt[2])
    return found


#: Widest gap between two words that are still one label. `Per` and `Diem:`
#: sit two points apart; `Rodriguez` and the `Level:` that follows it, ninety.
_LABEL_KERNING = 6.0


def _starts_a_label(word: Word, row: list[Word]) -> bool:
    """Whether a word begins a field label, so a value must stop before it.

    A label is a colon-word, or the word immediately in front of one:
    ``Diem:`` ends the label ``Per Diem:``, and a value before it has to stop
    at ``Per``. Reading only the colon-word left a trailing ``Per`` on every
    job name, ``Work`` on every contractor and ``Job`` on every location.

    **Immediately** is the load-bearing word. Any value whose last word happens
    to be followed somewhere on the line by a label would otherwise be treated
    as part of it — the technician came out as "Juan", because `Level:` sits
    further along the same row than `Rodriguez` does.
    """
    if _text(word).endswith(":"):
        return True
    after = [w for w in row if w[0] > word[0]]
    if not after:
        return False
    nxt = min(after, key=lambda w: w[0])
    return _text(nxt).endswith(":") and nxt[0] - word[2] <= _LABEL_KERNING


def _value_for(label: Word, end_x: float, words: list[Word]) -> str:
    """The words printed against a label, to the right of where it ends.

    The value sits on the label's own baseline or a few points under it — the
    drop runs from -0.4 to +3.6 across the corpus, because the forms typeset
    the entry a little low. A window that began *below* the label read the
    ticket number on 294 pages and missed it on 38, including every sheet
    where the two share a baseline exactly.

    Bounded on the right by the next label, not merely the next field this
    module collects: ``Technician:`` is followed by ``Level:`` and ``Work
    Order:`` by ``Miles:``, and without them as boundaries their values are
    swallowed into the field before — "Juan Rodriguez II" for the technician.
    """
    low, high = label[1] + _VALUE_DROP[0], label[1] + _VALUE_DROP[1]
    # The value may share the label's baseline, so the label's own words have
    # to be excluded by identity rather than by position: a word belongs to the
    # label exactly when it *ends* at or before the label does. Bounding on
    # where the label starts would drop a value that begins under it.
    row = [w for w in words
           if low <= w[1] <= high and w[0] >= label[0] and w[2] > end_x]
    right = min((w[0] for w in row if _starts_a_label(w, row)), default=1e9)
    picked = [w for w in row if w[0] < right]
    return " ".join(_text(w) for w in sorted(picked, key=lambda w: w[0])).strip()


def _columns(words: list[Word]) -> tuple[dict[str, float], float]:
    """``({column: centre x}, y below which the rows start)``.

    Anchored on ``ACC`` and ``REJ``, which appear once and only in the header.
    Everything else is taken from the band around them.

    The anchor is not decoration. The radiographic form repeats ``Size`` and
    ``Thick`` six hundred points down the page, in the technique block that
    records the film exposure — and a version of this function that scanned
    the whole sheet put the table's header *below* every row it was meant to
    bound, silently emptying eight hundred sheets.
    """
    anchors = [w for w in words if _text(w).lower() in ("acc", "rej")]
    if not anchors:
        return {}, 0.0
    top = min(w[1] for w in anchors)
    band = [w for w in words if abs(w[1] - top) <= _HEADER_BAND]

    seen: dict[str, Word] = {}
    for word in band:
        key = _text(word).lower()
        if key in _COLUMN_LABELS and key not in seen:
            seen[key] = word

    out: dict[str, float] = {}
    for field_name, spellings in _COLUMN_WORDS.items():
        for spelling in spellings:            # best spelling wins
            if spelling in seen:
                out[field_name] = _centre(seen[spelling])
                break
    return out, max(w[1] for w in band)


def _footer_y(words: list[Word]) -> float:
    """Where the shot table stops, in page coordinates.

    The penetrant form lists its consumables at the foot of the page — `VP-31A`
    penetrant, `E-59A` emulsifier, `D-70` developer — and a batch number is
    shaped exactly like a weld id. Five different PT sheets each yielded a
    phantom `VP-031A`, which then invented a whole series for the gap rule to
    find holes in.

    The panel is easy to find and always in the same place: 141 sheets across
    the corpus carry it, every one of them with the three codes on a single
    line at y≈619, under a heading row reading `Penetrant  Remover  Developer`.
    Bounding the table above it excludes the codes by *where they are* rather
    than by guessing at their shape.

    An earlier version instead required each row to carry a tick or a
    measurement in a known column. That worked on the radiographic sheet and
    silently emptied the penetrant one, whose single row has neither — the
    reading is a bare `360 Degrees` and the columns are labelled differently,
    so nothing on the row was recognisable. Two PLU sheets read as blank scans
    because of it.
    """
    by_line: dict[float, set[str]] = {}
    for word in words:
        text = _text(word).lower().rstrip(":")
        if text in _CONSUMABLE_HEADINGS:
            by_line.setdefault(round(word[1]), set()).add(text)
    tops = [y for y, found in by_line.items() if len(found) >= 2]
    return min(tops) if tops else float("inf")


def _has_a_row(y: float, x: float, words: list[Word]) -> bool:
    """Whether anything is written to the right of an identifier on its row.

    A blank numbered row carries only its printed row number, and the penetrant
    form prints twenty-five of them below the one weld it covers.
    """
    return any(abs(_mid_y(w) - y) <= _ROW_TOLERANCE and w[0] > x
               for w in words)


def parse_page(words: list[Word]) -> Sheet:
    """One examination report page."""
    sheet = Sheet()
    if not words:
        return sheet

    # Refused before anything is read, rather than left to fail by accident.
    # This form has no ACC/REJ header so nothing comes out of it today, but
    # that is luck: it does have a `Status` column, and the next parser that
    # learns a third spelling would start emitting `FPT-G18` accepted.
    if is_precision_group(words):
        sheet.needs_vision = "precision_group"
        return sheet

    for field_name, (label, end_x) in _labels(words).items():
        setattr(sheet, field_name, _value_for(label, end_x, words))

    # The procedure is written as a bare code rather than against a label.
    for word in words:
        if re.match(r"^IIA[-.][A-Z]{2}[-.]", _text(word), re.IGNORECASE):
            sheet.procedure = _text(word)
            break

    columns, header_y = _columns(words)
    if "accept" not in columns or "reject" not in columns:
        return sheet                    # not this form

    # Weld ids run down the leftmost column of the table, between its header
    # and the consumables panel at the foot.
    footer_y = _footer_y(words)
    ids = [w for w in words
           if header_y < w[1] < footer_y and _WELD_ID.match(_text(w))
           and _centre(w) < columns["accept"] - 40]
    if not ids:
        return sheet

    # A tick belongs to the accept or reject column, not to the checkboxes
    # elsewhere on the form.
    span = abs(columns["reject"] - columns["accept"])
    ticks = [w for w in words if _text(w) in TICKS
             and columns["accept"] - span <= _centre(w) <= columns["reject"] + span]

    for word in sorted(ids, key=lambda w: w[1]):
        row = Row(nde_id=_WELD_ID.match(_text(word)).group(1).upper(),
                  y=_mid_y(word))
        if not _has_a_row(row.y, word[2], words):
            continue
        near = [w for w in ticks if abs(_mid_y(w) - row.y) <= _ROW_TOLERANCE]
        if near:
            tick = min(near, key=lambda w: abs(_mid_y(w) - row.y))
            accept = abs(_centre(tick) - columns["accept"])
            reject = abs(_centre(tick) - columns["reject"])
            row.result = "ACC" if accept <= reject else "REJ"

        for name in ("diameter", "wall", "welders"):
            if name not in columns:
                continue
            # Half the gap to the nearest neighbouring column, so DIAM. and
            # THICK. — twenty-four points apart — cannot read each other's
            # values, and never more than _MAX_REACH, so a column with no
            # labelled neighbour does not reach across the unlabelled ones.
            reach = min(
                [abs(x - columns[name]) / 2 for key, x in columns.items()
                 if key != name] + [_MAX_REACH])
            cell = [w for w in words
                    if abs(_mid_y(w) - row.y) <= _ROW_TOLERANCE
                    and abs(_centre(w) - columns[name]) <= reach]
            if cell:
                setattr(row, name,
                        " ".join(_text(w) for w in sorted(cell, key=lambda w: w[0])))
        sheet.rows.append(row)
    return sheet
