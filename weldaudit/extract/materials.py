"""Material certificates, pipe/heat exports, and the approved materials list.

Three sources feed the material chain, cheapest first:

``pipes_csv``   the field system's pipe export - heat, manufacturer, grade,
                diameter and wall, already structured.  This is the only source
                that reliably names a *manufacturer*, so it carries the AML
                check on its own.
``mtr_file``    every material certificate on disk, identified from its
                filename.  Establishes which heats have paperwork.
``aml``         the approved materials list workbook.

An MTR's manufacturer usually cannot be read from the filename, and the
certificate itself is often a scan - and even when it is not, the company on
the letterhead is frequently a distributor or machine shop rather than the
mill.  So the AML check runs on heats whose manufacturer is known, and heats
where it is not are reported as such rather than assumed good.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

from ..aml import Aml, normalise_manufacturer, parse_nps
from ..db import Database
from ..mtrname import normalise_heat
from ..mtrname import parse as parse_mtr_name

#: Folder names that hold material certificates but are not called "MTR".
MATERIAL_FOLDER = re.compile(r"\bmtrs?\b|fittings|valve documents|(^|[\\/])pipe( lp)?([\\/]|$)",
                             re.IGNORECASE)

#: Folders that hold certificates alongside everything else.  GL 31 keeps 114
#: pipe and fitting certificates under HYDRO TEST DOCUMENTS, and until the
#: heat maps were read nothing asked whether those heats were certified — so
#: the gap sat there invisibly.  A document here is only taken when its
#: filename parses as a certificate, which is a heuristic; getting it wrong
#: adds a material row rather than losing a document, so it is affordable
#: here in a way it was not in the taxonomy.
MIXED_FOLDER = re.compile(r"hydro", re.IGNORECASE)


#: Shipping paperwork and placeholders, filed in the same folders as the
#: certificates. A material folder is otherwise taken at its word, so these
#: became `material` rows with no manufacturer, which is exactly the set the
#: vision pass targets — 136 bills of lading and 12 copies of a compliments
#: slip were read at five model calls each, and every one came back "not a
#: certificate". They are a quarter of the pages that pass paid for.
#:
#: Matched as a whole word so HALDEN, a flange maker, is not caught by it.
NOT_A_CERTIFICATE = re.compile(
    r"\bbol\b|\bbills?\s+of\s+lading\b|\bpacking\s+(list|slip)\b|"
    r"\bdelivery\s+(ticket|note)\b|\bsee\s+seg\b",
    re.IGNORECASE)


def _is_certificate(path: str, filename: str, installed: set[str]) -> bool:
    """Whether a document is a mill certificate, wherever it is filed.

    In a mixed folder the filename alone is not enough — `454M06.pdf` and
    `DI31-WELD LOG BLOWDOWN PDF.pdf` are equally terse — so the heat has to be
    corroborated: either the name also describes a material item, or the heat
    is one the maps say was actually installed on this job. That keeps the
    heuristic from inventing material rows for whatever else is in the folder,
    because it can only ever resolve a heat the audit was already asking about.
    """
    if NOT_A_CERTIFICATE.search(filename):
        return False
    parent = str(Path(path).parent)
    if MATERIAL_FOLDER.search(parent):
        return True
    if not MIXED_FOLDER.search(parent):
        return False
    ident = parse_mtr_name(filename)
    if not ident.heat or ident.confidence not in ("high", "medium"):
        return False
    return bool(ident.categories) or normalise_heat(ident.heat) in installed


# ---------------------------------------------------------------------------
# Approved materials list
# ---------------------------------------------------------------------------


#: What an approved list can arrive as. The PDF is the document the operator
#: actually issues; a workbook is somebody's transcription of one.
AML_PATTERNS = ("AML*.xlsx", "*Approved Material*.xlsx", "AML*.xlsm",
                "*AML*.pdf", "*Approved Manufacturer*.pdf")


def find_aml_workbook(root: str | Path) -> Path | None:
    """The approved list governing this job: nearest folder, newest revision.

    The list is maintained once and shared across jobs, so it usually sits
    above any individual job folder — hence the walk up.

    Where a folder holds more than one, the newest revision wins rather than
    the first name alphabetically. That used to decide it, and the cost was
    real: a job folder held a spreadsheet transcribed from a list that had
    expired in March and the current PDF sitting beside it, and every audit
    quietly used the expired one. Only the PDF states a validity date, so only
    the PDF can be ranked by it; a workbook is undated and is used when there
    is nothing dated to prefer.
    """
    root = Path(root).resolve()
    for folder in [root, *root.parents]:
        hits: list[Path] = []
        for pattern in AML_PATTERNS:
            hits.extend(folder.glob(pattern))
        if hits:
            return max(sorted(set(hits)), key=_revision_rank)
        # Do not wander above the user's profile.
        if folder == Path.home():
            break
    return None


def _revision_rank(path: Path) -> tuple[int, str]:
    """Sort key: dated revisions above undated ones, newest first."""
    if path.suffix.lower() == ".pdf":
        from ..amlpdf import revision

        try:
            _said, on = revision(path)
        except Exception:                  # noqa: BLE001 - unreadable PDF
            on = None
        if on is not None:
            return (2, on.isoformat())
        return (0, path.name)              # a PDF that states no date
    return (1, path.name)                  # a workbook, undated by nature


def load_aml(db: Database, project_id: int, root: str | Path,
             path: str | Path | None = None) -> tuple[Aml | None, Path | None]:
    """Load the AML into the database and return it ready to query."""
    book = Path(path) if path else find_aml_workbook(root)
    with db.tx() as c:
        c.execute("DELETE FROM aml_entry WHERE project_id=?", (project_id,))
    if not book or not book.exists():
        return None, None

    revision, valid_thru = "", None
    if book.suffix.lower() == ".pdf":
        from ..amlpdf import entries as pdf_entries
        from ..amlpdf import revision as pdf_revision

        # A PDF that cannot be read cleanly is not fallen back on quietly. A
        # half-read approved list reports every manufacturer it missed as "not
        # on the AML", which reads in the report exactly like a real finding.
        aml = Aml(pdf_entries(book))
        revision, on = pdf_revision(book)
        valid_thru = on.isoformat() if on else None
    else:
        aml = Aml.from_workbook(book)

    # Recorded so the report can say which list cleared the material, and so
    # AML-01 can notice the list had run out.
    with db.tx() as c:
        c.execute(
            """INSERT INTO aml_source(project_id, path, kind, revision,
                                      valid_thru, entries)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(project_id) DO UPDATE SET
                 path=excluded.path, kind=excluded.kind,
                 revision=excluded.revision, valid_thru=excluded.valid_thru,
                 entries=excluded.entries""",
            (project_id, str(book),
             "pdf" if book.suffix.lower() == ".pdf" else "workbook",
             revision, valid_thru, len(aml.entries)),
        )
    with db.tx() as c:
        c.executemany(
            """INSERT INTO aml_entry
               (project_id, category, manufacturer, location, limits_raw,
                min_nps, max_nps, conditions, norm_name)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            [
                (
                    project_id, e.category, e.manufacturer, e.location, e.limits_raw,
                    e.size_limit.min_nps if e.size_limit else None,
                    e.size_limit.max_nps if e.size_limit else None,
                    e.conditions, e.key,
                )
                for e in aml.entries
            ],
        )
    return aml, book


