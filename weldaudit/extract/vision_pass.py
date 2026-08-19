"""Choosing which scanned pages are worth reading, and folding the results back in.

The vision pass is targeted by findings rather than run over the corpus.  Two
target sets earn their cost:

``mtr``
    Certificates for heats that have no machine-readable manufacturer — the
    exact set MTR-08 reports.  Reading them lets the AML rules run on material
    that currently cannot be checked at all.

``reader_sheet``
    Sheets whose contents change a verdict: ones behind a weld the log calls
    rejected, ones covering a shot the weld map cites, then the rest.  Reading
    a sheet turns "the paperwork exists" into "the paperwork says accepted, by
    this welder, shot by this technician".

Both are ordered so that a ``--limit`` spends the budget on the pages that
matter most.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ..aml import parse_nps
from ..db import Database
from ..ids import parse_ids, parse_one
from ..mtrname import normalise_heat, same_heat_differently_read
from ..readersheet import is_precision_group
from ..vision import (
    Estimate, VisionReader, _same_company, is_decisive, page_count,
)
from .dwr import known_nde_prefixes
from .readersheets import _iso_date, sheet_date as filename_date


#: A material certificate states its heat and its issuer on the first page or
#: two; the rest is chemistry and mechanical tables. Reading a 17-page valve
#: dossier end to end costs 17x a flange cert for the same two fields.
MAX_PAGES = {"mtr": 3, "reader_sheet": 0}      # 0 = no cap


@dataclass
class Target:
    document_id: int
    path: str
    filename: str
    fingerprint: str
    pages: int
    reason: str
    segment: str = ""


@dataclass
class PassResult:
    kind: str
    documents: int = 0
    pages_read: int = 0
    pages_cached: int = 0
    updated: int = 0
    #: Fields a human must settle: close-ups of one box that could not be
    #: reconciled, on a field something downstream depends on.
    conflicts: int = 0
    failures: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Target selection
# ---------------------------------------------------------------------------


def mtr_targets(db: Database, project_id: int, limit: int | None = None) -> list[Target]:
    """Certificates whose manufacturer nothing else in the project supplies.

    Ordered by what reading them would actually settle: material installed in
    the line first (an unapproved mill there is a real non-conformance),
    then certificates that cannot even be tied to a heat, then the rest.

    **Installed means any register says so**, not just the weld log. Asking
    only ``weld.heat_us``/``heat_ds`` left that first rank empty on two of the
    three jobs — neither PLU's nor Bluewater's weld register records a heat at all
    — so 545 Bluewater certificates came out in no useful order and a ``--limit``
    would have bought stud-bolt and valve dossiers ahead of the pipe in the
    ground. The heat maps and the as-builts between them place 593 heats on
    Bluewater, 98 of which have a certificate here to read.
    """
    from ..rules.materials import _welded_heats

    installed = _welded_heats(db, project_id)
    #: How each register phrases its claim, weakest evidence last.
    _EVIDENCE = {"welds": "welded into the line",
                 "as-built": "on the as-built",
                 "heat map": "shown on the heat map"}

    rows = db.q(
        """SELECT m.document_id, m.heat_key, m.heat, m.segment, d.path, d.filename,
                  d.fingerprint
           FROM material m JOIN document d ON d.id = m.document_id
           WHERE m.project_id=? AND m.source='mtr_file'
             AND NOT EXISTS (
               SELECT 1 FROM material k
               WHERE k.project_id = m.project_id AND k.heat_key <> ''
                 AND k.heat_key = m.heat_key AND IFNULL(k.manufacturer,'') <> ''
             )""",
        (project_id,),
    )

    cap = MAX_PAGES["mtr"]
    seen: set[str] = set()
    scored: list[tuple[int, Target]] = []
    for r in rows:
        fp = r["fingerprint"] or str(r["document_id"])
        if fp in seen:
            continue
        seen.add(fp)
        if r["heat_key"] and r["heat_key"] in installed:
            where = _EVIDENCE.get(installed[r["heat_key"]].get("where"), "in the line")
            rank, reason = 0, f"heat {r['heat']} is {where}, mill unknown"
        elif not r["heat_key"]:
            rank, reason = 1, "no heat number readable from the filename"
        else:
            rank, reason = 2, f"heat {r['heat']} has no known manufacturer"
        pages = page_count(r["path"])
        scored.append(
            (rank, Target(r["document_id"], r["path"], r["filename"], fp,
                          min(pages, cap) if cap else pages, reason, r["segment"] or ""))
        )

    # Within a rank, cheapest first, so a --limit buys the most coverage.
    scored.sort(key=lambda t: (t[0], t[1].pages, t[1].filename))
    out = [t for _rank, t in scored]
    return out[:limit] if limit else out


def _pages_and_form(path: str) -> tuple[int, bool]:
    """``(page count, is it a Precision Group form)`` from a single open.

    Both answers come off the same handle because the caller needs them for
    every reader sheet in the project, and opening each PDF twice to ask two
    questions of it doubles the cost of building a target list.
    """
    import pymupdf

    try:
        with pymupdf.open(path) as doc:
            if doc.page_count == 0:
                return 0, False
            return doc.page_count, is_precision_group(doc[0].get_text("words"))
    except Exception:
        return 0, False


def reader_sheet_targets(db: Database, project_id: int,
                         limit: int | None = None) -> list[Target]:
    """Reader sheets, most decision-changing first.

    A sheet nothing could read comes first.  Everything else here reasons about
    what a sheet's shots are worth, but a scan yields no shots at all, so an
    earlier version of this function - which selected ``FROM nde_shot`` -
    could not see one.  The pages the vision pass exists for were the only ones
    it could never be pointed at.  Thirty sheets across the corpus are in that
    state, eleven of them PLU's, and the tool currently knows nothing whatever
    about them: not a weld, not a date, not a technician.

    After those, sheets behind a shot the weld log calls rejected, where
    reading the page can turn "a sheet exists" into an open, unresolved reject.
    """
    # Shots the weld map says were rejected, or that carry a defect.
    contested = {
        r["nde_id"]
        for r in db.q(
            """SELECT DISTINCT nde_id FROM weld
               WHERE project_id=? AND nde_id<>''
                 AND (nde_status LIKE '%eject%' OR nde_status LIKE '%ail%'
                      OR IFNULL(defect,'') <> '' OR repair_nde_id <> '')""",
            (project_id,),
        )
    }
    cited = {
        r["nde_id"]
        for r in db.q(
            "SELECT DISTINCT nde_id FROM weld WHERE project_id=? AND nde_id<>''",
            (project_id,),
        )
    }

    # Every reader sheet, one row per distinct content. The shots are attached
    # to whichever filing copy the extractor kept, so they are gathered by
    # fingerprint rather than by document: judging a copy on its own document_id
    # would call ten of the eleven copies of a well-read sheet "unreadable".
    ids_for: dict[str, set[str]] = defaultdict(set)
    for r in db.q(
        """SELECT IFNULL(s.fingerprint, CAST(s.document_id AS TEXT)) fp, s.nde_id
           FROM nde_shot s WHERE s.project_id=?""",
        (project_id,),
    ):
        ids_for[r["fp"]].add(r["nde_id"])

    rows = db.q(
        """SELECT MIN(id) id, path, filename, segment, fingerprint
           FROM document
           WHERE project_id=? AND kind='nde_reader_sheet' AND ext='.pdf'
           GROUP BY IFNULL(fingerprint, id)""",
        (project_id,),
    )

    scored: list[tuple[int, Target]] = []
    for r in rows:
        fp = r["fingerprint"] or str(r["id"])
        ids = ids_for.get(fp) or ids_for.get(str(r["id"])) or set()
        pages, precision = _pages_and_form(r["path"])
        if precision:
            # Having a filename full of shot numbers is not the same as being
            # readable. These pages carry an OCR layer that drops rejects.
            rank, reason = 0, "Precision Group form — its OCR layer drops results"
        elif not ids:
            rank, reason = 0, "no shot readable from the filename or the text layer"
        elif ids & contested:
            rank, reason = 1, "covers a shot the weld log records as rejected"
        elif ids & cited:
            rank, reason = 2, "covers a shot the weld map cites"
        else:
            rank, reason = 3, "not referenced by any weld map"
        scored.append(
            (rank, Target(r["id"], r["path"], r["filename"], fp,
                          pages, reason, r["segment"] or ""))
        )

    scored.sort(key=lambda t: (t[0], t[1].filename))
    out = [t for _rank, t in scored]
    return out[:limit] if limit else out


def welder_cert_targets(db: Database, project_id: int,
                        limit: int | None = None) -> list[Target]:
    """Qualification records, busiest welders first.

    A certificate's filename gives at most the stencil and the procedure; what
    the ticket actually covers — process, position, diameter — is only inside.
    Reading the certificate of someone who laid 500 passes settles far more
    than one who laid two, so passes are ordered by how much of the job each
    welder actually welded.
    """
    passes = {
        r["stencil"]: r["n"]
        for r in db.q(
            "SELECT stencil, COUNT(*) n FROM welder_pass WHERE project_id=? "
            "GROUP BY stencil",
            (project_id,),
        )
    }
    rows = db.q(
        """SELECT c.document_id, c.stencil, c.segment, d.path, d.filename,
                  d.fingerprint
           FROM welder_cert c JOIN document d ON d.id = c.document_id
           WHERE c.project_id=?""",
        (project_id,),
    )

    seen: set[str] = set()
    scored: list[tuple[int, Target]] = []
    for r in rows:
        fp = r["fingerprint"] or str(r["document_id"])
        if fp in seen:
            continue
        seen.add(fp)
        n = passes.get(r["stencil"] or "", 0)
        reason = (f"{n} passes welded under this stencil" if n
                  else "stencil does not appear on any weld report")
        scored.append(
            (-n, Target(r["document_id"], r["path"], r["filename"], fp,
                        page_count(r["path"]), reason, r["segment"] or ""))
        )

    scored.sort(key=lambda t: (t[0], t[1].filename))
    out = [t for _rank, t in scored]
    return out[:limit] if limit else out


def daily_weld_report_targets(db: Database, project_id: int,
                              limit: int | None = None) -> list[Target]:
    """Scanned weld reports — the ones no spreadsheet or text layer covers.

    A weld report is the spine of the audit: without it nothing knows which
    welds exist, so every weld-side rule stays dark. Reports that already
    parsed deterministically (an .xlsx alongside, or a text layer) are skipped.
    """
    parsed = {
        r["document_id"]
        for r in db.q(
            "SELECT DISTINCT document_id FROM weld WHERE project_id=? "
            "AND document_id IS NOT NULL AND source='daily_weld_report'",
            (project_id,),
        )
    }
    rows = db.q(
        """SELECT id, path, filename, segment, fingerprint FROM document
           WHERE project_id=? AND kind='daily_weld_report' AND ext IN ('.pdf','.PDF')
           ORDER BY segment, filename""",
        (project_id,),
    )

    seen: set[str] = set()
    out: list[Target] = []
    for r in rows:
        if r["id"] in parsed:
            continue
        fp = r["fingerprint"] or str(r["id"])
        if fp in seen:
            continue
        seen.add(fp)
        out.append(
            Target(r["id"], r["path"], r["filename"], fp, page_count(r["path"]),
                   "scanned weld report, no welds extracted", r["segment"] or "")
        )
        if limit and len(out) >= limit:
            break
    return out


def weld_map_targets(db: Database, project_id: int,
                     limit: int | None = None) -> list[Target]:
    """Weld and heat isometrics, weld maps first.

    A weld map carries the weld register — id, welders, date per joint — which
    is what the NDE rules join on. A heat map carries the material actually
    installed. Weld maps are ordered first because they unlock more rules.

    **A map is skipped once it has given up what its title promises**, and the
    two halves are judged separately. Most of these isometrics are plotted from
    CAD and their callouts survive as text: all fourteen of PLU's parse and
    thirteen of GL 31's sixteen, which is 526 welds already in hand and not
    worth paying to read again.

    Judging the halves apart is what makes the test worth anything. Bluewater's
    `WELD MAP AND HEAT MAP COMBIND GENESIS.pdf` is a scan whose OCR layer is
    noise — ten thousand words of it — and it yields 71 heats and **no welds at
    all**. Asked whether the document produced *something* it looks finished;
    asked whether the weld map produced welds, it is the one Bluewater document
    that most needs a model.
    """
    rows = db.q(
        """SELECT id, path, filename, segment, fingerprint FROM document
           WHERE project_id=? AND kind='weld_map' AND ext IN ('.pdf','.PDF')""",
        (project_id,),
    )
    welds = {
        r["document_id"]
        for r in db.q(
            "SELECT DISTINCT document_id FROM weld WHERE project_id=? "
            "AND document_id IS NOT NULL AND source='weld_map_text'",
            (project_id,),
        )
    }
    heats = {
        r["document_id"]
        for r in db.q(
            "SELECT DISTINCT document_id FROM installed_heat WHERE project_id=? "
            "AND document_id IS NOT NULL",
            (project_id,),
        )
    }

    seen: set[str] = set()
    scored: list[tuple[int, Target]] = []
    for r in rows:
        fp = r["fingerprint"] or str(r["id"])
        if fp in seen:
            continue
        seen.add(fp)
        title = (r["filename"] or "").lower()
        names_welds, names_heats = "weld" in title, "heat" in title
        missing: list[str] = []
        if names_welds and r["id"] not in welds:
            missing.append("weld callouts")
        if names_heats and r["id"] not in heats:
            missing.append("heat callouts")
        if not names_welds and not names_heats and r["id"] not in welds | heats:
            missing.append("callouts")
        if not missing:
            continue           # its text layer already gave up what it holds
        scored.append(
            (0 if names_welds else 1,
             Target(r["id"], r["path"], r["filename"], fp, page_count(r["path"]),
                    f"no {' or '.join(missing)} read from the text layer",
                    r["segment"] or ""))
        )
    scored.sort(key=lambda t: (t[0], t[1].filename))
    out = [t for _rank, t in scored]
    return out[:limit] if limit else out


#: Reference standards that are filed in every hydro folder on the job and
#: state a policy rather than record a test.  Reading them buys nothing.
_HYDRO_BOILERPLATE = re.compile(
    r"guidance|guideline|\bspec(ification)?\b|standard|\bgppb\b|procedure", re.IGNORECASE
)

#: Names that say outright this is a pressure test.  "Hydor" is a real and
#: repeated typo in the Kestrel 8 filenames.
_HYDRO_NAME = re.compile(
    r"hydro|hydor|pressure test|test package|test log|conditioning", re.IGNORECASE
)

#: A genuine package is a plan, a requirements sheet, a record, recorder
#: charts and calibration certificates - never one or two pages.  GL 31 files
#: 114 mill certificates under HYDRO TEST DOCUMENTS and they are all short,
#: which is what separates them from the tests when the folder cannot.
MIN_PACKAGE_PAGES = 5

#: Text on the first pages of a document that is plainly a pressure test.
#: "Hydrostatic" is deliberately absent: mill certificates report the
#: hydrostatic test the pipe passed at the mill, which is not this test.
_TEST_WORDS = re.compile(
    r"pressure\s+(proof\s+)?test|deadweight|test\s+medium", re.IGNORECASE
)

#: Below this many characters the front of the document is a scan, and its
#: text layer says nothing either way.
_SCANNED_CHARS = 200


def _front_text(path: str, pages: int = 2) -> str:
    try:
        import pymupdf

        with pymupdf.open(path) as doc:
            return " ".join(doc[i].get_text() for i in range(min(pages, len(doc))))
    except Exception:                       # noqa: BLE001 - absent text is the answer
        return ""


def _reads_as_a_test(path: str) -> bool:
    """Whether a document filed under hydro is plausibly a pressure test.

    Every genuine package in the corpus is either a scan with no text layer or
    a searchable plan that says "Pressure Test" on its first page.  What is
    filed alongside them — GL 31's mill certificates, valve dossiers and weld
    logs — carries a full text layer of something else entirely, and this is
    the only signal that separates the two once the folder has stopped being
    informative.
    """
    text = " ".join(_front_text(path).split())
    return len(text) < _SCANNED_CHARS or bool(_TEST_WORDS.search(text))


def hydrotest_targets(db: Database, project_id: int,
                      limit: int | None = None) -> list[Target]:
    """Pressure test packages, completed tests first.

    Only the record settles anything on its own, so packages whose names say
    they are tests are read before ones identified only by where they are
    filed, and standalone plans last.  Company guidance and the certificates
    that contractors file in the same folder are skipped outright.
    """
    rows = db.q(
        """SELECT id, path, filename, segment, fingerprint FROM document
           WHERE project_id=? AND kind='hydrotest' AND ext IN ('.pdf','.PDF')""",
        (project_id,),
    )
    seen: set[str] = set()
    scored: list[tuple[int, Target]] = []
    for r in rows:
        name = r["filename"] or ""
        if _HYDRO_BOILERPLATE.search(name):
            continue
        fp = r["fingerprint"] or str(r["id"])
        if fp in seen:
            continue
        seen.add(fp)

        pages = page_count(r["path"])
        named = _HYDRO_NAME.search(name) is not None
        if not named and pages < MIN_PACKAGE_PAGES:
            continue
        if not _reads_as_a_test(r["path"]):
            continue
        rank, reason = _hydro_rank(name, named)
        scored.append(
            (rank, Target(r["id"], r["path"], r["filename"], fp, pages, reason,
                          r["segment"] or ""))
        )
    scored.sort(key=lambda t: (t[0], t[1].filename))
    out = [t for _rank, t in scored]
    return out[:limit] if limit else out


def _hydro_rank(name: str, named: bool) -> tuple[int, str]:
    if re.search(r"\bplans?\b", name, re.IGNORECASE):
        return 2, "test plan only, no record"
    if named:
        return 0, "pressure test package"
    return 1, "filed under the hydro test section"


#: Reference material filed in every coating folder: the XTO coatings
#: specification, and placeholder sheets the crews scan in when a segment had
#: no coating activity.
_COATING_BOILERPLATE = re.compile(
    r"\bgppb\b|specification|protective coatings|guidance|"
    r"^no data|^not applicable", re.IGNORECASE
)


def coating_targets(db: Database, project_id: int,
                    limit: int | None = None) -> list[Target]:
    """Daily field coating inspection reports.

    Ordered by page count so a ``--limit`` buys the most reports: the multi-day
    bundles ("8-22 & 8-23 Coating Reports.pdf") carry one form per page and are
    the cheapest way to cover a job, but a run capped mid-bundle would leave a
    document half read, so whole documents are the unit either way.
    """
    rows = db.q(
        """SELECT id, path, filename, segment, fingerprint FROM document
           WHERE project_id=? AND kind='coating' AND ext IN ('.pdf','.PDF')""",
        (project_id,),
    )
    seen: set[str] = set()
    out: list[Target] = []
    for r in rows:
        name = r["filename"] or ""
        if _COATING_BOILERPLATE.search(name):
            continue
        fp = r["fingerprint"] or str(r["id"])
        if fp in seen:
            continue
        seen.add(fp)
        pages = page_count(r["path"])
        reason = (f"{pages} daily reports in one file" if pages > 1
                  else "daily coating report")
        out.append(Target(r["id"], r["path"], r["filename"], fp, pages, reason,
                          r["segment"] or ""))

    out.sort(key=lambda t: (-t.pages, t.filename))
    return out[:limit] if limit else out


def backfill_targets(db: Database, project_id: int,
                     limit: int | None = None) -> list[Target]:
    """Release for backfill forms.

    Filed as bundles — Bluewater's are 27 pages, one release per page — so every
    page is worth reading and the whole document is the unit.
    """
    rows = db.q(
        """SELECT id, path, filename, segment, fingerprint FROM document
           WHERE project_id=? AND kind='backfill' AND ext IN ('.pdf','.PDF')""",
        (project_id,),
    )
    seen: set[str] = set()
    out: list[Target] = []
    for r in rows:
        fp = r["fingerprint"] or str(r["id"])
        if fp in seen:
            continue
        seen.add(fp)
        pages = page_count(r["path"])
        out.append(Target(r["id"], r["path"], r["filename"], fp, pages,
                          f"{pages} release forms in one file" if pages > 1
                          else "release for backfill", r["segment"] or ""))
    out.sort(key=lambda t: (-t.pages, t.filename))
    return out[:limit] if limit else out


TARGETS = {
    "mtr": mtr_targets,
    "reader_sheet": reader_sheet_targets,
    "welder_cert": welder_cert_targets,
    "daily_weld_report": daily_weld_report_targets,
    "weld_map": weld_map_targets,
    "hydrotest": hydrotest_targets,
    "coating": coating_targets,
    "backfill": backfill_targets,
}

#: Order matters on replay: weld reports create welds that the other kinds and
#: the deterministic extractors then hang results off.
REPLAY_ORDER = ("daily_weld_report", "weld_map", "reader_sheet", "mtr",
                "welder_cert", "hydrotest", "coating", "backfill")


#: Where each kind's documents live, for replay. Deliberately *not* the
#: finding-driven target selection: once a cached MTR has supplied its
#: manufacturer, that document is no longer a target, and replaying only
#: targets would silently drop it on the next audit.
_REPLAY_SOURCES: dict[str, str] = {
    "daily_weld_report": """SELECT id, path, filename, segment, fingerprint
                            FROM document WHERE project_id=?
                              AND kind='daily_weld_report' AND ext IN ('.pdf','.PDF')""",
    "weld_map": """SELECT id, path, filename, segment, fingerprint
                   FROM document WHERE project_id=?
                     AND kind='weld_map' AND ext IN ('.pdf','.PDF')""",
    "reader_sheet": """SELECT id, path, filename, segment, fingerprint
                       FROM document WHERE project_id=? AND kind='nde_reader_sheet'""",
    "welder_cert": """SELECT id, path, filename, segment, fingerprint
                      FROM document WHERE project_id=? AND kind='welder_cert'""",
    "mtr": """SELECT DISTINCT d.id, d.path, d.filename, d.segment, d.fingerprint
              FROM material m JOIN document d ON d.id = m.document_id
              WHERE m.project_id=? AND m.source='mtr_file'""",
    # These two kinds create standalone records rather than annotating rows
    # that already exist, so a document filed into several segment books would
    # replay into several copies of the same day's work. Kestrel 8 files
    # `4 FG 7-11-25.pdf` under four segments; without the grouping its one
    # report becomes four, and every finding on it is raised four times.
    "hydrotest": """SELECT MIN(id) id, path, filename, segment, fingerprint
                    FROM document WHERE project_id=?
                      AND kind='hydrotest' AND ext IN ('.pdf','.PDF')
                    GROUP BY IFNULL(fingerprint, id)""",
    "coating": """SELECT MIN(id) id, path, filename, segment, fingerprint
                  FROM document WHERE project_id=?
                    AND kind='coating' AND ext IN ('.pdf','.PDF')
                  GROUP BY IFNULL(fingerprint, id)""",
    "backfill": """SELECT MIN(id) id, path, filename, segment, fingerprint
                   FROM document WHERE project_id=?
                     AND kind='backfill' AND ext IN ('.pdf','.PDF')
                   GROUP BY IFNULL(fingerprint, id)""",
}

_APPLIERS = {
    "daily_weld_report": lambda db, pid, t, p, n: _apply_weld_report(db, pid, t, p, n),
    "weld_map": lambda db, pid, t, p, n: _apply_weld_map(db, pid, t, p, n),
    "reader_sheet": lambda db, pid, t, p, n: _apply_reader_sheet(db, pid, t, p, n),
    "mtr": lambda db, pid, t, p, n: _apply_mtr(db, pid, t, p, n),
    "welder_cert": lambda db, pid, t, p, _n: _apply_welder_cert(db, pid, t, p),
    "hydrotest": lambda db, pid, t, p, n: _apply_hydrotest(db, pid, t, p, n),
    "coating": lambda db, pid, t, p, n: _apply_coating(db, pid, t, p, n),
    "backfill": lambda db, pid, t, p, n: _apply_backfill(db, pid, t, p, n),
}


def replay(db: Database, project_id: int,
           kinds: tuple[str, ...] = REPLAY_ORDER) -> dict[str, int]:
    """Re-apply every cached vision result. Reads the cache only — no API calls.

    Re-indexing a project clears the tables the vision pass writes into, so
    without this an ``audit`` run after a vision run would quietly throw the
    results away and the pages would have to be paid for again.
    """
    counts: dict[str, int] = {}
    for kind in kinds:
        applied = 0
        for r in db.q(_REPLAY_SOURCES[kind], (project_id,)):
            fingerprint = r["fingerprint"] or str(r["id"])
            pages = page_count(r["path"])
            cap = MAX_PAGES.get(kind, 0)
            if cap:
                pages = min(pages, cap)
            target = Target(r["id"], r["path"], r["filename"], fingerprint,
                            pages, "replay", r["segment"] or "")
            for page_no in range(max(pages, 1)):
                payload = db.ocr_any(fingerprint, kind, page_no)
                if payload is None or payload.get("_error"):
                    continue
                # Re-filed, not just re-applied. Re-indexing clears
                # vision_conflict along with everything else, so without this
                # the pages a human still owes a look at would quietly empty
                # out on the next audit — and an empty review list reads as
                # "nothing to check" rather than "the record was discarded".
                _record_conflicts(db, project_id, kind, target, payload, page_no)
                applied += _APPLIERS[kind](db, project_id, target, payload, page_no)
        if applied:
            counts[kind] = applied
    return counts


def estimate_pass(db: Database, project_id: int, kind: str, *, model: str,
                  max_edge: int, limit: int | None = None,
                  tiles: str = "auto") -> tuple[Estimate, list[Target]]:
    targets = TARGETS[kind](db, project_id, limit)
    from ..vision import estimate as _estimate

    est = _estimate(
        db, [(t.path, t.fingerprint, t.pages) for t in targets],
        model=model, max_edge=max_edge, kind=kind, tiles=tiles,
    )
    return est, targets


# ---------------------------------------------------------------------------
# Running a pass
# ---------------------------------------------------------------------------


def supplier_roles(db: Database, project_id: int) -> set[str]:
    """Companies this project's own certificates call a steel supplier.

    ``mill_source`` is the model's answer to "which kind of line did you read
    this from", and it is right about half the time it matters: across 111
    certificates it labelled 53 supplier lines correctly, but left Tubos
    Reunidos and JSW Steel unlabelled on three others, where the name went on
    to be recorded as the manufacturer.

    Those three are recoverable without reading anything again, because the
    corpus has already answered for them elsewhere. A company that some
    certificate labels a supplier, and that no certificate labels a works, is
    a supplier on the pages where the label was missed too.

    Deliberately not a list of steelmakers. Calderon appears here as a
    supplier on a fitting certificate and as the producer on its own pipe
    certificates, and both are true; the ``works_line`` veto is what keeps it
    and Steel Dynamics out of this set.
    """
    from ..aml import normalise_manufacturer

    said_supplier: set[str] = set()
    said_works: set[str] = set()
    for row in db.q(
        """SELECT o.payload FROM ocr_cache o
           JOIN document d ON d.fingerprint = substr(o.sha1, 1, instr(o.sha1, ':') - 1)
           WHERE d.project_id = ? AND o.sha1 LIKE '%:mtr:%'""",
        (project_id,),
    ):
        payload = json.loads(row["payload"])
        name = normalise_manufacturer(payload.get("mill_name") or "")
        if not name:
            continue
        source = payload.get("mill_source")
        if source == "supplier_line":
            said_supplier.add(name)
        elif source == "works_line":
            said_works.add(name)
    return said_supplier - said_works


def note_reader_disagreements(db: Database, project_id: int) -> int:
    """Record where two readers of the same page named different companies.

    Three things read these certificates — the text layer, a paid vision
    model, and OCR — and where two of them looked at one page and disagreed
    about the letterhead, that is the same fact VIS-02 already reports about
    two close-ups: the name is not reliably legible.

    It matters because a confident misread is invisible to every other guard.
    Haiku read one Tex-Tubo letterhead as TECKCUBO, TECKQUBO, TEKSUMEO,
    TECKUOEO, Tekkubeo, Tekube and Tecumseh across seven certificates, its
    close-ups agreeing each time, and each became a critical "not on the
    approved list" finding. OCR reads the same letterhead as TEXTUBO.

    Nothing is overturned here and OCR does not win: the disagreement is
    filed, which withholds the critical finding and puts the page in front of
    someone. Deciding between two readers by rule would be guessing with extra
    steps — and guessing toward the AML-recognised one would be worse, because
    it would resolve every such case toward "approved".
    """
    from ..vision import _same_company

    seen: dict[str, dict[str, str]] = defaultdict(dict)
    for r in db.q(
        """SELECT o.sha1, o.model, o.payload FROM ocr_cache o
           JOIN document d ON d.fingerprint = substr(o.sha1, 1, instr(o.sha1, ':') - 1)
           WHERE d.project_id=? AND o.sha1 LIKE '%:mtr:%' AND o.page_no=0""",
        (project_id,),
    ):
        payload = json.loads(r["payload"])
        name = (payload.get("issuing_company") or "").strip()
        if payload.get("page_is_certificate") and name:
            seen[r["sha1"].split(":")[0]][r["model"]] = name

    filed = 0
    for fingerprint, by_model in seen.items():
        names = list(dict.fromkeys(by_model.values()))
        if len(names) < 2:
            continue
        # Same company spelled differently is not a disagreement; the AML
        # lookup these feed is fuzzy and would resolve them alike.
        if all(_same_company(names[0], other) for other in names[1:]):
            continue
        doc = db.one(
            "SELECT id, filename, segment FROM document "
            "WHERE project_id=? AND fingerprint=? LIMIT 1", (project_id, fingerprint))
        if not doc:
            continue
        with db.tx() as c:
            c.execute(
                "DELETE FROM vision_conflict WHERE project_id=? AND fingerprint=? "
                "AND kind='mtr' AND field='issuing_company' AND page_no=1",
                (project_id, fingerprint))
            c.execute(
                """INSERT INTO vision_conflict(project_id, document_id, fingerprint,
                       filename, segment, kind, page_no, field, readings, chosen,
                       decisive)
                   VALUES(?,?,?,?,?, 'mtr', 1, 'issuing_company', ?, ?, 0)""",
                (project_id, doc["id"], fingerprint, doc["filename"],
                 doc["segment"] or "", json.dumps(names),
                 # Whichever reader is named first alphabetically; the value
                 # is only there so VIS-02 can quote something, and the point
                 # of the row is that no reader is being believed.
                 by_model[sorted(by_model)[0]]),
            )
        filed += 1
    return filed


def demote_known_suppliers(db: Database, project_id: int) -> int:
    """Take back a manufacturer that this project elsewhere calls a supplier."""
    from ..aml import normalise_manufacturer

    suppliers = supplier_roles(db, project_id)
    if not suppliers:
        return 0

    changed = 0
    for r in db.q(
        """SELECT id, manufacturer, mill_name, issuing_company FROM material
           WHERE project_id=? AND confidence='vision'
             AND IFNULL(mill_name,'') <> ''""",
        (project_id,),
    ):
        if normalise_manufacturer(r["mill_name"]) not in suppliers:
            continue
        if (normalise_manufacturer(r["manufacturer"] or "")
                != normalise_manufacturer(r["mill_name"])):
            continue                       # the letterhead already won

        # Falls back to the letterhead, or to nothing. Nothing is the right
        # answer when the letterhead was not read either: the certificate then
        # reports as MTR-08, "heat has no determinable manufacturer", which is
        # true and checkable. Keeping the steel supplier would instead credit a
        # Valvitalia elbow to the mill that rolled the pipe it was cut from —
        # and that name is on the AML, so it would read as a clean result.
        letterhead = (r["issuing_company"] or "").strip()
        note = (f"'{r['mill_name']}' supplies steel elsewhere in this project"
                + ("" if letterhead else ", and no letterhead was read"))
        with db.tx() as c:
            c.execute(
                """UPDATE material SET manufacturer=?, mill_name=NULL,
                       evidence=? WHERE id=?""",
                (letterhead, note, r["id"]),
            )
        changed += 1
    return changed


def _still_needed(db: Database, project_id: int, kind: str, target: Target,
                  field_name: str) -> bool:
    """Whether losing this field actually costs the audit anything here.

    A field can be decisive for a kind and irrelevant on a given document. The
    heat number is the case that matters: ``_apply_mtr`` adopts a scanned heat
    only when the filename did not already supply one, so on a certificate
    named ``24179651 20 .375 ... KANDAL PIPE.pdf`` the read heat is discarded
    whatever it says. Flagging those put half the review list on pages where
    nothing was at stake, and a review list nobody finishes protects nothing.
    """
    if kind == "mtr" and field_name == "heat":
        row = db.one(
            "SELECT heat_key FROM material WHERE project_id=? AND document_id=?",
            (project_id, target.document_id),
        )
        return not (row and row["heat_key"])
    return True


def _record_conflicts(db: Database, project_id: int, kind: str, target: Target,
                      payload: dict, page_no: int) -> int:
    """File the fields two close-ups of this page read differently.

    Only the irreconcilable ones reach a human — see ``DECISIVE_FIELDS`` — but
    all of them are filed, because "the close-ups disagreed and the majority
    settled it" is the first thing worth knowing when a value later turns out
    to be wrong.
    """
    notes = payload.get("_tiles_disagreed")
    if not notes:
        return 0
    flagged = 0
    with db.tx() as c:
        # Replaying the same cached page twice must not raise the same value
        # twice. Cleared per page rather than wholesale, because a pass reads
        # one page at a time and re-filing the lot on each would be quadratic.
        c.execute(
            "DELETE FROM vision_conflict WHERE project_id=? AND fingerprint=? "
            "AND kind=? AND page_no=?",
            (project_id, target.fingerprint, kind, page_no + 1),
        )
        for name, note in notes.items():
            chosen = note.get("chose")
            decisive = int(is_decisive(kind, name) and chosen is None
                           and _still_needed(db, project_id, kind, target, name))
            flagged += decisive
            c.execute(
                """INSERT INTO vision_conflict(project_id, document_id,
                       fingerprint, filename, segment, kind, page_no, field,
                       readings, chosen, decisive)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (project_id, target.document_id, target.fingerprint,
                 target.filename, target.segment, kind, page_no + 1, name,
                 json.dumps(note.get("readings")),
                 None if chosen is None else str(chosen), decisive),
            )
    return flagged


