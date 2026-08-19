"""Recovering the weld register from an isometric's text layer.

Most weld and heat maps in the corpus were plotted from CAD, not scanned, so
their callouts are text.  That makes a second weld register available for
nothing, on jobs where it is the *only* register: Kestrel 8's daily reports leave
the weld number blank, so before this its weld map was the sole record of
which joints exist and it could only be read by a vision pass.

The results land in the same tables the vision pass writes to, under their own
source tag, so everything downstream — coverage, register reconciliation, the
NDE and welder rules — treats a map read this way exactly as it treats one
read from a scan.
"""

from __future__ import annotations

import re
from collections import Counter

import pymupdf

from ..db import Database
from ..mtrname import normalise_heat
from ..weldmap import (
    MIN_UNKNOWN_PREFIX, group_spans, is_concentrated, parse_heat_token,
)
from .dwr import known_nde_prefixes

#: Kept distinct from ``weld_map_vision`` so a job that has been through both
#: does not count its map twice, and so either can be cleared alone.
SOURCE = "weld_map_text"
HEAT_SOURCE = "heat_map_text"

#: Below this the page is a scan and there is nothing to read.
MIN_TEXT = 120

#: LINE NO. as the title blocks write it. Both site conventions appear:
#: `DTD22MP-LP-16-1A` on PLU, `16"-A1-0-PG-0417` on GL 31.
_LINE_NO = re.compile(
    r"\b(?:DTD\d*MP[-_][A-Z]{2}[-_]\d{1,2}[-_][0-9A-Z]+|"
    r"\d{1,2}\"?[-_][A-Z0-9]{2}[-_]\d[-_][A-Z]{2}[-_]\d{3,4})\b",
    re.IGNORECASE,
)


def _spans(page) -> list[tuple[float, float, float, float, str]]:
    """``(x0, y0, x1, y1, text)`` for every non-empty text line on the page."""
    out: list[tuple[float, float, float, float, str]] = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", ()):
            text = " ".join(s["text"] for s in line.get("spans", ())).strip()
            if text:
                x0, y0, x1, y1 = line["bbox"]
                out.append((x0, y0, x1, y1, " ".join(text.split())))
    return out


def read_page(page, allow_short: frozenset[str]) -> tuple[list, list[str], str]:
    """Callouts, heats and the line number from one drawing sheet."""
    spans = _spans(page)
    if sum(len(s[4]) for s in spans) < MIN_TEXT:
        return [], [], ""

    line_no = ""
    heats: list[str] = []
    remaining: list[tuple[float, float, float, float, str]] = []

    for span in spans:
        text = span[4]
        if not line_no and (m := _LINE_NO.search(text)):
            line_no = m.group(0).upper().replace('"', "")
        if heat := parse_heat_token(text):
            heats.append(heat)
            continue
        remaining.append(span)

    return group_spans(remaining, allow_short=allow_short), heats, line_no


def extract(db: Database, project_id: int) -> tuple[int, int, int]:
    """Read every weld and heat map that has a text layer.

    Returns ``(documents read, welds, heats)``.
    """
    # Two-letter prefixes are only trusted where the project's reader sheets
    # already use them - see weldmap.parse_id_token.
    allow_short = frozenset(
        p for p in known_nde_prefixes(db, project_id) if len(p) < 3)

    documents = db.q(
        """SELECT id, path, filename, segment, fingerprint FROM document
           WHERE project_id=? AND kind='weld_map' AND ext IN ('.pdf','.PDF')""",
        (project_id,),
    )

    welds: list[tuple] = []
    heats: list[tuple] = []
    seen: set[str] = set()
    read = 0
    known = known_nde_prefixes(db, project_id)

    for doc in documents:
        key = doc["fingerprint"] or f"doc:{doc['id']}"
        if key in seen:
            continue
        seen.add(key)

        try:
            with pymupdf.open(doc["path"]) as pdf:
                pages = [read_page(page, allow_short) for page in pdf]
        except Exception:                        # noqa: BLE001 - a scan or a
            continue                             # broken file reads as nothing

        # A weld map numbers its welds in one or two series. Where the
        # identifiers scatter wider, the text layer is OCR of a scan rather
        # than a plotted drawing, and every "weld" in it is invented.
        #
        # This gates the welds only. A heat map has no weld callouts at all,
        # so testing its concentration would throw away every heat on it — and
        # heats need no such test, because a heat callout has to be printed
        # with an `HT` marker in front of it, which noise does not supply.
        trust_welds = is_concentrated(c.prefix for page in pages for c in page[0])

        found = False
        for page_no, (callouts, page_heats, line_no) in enumerate(pages, start=1):
            for callout in (callouts if trust_welds else ()):
                found = True
                welds.append((
                    project_id, doc["id"], doc["segment"] or "", line_no,
                    callout.weld_id, callout.welders, _iso(callout.date),
                    callout.weld_id, page_no, SOURCE,
                ))
            for heat in page_heats:
                found = True
                heats.append((
                    project_id, doc["id"], doc["segment"] or "", line_no, line_no,
                    heat, normalise_heat(heat), "", HEAT_SOURCE,
                ))
        read += 1 if found else 0

    # A prefix nothing else on the job recognises has to earn its place by
    # repeating: a real series runs to dozens of joints, and a stray token
    # that parsed like an identifier appears once. Done across the project
    # rather than per sheet, because a series continues across sheets.
    counts = Counter(w[4].split("-")[0] for w in welds)
    welds = [w for w in welds if _believable(w[4].split("-")[0], counts, known)]

    with db.tx() as c:
        c.execute("DELETE FROM weld WHERE project_id=? AND source=?",
                  (project_id, SOURCE))
        c.execute("DELETE FROM installed_heat WHERE project_id=? AND source=?",
                  (project_id, HEAT_SOURCE))
        if welds:
            c.executemany(
                """INSERT INTO weld
                   (project_id, document_id, segment, line, weld_no, welder_root,
                    date_welded, nde_id, page_no, source)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                welds,
            )
        if heats:
            c.executemany(
                """INSERT INTO installed_heat
                   (project_id, document_id, segment, line, drawing_no, heat,
                    heat_key, note, source)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                heats,
            )
    return read, len(welds), len(heats)


#: An NDE prefix the job has never heard of is only believed if it repeats
#: *and* looks like one. Real series in this corpus are three or four letters
#: (AFB, CAFB); five is a word the drawing happened to print next to a number,
#: which is how `ELBOW 90` became weld ELBOW-090.
MAX_UNKNOWN_PREFIX_LEN = 4


def _believable(prefix: str, counts: Counter, known: set[str]) -> bool:
    if prefix in known:
        return True
    return (counts[prefix] >= MIN_UNKNOWN_PREFIX
            and len(prefix) <= MAX_UNKNOWN_PREFIX_LEN)


_DATE_ANY = re.compile(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})")


def _iso(value: str) -> str:
    from datetime import date as _date

    m = _DATE_ANY.search(value or "")
    if not m:
        return ""
    mm, dd, yy = (int(g) for g in m.groups())
    if yy < 100:
        yy += 2000
    try:
        return _date(yy, mm, dd).isoformat()
    except ValueError:
        return ""
