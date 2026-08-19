"""Reader sheets: which NDE shots have paperwork actually on file.

The filename alone is strong evidence.  Reader sheets on these jobs are filed
as ``<size> <line> <date> <ids>.pdf`` - for example
``20IN LP 09.09.25 GFB-037-040.pdf`` - so the set of shots that exist can be
established for a whole project without opening a single PDF.

That matters because roughly half these sheets are image-only scans.  Reading
filenames first means the expensive vision pass only ever has to confirm
details (accept/reject, welder stencil) for shots we already know are there,
and chase the handful that cannot be resolved any other way.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from ..db import Database
from ..ids import NdeId, parse_ids
from ..readersheet import (
    parse_page, stated_pagination, stated_ticket, stated_weld_count,
)

#: Dates as written in reader-sheet filenames: 09.09.25, 9.30.25, 10.7.25.
#: Note the corpus also contains typos such as "0.16.25" - month 0 - which we
#: deliberately let through as an unparsed date rather than guessing.
_DATE = re.compile(r"\b(\d{1,2})[.\-_](\d{1,2})[.\-_](\d{2,4})\b")

#: Filenames that are not reader sheets even though they sit in the folder.
_NOT_A_SHEET = re.compile(r"^\s*(info|index|cover|toc|notes?)\b", re.IGNORECASE)

#: Trailing "(1)", "(2)" that Windows adds to duplicate downloads.
_DUP = re.compile(r"\s*\((\d+)\)\s*$")


def sheet_date(filename: str) -> str | None:
    """Best-effort date from the filename, ISO formatted."""
    m = _DATE.search(filename)
    if not m:
        return None
    mm, dd, yy = (int(g) for g in m.groups())
    if yy < 100:
        yy += 2000
    if not (1 <= mm <= 12 and 1 <= dd <= 31):
        return None  # e.g. the "0.16.25" typos - flagged elsewhere, not guessed
    try:
        return date(yy, mm, dd).isoformat()
    except ValueError:
        return None


def canonical_name(filename: str) -> str:
    """Filename with any Windows ``(1)`` copy marker removed.

    A trailing ``(1)`` is *not* on its own grounds to ignore a sheet - the
    corpus contains sheets that only exist in their ``(1)`` form, and dropping
    them invents missing shots.  Real duplicates collapse on content
    fingerprint instead; this is only used to recognise two paths as the same
    logical sheet when reporting.
    """
    return _DUP.sub("", Path(filename).stem).strip()


def ids_from_filename(filename: str) -> list[NdeId]:
    stem = Path(filename).stem
    if _NOT_A_SHEET.match(stem):
        return []
    stem = _DUP.sub("", stem)
    # Strip the leading size/line/date prefix so it cannot be mistaken for an id.
    stem = _DATE.sub(" ", stem)
    stem = re.sub(r"^\s*\d{1,2}\s*(in|\")\s*", " ", stem, flags=re.IGNORECASE)
    return parse_ids(stem)


def extract(db: Database, project_id: int) -> int:
    """Populate ``nde_shot`` from every reader-sheet filename in the project.

    Sheets filed into several segment books collapse to one logical shot.  The
    copy count and the list of segments the sheet appears under are kept, since
    "this sheet is filed in five books" is itself useful to an auditor.
    """
    docs = db.q(
        """SELECT id, path, filename, segment, fingerprint FROM document
           WHERE project_id=? AND kind='nde_reader_sheet' AND ext='.pdf'
           ORDER BY LENGTH(path)""",
        (project_id,),
    )

    with db.tx() as c:
        c.execute("DELETE FROM nde_shot WHERE project_id=? AND evidence='filename'", (project_id,))

    # Collapse physical copies: one entry per content fingerprint.
    groups: dict[str, dict] = {}
    for d in docs:
        fp = d["fingerprint"] or f"path:{d['path']}"
        g = groups.get(fp)
        if g is None:
            groups[fp] = {
                "doc_id": d["id"],           # shallowest path wins as canonical
                "filename": d["filename"],
                "segments": {d["segment"]},
                "copies": 1,
            }
        else:
            g["segments"].add(d["segment"])
            g["copies"] += 1

    rows: list[tuple] = []
    for fp, g in groups.items():
        when = sheet_date(g["filename"])
        segments = sorted(s for s in g["segments"] if s)
        for nid in ids_from_filename(g["filename"]):
            rows.append(
                (
                    project_id, g["doc_id"], fp, g["copies"], "; ".join(segments),
                    segments[0] if segments else "", str(nid),
                    nid.prefix, nid.number, nid.suffix, when,
                    _technique_hint(nid.prefix), "filename", 0.9,
                )
            )

    if rows:
        with db.tx() as c:
            c.executemany(
                """INSERT INTO nde_shot
                   (project_id, document_id, fingerprint, copies, segments,
                    segment, nde_id, prefix, number, suffix, sheet_date,
                    technique, evidence, confidence)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
    return len(rows)