def run(db: Database, project_id: int, kind: str, reader: VisionReader,
        targets: list[Target], progress=None) -> PassResult:
    result = PassResult(kind=kind)
    for i, target in enumerate(targets, 1):
        result.documents += 1
        if progress:
            progress(i, len(targets), target.filename)
        for page_no in range(target.pages):
            cached = reader.cached(target.fingerprint, page_no, kind) is not None
            try:
                payload = reader.read_page(target.path, page_no, kind, target.fingerprint)
            except Exception as exc:              # noqa: BLE001 - reported, not raised
                result.failures.append(f"{target.filename} p{page_no + 1}: {exc}")
                continue
            if cached:
                result.pages_cached += 1
            else:
                result.pages_read += 1
            if payload.get("_error"):
                result.failures.append(
                    f"{target.filename} p{page_no + 1}: {payload['_error']}"
                )
                continue
            result.conflicts += _record_conflicts(
                db, project_id, kind, target, payload, page_no)
            if kind == "mtr":
                result.updated += _apply_mtr(db, project_id, target, payload, page_no)
                # A certificate names its heat and issuer up front. Once we
                # have both, the remaining pages are chemistry tables that
                # cost money and add nothing.
                if payload.get("page_is_certificate") and _clean(
                    payload.get("issuing_company")
                ) and _clean(payload.get("heat")):
                    break
            elif kind == "welder_cert":
                result.updated += _apply_welder_cert(db, project_id, target, payload)
                if payload.get("page_is_qualification_record"):
                    break          # the record is one page
            elif kind == "daily_weld_report":
                result.updated += _apply_weld_report(db, project_id, target, payload,
                                                     page_no)
            elif kind == "weld_map":
                result.updated += _apply_weld_map(db, project_id, target, payload,
                                                  page_no)
            elif kind == "hydrotest":
                result.updated += _apply_hydrotest(db, project_id, target, payload,
                                                   page_no)
            elif kind == "coating":
                result.updated += _apply_coating(db, project_id, target, payload,
                                                 page_no)
            elif kind == "backfill":
                result.updated += _apply_backfill(db, project_id, target, payload,
                                                  page_no)
            else:
                result.updated += _apply_reader_sheet(db, project_id, target, payload, page_no)
    return result


