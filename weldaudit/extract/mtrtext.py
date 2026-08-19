"""Reading the issuing company off certificates that were never scanned.

A fifth of the mill certificates in the corpus carry a full text layer — they
were produced digitally and filed as PDFs, not photographed. Nothing has ever
looked at that text. `materials.py` takes manufacturers from the pipe export
and from filenames, and the certificate PDFs themselves are opened only by the
vision pass, which renders them to JPEG and pays a model to read characters
that are sitting in the file.

On Bluewater 14 that is 104 of the 475 certificates still without a
manufacturer. Reading them here is free, and — the part that matters more — it
is *exact*. A name taken from the text layer cannot be a misread letterhead,
so it raises none of the VIS-02 disagreements or VIS-03 approximate approvals
that a scanned name has to be hedged with.

The hard part is not the text, it is deciding which of the four or five
companies on the page is the producer. The same trap as the vision prompt: the
customer is usually printed more prominently than the letterhead, and a
`Supplier` line names whoever supplied the steel. This module refuses rather
than guesses, and leaves anything it cannot settle to the paid pass.
"""

from __future__ import annotations

import re
from typing import Iterable

from ..aml import Aml
from ..db import Database

#: Below this, page one is a scan and its text layer settles nothing.
MIN_CHARS = 400

#: A letterhead sits above the body of the form. Past this fraction of the page
#: the text is the form itself — customer blocks, tables, notes.
TOP_BAND = 0.30

#: Of the largest type in that band. A letterhead is set big; phone numbers,
#: dates and spec codes are not. Without this the search compares twenty lines
#: against 1,345 AML entries and chance does the rest — a real run matched
#: "Tel. +49 34691 40 0" to Bebitz, "14-Nov-2023" to NOV (National Oilwell
#: Varco) and "AS-ROLLED" to Ajax Rolled Rings.
PROMINENT = 0.80

#: How well a candidate line must match an AML entry to be taken as that
#: company, by ``Aml.nearest`` rather than ``Aml.match`` — the latter's
#: leading-word-prefix rule is far too generous for choosing between lines on
#: a page. Measured on the real corpus: the false matches this rejected scored
#: 19 ("MEGAPAD TAKEAWAY" against "MEGA") and 69 ("Component" against "Forged
#: Components Inc"), while every genuine letterhead scored 100.
AML_CERTAIN = 92

#: A normalised name shorter than this is a word, not a company.
MIN_NAME = 5

#: Single words too generic to identify anyone on their own, however well they
#: score against an AML entry that happens to start with them.
_GENERIC = frozenset({
    "steel", "pipe", "pipes", "tube", "tubes", "tubular", "flange", "flanges",
    "forge", "forged", "forgings", "valve", "valves", "fitting", "fittings",
    "group", "industries", "industrial", "component", "components", "metals",
    "alloy", "alloys", "products", "supply", "energy", "international",
})

#: Labels whose value is a company that did not make this item. A line holding
#: one of these is skipped whole: on these forms the label and the value are
#: usually the same text line.
_NOT_THE_MAKER = re.compile(
    r"\b(client|cliente|customer|besteller|purchaser|buyer|sold\s*to|ship\s*to|"
    r"consignee|distributor|supplier|proveedor|steel\s*supplier|steel\s*rolling|"
    r"starting\s*material|end\s*user|order(ed)?\s*by|vendor|going\s*to|"
    r"deliver(ed)?\s*to|destination|attention|attn)\b",
    re.IGNORECASE)

#: Words that make a line the title of the form rather than somebody's name.
_TITLE_WORDS = re.compile(
    r"\b(certificate|certificado|zeugnis|inspection|inspeccion|material\s*test|"
    r"test\s*report|mill\s*test|analysis|analyse|report|page|rev(ision)?|"
    r"according\s*to|acc\.?\s*to)\b",
    re.IGNORECASE)

#: Heat-treatment conditions and finishes, printed as prominently as a name on
#: some certificates. "AS-ROLLED" reached "Ajax Rolled Rings" on a real page.
_CONDITION = re.compile(
    r"\b(as[\s-]*(rolled|forged|cast|welded|drawn)|normali[sz]ed|annealed|"
    r"quenched|tempered|galvani[sz]ed|seamless|hot[\s-]*finished)\b",
    re.IGNORECASE)

def prominent_lines(lines: Iterable[tuple[str, float, float]],
                    height: float) -> list[str]:
    """The big text at the head of the page, in reading order.

    Takes ``(text, top, size)`` rather than a PDF page, because the same
    judgement has to be made about words a text layer supplies and words an
    OCR engine reads off a scan. For a text layer the size is the font size;
    for OCR it is how tall the word is on the page. Either way a letterhead is
    set large and a phone number is not, which is the only distinction that
    reliably separates a company name from the thirty other strings up there.
    """
    found = [(size, top, " ".join(str(text).split()))
             for text, top, size in lines
             if str(text).strip() and top <= height * TOP_BAND]
    if not found:
        return []
    biggest = max(size for size, _t, _x in found)
    return [text for size, _t, text in sorted(found, key=lambda f: f[1])
            if size >= biggest * PROMINENT]