def extract_text(db: Database, project_id: int) -> tuple[int, int]:
    """Read shots out of the sheets' own text layer.

    Necessary because the filename convention is not universal. Kestrel 8 names
    its sheets for the day rather than the welds — `DTD22 NDE 5.28.25 FG
    SEG.A RT RIG.A.pdf` — so the filename pass finds nothing in any of its
    sixty-five, and every NDE rule on that job stays dark.

    The sheets themselves are generated rather than scanned, so their text
    layer carries more than the Bluewater filenames do: the result, the welder
    stencils, the technician and the procedure as well as the shot number.
    Where the filename already established a shot, this fills in the rest of
    it rather than adding a second row.

    Returns ``(shots recorded, sheets read)``.
    """
    import pymupdf

    docs = db.q(
        """SELECT id, path, filename, segment, fingerprint FROM document
           WHERE project_id=? AND kind='nde_reader_sheet' AND ext IN ('.pdf','.PDF')
           ORDER BY LENGTH(path)""",
        (project_id,),
    )

    with db.tx() as c:
        c.execute("DELETE FROM nde_shot WHERE project_id=? AND evidence='text'",
                  (project_id,))
        c.execute("DELETE FROM reader_sheet WHERE project_id=? AND evidence='text'",
                  (project_id,))

    seen: set[str] = set()
    shots = 0
    sheets = 0
    for d in docs:
        fp = d["fingerprint"] or f"path:{d['path']}"
        if fp in seen:
            continue
        seen.add(fp)
        try:
            with pymupdf.open(d["path"]) as pdf:
                parsed = [(parse_page(page.get_text("words")), page.get_text())
                          for page in pdf]
        except Exception:                     # noqa: BLE001 - an unreadable
            continue                          # sheet is the vision pass's job

        # Recorded before the rows are, and independently of them: the sheets
        # that state a count are the Precision Group ones, whose tables are
        # refused. Skipping a refused page here would lose the count on
        # exactly the sheets where it is the only thing we can read.
        for page_no, (page, text) in enumerate(parsed, start=1):
            count = stated_weld_count(text)
            pagination = stated_pagination(text)
            # The positional read is the good one — it finds the ticket on 314
            # pages against the flowed-text regex's 38, because the two are
            # split into different text blocks on most sheets. The regex still
            # earns its place on the Precision Group form, whose header this
            # module declines to parse but which prints a REF# instead.
            ticket = page.ticket.strip() or stated_ticket(text)
            if count is None and pagination is None and not ticket:
                continue
            _record_sheet(db, project_id, d, fp, page_no, count, ticket,
                          _iso_date(page.sheet_date), pagination)

        pages = [p for p, _text in parsed if p.is_report]
        if not pages:
            continue
        sheets += 1

        for page_no, sheet in enumerate(pages, start=1):
            when = _iso_date(sheet.sheet_date) or sheet_date(d["filename"])
            for row in sheet.rows:
                nid = parse_ids(row.nde_id)
                if not nid:
                    continue
                shots += _record(db, project_id, d, fp, nid[0], row, sheet,
                                 when, page_no)
    return shots, sheets