def _clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


#: US state and Canadian province codes, for spotting a town in a name field.
_PLACE_TAIL = re.compile(
    r",\s*(?:[A-Z]{2}|ohio|texas|indiana|alabama|louisiana|california|"
    r"oklahoma|arkansas|kansas|pennsylvania|alberta|ontario)\.?$",
    re.IGNORECASE)

#: Words that make a string a place rather than a maker, even without a state.
_PLACE_WORD = re.compile(
    r"(junction|city|county|township|province|prefecture|industrial\s+park|"
    r"free\s+zone)", re.IGNORECASE)


#: A branch or account code. Distributors ship from numbered depots — "MRC
#: GLOBAL #172", "MRC GLOBAL 8078" — and those strings appear in the SHIP TO
#: and SOLD TO blocks, never on a mill's letterhead. The vision pass took one
#: as the manufacturer on seven certificates whose actual maker was Halden,
#: which is on the AML; each became a critical "not approved" finding against
#: approved material.
#:
#: The prompt already forbids taking the customer, and a label filter cannot
#: help here because the payload carries the name without its label. The code
#: is the only part of the string that gives it away. Checked against all 1,345
#: AML entries on this job: it rejects none of them.
_BRANCH_CODE = re.compile(r"#\s*\w*\d|\s[A-Z]?\d{3,}\s*$")