# ---------------------------------------------------------------------------
# Material certificates on disk
# ---------------------------------------------------------------------------


def extract_certificates(db: Database, project_id: int) -> int:
    """One ``material`` row per certificate found on disk."""
    docs = db.q(
        """SELECT id, path, filename, segment, fingerprint FROM document
           WHERE project_id=? AND ext IN ('.pdf','.PDF')""",
        (project_id,),
    )

    with db.tx() as c:
        c.execute("DELETE FROM material WHERE project_id=? AND source='mtr_file'", (project_id,))

    # Heats the drawings say went into the line, for corroborating a
    # certificate filed outside the material folders.
    installed = {
        r["heat_key"] for r in db.q(
            "SELECT DISTINCT heat_key FROM installed_heat "
            "WHERE project_id=? AND heat_key<>''", (project_id,))
    }

    seen_fp: set[str] = set()
    rows: list[tuple] = []
    for d in docs:
        if not _is_certificate(d["path"], d["filename"], installed):
            continue
        # Certificates get filed into several books just as reader sheets do.
        fp = d["fingerprint"] or d["path"]
        if fp in seen_fp:
            continue
        seen_fp.add(fp)

        ident = parse_mtr_name(d["filename"])
        # One mill certificate routinely covers a whole rolling, and the
        # filename lists every heat in it. A row each, or the rest of the
        # rolling reads as uncertified material.
        for heat in ident.heats or [ident.heat]:
            rows.append(
                (
                    project_id, d["id"], d["segment"], heat,
                    normalise_heat(heat), ident.material,
                    ident.nps, "", ident.spec, ident.schedule, ident.description,
                    "; ".join(ident.categories), "mtr_file", ident.confidence,
                )
            )

    if rows:
        with db.tx() as c:
            c.executemany(
                """INSERT INTO material
                   (project_id, document_id, segment, heat, heat_key, grade,
                    nps, wall, spec, schedule, description, categories,
                    source, confidence)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
    return len(rows)


# ---------------------------------------------------------------------------
# Pipe / heat exports
# ---------------------------------------------------------------------------

_PIPE_COLUMNS = {
    "heat": ("heat",),
    "manufacturer": ("manufacturer",),
    "grade": ("grade",),
    "nps": ("diameter",),
    "wall": ("wall thickness",),
    "line": ("line",),
    "description": ("description", "type"),
    "status": ("status",),
    "pipe_no": ("pipe number",),
}


def _norm(h: str) -> str:
    return re.sub(r"\s+", " ", (h or "").strip().lower())


def parse_pipes_csv(path: str) -> list[dict]:
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            return []
        available = {_norm(f): f for f in reader.fieldnames if f}
        colmap: dict[str, str] = {}
        for field, variants in _PIPE_COLUMNS.items():
            for v in variants:
                if v in available:
                    colmap[field] = available[v]
                    break
        if "heat" not in colmap:
            return []

        out: list[dict] = []
        for raw in reader:
            rec = {f: (raw.get(col) or "").strip() for f, col in colmap.items()}
            if not rec.get("heat"):
                continue
            out.append(rec)
        return out


def extract_pipes(db: Database, project_id: int) -> tuple[int, int]:
    """Load pipe exports.  Returns ``(files, distinct heats)``."""
    docs = db.q(
        """SELECT id, path, segment, filename FROM document
           WHERE project_id=? AND ext='.csv' AND LOWER(filename) LIKE 'pipes_export%'""",
        (project_id,),
    )
    with db.tx() as c:
        c.execute("DELETE FROM material WHERE project_id=? AND source='pipes_csv'", (project_id,))

    # One row per heat, not per joint - a heat is certified once.
    by_heat: dict[str, tuple] = {}
    ok_files = 0
    for d in docs:
        try:
            records = parse_pipes_csv(d["path"])
        except OSError:
            continue
        if not records:
            continue
        ok_files += 1
        for r in records:
            key = normalise_heat(r["heat"])
            if not key or key in by_heat:
                continue
            # Pipe exports usually sit loose at the project root rather than
            # inside a segment book; name them by the line they cover.
            segment = d["segment"]
            if segment in ("", "(unassigned)", None):
                segment = r.get("line") or Path(d["filename"]).stem[:48]
            by_heat[key] = (
                project_id, d["id"], segment, r["heat"], key,
                r.get("manufacturer", ""), r.get("grade", ""),
                parse_nps(r.get("nps")), r.get("wall", ""), "", "",
                (r.get("description") or "Pipe").strip(), "1.0 Pipe",
                r.get("line", ""), "pipes_csv", "high",
            )

    if by_heat:
        with db.tx() as c:
            c.executemany(
                """INSERT INTO material
                   (project_id, document_id, segment, heat, heat_key,
                    manufacturer, grade, nps, wall, spec, schedule,
                    description, categories, line, source, confidence)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                list(by_heat.values()),
            )
    return ok_files, len(by_heat)