def _record_sheet(db: Database, project_id: int, doc, fingerprint: str,
                  page_no: int, weld_count: int | None, ticket: str,
                  when: str | None, pagination: tuple[int, int] | None) -> None:
    """One report page's own statement about itself.

    The date is kept here rather than looked up through the shots later: a
    bundle is one document with one fingerprint and sixteen days in it, so a
    date joined at document level belongs to every report in the file at once.
    """
    stated_page, stated_pages = pagination or (None, None)
    with db.tx() as c:
        c.execute(
            """INSERT INTO reader_sheet
               (project_id, document_id, fingerprint, filename, segment,
                page_no, weld_count, ticket, sheet_date, stated_page,
                stated_pages, evidence)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,'text')""",
            (project_id, doc["id"], fingerprint, doc["filename"],
             doc["segment"] or "", page_no, weld_count, ticket, when,
             stated_page, stated_pages),
        )


def _record(db: Database, project_id: int, doc, fingerprint: str, nid: NdeId,
            row, sheet, when: str | None, page_no: int) -> int:
    """Write one shot, filling in a filename row rather than shadowing it."""
    existing = db.one(
        """SELECT id FROM nde_shot
           WHERE project_id=? AND nde_id=? AND fingerprint=?""",
        (project_id, str(nid), fingerprint),
    )
    values = {
        "result": row.result,
        "welder": row.welders,
        "pipe_size": row.diameter,
        "wall_thk": row.wall,
        "technician": sheet.technician,
        "technique": sheet.procedure or _technique_hint(nid.prefix) or "",
        "sheet_date": when,
        "page_no": page_no,
        "evidence": "text",
        "confidence": 1.0 if row.result else 0.8,
    }
    with db.tx() as c:
        if existing:
            assignments = ", ".join(f"{k}=?" for k in values)
            c.execute(f"UPDATE nde_shot SET {assignments} WHERE id=?",
                      (*values.values(), existing["id"]))
        else:
            c.execute(
                """INSERT INTO nde_shot
                   (project_id, document_id, fingerprint, copies, segments,
                    segment, nde_id, prefix, number, suffix, sheet_date,
                    technique, result, welder, pipe_size, wall_thk, technician,
                    page_no, evidence, confidence)
                   VALUES (?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (project_id, doc["id"], fingerprint, doc["segment"] or "",
                 doc["segment"] or "", str(nid), nid.prefix, nid.number,
                 nid.suffix, when, values["technique"], values["result"],
                 values["welder"], values["pipe_size"], values["wall_thk"],
                 values["technician"], page_no, "text", values["confidence"]),
            )
    return 1


def _iso_date(text: str | None) -> str | None:
    """`05/28/2025` from the sheet's own Date field."""
    from datetime import datetime

    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y"):
        try:
            return datetime.strptime((text or "").strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


#: The last letters of an NDE prefix encode the method on these jobs.
_TECHNIQUE = {
    "XR": "RT",   # radiography
    "TI": "VT",   # tie-in visual
    "FB": "VT",   # fabrication visual
    "PT": "PT",   # dye penetrant
    "MT": "MT",   # magnetic particle
    "UT": "UT",   # ultrasonic
    "BR": "RT",
}


def _technique_hint(prefix: str) -> str | None:
    for suffix, method in _TECHNIQUE.items():
        if prefix.upper().endswith(suffix):
            return method
    return None


def duplicates(db: Database, project_id: int) -> list[dict]:
    """Shots claimed by more than one reader sheet - possible double filing."""
    rows = db.q(
        """SELECT s.nde_id, s.segment, COUNT(DISTINCT s.document_id) n,
                  GROUP_CONCAT(DISTINCT d.filename) files
           FROM nde_shot s JOIN document d ON d.id = s.document_id
           WHERE s.project_id=? AND s.evidence='filename'
           GROUP BY s.nde_id, s.segment HAVING n > 1""",
        (project_id,),
    )
    return [dict(r) for r in rows]