#: Words that belong to a specification rather than to whoever made the thing.
#: A string built only from these and part numbers is a material grade the
#: model lifted out of the body of the form when it could not find a
#: letterhead — 'A351-CF8M' was recorded as the manufacturer of four items.
_SPEC_WORDS = frozenset({
    "astm", "asme", "api", "iso", "din", "mss", "sae", "uns", "nace", "smls",
    "erw", "saw", "hfw", "wpb", "wpl", "wphy", "grade", "type", "class", "sch",
    "std", "xs", "xh", "rfwn", "flg", "psl", "edition", "rev", "spec",
    "material", "seamless", "welded", "normalized", "forged", "bare", "pipe",
})

#: Words start with a letter — any letter, so Brück survives — and may carry
#: digits, ampersands and dots after it.
_WORD = re.compile(r"[^\W\d_][\w&.'\-]*", re.UNICODE)

#: Shorter than this and it cannot be a standards designation, so a name like
#: "3M" is left alone rather than caught by the digit rule.
_TOO_SHORT_TO_BE_A_SPEC = 3


#: An address under a letterhead is the next thing down from the company name,
#: and the model sometimes takes it instead — '3245 S. Harte Avenue' was
#: recorded as a manufacturer. A depot is the same mistake one line further on.
_STREET = (r"avenue|ave|street|road|rd|drive|boulevard|blvd|lane|"
           r"highway|hwy|parkway|pkwy|suite|ste|circle|terrace")