def _lines_near_the_top(page) -> list[str]:
    """``prominent_lines`` for a PDF page that carries its own text."""
    def spans_of(page):
        for block in page.get_text("dict").get("blocks", []):
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                yield ("".join(s.get("text", "") for s in spans),
                       line.get("bbox", (0, 0, 0, 0))[1],
                       max((s.get("size", 0) for s in spans), default=0))

    return prominent_lines(spans_of(page), page.rect.height or 1)


def _plausible_company(line: str) -> bool:
    if len(line) < 4 or len(line) > 90:
        return False
    if (_NOT_THE_MAKER.search(line) or _TITLE_WORDS.search(line)
            or _CONDITION.search(line)):
        return False
    letters = [w for w in re.findall(r"[A-Za-z][\w&.-]*", line) if len(w) > 2]
    return len(letters) >= 1


def _identifying(line: str) -> bool:
    """Whether a line says enough to name a company by itself."""
    from ..aml import normalise_manufacturer

    key = normalise_manufacturer(line)
    if len(key) < MIN_NAME:
        return False
    words = [w for w in key.split() if w not in _GENERIC]
    return bool(words)


def letterhead_from(candidates: list[str], aml: Aml | None) -> tuple[str, str] | None:
    """``(AML name, the line it was read from)`` from already-chosen lines."""
    candidates = [ln for ln in candidates if _plausible_company(ln)]
    if not candidates or aml is None:
        return None
    recognised: list[tuple[int, str, str]] = []
    for line in candidates:
        if not _identifying(line):
            continue
        score, entry = aml.nearest(line)
        if entry and score >= AML_CERTAIN:
            recognised.append((score, line, entry))
    if not recognised:
        return None
    # One company, however many lines mention it, is an answer. Two different
    # ones near the top is a page this cannot read.
    if len({entry for _s, _l, entry in recognised}) > 1:
        return None
    best = max(recognised)
    return best[2], best[1]


def letterhead_of(page, aml: Aml | None) -> tuple[str, str] | None:
    """``(AML name, the line it was read from)``, or None to leave the page.

    The only accepted evidence is that the AML already knows the name. That is
    self-validating in a way nothing else here is: a string the approved list
    recognises is a real company, and it is the exact question being asked
    downstream anyway.

    A structural rule — "the one line above the form carrying a legal form" —
    was tried and removed. Not every text layer is exact: a good part of this
    corpus is scans that someone OCR'd before filing, and on those the rule
    produced ``bolfex mfg.co.lo.``, ``BALOR. CORPORATION`` and
    ``~~!t,~ ~~x~'?i1LP.`` as manufacturer names. A recognisable company or
    nothing is the right trade here, because the alternative to a name is a
    paid reading, not a gap.
    """
    return letterhead_from(_lines_near_the_top(page), aml)


def extract_letterheads(db: Database, project_id: int,
                        aml: Aml | None = None) -> int:
    """Fill in manufacturers readable from certificate text layers.

    Only touches certificates that have no manufacturer from any other source,
    so an export or a filename always wins — those did not come through a
    parser guessing which block is the letterhead.
    """
    import pymupdf

    rows = db.q(
        """SELECT m.id, m.heat, d.path
           FROM material m JOIN document d ON d.id = m.document_id
           WHERE m.project_id=? AND m.source='mtr_file'
             AND IFNULL(m.manufacturer,'') = ''""",
        (project_id,),
    )

    found = 0
    for r in rows:
        try:
            with pymupdf.open(r["path"]) as doc:
                if not doc.page_count:
                    continue
                page = doc[0]
                if len(page.get_text("text").strip()) < MIN_CHARS:
                    continue          # a scan; the paid pass will read it
                decided = letterhead_of(page, aml)
        except Exception:             # noqa: BLE001 - an unreadable file is a skip
            continue
        if not decided:
            continue

        company, as_printed = decided
        with db.tx() as c:
            # The AML's spelling in `manufacturer`, because that is what the
            # check consumes; the page's own words in `issuing_company`, so a
            # finding can quote the certificate rather than the list.
            c.execute(
                """UPDATE material
                   SET manufacturer=?, issuing_company=?, confidence='text',
                       evidence=?
                   WHERE id=?""",
                (company, as_printed, f"text layer reads '{as_printed}'", r["id"]),
            )
        found += 1
    return found
