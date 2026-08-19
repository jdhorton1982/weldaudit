"""Read an the operator Piping AML PDF into the rows the spreadsheet holds.

There are no ruled lines, so each table's columns come from its own header row
("Manufacturer / Approved Location / Specific Limits"), restated above every
table and at different x on different pages.

The hard part is that one entry spans several physical lines, and the lines
are not in the order you would guess. A one-line cell is centred against its
multi-line neighbour, so the first line of a wrapped location sits ABOVE the
manufacturer it belongs to:

    y=418 +11        Sault Ste. Marie, Ontario,
    y=423 + 5   Norvale Algoma
    y=428 + 5        CANADA
    y=439 +11   Norvale Dalmine    Bergamo, ITALY

Reading line by line therefore gets it wrong however carefully the rules are
written -- and wrong here means an approved manufacturer silently disappears
and its neighbour's name grows. What is reliable is the spacing: lines within
one row sit 1-5pt apart and the next row starts 11-14pt below. So rows are
found by clustering on that gap, and each column is then joined in y order
within its cluster. Line order inside a row carries no meaning; the cluster
does.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

#: A section heading. The number and the title are sometimes two spans
#: ("1.0" | "Pipe") and sometimes one ("11.0  Fasteners (Stud Bolts and
#: Nuts)"), which is not cosmetic: requiring two spans dropped section 11
#: entirely and took all 26 approved fastener suppliers with it, without
#: complaint. Both shapes are accepted, and check_sections() below refuses a
#: parse where any section came back empty.
SECTION = re.compile(r"^(\d{1,2}\.\d)(?:\s+(\S.*))?$")

#: Section number -> the category name the program already uses. Keyed on the
#: number because the wording is not stable: the PDF's "4.0 Gaskets" is the
#: spreadsheet's "4.0 Pipe Gaskets" and "5.0 Valves - Gate / Globe / Check" is
#: just "5.0 Valves". Matching on the title would silently empty a category.
CANON = {
    "1": "1.0 Pipe",             "2": "2.0 Pipe Fittings",
    "3": "3.0 Flanges",          "4": "4.0 Pipe Gaskets",
    "5": "5.0 Valves",           "6": "6.0 Valves",
    "7": "7.0 Valves - Ball",    "8": "8.0 Valves - Plug",
    "9": "9.0 Valves - Special", "10": "10.0 Welding Consumables",
    "11": "11.0 Fasteners",      "12": "12.0 Valve Packing",
    "13": "13.0 Pipe Unions",    "14": "14.0 Valve Chainwheels",
}

#: Lines this far apart are sub-lines of one entry. Measured across the
#: document: sub-lines sit 1-5pt apart, the next entry 13-14pt below.
SUBLINE = 8

#: The running footer. Rows never reach it; notes and disclaimers do.
BODY_BOTTOM = 720
BODY_TOP = 90

SKIP = re.compile(r"^\s*(-|note|click|the latest version|confidential)", re.I)


def _lines(page):
    """Spans grouped by the line they sit on, left to right."""
    out = {}
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for s in line["spans"]:
                if s["text"].strip():
                    out.setdefault(round(s["bbox"][1]), []).append(
                        {"x": s["bbox"][0], "bold": "Bold" in s["font"],
                         "t": s["text"].strip()})
    return [(y, sorted(v, key=lambda s: s["x"])) for y, v in sorted(out.items())]


def _cells(line, loc_x, lim_x):
    man = " ".join(s["t"] for s in line if s["x"] < loc_x - 10)
    loc = " ".join(s["t"] for s in line if loc_x - 10 <= s["x"] < lim_x - 10)
    lim = " ".join(s["t"] for s in line if s["x"] >= lim_x - 10)
    return (" ".join(man.split()), " ".join(loc.split()), " ".join(lim.split()))


def _rows(cells):
    """Group lines into logical rows on the vertical gap between them."""
    out, run = [], []
    for cell in cells:
        if run and cell[0] - run[-1][0] > SUBLINE:
            out.append(run)
            run = []
        run.append(cell)
    if run:
        out.append(run)
    return out


def _stitch(cells, section, out):
    """One table's lines into entries."""
    for run in _rows(cells):
        man = " ".join(c[1] for c in run if c[1]).strip()
        # A bare asterisk in the left margin marks an entry that changed in
        # this revision. It is a footnote marker, not part of the company
        # name, and joining the sub-lines drops it into the middle of one
        # ("Houston Fastener - * PROVISIONAL").
        marked = "*" in man.split()
        man = " ".join(w for w in man.split() if w != "*")
        loc = " ".join(c[2] for c in run if c[2]).strip()
        lim = " ".join(c[3] for c in run if c[3]).strip()
        if not man or not loc or SKIP.match(man):
            continue
        out.append([CANON.get(section.split(".")[0], section), man, loc, lim,
                    section, "changed" if marked else ""])