_HOUSE_NUMBER = re.compile(r"^\s*\d+[\w-]*[\s,]")
_STREET_WORD = re.compile(rf"\b({_STREET})\b", re.IGNORECASE)
_POSTCODE = re.compile(r",[^,]*\b\d{5}(-\d{4})?\b")
_DEPOT = re.compile(r"\b(distribution|warehouse|depot|service)\s+cent(er|re)\b",
                    re.IGNORECASE)


def _looks_like_an_address(name: str) -> bool:
    """Whether a name is where a company is rather than who it is.

    A house number alone is not enough — "84 Lumber Company" is a real firm —
    so it has to be a number followed by either a street word or the comma-
    separated run of a postal address. Checked against every AML entry on the
    job: it rejects none of them.
    """
    if _DEPOT.search(name) or _POSTCODE.search(name):
        return True
    return bool(_HOUSE_NUMBER.match(name)
                and (_STREET_WORD.search(name) or name.count(",") >= 3))


def _looks_like_a_spec(name: str) -> bool:
    """Whether a name is a material specification rather than a company.

    Deliberately conservative, and checked against every AML entry on the job:
    it rejects none of them. `Hy-Grade`, `C&C`, `L&T` and `S.C.O.T.` are all
    real approved manufacturers, and an earlier version of this threw all four
    away — losing a true approval is worse than keeping a spec code, because
    the spec code produces a finding somebody reads while the lost approval
    produces silence.
    """
    if len(name.strip()) <= _TOO_SHORT_TO_BE_A_SPEC:
        return False
    tokens = [t.strip(".-'").lower() for t in _WORD.findall(name)]

    def names_somebody(token: str) -> bool:
        # Three letters or more, no digits in it, and not a standards word.
        # The no-digits part is what separates 'Hy-Grade', a real approved
        # manufacturer, from 'A351-CF8M', which is a casting grade.
        letters = sum(ch.isalpha() for ch in token)
        return (letters >= 3 and not any(ch.isdigit() for ch in token)
                and token not in _SPEC_WORDS)

    if any(names_somebody(t) for t in tokens):
        return False        # something in here names somebody
    # Nothing does. Call it a spec only if it reads like one.
    return bool(re.search(r"\d", name)) or any(t in _SPEC_WORDS for t in tokens)


def _looks_like_a_company(name: str | None) -> bool:
    """Whether a name is plausibly a manufacturer rather than a place or noise.

    Deliberately crude and one-sided: it only rejects what is clearly not a
    company, because a false rejection costs a fallback to the letterhead
    while a false acceptance sends the AML check after a town.
    """
    if not name:
        return False
    text = name.strip(" .,-")
    if len(text) < 3 or not any(ch.isalpha() for ch in text):
        return False
    if (_PLACE_WORD.search(text) or _BRANCH_CODE.search(text)
            or _looks_like_a_spec(text) or _looks_like_an_address(text)):
        return False
    # "Mingo Junction, OH" is caught above; "Baytown, TX" needs the state tail.
    return not _PLACE_TAIL.search(text)


def _apply_mtr(db: Database, project_id: int, target: Target,
               payload: dict, page_no: int) -> int:
    """Write a certificate's manufacturer and spec back onto its material row."""
    if not payload.get("page_is_certificate"):
        return 0

    issuer = _clean(payload.get("issuing_company"))
    mill = _clean(payload.get("mill_name"))
    # The same test the mill gets. A ship-to depot is not the maker whichever
    # field the model happened to put it in.
    if issuer and not _looks_like_a_company(issuer):
        issuer = ""

    # The buyer, reported so it can be excluded. No filter can separate a
    # customer from a maker by looking at the name alone — 'MRC GLOBAL #172'
    # only gave itself away by its branch code, and 'DODSON GLOBAL' had
    # nothing at all — so the model is asked who bought the material, and
    # whatever it names is disqualified from having made it.
    #
    # Cleared rather than replaced. Falling back to nothing reports as MTR-08,
    # "manufacturer unknown", which is true and checkable; keeping the buyer
    # reports a company that never touched the item, and on this corpus that
    # name is often on the approved list.
    customer = _clean(payload.get("customer"))
    if customer:
        if issuer and _same_company(issuer, customer):
            issuer = ""
        if mill and _same_company(mill, customer):
            mill = None
    # Where the name was read from decides whether it is the producer. Asking
    # the model to report the line it used, and applying the policy here, works
    # where asking it to apply the policy itself did not: a paragraph telling
    # it that a Supplier line is not the maker was in the prompt for a full
    # pass, and Valvitalia's elbows still came back made by Tubos Reunidos,
    # who supplied the pipe they were forged from. That name is a real company
    # the AML approves, so the substitution passes as a clean result.
    # A melt line states the heat of the steel it supplied, and that heat is
    # not this certificate's. Ryeburn International certified a flexolet under heat
    # 8410BB and named NORTHFIELD STAINLESS on its MILL/COUNTRY OF ORIGIN
    # line under heat CN1G; the model called that a works_line — twice, on two
    # separate readings — and the fitting was credited to the steel supplier,
    # who is not on the approved list, while Ryeburn, who is, was discarded.
    #
    # No label survives this test. A works line describes the item in hand and
    # so carries the item's heat or none at all; only a supply line brings its
    # own.
    # Compared with the tolerance a scanned number deserves. A works line
    # stating this certificate's own heat, misread by one character, is not a
    # supply line: `867985` against `367985` is one heat read twice, and
    # treating it as two would throw away a producer the page does name.
    mill_heat = _clean(payload.get("mill_heat"))
    own_heat = _clean(payload.get("heat"))
    if mill and mill_heat and own_heat             and not same_heat_differently_read(mill_heat, own_heat):
        mill = None

    source = _clean(payload.get("mill_source"))
    if source == "supplier_line":
        mill = None
    elif source == "letterhead":
        # Not a second company, just the first one written twice. Moved rather
        # than dropped: on the BQN certificate the model put the letterhead in
        # mill_name and left issuing_company empty, and discarding it there
        # would have thrown away the only name on the page.
        issuer = issuer or mill
        mill = None
    if not _looks_like_a_company(mill):
        # The mill outranks the letterhead, so anything junk landing in it
        # becomes the manufacturer the AML is asked about. A real pass put
        # '.', 'Mingo Junction, OH' and 'Alloy Junctions, OH' there — the
        # prompt says a place is not a company and the model does not always
        # listen, so the precedence is guarded here rather than only asked for.
        mill = None
    # The mill is the AML-checkable party; fall back to the letterhead only
    # when the certificate names no separate producer.
    manufacturer = mill or issuer
    heat = _clean(payload.get("heat"))

    row = db.one(
        "SELECT id, heat_key FROM material WHERE project_id=? AND document_id=?",
        (project_id, target.document_id),
    )
    if not row:
        return 0

    fields = {
        "manufacturer": manufacturer,
        "issuing_company": issuer,
        "mill_name": mill,
        "spec": _clean(payload.get("specification")),
        "grade": _clean(payload.get("grade")),
        "confidence": "vision",
    }
    # Only adopt a heat the filename could not supply - a filename heat is
    # exact text, while a scanned one has been through OCR.
    if heat and not row["heat_key"]:
        fields["heat"] = heat
        fields["heat_key"] = normalise_heat(heat)
    if nps := parse_nps(payload.get("size")):
        fields["nps"] = nps

    assignments = ", ".join(f"{k}=?" for k in fields)
    with db.tx() as c:
        c.execute(
            f"UPDATE material SET {assignments} WHERE id=?",
            (*fields.values(), row["id"]),
        )
    return 1


#: Source tag for welds recovered from a scan, kept distinct from the
#: spreadsheet path so the two are never confused and can be cleared apart.
VISION_WELD_SOURCE = "daily_weld_report_vision"

#: A dash in a welder column means "no welder for this pass", not a name.
_DASH = re.compile(r"^[\s\-–—_/]*$")


def _cell(value) -> str:
    text = _clean(value)
    return "" if _DASH.match(text) else text


def _apply_weld_report(db: Database, project_id: int, target: Target,
                       payload: dict, page_no: int) -> int:
    """Create weld rows from a scanned daily weld report."""
    if not payload.get("page_is_weld_report"):
        return 0

    rows = payload.get("rows") or []
    if not rows:
        return 0

    line = _clean(payload.get("service")) or _clean(payload.get("job_name"))
    report_date = _parse_date(payload.get("report_date"))
    default_size = _clean(payload.get("line_size"))

    # The NOTES column carries the NDE report on some crews' sheets and a bore
    # or spool reference on others. Only prefixes that exist in this project's
    # own reader sheets count as a citation; with none on file, nothing does.
    known = known_nde_prefixes(db, project_id)

    # Replaying the same page must not duplicate its welds.
    with db.tx() as c:
        c.execute(
            "DELETE FROM weld WHERE project_id=? AND document_id=? AND source=?",
            (project_id, target.document_id, VISION_WELD_SOURCE),
        )

    records = []
    for i, row in enumerate(rows, 1):
        # A blank WELD # is normal on these sheets. Rather than invent a number,
        # identify the weld by its position on the report it came from.
        weld_no = _cell(row.get("weld_no")) or f"row {i}"
        note = _clean(row.get("notes"))
        nid = parse_one(note)
        nde_id = str(nid) if nid and known and nid.prefix in known else ""
        records.append(
            (
                project_id, target.document_id, target.segment, line, weld_no,
                _cell(row.get("size")) or default_size,
                _cell(row.get("weld_type")), _cell(row.get("process")),
                _cell(row.get("welder_root")), _cell(row.get("welder_hot_pass")),
                _cell(row.get("welder_fill")), _cell(row.get("welder_cap")),
                report_date, note, nde_id, VISION_WELD_SOURCE,
            )
        )

    with db.tx() as c:
        c.executemany(
            """INSERT INTO weld
               (project_id, document_id, segment, line, weld_no, weld_size,
                weld_type, process, welder_root, welder_hp, welder_fill,
                welder_cap, date_welded, note, nde_id, source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            records,
        )
    return len(records)


#: Welds recovered from a weld-map isometric, kept apart from the daily-report
#: register: the two are different documents describing the same work, and
#: collapsing them would hide a disagreement rather than surface it.
WELD_MAP_SOURCE = "weld_map_vision"


def _apply_weld_map(db: Database, project_id: int, target: Target,
                    payload: dict, page_no: int) -> int:
    """Create welds and installed-heat records from a piping isometric."""
    if not payload.get("page_is_isometric"):
        return 0

    line = _clean(payload.get("line_no")) or _clean(payload.get("drawing_no"))
    drawing = _clean(payload.get("drawing_no")) or line

    # Replaying a page must replace its own rows, never add to them.
    with db.tx() as c:
        c.execute(
            "DELETE FROM weld WHERE project_id=? AND document_id=? AND source=?",
            (project_id, target.document_id, WELD_MAP_SOURCE),
        )
        c.execute(
            "DELETE FROM installed_heat WHERE project_id=? AND document_id=?",
            (project_id, target.document_id),
        )

    applied = 0

    welds = []
    for callout in payload.get("weld_callouts") or []:
        raw = _cell(callout.get("weld_id"))
        if not raw:
            continue
        ids = parse_ids(raw)
        welds.append(
            (
                project_id, target.document_id, target.segment, line, raw,
                _cell(callout.get("welders")),
                _parse_date(callout.get("date")),
                str(ids[0]) if ids else "", WELD_MAP_SOURCE,
            )
        )
    if welds:
        with db.tx() as c:
            c.executemany(
                """INSERT INTO weld
                   (project_id, document_id, segment, line, weld_no,
                    welder_root, date_welded, nde_id, source)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                welds,
            )
        applied += len(welds)

    heats = []
    for callout in payload.get("heat_callouts") or []:
        heat = _cell(callout.get("heat"))
        if not heat:
            continue
        heats.append(
            (project_id, target.document_id, target.segment, line, drawing,
             heat, normalise_heat(heat), _clean(callout.get("note")), "weld_map_vision")
        )
    if heats:
        with db.tx() as c:
            c.executemany(
                """INSERT INTO installed_heat
                   (project_id, document_id, segment, line, drawing_no, heat,
                    heat_key, note, source)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                heats,
            )
        applied += len(heats)

    return applied


#: A pressure test package is one document holding several kinds of page: the
#: requirements on one sheet, the completed record on another, calibration
#: certificates at the back.  They describe one test, so they merge onto one
#: row rather than becoming three unrelated records.
_HYDRO_REQUIRED = {
    "req_min_press": "required_min_pressure",
    "req_max_press": "required_max_pressure",
    "req_hours": "required_duration_hours",
}
_HYDRO_TEXT = {
    "service": "service", "line_no": "line_no", "code": "code",
    "medium": "test_medium", "deadweight_sn": "deadweight_sn",
    "press_rec_sn": "pressure_recorder_sn", "temp_rec_sn": "temp_recorder_sn",
    "contractor_rep": "contractor_representative", "inspector": "inspector",
}


def _apply_hydrotest(db: Database, project_id: int, target: Target,
                     payload: dict, page_no: int) -> int:
    """Merge one page of a pressure test package onto the test it belongs to."""
    page_type = _clean(payload.get("page_type"))
    if page_type in ("", "other", "chart"):
        return 0

    if page_type == "calibration_certificate":
        return _apply_calibration(db, project_id, target, payload, page_no)

    fields: dict[str, object] = {}
    for column, key in _HYDRO_TEXT.items():
        if value := _clean(payload.get(key)):
            fields[column] = value
    for column, key in _HYDRO_REQUIRED.items():
        if (value := _number(payload.get(key))) is not None:
            fields[column] = value

    if page_type == "test_record":
        if (hours := _number(payload.get("stated_duration_hours"))) is not None:
            fields["stated_hours"] = hours
        for column, key in (("started", "started_at"), ("completed", "completed_at")):
            if raw := _clean(payload.get(key)):
                fields[f"{column}_raw"] = raw
                fields[f"{column}_at"] = _parse_datetime(raw)
        # Null means neither box was ticked, which is the finding HYD-05
        # reports. Recording it as unknown rather than skipping the field is
        # the point: an absent result must not read as "not yet transcribed".
        fields["result"] = (_clean(payload.get("result")) or "").upper()
        fields["page_no"] = page_no + 1

    row = db.one("SELECT id FROM hydrotest WHERE project_id=? AND document_id=?",
                 (project_id, target.document_id))
    with db.tx() as c:
        if row:
            if fields:
                assignments = ", ".join(f"{k}=?" for k in fields)
                c.execute(f"UPDATE hydrotest SET {assignments} WHERE id=?",
                          (*fields.values(), row["id"]))
            hydro_id = row["id"]
        else:
            fields.update(project_id=project_id, document_id=target.document_id,
                          fingerprint=target.fingerprint, segment=target.segment,
                          source="hydrotest_vision")
            columns = ", ".join(fields)
            marks = ", ".join("?" * len(fields))
            cur = c.execute(f"INSERT INTO hydrotest({columns}) VALUES({marks})",
                            tuple(fields.values()))
            hydro_id = cur.lastrowid

    readings = payload.get("readings") or []
    if page_type == "test_record":
        rows = [
            (hydro_id, i, _clean(r.get("time")), _number(r.get("pressure_psig")),
             _clean(r.get("ambient_temp")))
            for i, r in enumerate(readings)
        ]
        with db.tx() as c:
            c.execute("DELETE FROM hydrotest_reading WHERE hydrotest_id=?", (hydro_id,))
            if rows:
                c.executemany(
                    """INSERT INTO hydrotest_reading
                       (hydrotest_id, seq, reading_time, pressure, ambient)
                       VALUES (?,?,?,?,?)""",
                    rows,
                )
    return 1


def _apply_coating(db: Database, project_id: int, target: Target,
                   payload: dict, page_no: int) -> int:
    """Create one coating report, with its readings, coats and instruments.

    Unlike a pressure test package, each page here is a whole separate day's
    report — the multi-day bundles hold one form per page — so pages become
    rows rather than merging onto one.
    """
    if not payload.get("page_is_coating_report"):
        return 0

    from ..aml import parse_nps
    from ..instruments import serial_key

    fields = {
        "project_id": project_id, "document_id": target.document_id,
        "fingerprint": target.fingerprint, "segment": target.segment,
        "page_no": page_no + 1,
        "report_date": _parse_date(payload.get("report_date")),
        "line_size": _clean(payload.get("line_size")),
        "line_nps": parse_nps(payload.get("line_size")),
        "material": _clean(payload.get("material")).upper(),
        "service": _clean(payload.get("service")),
        "contractor": _clean(payload.get("contractor")),
        "inspector": _clean(payload.get("inspector")),
        "start_station": _clean(payload.get("starting_station")),
        "end_station": _clean(payload.get("ending_station")),
        "blast_media": _clean(payload.get("blast_media")),
        "cleanliness": _clean(payload.get("cleanliness_standard")),
        "profile_reqd": _number(payload.get("profile_required")),
        "welds_coated": _number(payload.get("total_welds_coated")),
        "jeep_from": _clean(payload.get("jeeped_from_station")),
        "jeep_to": _clean(payload.get("jeeped_to_station")),
        "comments": _clean(payload.get("comments")),
        "source": COATING_SOURCE,
    }

    # Replaying a page replaces its own report; the children cascade.
    with db.tx() as c:
        c.execute(
            "DELETE FROM coating_report WHERE project_id=? AND document_id=? "
            "AND page_no=? AND source=?",
            (project_id, target.document_id, page_no + 1, COATING_SOURCE),
        )
        columns = ", ".join(fields)
        marks = ", ".join("?" * len(fields))
        cur = c.execute(f"INSERT INTO coating_report({columns}) VALUES({marks})",
                        tuple(fields.values()))
        report_id = cur.lastrowid

        c.executemany(
            """INSERT INTO coating_environment
               (report_id, seq, reading_time, air_temp, humidity, steel_temp, dew_point)
               VALUES (?,?,?,?,?,?,?)""",
            [(report_id, i, _clean(r.get("time")), _number(r.get("air_temp_f")),
              _number(r.get("relative_humidity")), _number(r.get("steel_temp_f")),
              _number(r.get("dew_point_f")))
             for i, r in enumerate(payload.get("environmental") or [])],
        )
        c.executemany(
            "INSERT INTO coating_profile(report_id, seq, mils) VALUES (?,?,?)",
            [(report_id, i, _number(v))
             for i, v in enumerate(payload.get("profile_readings") or [])
             if _number(v) is not None],
        )
        c.executemany(
            """INSERT INTO coating_coat
               (report_id, seq, nde_weld_no, nde_id, manufacturer, product,
                color, batch_a, batch_b, method, wft, dft, layer)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(report_id, i, (raw := _cell(r.get("nde_weld_no"))),
              _coating_weld_id(raw),
              _cell(r.get("manufacturer")), _cell(r.get("product")),
              _cell(r.get("color")), _cell(r.get("batch_a")),
              _cell(r.get("batch_b")), _cell(r.get("application_method")),
              _number(r.get("wft_mils")), _number(r.get("dft_mils")),
              _clean(r.get("dft_layer")).lower())
             for i, r in enumerate(payload.get("coats") or [])],
        )
        c.executemany(
            """INSERT INTO coating_instrument(report_id, kind, serial, serial_key)
               VALUES (?,?,?,?)""",
            [(report_id, _clean(r.get("role")), _clean(r.get("serial")),
              serial_key(r.get("serial")))
             for r in payload.get("instruments") or [] if _clean(r.get("serial"))],
        )
    return 1