def parse(path):
    import pymupdf

    doc = pymupdf.open(path)
    out, section = [], None

    for page in doc:
        cells, cols = [], None
        for y, line in _lines(page):
            text = " ".join(s["t"] for s in line)

            head = SECTION.match(line[0]["t"]) if line else None
            if (head and line[0]["bold"] and line[0]["x"] < 100
                    and (head.group(2) or (len(line) > 1 and line[1]["bold"]))):
                if cols and section:
                    _stitch(cells, section, out)
                section, cells, cols = head.group(1), [], None
                continue

            if "Manufacturer" in text and "Approved Location" in text:
                if cols and section:
                    _stitch(cells, section, out)
                cells = []
                cols = (next(s["x"] for s in line if s["t"].startswith("Approved")),
                        next((s["x"] for s in line if s["t"].startswith("Specific")), 1e9))
                continue

            if cols and section and BODY_TOP < y < BODY_BOTTOM:
                cells.append((y, *_cells(line, *cols)))

        if cols and section:
            _stitch(cells, section, out)
    return out


if __name__ == "__main__":
    import sys
    got = parse(sys.argv[1])
    print(f"{len(got):,} entries")


#: Every section the document is expected to contain. A parse that returns
#: nothing for one of these has not found an empty section -- it has failed to
#: read one, and the result is a list that silently refuses every manufacturer
#: in it. That is the failure worth stopping on: "not on the approved list"
#: reads exactly the same whether it is true or whether the page was missed.
EXPECTED = ["1.0 Pipe", "2.0 Pipe Fittings", "3.0 Flanges", "4.0 Pipe Gaskets",
            "5.0 Valves", "6.0 Valves", "7.0 Valves - Ball", "8.0 Valves - Plug",
            "9.0 Valves - Special", "11.0 Fasteners", "12.0 Valve Packing",
            "13.0 Pipe Unions"]

#: Section 10 is prose, not a list. It says so itself -- it "has minimized the
#: past prescriptive list of manufacturers and their location of manufacture"
#: and replaced it with requirements to qualify a supplier. So no table, and
#: no entries, on purpose. Named here rather than left out of EXPECTED so the
#: next person does not have to work out whether it was missed.
NO_LIST = ["10.0 Welding Consumables"]


def check(rows):
    """Complaints about a parse. Empty means it looks sound."""
    from collections import Counter
    per = Counter(r[0] for r in rows)
    bad = [f"{name}: no entries found" for name in EXPECTED if not per[name]]
    bad += [f"{r[1][:40]!r} has no location" for r in rows if not r[2]]
    bad += [f"{r[2][:40]!r} has no manufacturer" for r in rows if not r[1]]
    # A name this long is a row that swallowed its neighbour.
    bad += [f"suspiciously long name: {r[1][:60]!r}" for r in rows if len(r[1]) > 90]
    return bad, per


# ---------------------------------------------------------------------------
# The revision, and whether it is still in date
# ---------------------------------------------------------------------------
#
# A spreadsheet cannot say when it stops being true. The PDF can, and does, on
# every page: "Valid Thru Sept 30, 2026". Carrying that through is the whole
# reason for reading the PDF rather than a copy of it -- an audit that checked
# its manufacturers against a list which lapsed months ago looks exactly like
# one that checked them against a current list.

VALID_THRU = re.compile(r"Valid\s*Thru\s*(.+)", re.I)

#: How the cover writes its month. "Sept" is not a Python abbreviation.
_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11,
           "dec": 12}