def _coating_weld_id(raw: str) -> str:
    """The NDE Weld # column, normalised to the project-wide spelling.

    The coating form writes it with a space — `GXR 048` — where the reader
    sheets and weld maps write `GXR-048`, so without this the column joins to
    nothing.  The isometric grammar already handles both spellings and rejects
    everything that is not an identifier, which is what a hand-filled column
    needs.
    """
    from ..weldmap import format_id, parse_id_token

    parsed = parse_id_token(raw)
    return format_id(*parsed) if parsed else ""


#: Coating reports recovered from a scan. There is no deterministic path to
#: these — the form is hand-filled and its text layer is OCR noise — so the
#: tag exists for symmetry and for clearing them apart from anything later.
COATING_SOURCE = "coating_vision"


#: Releases recovered from a scan. Every one of these documents is a scan;
#: there is no deterministic path to them at all.
BACKFILL_SOURCE = "backfill_vision"


def _apply_backfill(db: Database, project_id: int, target: Target,
                    payload: dict, page_no: int) -> int:
    """One release for backfill. Each page of a bundle is its own release."""
    if not payload.get("page_is_release"):
        return 0

    dates = {
        "inspector_date": _parse_date(payload.get("inspector_date")),
        "contractor_date": _parse_date(payload.get("contractor_date")),
        "survey_date": _parse_date(payload.get("survey_date")),
    }
    # The ditch could be closed from the moment the first party signed, so the
    # earliest date is the one everything else is measured against. Taking the
    # latest would let a late counter-signature excuse a weld made in between.
    signed = sorted(d for d in dates.values() if d)

    fields = {
        "project_id": project_id, "document_id": target.document_id,
        "fingerprint": target.fingerprint, "segment": target.segment,
        "page_no": page_no + 1,
        "line_size": _clean(payload.get("line_size")),
        "wall": _clean(payload.get("wall")),
        "material": _clean(payload.get("material")).upper(),
        "yield_grade": _clean(payload.get("yield_grade")).upper(),
        "service": _clean(payload.get("service")),
        "from_station": _clean(payload.get("from_station")),
        "to_station": _clean(payload.get("to_station")),
        "inspector_signed": int(bool(payload.get("inspector_signed"))),
        "contractor_signed": int(bool(payload.get("contractor_signed"))),
        "survey_signed": int(bool(payload.get("survey_signed"))),
        "released_on": signed[0] if signed else "",
        "source": BACKFILL_SOURCE,
        **dates,
    }

    with db.tx() as c:
        c.execute(
            "DELETE FROM backfill_release WHERE project_id=? AND document_id=? "
            "AND page_no=? AND source=?",
            (project_id, target.document_id, page_no + 1, BACKFILL_SOURCE),
        )
        columns = ", ".join(fields)
        marks = ", ".join("?" * len(fields))
        c.execute(f"INSERT INTO backfill_release({columns}) VALUES({marks})",
                  tuple(fields.values()))
    return 1


def _apply_calibration(db: Database, project_id: int, target: Target,
                       payload: dict, page_no: int) -> int:
    from ..instruments import kind_of

    serial = _clean(payload.get("instrument_sn"))
    calibrated = _parse_date(payload.get("calibration_date"))
    if not serial:
        return 0
    with db.tx() as c:
        c.execute(
            "DELETE FROM instrument_cal WHERE project_id=? AND document_id=? AND page_no=?",
            (project_id, target.document_id, page_no + 1),
        )
        c.execute(
            """INSERT INTO instrument_cal
               (project_id, document_id, kind, serial, serial_key, calibrated,
                description, page_no, evidence, source)
               VALUES (?,?,?,?,?,?,?,?,'vision','hydrotest_vision')""",
            (project_id, target.document_id, kind_of(target.filename), serial,
             serial_key(serial), calibrated, target.filename, page_no + 1),
        )
    return 1


#: Instrument serials normalise the same way whether they came off a
#: certificate filename or a scanned form, so both sides share one function.
from ..instruments import serial_key  # noqa: E402  (re-exported for callers)