def revision(path: str | Path) -> tuple[str, date | None]:
    """``(as printed, parsed)`` for the validity date on the cover."""
    import pymupdf

    with pymupdf.open(path) as doc:
        if not doc.page_count:
            return "", None
        found = VALID_THRU.search(doc[0].get_text("text"))
    if not found:
        return "", None
    text = " ".join(found.group(1).split())
    said = re.match(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", text)
    if not said:
        return text, None
    month = _MONTHS.get(said.group(1).lower()[:4].rstrip("."))
    month = month or _MONTHS.get(said.group(1).lower()[:3])
    if not month:
        return text, None
    try:
        return text, date(int(said.group(3)), month, int(said.group(2)))
    except ValueError:
        return text, None


def looks_like_an_aml(path: str | Path) -> bool:
    """Is this PDF a Piping AML? Cheap enough to ask before parsing."""
    import pymupdf

    try:
        with pymupdf.open(path) as doc:
            if not doc.page_count:
                return False
            return "Approved Manufacturer List" in doc[0].get_text("text")
    except Exception:                     # noqa: BLE001 - not a readable PDF
        return False


def entries(path: str | Path):
    """AML entries from a PDF, or raise ValueError saying what is wrong.

    Refusing is the point. A parse that quietly returns most of a list is the
    dangerous outcome, because every manufacturer it failed to read comes back
    "not on the approved list" -- which is indistinguishable, in the report,
    from a manufacturer that genuinely is not on it.
    """
    from .aml import AmlEntry, normalise_manufacturer, parse_limit

    rows = parse(path)
    complaints, _per = check(rows)
    if complaints:
        raise ValueError("; ".join(complaints[:4]))

    out = []
    for category, manufacturer, location, limits, _section, _changed in rows:
        size_limit, conditions = parse_limit(limits)
        out.append(AmlEntry(category=category, manufacturer=manufacturer,
                            location=location, limits_raw=limits,
                            size_limit=size_limit, conditions=conditions,
                            key=normalise_manufacturer(manufacturer)))
    return out


def expired(on: date | None, today: date | None = None) -> int | None:
    """Days past its validity date, or None while it is still in date."""
    if on is None:
        return None
    today = today or datetime.now().date()
    return (today - on).days if today > on else None


# ---------------------------------------------------------------------------
# Writing one out as a workbook
# ---------------------------------------------------------------------------
#
# The program reads the PDF directly, so this is not needed to run an audit.
# It exists because the AML is something people look things up in by hand, and
# a filterable sheet is better for that than 57 pages; and because a site that
# keeps its own annotated list needs somewhere to start from.

def _norm(text: str) -> str:
    import unicodedata

    plain = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", plain.lower()).strip()


def compare(rows, previous) -> tuple[list, list]:
    """``(added, removed)`` between a parsed list and the one in use.

    Names are keyed the way the matcher keys them, not by their printed form.
    The AML marks a flange maker with in-house forging as "(F)", and between
    two revisions that marker moved from the front of the name to the back —
    which, compared literally, reported the same company as both removed and
    added, on every flange page at once.
    """
    from .aml import normalise_manufacturer

    now = {(normalise_manufacturer(r[1]), _norm(r[2])): (r[1], r[2], r[0])
           for r in rows}
    before = {(normalise_manufacturer(m), _norm(loc)): (m, loc)
              for m, loc in previous}
    return ([now[k] for k in now.keys() - before.keys()],
            [before[k] for k in before.keys() - now.keys()])


def write_workbook(pdf: str | Path, out: str | Path, previous=None) -> Path:
    """Write the PDF's list to an .xlsx in the shape ``Aml.from_workbook`` reads."""
    import xlsxwriter

    rows = parse(pdf)
    complaints, per = check(rows)
    if complaints:
        raise ValueError("; ".join(complaints[:4]))
    said, on = revision(pdf)
    out = Path(out)

    book = xlsxwriter.Workbook(str(out))
    head = book.add_format({"bold": True, "bg_color": "#1F3864",
                            "font_color": "white", "border": 1})
    wrap = book.add_format({"text_wrap": True, "valign": "top"})

    sheet = book.add_worksheet("AllData")
    for col, title in enumerate(("Category", "Manufacturer", "Approved Location",
                                 "Specific Limits", "Section", "Changed")):
        sheet.write(0, col, title, head)
    for i, row in enumerate(rows, 1):
        for col, value in enumerate(row):
            sheet.write(i, col, value)
    sheet.set_column("A:A", 22)
    sheet.set_column("B:B", 46)
    sheet.set_column("C:C", 34)
    sheet.set_column("D:D", 40, wrap)
    sheet.set_column("E:F", 10)
    sheet.freeze_panes(1, 0)
    sheet.autofilter(0, 0, len(rows), 5)

    if previous is not None:
        added, removed = compare(rows, previous)
        changes = book.add_worksheet("Changes")
        changes.write_row(0, 0, ["Change", "Manufacturer", "Approved Location",
                                 "Category"], head)
        at = 1
        for man, loc, cat in sorted(added, key=lambda x: x[0].lower()):
            changes.write_row(at, 0, ["ADDED", man, loc, cat])
            at += 1
        for man, loc in sorted(removed, key=lambda x: x[0].lower()):
            changes.write_row(at, 0, ["REMOVED", man, loc, ""])
            at += 1
        changes.set_column("A:A", 11)
        changes.set_column("B:B", 46)
        changes.set_column("C:C", 34)
        changes.set_column("D:D", 22)
        changes.freeze_panes(1, 0)

    about = book.add_worksheet("About")
    lines = [("Source", str(pdf)), ("Revision", said or "(not stated)"),
             ("Valid thru", on.isoformat() if on else "(could not be read)"),
             ("Entries", len(rows)), ("", "")]
    lines += [(f"  {name}", n) for name, n in sorted(per.items())]
    for i, (key, value) in enumerate(lines):
        about.write_row(i, 0, [key, value])
    about.set_column("A:A", 34)
    about.set_column("B:B", 80)

    book.close()
    return out