def _number(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


_TIME = re.compile(r"(\d{1,2})\s*:\s*(\d{2})\s*([ap])\.?m?\.?", re.IGNORECASE)


def _parse_datetime(value) -> str:
    """ISO datetime from '8/18/25 7:00am'; falls back to the date alone."""
    from datetime import datetime as _dt

    day = _parse_date(value)
    if not day:
        return ""
    m = _TIME.search(str(value))
    if not m:
        return day
    hour, minute, half = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    if hour == 12:
        hour = 0
    if half == "p":
        hour += 12
    if hour > 23 or minute > 59:
        return day
    return _dt.fromisoformat(day).replace(hour=hour, minute=minute).isoformat(" ")


def _apply_welder_cert(db: Database, project_id: int, target: Target,
                       payload: dict) -> int:
    """Write a qualification record's scope onto its ``welder_cert`` row."""
    if not payload.get("page_is_qualification_record"):
        return 0

    row = db.one(
        "SELECT id, stencil FROM welder_cert WHERE project_id=? AND document_id=?",
        (project_id, target.document_id),
    )
    if not row:
        return 0

    fields = {
        "name": _clean(payload.get("welder_name")),
        "code": _clean(payload.get("code")),
        "result": (_clean(payload.get("result")) or "").upper(),
        "process": _clean(payload.get("processes_tested")),
        "test_position": _clean(payload.get("test_position")),
        "progression": _clean(payload.get("progression")),
        "test_od": _clean(payload.get("test_od")),
        "test_wall": _clean(payload.get("test_wall")),
        "qual_process": _clean(payload.get("qual_process")),
        "qual_position": _clean(payload.get("qual_position")),
        "qual_diameter": _clean(payload.get("qual_diameter")),
        "qual_thickness": _clean(payload.get("qual_thickness")),
        "f_number": _clean(payload.get("f_number")),
        "qualifier_name": _clean(payload.get("qualifier_name")),
        "qualifier_cwi": _clean(payload.get("qualifier_cert_number")),
        "qualifier_expiry": _parse_date(payload.get("qualifier_cert_expiry")),
        "evidence": "vision",
    }
    if wps := _clean(payload.get("wps")):
        fields["wps"] = wps
    if date := _parse_date(payload.get("test_date")):
        fields["cert_date"] = date
    # A stencil read off the page is only adopted where the filename gave none:
    # filename text is exact, a hand-written stencil has been through OCR.
    stencil = _clean(payload.get("stencil")).upper()
    if stencil and not row["stencil"]:
        fields["stencil"] = stencil

    assignments = ", ".join(f"{k}=?" for k in fields)
    with db.tx() as c:
        c.execute(f"UPDATE welder_cert SET {assignments} WHERE id=?",
                  (*fields.values(), row["id"]))
    return 1


_DATE_ANY = re.compile(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})")


def _parse_date(value) -> str:
    """ISO date from the many ways these forms write one."""
    from datetime import date as _date

    m = _DATE_ANY.search(str(value or ""))
    if not m:
        return ""
    mm, dd, yy = (int(g) for g in m.groups())
    if yy < 100:
        yy += 2000
    try:
        return _date(yy, mm, dd).isoformat()
    except ValueError:
        return ""


_RESULTS = {"ACC": "ACC", "REJ": "REJ"}


def _record_sheet_facts(db: Database, project_id: int, target: Target,
                        payload: dict, page_no: int) -> None:
    """What the sheet says about itself, as the model read it.

    Supersedes whatever the text pass got for the same page. Both figures sit
    on scans — the weld count on Precision Group reports, whose OCR layer keeps
    it more reliably than it keeps the results, and the pagination on sheets
    with no text layer at all, which are the ones most likely to be filed a
    page at a time in the first place.
    """
    def positive(name: str) -> int | None:
        value = payload.get(name)
        return value if isinstance(value, int) and value > 0 else None

    count = positive("weld_count")
    page = positive("page_number")
    total = positive("page_total")
    ticket = _clean(payload.get("ticket_no"))
    if count is None and total is None:
        return
    with db.tx() as c:
        c.execute(
            """DELETE FROM reader_sheet
               WHERE project_id=? AND fingerprint=? AND page_no=?""",
            (project_id, target.fingerprint, page_no + 1),
        )
        c.execute(
            """INSERT INTO reader_sheet
               (project_id, document_id, fingerprint, filename, segment,
                page_no, weld_count, ticket, stated_page, stated_pages,
                evidence)
               VALUES (?,?,?,?,?,?,?,?,?,?,'vision')""",
            (project_id, target.document_id, target.fingerprint,
             target.filename, target.segment, page_no + 1, count, ticket,
             page, total),
        )


def _one_row_per_weld(rows: list[dict]) -> list[dict]:
    """Collapse a weld's several assessed areas into one row, reject winning.

    A radiograph goes round the pipe in overlapping exposures, and the
    Precision Group form assesses each separately: ``FTI-039`` is three rows —
    area 0-A accepted, area A-B **rejected** on an elongated slag inclusion,
    area B-0 accepted. A weld with a rejected area is a rejected weld.

    Order is what makes this necessary rather than tidy. The rows arrive top to
    bottom and each upserts onto the same ``(project, nde_id, fingerprint)``,
    so the last one written wins — and for FTI-039 the last one says accepted.
    The reject would be overwritten by the row beneath it and NDE-10 would
    never see the one thing on the page worth reporting.
    """
    merged: dict[str, dict] = {}
    for row in rows:
        key = _clean(row.get("weld_id")).upper()
        if not key:
            continue
        first = merged.setdefault(key, dict(row))
        if _RESULTS.get((row.get("result") or "").upper()) == "REJ":
            # Keep the rejecting area's own evidence with it - the indication
            # and its size are what the auditor needs to chase.
            first["result"] = row.get("result")
            for carry in ("indications", "remarks", "area"):
                if row.get(carry):
                    first[carry] = row[carry]
        elif not first.get("result"):
            first["result"] = row.get("result")
        # A later area often repeats nothing but the assessment; keep whatever
        # detail any of them carried.
        for carry in ("pipe_diameter", "wall_thickness", "welder_stencil"):
            if not first.get(carry) and row.get(carry):
                first[carry] = row[carry]
    return list(merged.values())


def _apply_reader_sheet(db: Database, project_id: int, target: Target,
                        payload: dict, page_no: int) -> int:
    """Write a sheet's per-weld results back onto the shots it covers."""
    if not payload.get("page_is_reader_sheet"):
        return 0

    technician = _clean(payload.get("technician"))
    # The date the sheet itself prints, else the one in its filename. Without
    # this a vision-read sheet has no date at all: the row it creates here is
    # the only one there is, and it was being inserted with a hardcoded NULL
    # even though the schema asks the model for the date and it comes back.
    sheet_date = (_iso_date(_clean(payload.get("sheet_date")))
                  or filename_date(target.filename))
    _record_sheet_facts(db, project_id, target, payload, page_no)
    updated = 0

    rows = _one_row_per_weld(payload.get("rows") or [])
    for row in rows:
        raw_id = _clean(row.get("weld_id"))
        if not raw_id:
            continue
        ids = parse_ids(raw_id)
        if not ids:
            continue
        nde_id = str(ids[0])
        result = _RESULTS.get((row.get("result") or "").upper())

        existing = db.one(
            """SELECT id FROM nde_shot
               WHERE project_id=? AND nde_id=? AND fingerprint=?""",
            (project_id, nde_id, target.fingerprint),
        )
        values = {
            "result": result or "",
            "welder": _clean(row.get("welder_stencil")),
            "pipe_size": _clean(row.get("pipe_diameter")),
            "wall_thk": _clean(row.get("wall_thickness")),
            "technician": technician,
            "page_no": page_no + 1,
            "evidence": "vision",
            "confidence": 1.0 if result else 0.6,
        }
        if sheet_date:
            # Only when we have one — a null here would wipe a date the text
            # layer or the filename already established.
            values["sheet_date"] = sheet_date
        with db.tx() as c:
            if existing:
                assignments = ", ".join(f"{k}=?" for k in values)
                c.execute(
                    f"UPDATE nde_shot SET {assignments} WHERE id=?",
                    (*values.values(), existing["id"]),
                )
            else:
                # The sheet carries a shot its filename never advertised -
                # itself worth recording, since filename-only gap analysis
                # would otherwise call this shot missing.
                c.execute(
                    """INSERT INTO nde_shot
                       (project_id, document_id, fingerprint, copies, segments,
                        segment, nde_id, prefix, number, suffix, sheet_date,
                        result, welder, pipe_size, wall_thk, technician,
                        page_no, evidence, confidence)
                       VALUES (?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (project_id, target.document_id, target.fingerprint,
                     target.segment, target.segment, nde_id, ids[0].prefix,
                     ids[0].number, ids[0].suffix, sheet_date or None,
                     values["result"], values["welder"], values["pipe_size"],
                     values["wall_thk"], technician, values["page_no"],
                     "vision", values["confidence"]),
                )
        updated += 1
    return updated
