"""NDE versus weld map reconciliation.

The question this answers is the one an auditor actually asks: *for every weld
that was made, is there a filed NDE report, and for every NDE report on file,
is there a weld it belongs to?*

Two facts about how these books are kept shape every rule here.

**Reader sheets are shared between books.**  A crew shooting a spread that
serves several lines files the same sheet into every affected segment folder -
``GFB-13 11-12-25.pdf`` appears eleven times across six segments.  So the set
of shots on file is computed project-wide, over content fingerprints, and the
natural scope for a sequence is the NDE *series* (the ``GFB`` in ``GFB-013``)
rather than the folder a copy happens to sit in.

**Daily weld reports record the NDE id inconsistently.**  Across this corpus
the NOTES column carries an id for anywhere between 0% and 29% of welds
depending on the segment.  Rules that need that link are therefore gated on
whether the segment demonstrably keeps it; where it does not, the auditor gets
one finding saying the link cannot be verified, not one per weld.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from ..db import Database
from ..ids import NdeId, cutout_series, gaps, sequences
from . import Finding, register

# Status text that means the shot passed.  Everything else is treated as
# unresolved until proven otherwise - the safe direction for an audit.
_ACCEPTED = re.compile(r"\b(accept|acc|pass|satisfactory|ok)\b", re.IGNORECASE)
_REJECTED = re.compile(r"\b(reject|rej|fail|unacceptable|cut ?out)\b", re.IGNORECASE)

#: Below this fraction of welds carrying an NDE id, a segment's weld reports
#: are not keeping the link at all and per-weld rules would be pure noise.
LINK_THRESHOLD = 0.60

#: A register has to record at least this share of the segment's largest
#: register before it is used as a yardstick for judging another document.
#: One isometric out of a job's forty is evidence of what it shows, never
#: evidence of what the rest of the job is missing.
REPRESENTATIVE_SHARE = 0.20

#: Once this fraction of a series is missing from the package the gap is
#: systemic, and reporting it joint by joint restates one fact hundreds of
#: times. GL 31's maps balloon AFB-001 to AFB-296 and the book holds reader
#: sheets to AFB-042: one finding, not one hundred and eighty-seven.
SERIES_GAP_SHARE = 0.50


def _detail(**kw) -> str:
    return json.dumps({k: v for k, v in kw.items() if v not in (None, "")})


# ---------------------------------------------------------------------------
# Shared views
# ---------------------------------------------------------------------------


def _shots_on_file(db: Database, project_id: int) -> dict[str, list]:
    """``{nde_id: [rows]}`` for every distinct sheet, project-wide.

    Project-wide rather than per-segment because sheets are deliberately filed
    into several books at once.
    """
    rows = db.q(
        """SELECT s.nde_id, s.segment, s.segments, s.prefix, s.number, s.suffix,
                  s.sheet_date, s.copies, s.fingerprint, s.document_id, d.filename
           FROM nde_shot s LEFT JOIN document d ON d.id = s.document_id
           WHERE s.project_id=?""",
        (project_id,),
    )
    out: dict[str, list] = defaultdict(list)
    for r in rows:
        out[r["nde_id"]].append(r)
    return out


def _series_groups(db: Database, project_id: int) -> dict[tuple[str, int], list]:
    """Split each NDE prefix into independently numbered series.

    A prefix is not unique across a project.  ``GFB`` numbers 1-89 on the 20"
    LP line and, separately, 1-19 on the Flexsteel spread.  Treating them as
    one series invents dozens of phantom gaps and conflicts.

    Sheets belong to the same series when they share the prefix *and* their
    filing locations overlap - a crew files its sheets into the same set of
    books throughout a spread.  Grouping is by connected component, so a series
    still holds together when one sheet is filed into a book the others missed.
    """
    rows = db.q(
        """SELECT s.nde_id, s.prefix, s.number, s.suffix, s.segments, s.segment,
                  s.sheet_date, s.fingerprint, s.copies, s.document_id, d.filename
           FROM nde_shot s LEFT JOIN document d ON d.id = s.document_id
           WHERE s.project_id=?""",
        (project_id,),
    )

    by_prefix: dict[str, list] = defaultdict(list)
    for r in rows:
        by_prefix[r["prefix"]].append(r)

    groups: dict[tuple[str, int], list] = {}
    for prefix, shots in by_prefix.items():
        # One node per distinct sheet; union nodes whose segment sets overlap.
        sheets: dict[str, set[str]] = {}
        for r in shots:
            fp = r["fingerprint"] or str(r["document_id"])
            segs = {s for s in (r["segments"] or "").split("; ") if s}
            sheets.setdefault(fp, set()).update(segs)

        parent: dict[str, str] = {fp: fp for fp in sheets}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        # Index sheets by segment so the union is linear rather than quadratic.
        by_segment: dict[str, list[str]] = defaultdict(list)
        for fp, segs in sheets.items():
            for seg in segs:
                by_segment[seg].append(fp)
        for members in by_segment.values():
            for other in members[1:]:
                union(members[0], other)

        # Sheets filed nowhere identifiable each form their own component.
        roots = sorted({find(fp) for fp in sheets})
        index = {root: i for i, root in enumerate(roots)}
        for r in shots:
            fp = r["fingerprint"] or str(r["document_id"])
            groups.setdefault((prefix, index[find(fp)]), []).append(r)
    return groups


def series_label(prefix: str, shots: list) -> str:
    """Human-readable name for a series, e.g. ``GFB (20 LP)``."""
    segs: set[str] = set()
    for r in shots:
        segs.update(s for s in (r["segments"] or "").split("; ") if s)
    if not segs:
        return prefix
    if len(segs) == 1:
        return f"{prefix} ({next(iter(segs))})"
    return f"{prefix} ({sorted(segs)[0]} +{len(segs) - 1} more)"


def link_quality(db: Database, project_id: int) -> dict[tuple[str, str], tuple[int, int]]:
    """``{(segment, register): (welds, welds carrying an NDE id)}``.

    Measured per *register*, not per segment. Whether welds are numbered is a
    property of the document type: a weld map numbers every joint, while these
    daily reports frequently number none. Pooling both in one segment figure
    makes the daily reports look like negligent outliers inside a
    well-numbered segment, and fires NDE-01 on every one of them.
    """
    from .registers import REGISTER_OF

    out: dict[tuple[str, str], tuple[int, int]] = {}
    for r in db.q(
        """SELECT segment, source, COUNT(*) n,
                  SUM(CASE WHEN nde_id<>'' THEN 1 ELSE 0 END) linked
           FROM weld WHERE project_id=? GROUP BY segment, source""",
        (project_id,),
    ):
        key = (r["segment"], REGISTER_OF.get(r["source"], r["source"] or "welds"))
        n, linked = out.get(key, (0, 0))
        out[key] = (n + r["n"], linked + (r["linked"] or 0))
    return out


def _well_linked(db: Database, project_id: int) -> set[tuple[str, str]]:
    """The (segment, register) pairs fit to judge the NDE package against.

    Keeping the NDE link is necessary but not sufficient, and the second test
    matters most for weld maps.  A map's balloon *is* the report number, so a
    map register is always 100% linked however little of the segment it
    covers — and Bluewater has two isometrics with a text layer against nineteen
    hundred welds recorded on daily reports.  Judging fourteen hundred reader
    sheets against those ten welds reported two hundred and seventy-five
    sheets as orphaned, all of them because the yardstick was 0.5% of the job.
    """
    quality = link_quality(db, project_id)

    largest: dict[str, int] = {}
    for (segment, _register), (n, _linked) in quality.items():
        largest[segment] = max(largest.get(segment, 0), n)

    return {
        key for key, (n, linked) in quality.items()
        if n and linked / n >= LINK_THRESHOLD
        and n >= largest[key[0]] * REPRESENTATIVE_SHARE
    }


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


@register("NDE-00", "Weld reports do not record NDE report numbers")
def link_not_kept(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """Flag, once per segment, that the weld-map-to-NDE link is not being kept.

    This is a finding in its own right - the turnover package cannot be
    reconciled by anyone, not just by this tool - and it explains why the
    per-weld rules stay silent for that segment.
    """
    # Where a project keeps no reader sheets at all, the NDE package lives
    # somewhere else entirely and "the link is missing" would be misleading.
    has_sheets = db.one("SELECT 1 FROM nde_shot WHERE project_id=? LIMIT 1", (project_id,))
    if not has_sheets:
        return []

    findings: list[Finding] = []
    for (segment, reg), (n, linked) in sorted(link_quality(db, project_id).items()):
        if not n or linked / n >= LINK_THRESHOLD:
            continue
        findings.append(
            {
                "project_id": project_id, "run_id": run_id, "rule": "NDE-00",
                "severity": "major", "segment": segment,
                "subject": f"{reg}: NDE link",
                "message": (
                    f"Only {linked} of {n} welds ({round(100 * linked / n)}%) on "
                    f"{reg} for this segment record an NDE report number. Those "
                    f"welds cannot be reconciled against the NDE package until the "
                    f"weld numbers are filled in."
                ),
                "detail": _detail(register=reg, welds=n, linked=linked,
                                  threshold=LINK_THRESHOLD),
                "document_id": None, "page_no": None,
            }
        )
    return findings


@register("NDE-01", "Weld has no NDE reference")
def weld_without_nde(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A weld on a map that otherwise keeps NDE ids, but names none itself."""
    from .registers import REGISTER_OF

    good = _well_linked(db, project_id)
    if not good:
        return []

    # A weld counts as uninspected only when *no* evidence of inspection exists.
    # Shop welds are routinely signed off visually with a date and a result but
    # no report number of their own - that is a complete record, not a gap.
    rows = db.q(
        """SELECT w.id, w.segment, w.line, w.weld_no, w.weld_size, w.date_welded,
                  w.document_id, w.source, d.filename
           FROM weld w LEFT JOIN document d ON d.id = w.document_id
           WHERE w.project_id=?
             AND IFNULL(w.nde_id,'')     = ''
             AND IFNULL(w.nde_report,'') = ''
             AND IFNULL(w.nde_date,'')   = ''
             AND IFNULL(w.nde_status,'') = ''""",
        (project_id,),
    )
    return [
        {
            "project_id": project_id, "run_id": run_id, "rule": "NDE-01",
            "severity": "critical", "segment": r["segment"],
            "subject": f"Weld {r['weld_no']}",
            "message": (
                f"Weld {r['weld_no']} on {r['line'] or r['segment']} records no NDE "
                f"report, though the rest of this segment's welds do. Recorded in "
                f"{r['filename'] or r['source']}."
            ),
            "detail": _detail(
                line=r["line"], size=r["weld_size"], date_welded=r["date_welded"],
                source=r["source"], report=r["filename"],
            ),
            "document_id": r["document_id"], "page_no": None,
        }
        for r in rows
        # Judged against the welder's own register: a weld map that numbers
        # every joint says nothing about a daily report that numbers none.
        if (r["segment"], REGISTER_OF.get(r["source"], r["source"] or "welds")) in good
    ]


@register("NDE-02", "NDE report cited but no reader sheet on file")
def nde_without_sheet(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A weld map cites a report that no sheet anywhere in the project carries.

    This is the core turnover gap: the log says the shot was taken, but the
    evidence is not in the package.  Checked project-wide, so a sheet filed
    under a neighbouring segment still counts as present.

    Where most of a series is missing the gap stops being a list of joints and
    becomes one fact about the package, and is reported that way — see
    ``SERIES_GAP_SHARE``.
    """
    on_file = _shots_on_file(db, project_id)
    if not on_file:
        return []

    rows = db.q(
        """SELECT w.segment, w.line, w.weld_no, w.nde_id, w.nde_date,
                  w.nde_technique, w.document_id, w.source, d.filename
           FROM weld w LEFT JOIN document d ON d.id = w.document_id
           WHERE w.project_id=? AND w.nde_id <> ''""",
        (project_id,),
    )

    # Group by series before judging: whether a missing report is one slip or
    # part of a systemic gap is a property of the series, not of the weld.
    cited: dict[tuple[str, str], list] = defaultdict(list)
    seen: set[str] = set()
    for r in rows:
        if r["nde_id"] in seen:
            continue
        seen.add(r["nde_id"])
        cited[(r["segment"], r["nde_id"].split("-")[0])].append(r)

    findings: list[Finding] = []
    for (segment, prefix), series in sorted(cited.items()):
        missing = [r for r in series if r["nde_id"] not in on_file]
        if not missing:
            continue

        if len(missing) / len(series) >= SERIES_GAP_SHARE and len(missing) > 3:
            ids = sorted(r["nde_id"] for r in missing)
            findings.append({
                "project_id": project_id, "run_id": run_id, "rule": "NDE-02",
                "severity": "critical", "segment": segment,
                "subject": f"{prefix} series",
                "message": (
                    f"{len(missing)} of the {len(series)} {prefix} reports cited "
                    f"on this segment have no reader sheet filed anywhere in the "
                    f"project — {ids[0]} through {ids[-1]}. The gap runs through "
                    f"the whole series rather than a few joints, so the NDE "
                    f"package is incomplete for this line rather than mislaid."
                ),
                "detail": _detail(series=prefix, cited=len(series),
                                  missing=len(missing),
                                  ids=", ".join(ids[:40])),
                "document_id": missing[0]["document_id"], "page_no": None,
            })
            continue

        for r in missing:
            findings.append({
                "project_id": project_id, "run_id": run_id, "rule": "NDE-02",
                "severity": "critical", "segment": r["segment"],
                "subject": r["nde_id"],
                "message": (
                    f"Weld {r['weld_no']} cites NDE report {r['nde_id']}, but no "
                    f"reader sheet for {r['nde_id']} is filed anywhere in this project."
                ),
                "detail": _detail(
                    weld_no=r["weld_no"], line=r["line"], nde_date=r["nde_date"],
                    technique=r["nde_technique"], report=r["filename"],
                ),
                "document_id": r["document_id"], "page_no": None,
            })
    return findings


@register("NDE-03", "Reader sheet with no matching weld")
def sheet_without_weld(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A filed sheet whose shot id appears on no weld map.

    Only meaningful where the weld maps actually keep NDE ids, so this is
    limited to series whose welds come from well-linked segments.
    """
    from .registers import REGISTER_OF

    good = _well_linked(db, project_id)
    if not good:
        return []

    claimed = {
        r["nde_id"]
        for r in db.q(
            "SELECT DISTINCT nde_id FROM weld WHERE project_id=? AND nde_id<>''",
            (project_id,),
        )
    }
    # Only judge series a well-linked register actually uses; otherwise every
    # sheet belonging to an unnumbered register looks like an orphan.
    good_prefixes = {
        r["nde_id"].split("-")[0]
        for r in db.q(
            "SELECT DISTINCT nde_id, segment, source FROM weld "
            "WHERE project_id=? AND nde_id<>''",
            (project_id,),
        )
        if (r["segment"],
            REGISTER_OF.get(r["source"], r["source"] or "welds")) in good
    }

    rows = db.q(
        """SELECT s.nde_id, s.prefix, s.segment, s.segments, s.document_id, d.filename
           FROM nde_shot s LEFT JOIN document d ON d.id = s.document_id
           WHERE s.project_id=?""",
        (project_id,),
    )

    # The registers have to account for most of a series before their silence
    # about a sheet means anything. Bluewater's only weld map with a text layer
    # covers one segment and names ten welds, while the job holds fourteen
    # hundred reader sheets in the same four series - judging those sheets
    # against those ten welds reported two hundred and seventy-five orphans,
    # every one of them a statement about the map's size rather than the
    # sheet. Whether a series is thinly covered is the coverage table's job.
    on_file = Counter(r["prefix"] for r in rows)
    covered = Counter(nde_id.split("-")[0] for nde_id in claimed)
    good_prefixes = {
        prefix for prefix in good_prefixes
        if on_file[prefix] and covered[prefix] / on_file[prefix] >= SERIES_GAP_SHARE
    }

    seen: set[str] = set()
    findings: list[Finding] = []
    for r in rows:
        if r["prefix"] not in good_prefixes or r["nde_id"] in claimed or r["nde_id"] in seen:
            continue
        seen.add(r["nde_id"])
        findings.append(
            {
                "project_id": project_id, "run_id": run_id, "rule": "NDE-03",
                "severity": "major", "segment": r["segment"],
                "subject": r["nde_id"],
                "message": (
                    f"Reader sheet for {r['nde_id']} is filed, but no weld on any "
                    f"weld map references it. Either the weld map is incomplete or "
                    f"the sheet belongs to another job."
                ),
                "detail": _detail(filename=r["filename"], filed_under=r["segments"]),
                "document_id": r["document_id"], "page_no": None,
            }
        )
    return findings


@register("NDE-04", "Gap in NDE report sequence")
def sequence_gap(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A hole in a consecutively numbered series of shots.

    Shots are numbered as they are taken, so ``FXR-080`` followed by
    ``FXR-085`` means four sheets were never filed - regardless of what the
    weld map does or does not say.  This is the strongest signal in the whole
    rule set because it needs no weld map at all.

    Scoped per independently numbered series, so the ``GFB`` run on the 20" LP
    line is not compared against the unrelated ``GFB`` run on the Flexsteel
    spread.

    A cut-out borrows the number of the weld it removed, so it neither extends
    the run nor leaves a hole in it; :func:`weldaudit.ids.gaps` is where that
    is worked out.
    """
    findings: list[Finding] = []
    for (prefix, _idx), shots in sorted(_series_groups(db, project_id).items()):
        ids = [NdeId(r["prefix"], r["number"], r["suffix"] or "") for r in shots]
        missing_ids = gaps(ids)
        if not missing_ids:
            continue
        label = series_label(prefix, shots)
        segs = sorted({s for r in shots for s in (r["segments"] or "").split("; ") if s})
        # Quote the run the gaps were measured against, not the outermost shot
        # on file - a trailing cut-out is outside the run by design.
        lo, hi, _count = sequences(ids)[prefix]
        for missing in missing_ids:
            findings.append(
                {
                    "project_id": project_id, "run_id": run_id, "rule": "NDE-04",
                    "severity": "major",
                    "segment": segs[0] if segs else label,
                    "subject": str(missing),
                    "message": (
                        f"No reader sheet filed for {missing} in the {label} series. "
                        f"That series runs {prefix}-{lo:03d} to {prefix}-{hi:03d}, so "
                        f"the shot was taken but the sheet is missing from the package."
                    ),
                    "detail": _detail(
                        series=label, number=missing.number,
                        series_range=f"{prefix}-{lo:03d}..{prefix}-{hi:03d}",
                        filed_under="; ".join(segs[:6]),
                    ),
                    "document_id": None, "page_no": None,
                }
            )
    return findings


@register("NDE-12", "Cut-out series, not gap-checked")
def cutout_only_series(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A prefix under which every filed sheet is a cut-out.

    Reported so the silence is deliberate rather than accidental.  These series
    look alarming in a sequence check - ``GCFB`` holds four sheets numbered 31,
    37, 39 and 114 - and the eighty absent numbers between them are welds that
    were simply never cut out.  NDE-04 says nothing about them, and an auditor
    reading the report is entitled to know that was a decision.

    The finding also lists the numbers themselves, because that *is* the
    audit-worthy content: this is the register of welds removed from the line.
    """
    findings: list[Finding] = []
    for (prefix, _idx), shots in sorted(_series_groups(db, project_id).items()):
        ids = [NdeId(r["prefix"], r["number"], r["suffix"] or "") for r in shots]
        if prefix not in cutout_series(ids):
            continue
        numbers = sorted({i.number for i in ids})
        label = series_label(prefix, shots)
        segs = sorted({s for r in shots for s in (r["segments"] or "").split("; ") if s})
        shown = ", ".join(f"{prefix}-{n:03d}" for n in numbers[:8])
        if len(numbers) > 8:
            shown += f" +{len(numbers) - 8} more"
        findings.append(
            {
                "project_id": project_id, "run_id": run_id, "rule": "NDE-12",
                "severity": "info",
                "segment": segs[0] if segs else label,
                "subject": label,
                "message": (
                    f"Every sheet filed under {label} is a cut-out "
                    f"({len(numbers)} of them: {shown}). A cut-out is named for "
                    f"the weld it removed, so these numbers are borrowed from "
                    f"the line's own run and the spaces between them are welds "
                    f"that were never cut out. Not checked for sequence gaps."
                ),
                "detail": _detail(
                    series=label, cut_outs=len(numbers),
                    numbers=", ".join(str(n) for n in numbers),
                    filed_under="; ".join(segs[:6]),
                ),
                "document_id": None, "page_no": None,
            }
        )
    return findings


@register("NDE-13", "Sheet examined more welds than the package accounts for")
def weld_count_short(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A reader sheet's own count of welds against the shots attributed to it.

    The Precision Group form prints ``Weld Count`` at the foot of each report,
    and it is the only self-check of its kind in the corpus: everywhere else,
    how many welds a sheet covers is inferred from its filename or from the
    rows that could be read off it. Here the sheet says so itself.

    **The comparison is one-sided, and that is the whole design.** A bundle
    holds several reports — ``FAB COMBINED.pdf`` is fifteen pages stating 18
    welds and 42 — and there is no way to know every count was found, because
    these pages are scans and the count is read through OCR. So the sum is a
    *lower bound* on welds examined. Fewer shots than that is sound: the
    package cannot account for welds the sheet says were examined. More shots
    than that says nothing at all, because the reports whose counts went
    unread would explain the difference, so the rule stays quiet.
    """
    rows = db.q(
        """SELECT r.fingerprint, MIN(r.document_id) document_id,
                  MIN(r.filename) filename, MIN(r.segment) segment,
                  SUM(r.weld_count) stated, COUNT(*) reports,
                  MIN(r.evidence) evidence
           FROM reader_sheet r
           WHERE r.project_id=? AND r.weld_count > 0
           GROUP BY r.fingerprint""",
        (project_id,),
    )

    findings: list[Finding] = []
    for r in rows:
        attributed = db.one(
            """SELECT COUNT(DISTINCT nde_id) n FROM nde_shot
               WHERE project_id=? AND IFNULL(fingerprint, CAST(document_id AS TEXT))=?""",
            (project_id, r["fingerprint"]),
        )
        found = attributed["n"] if attributed else 0
        if found >= r["stated"]:
            continue
        several = r["reports"] > 1
        findings.append(
            {
                "project_id": project_id, "run_id": run_id, "rule": "NDE-13",
                "severity": "major", "segment": r["segment"] or "",
                "subject": r["filename"],
                "message": (
                    f"{r['filename']} states {r['stated']} welds examined"
                    + (f" across {r['reports']} reports" if several else "")
                    + f", but the package attributes only {found} shot"
                    + ("s" if found != 1 else "")
                    + ". Either shots are missing from the package or the "
                      "sheet covers welds nothing else names."
                    + (" The stated figure is a lower bound: further reports "
                       "in this bundle may state counts that could not be read."
                       if several else "")
                ),
                "detail": _detail(
                    stated=r["stated"], attributed=found,
                    shortfall=r["stated"] - found, reports=r["reports"],
                    read_by=r["evidence"],
                ),
                "document_id": r["document_id"], "page_no": None,
            }
        )
    return findings


@register("NDE-14", "Reader sheet pages missing from the package")
def missing_report_pages(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A report says how many pages it has; how many of them are on file.

    The crew files a multi-page report as separate PDFs, one page each, so a
    document holding one page that says ``Page 8 of 8`` is not by itself
    evidence of anything — the other seven may be filed alongside it under
    their own names. **The ticket number is what settles it**, being the only
    field that ties a report's pages together once they are split up.

    Two things about the grouping matter, and both came from getting them
    wrong first.

    A page may state its number without stating the ticket. Page 1 of the
    Precision Group radiography report does exactly that, so grouping strictly
    by the ticket printed on each page reported page 1 of ``RT-1061-0794`` as
    missing while it sat in the same PDF as pages 2 and 3. A page with no
    ticket of its own therefore inherits the document's, where the document
    has exactly one.

    And a blank ticket is reported rather than skipped. ``DBR 1P-8 7-16-25``
    says page 3 of 4 and leaves the Ticket No box empty, so its other three
    pages cannot be traced at all — which is a weaker finding than a missing
    page but not nothing, and silently dropping it would be the sort of
    quiet omission this rule set exists to avoid.
    """
    rows = db.q(
        """SELECT fingerprint, document_id, filename, segment, page_no,
                  IFNULL(ticket,'') ticket, stated_page, stated_pages
           FROM reader_sheet
           WHERE project_id=? AND stated_pages > 0""",
        (project_id,),
    )
    if not rows:
        return []

    # A document's ticket, where it prints exactly one.
    tickets_of: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        if r["ticket"]:
            tickets_of[r["fingerprint"]].add(r["ticket"])

    groups: dict[tuple[str, str], list] = defaultdict(list)
    for r in rows:
        known = tickets_of.get(r["fingerprint"], set())
        ticket = r["ticket"] or (next(iter(known)) if len(known) == 1 else "")
        key = ("ticket", ticket) if ticket else ("document", r["fingerprint"])
        groups[key].append(r)

    findings: list[Finding] = []
    for (kind, key), items in sorted(groups.items()):
        stated = max(r["stated_pages"] for r in items)
        have = {r["stated_page"] for r in items if r["stated_page"]}
        missing = [p for p in range(1, stated + 1) if p not in have]
        if not missing:
            continue
        first = items[0]
        names = sorted({r["filename"] for r in items})
        gap = ", ".join(str(p) for p in missing)
        if kind == "ticket":
            message = (
                f"{names[0]} is page {min(have)} of {stated} on ticket {key}, "
                f"and no sheet anywhere in the project carries "
                f"{'the other pages' if len(missing) > 1 else 'the other page'} "
                f"({gap}). The rest of the report is not in the package."
            )
            severity = "major"
        else:
            message = (
                f"{names[0]} says it is page {min(have)} of {stated}, but its "
                f"Ticket No box is blank, so pages {gap} cannot be traced to "
                f"any other sheet. Whether they are filed is unknowable from "
                f"the package."
            )
            severity = "minor"
        findings.append(
            {
                "project_id": project_id, "run_id": run_id, "rule": "NDE-14",
                "severity": severity, "segment": first["segment"] or "",
                "subject": names[0],
                "message": message,
                "detail": _detail(
                    ticket=key if kind == "ticket" else "",
                    states=stated, on_file=len(have),
                    missing_pages=gap,
                    filed_as="; ".join(names[:4]),
                ),
                "document_id": first["document_id"], "page_no": None,
            }
        )
    return findings


#: A ticket's first three digits are its issuing block; the rest is a serial.
_TICKET_BLOCK = 3


@register("NDE-15", "Ticket number is malformed")
def malformed_ticket(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A ticket written to a different length from every other in its block.

    The ticket is the only thing tying a report's pages together once the crew
    files them as separate PDFs, so a mistyped one quietly breaks the join
    NDE-14 depends on — the sheet stops belonging to its own report and
    nothing says so.

    Judged against the project's own numbering rather than a fixed format:
    tickets come in blocks by their first three digits, and within a block
    every one is the same length. Of 314 read off the Bluewater and PLU sheets,
    310 are eight digits. The exceptions are printed that way on the page —
    `GXR-1P-7 10-27-25.pdf` really does read ``Ticket No: 1860042`` where its
    neighbours read ``18600289`` — so this is the crew dropping a digit, not
    the parser.
    """
    rows = db.q(
        """SELECT fingerprint, MIN(document_id) document_id, MIN(filename) filename,
                  MIN(segment) segment, ticket
           FROM reader_sheet
           WHERE project_id=? AND IFNULL(ticket,'') <> ''
           GROUP BY fingerprint, ticket""",
        (project_id,),
    )
    digits = [r for r in rows if r["ticket"].isdigit()]
    if len(digits) < 10:
        return []                       # too few to know the house style

    # The usual length within each issuing block.
    lengths: dict[str, Counter] = defaultdict(Counter)
    for r in digits:
        lengths[r["ticket"][:_TICKET_BLOCK]][len(r["ticket"])] += 1

    findings: list[Finding] = []
    for r in digits:
        block = r["ticket"][:_TICKET_BLOCK]
        seen = lengths[block]
        usual, count = seen.most_common(1)[0]
        # One oddity against a settled convention, not two rival spellings.
        if len(r["ticket"]) == usual or count < 3:
            continue
        findings.append(
            {
                "project_id": project_id, "run_id": run_id, "rule": "NDE-15",
                "severity": "minor", "segment": r["segment"] or "",
                "subject": r["filename"],
                "message": (
                    f"{r['filename']} carries ticket {r['ticket']}, "
                    f"{len(r['ticket'])} digits where the other {count} tickets "
                    f"in the {block} block are {usual}. A mistyped ticket cannot "
                    f"be matched to the rest of its report, which is filed under "
                    f"the ticket and nothing else."
                ),
                "detail": _detail(ticket=r["ticket"], block=block,
                                  digits=len(r["ticket"]), usual=usual),
                "document_id": r["document_id"], "page_no": None,
            }
        )
    return findings


@register("NDE-16", "One ticket covering more than one day")
def ticket_spans_days(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """The same ticket number on sheets examined on different days.

    A ticket is one service call — one crew, one day — so it may legitimately
    cover several sheets and several pages, but not several dates. Two dates
    against one ticket means one of the two sheets carries the wrong number,
    and since the ticket is what ties a report together, whichever is wrong is
    filed under a report it does not belong to.

    The date is read from each report's own header, page by page. Taking it
    from the document instead made every ticket in a bundle inherit every day
    in the file — ``4IN FLEX FAB READERS.pdf`` holds fifteen days across
    sixteen pages, and each of its fourteen tickets was reported as spanning
    all fifteen.
    """
    rows = db.q(
        """SELECT ticket, fingerprint, page_no, sheet_date,
                  document_id, filename, segment
           FROM reader_sheet
           WHERE project_id=? AND IFNULL(ticket,'') <> ''
             AND IFNULL(sheet_date,'') <> ''""",
        (project_id,),
    )
    if not rows:
        return []

    by_ticket: dict[str, list] = defaultdict(list)
    for r in rows:
        by_ticket[r["ticket"]].append(r)

    findings: list[Finding] = []
    for ticket, items in sorted(by_ticket.items()):
        days = {r["sheet_date"] for r in items}
        if len(days) < 2:
            continue
        names = sorted({r["filename"] for r in items})
        findings.append(
            {
                "project_id": project_id, "run_id": run_id, "rule": "NDE-16",
                "severity": "major", "segment": items[0]["segment"] or "",
                "subject": f"Ticket {ticket}",
                "message": (
                    f"Ticket {ticket} appears on sheets examined on "
                    f"{' and '.join(sorted(days))} — {', '.join(names[:3])}. "
                    f"One ticket is one service call, so one of these sheets "
                    f"carries the wrong number and is filed under a report it "
                    f"does not belong to."
                ),
                "detail": _detail(ticket=ticket, days="; ".join(sorted(days)),
                                  sheets="; ".join(names[:4])),
                "document_id": items[0]["document_id"], "page_no": None,
            }
        )
    return findings


#: How many numeric neighbours either side a ticket is judged against. The
#: corpus gives the same four sheets at 4, 5 and 6, so nothing here turns on
#: the exact value; at 3 a one-day inversion starts to register.
_TICKET_NEIGHBOURS = 5


def _well_formed_tickets(db: Database, project_id: int) -> dict[str, list]:
    """Single-dated, correctly-formed numeric tickets, grouped by block.

    Deliberately excludes what NDE-15 and NDE-16 already own — a mistyped
    ticket and a ticket covering two days are both out of order by
    construction, and reporting them three times would say nothing new.
    """
    rows = db.q(
        """SELECT ticket, sheet_date, MIN(document_id) document_id,
                  MIN(filename) filename, MIN(segment) segment
           FROM reader_sheet
           WHERE project_id=? AND IFNULL(ticket,'') <> ''
             AND IFNULL(sheet_date,'') <> ''
           GROUP BY ticket, sheet_date""",
        (project_id,),
    )
    dates_of: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        dates_of[r["ticket"]].add(r["sheet_date"])

    blocks: dict[str, list] = defaultdict(list)
    lengths: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        ticket = r["ticket"]
        if not ticket.isdigit() or len(dates_of[ticket]) != 1:
            continue
        blocks[ticket[:_TICKET_BLOCK]].append(r)
        lengths[ticket[:_TICKET_BLOCK]][len(ticket)] += 1

    out: dict[str, list] = {}
    for block, items in blocks.items():
        usual = lengths[block].most_common(1)[0][0]
        keep = [r for r in items if len(r["ticket"]) == usual]
        out[block] = sorted(keep, key=lambda r: r["ticket"])
    return out


@register("NDE-17", "Ticket number out of date order")
def ticket_out_of_order(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A ticket whose date disagrees with where its number places it.

    Tickets are issued in sequence, so within an issuing block the numbers run
    in date order. That is an inference rather than something a document
    states, but the corpus supports it plainly: three of the six blocks —
    twenty-four, twenty-three and thirty-nine tickets — are in perfect order,
    and the exceptions are few and large.

    Judged **locally and unanimously**: a ticket is reported only when its date
    is earlier than every one of its five nearest lower-numbered neighbours, or
    later than every one of its five nearest higher. That needs no threshold in
    days, which is the point — a global sort blames whichever side of an
    inversion it happens to drop, and it flagged sheets sitting correctly
    between their neighbours. Unanimity against a local window also survives
    one bad neighbour, so a single mistyped sheet does not implicate the four
    around it.

    Which of the two is wrong is not for this rule to say. Sometimes it is the
    date — `20IN LP 08.26.25 CXR-034-044.pdf` is dated 08/26/**26** in a job
    that ran through 2025, and its own filename says 25. Sometimes the date is
    corroborated by the filename and it is the ticket that was mistyped.
    """
    findings: list[Finding] = []
    k = _TICKET_NEIGHBOURS
    for block, seq in sorted(_well_formed_tickets(db, project_id).items()):
        if len(seq) < 2 * k + 1:
            continue                     # too short to have a neighbourhood
        for i, r in enumerate(seq):
            when = r["sheet_date"]
            lower = [x["sheet_date"] for x in seq[max(0, i - k):i]]
            upper = [x["sheet_date"] for x in seq[i + 1:i + 1 + k]]
            early = len(lower) == k and all(when < e for e in lower)
            late = len(upper) == k and all(when > e for e in upper)
            if not (early or late):
                continue
            side = "earlier" if early else "later"
            others = lower if early else upper
            neighbours = f"{seq[max(0, i - k)]['ticket']}..{seq[i - 1]['ticket']}" \
                if early else f"{seq[i + 1]['ticket']}..{seq[min(len(seq) - 1, i + k)]['ticket']}"
            findings.append(
                {
                    "project_id": project_id, "run_id": run_id, "rule": "NDE-17",
                    "severity": "minor", "segment": r["segment"] or "",
                    "subject": r["filename"],
                    "message": (
                        f"{r['filename']} is dated {when} on ticket {r['ticket']}, "
                        f"{side} than all five tickets "
                        f"{'below' if early else 'above'} it ({neighbours}, "
                        f"{min(others)} to {max(others)}). Tickets are issued in "
                        f"order, so either the date or the ticket number was "
                        f"written down wrong."
                    ),
                    "detail": _detail(
                        ticket=r["ticket"], block=block, sheet_date=when,
                        neighbours=neighbours,
                        neighbour_dates=f"{min(others)}..{max(others)}",
                    ),
                    "document_id": r["document_id"], "page_no": None,
                }
            )
    return findings


@register("NDE-05", "Rejected weld not closed out")
def open_reject(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A weld recorded as rejected with no repair or cut-out closing it."""
    rows = db.q(
        """SELECT w.segment, w.line, w.weld_no, w.nde_id, w.nde_status, w.defect,
                  w.repair_nde_id, w.repair_status, w.document_id, d.filename
           FROM weld w LEFT JOIN document d ON d.id = w.document_id
           WHERE w.project_id=? AND (w.nde_status <> '' OR w.defect <> '')""",
        (project_id,),
    )
    on_file = _shots_on_file(db, project_id)

    findings: list[Finding] = []
    for r in rows:
        status = r["nde_status"] or ""
        rejected = bool(_REJECTED.search(status)) or bool(
            r["defect"] and not _ACCEPTED.search(status)
        )
        if not rejected:
            continue
        if r["repair_status"] and _ACCEPTED.search(r["repair_status"]):
            continue
        # A cut-out shot filed against the same number also closes it.
        base = re.sub(r"(P|R|RR|CO)$", "", r["nde_id"] or "")
        if base and any(k.startswith(base) and k.endswith("CO") for k in on_file):
            continue
        findings.append(
            {
                "project_id": project_id, "run_id": run_id, "rule": "NDE-05",
                "severity": "critical", "segment": r["segment"],
                "subject": r["nde_id"] or f"Weld {r['weld_no']}",
                "message": (
                    f"Weld {r['weld_no']} ({r['nde_id'] or 'no id'}) is recorded as "
                    f"{status or 'defective'} with no accepted repair or cut-out "
                    f"closing it out."
                ),
                "detail": _detail(
                    defect=r["defect"], status=status, line=r["line"],
                    repair_report=r["repair_nde_id"], repair_status=r["repair_status"],
                    report=r["filename"],
                ),
                "document_id": r["document_id"], "page_no": None,
            }
        )
    return findings


@register("NDE-06", "Repair shot has no reader sheet")
def repair_without_sheet(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A repair was shot per the log, but no sheet carrying that id is filed."""
    on_file = _shots_on_file(db, project_id)
    rows = db.q(
        """SELECT segment, weld_no, nde_id, repair_nde_id, document_id
           FROM weld WHERE project_id=? AND repair_nde_id <> ''""",
        (project_id,),
    )
    seen: set[str] = set()
    findings: list[Finding] = []
    for r in rows:
        if r["repair_nde_id"] in on_file or r["repair_nde_id"] in seen:
            continue
        seen.add(r["repair_nde_id"])
        findings.append(
            {
                "project_id": project_id, "run_id": run_id, "rule": "NDE-06",
                "severity": "critical", "segment": r["segment"],
                "subject": r["repair_nde_id"],
                "message": (
                    f"Repair shot {r['repair_nde_id']} on weld {r['weld_no']} is "
                    f"recorded in the log but no reader sheet for it is filed."
                ),
                "detail": _detail(original=r["nde_id"], weld_no=r["weld_no"]),
                "document_id": r["document_id"], "page_no": None,
            }
        )
    return findings


@register("NDE-07", "Overlapping reader sheets within one series")
def conflicting_sheet(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """Two genuinely different sheets in the same series claiming the same shot.

    Filing one sheet into several books is normal practice here and is not
    reported - copies collapse on content fingerprint before this runs.  What
    is reported is two *different* sheets covering the same shot number within
    the same numbering series: one of them is misfiled, or a weld was re-shot
    without being renumbered.

    Reported once per series rather than once per shot, because a single
    misfiled range sheet otherwise generates one finding per weld it covers.
    """
    findings: list[Finding] = []
    for (prefix, _idx), shots in sorted(_series_groups(db, project_id).items()):
        by_id: dict[str, set[str]] = defaultdict(set)
        sheet_of: dict[str, str] = {}
        for r in shots:
            fp = r["fingerprint"] or str(r["document_id"])
            by_id[r["nde_id"]].add(fp)
            sheet_of[fp] = r["filename"] or ""

        clashing = {nid: fps for nid, fps in by_id.items() if len(fps) > 1}
        if not clashing:
            continue

        involved = sorted({fp for fps in clashing.values() for fp in fps})
        numbers = sorted(clashing)
        label = series_label(prefix, shots)
        segs = sorted({s for r in shots for s in (r["segments"] or "").split("; ") if s})
        findings.append(
            {
                "project_id": project_id, "run_id": run_id, "rule": "NDE-07",
                "severity": "minor", "segment": segs[0] if segs else label,
                "subject": f"{label}: {len(numbers)} shots",
                "message": (
                    f"In the {label} series, {len(involved)} different reader sheets "
                    f"make overlapping claims on {len(numbers)} shot numbers "
                    f"({', '.join(numbers[:4])}{'...' if len(numbers) > 4 else ''}). "
                    f"Confirm which sheet is the record copy for each."
                ),
                "detail": _detail(
                    sheets="; ".join(sheet_of[fp] for fp in involved[:8]),
                    shots=", ".join(numbers[:25]),
                ),
                "document_id": None, "page_no": None,
            }
        )
    return findings


@register("NDE-08", "Reader sheet filename has an unreadable date")
def bad_sheet_date(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """Filing dates that cannot be read - e.g. ``20IN LP 0.16.25 DBR-001P-005``.

    Minor on its own, but these sheets drop out of any date-ordered check
    (welder continuity, technician cert currency) so they are worth correcting.
    """
    rows = db.q(
        """SELECT DISTINCT s.segment, s.segments, s.document_id, d.filename
           FROM nde_shot s JOIN document d ON d.id = s.document_id
           WHERE s.project_id=? AND (s.sheet_date IS NULL OR s.sheet_date='')""",
        (project_id,),
    )
    return [
        {
            "project_id": project_id, "run_id": run_id, "rule": "NDE-08",
            "severity": "minor", "segment": r["segment"],
            "subject": r["filename"],
            "message": (
                f"Cannot read a valid date from reader sheet filename "
                f"'{r['filename']}'. Date-based checks will skip this sheet."
            ),
            "detail": _detail(filename=r["filename"], filed_under=r["segments"]),
            "document_id": r["document_id"], "page_no": None,
        }
        for r in rows
    ]


@register("NDE-10", "Reader sheet records a reject that nothing closes")
def sheet_reject_unclosed(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """The sheet itself says REJ, and no repair or cut-out follows it.

    Only fires on sheets that have actually been read (``evidence='vision'``) -
    a filename tells you a shot exists, not what it concluded. This is the
    check the whole vision pass exists for: NDE-05 can only see rejects the
    *weld log* recorded, and on this corpus the log frequently does not.
    """
    rejected = db.q(
        """SELECT s.nde_id, s.prefix, s.number, s.segment, s.segments,
                  s.sheet_date, s.document_id, s.page_no, d.filename
           FROM nde_shot s LEFT JOIN document d ON d.id = s.document_id
           WHERE s.project_id=? AND s.evidence='vision' AND s.result='REJ'""",
        (project_id,),
    )
    if not rejected:
        return []

    on_file = {r["nde_id"] for r in db.q(
        "SELECT DISTINCT nde_id FROM nde_shot WHERE project_id=?", (project_id,))}
    closed_by_log = {
        r["nde_id"] for r in db.q(
            """SELECT nde_id FROM weld WHERE project_id=? AND nde_id<>''
               AND (repair_nde_id <> '' OR repair_status LIKE '%ccept%')""",
            (project_id,),
        )
    }

    findings: list[Finding] = []
    for r in rejected:
        base = f"{r['prefix']}-{r['number']:03d}"
        # A re-shoot or a cut-out against the same number closes the reject.
        closed = any(
            k.startswith(base) and k != r["nde_id"] and k[len(base):] in ("R", "RR", "CO")
            for k in on_file
        )
        if closed or r["nde_id"] in closed_by_log:
            continue
        findings.append(
            {
                "project_id": project_id, "run_id": run_id, "rule": "NDE-10",
                "severity": "critical", "segment": r["segment"],
                "subject": r["nde_id"],
                "message": (
                    f"The reader sheet for {r['nde_id']} is marked REJECTED"
                    + (f" ({r['sheet_date']})" if r["sheet_date"] else "")
                    + ", and no repair or cut-out shot closes it. This weld is "
                      "recorded as failing inspection with no evidence of rework."
                ),
                "detail": _detail(filename=r["filename"], page=r["page_no"],
                                  filed_under=r["segments"]),
                "document_id": r["document_id"], "page_no": r["page_no"],
            }
        )
    return findings


@register("NDE-11", "Welder on the reader sheet differs from the weld report")
def sheet_welder_mismatch(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """The stencil printed on the NDE sheet is not the one on the weld report.

    Two records of who welded the same joint disagreeing is a traceability
    break: whichever is wrong, the weld cannot be tied to a qualified welder.

    **Scoped to a segment**, because an NDE prefix is reused with independent
    numbering on each line. PLU files seven different welds called `AFB-001P`,
    one per segment, welded by five different people; matching them on the
    identifier alone cross-joins all seven sheets against all seven welds and
    calls forty-two of the forty-nine pairs a disagreement. That is 507
    findings on this corpus, of which 15 are real.

    **Compared per weld rather than per pair.** A weld may have several sheets
    and the register several rows, and a joint welded by two people is normally
    written up with both. Comparing the union of what the sheets say against
    the union of what the reports say is what makes `AFB-008` on GL 31 quiet:
    one sheet names AM53, another EM93, and the register names both.
    """
    from ..welders import parse_field

    rows = db.q(
        """SELECT s.nde_id, s.segment sheet_segment, s.segments, s.welder,
                  s.document_id, s.page_no, d.filename,
                  w.segment, w.weld_no, w.line, w.source,
                  w.welder_root, w.welder_hp, w.welder_fill, w.welder_cap
           FROM nde_shot s
           LEFT JOIN document d ON d.id = s.document_id
           JOIN weld w ON w.project_id = s.project_id AND w.nde_id = s.nde_id
           WHERE s.project_id=? AND IFNULL(s.welder,'') <> ''""",
        (project_id,),
    )

    @dataclass
    class Joint:
        sheet: set = field(default_factory=set)
        report: set = field(default_factory=set)
        weld_no: str = ""
        line: str = ""
        source: str = ""
        filename: str = ""
        document_id: int | None = None
        page_no: int | None = None

    joints: dict[tuple[str, str], Joint] = defaultdict(Joint)
    for r in rows:
        # The sheet is filed into several books; it describes the weld in the
        # book it was filed under, not one with the same number elsewhere.
        books = {b for b in (r["segments"] or "").split("; ") if b}
        books.add(r["sheet_segment"] or "")
        if (r["segment"] or "") not in books:
            continue
        joint = joints[(r["nde_id"], r["segment"] or "")]
        joint.sheet |= set(parse_field(r["welder"]).stencils)
        for col in ("welder_root", "welder_hp", "welder_fill", "welder_cap"):
            joint.report |= set(parse_field(r[col] or "").stencils)
        joint.weld_no = joint.weld_no or (r["weld_no"] or "")
        joint.line = joint.line or (r["line"] or "")
        joint.source = joint.source or (r["source"] or "")
        joint.filename = joint.filename or (r["filename"] or "")
        if joint.document_id is None:
            joint.document_id, joint.page_no = r["document_id"], r["page_no"]

    findings: list[Finding] = []
    for (nde_id, segment), j in sorted(joints.items()):
        if not j.sheet or not j.report or (j.sheet & j.report):
            continue
        findings.append(
            {
                "project_id": project_id, "run_id": run_id, "rule": "NDE-11",
                "severity": "major", "segment": segment,
                "subject": nde_id,
                "message": (
                    f"The reader sheet for {nde_id} names welder "
                    f"{'/'.join(sorted(j.sheet))}, but the weld report for weld "
                    f"{j.weld_no} on {segment} names "
                    f"{'/'.join(sorted(j.report))}. The two records of who "
                    f"welded this joint disagree."
                ),
                "detail": _detail(
                    sheet_welder=", ".join(sorted(j.sheet)),
                    report_welder=", ".join(sorted(j.report)),
                    line=j.line, register=j.source, filename=j.filename,
                ),
                "document_id": j.document_id, "page_no": j.page_no,
            }
        )
    return findings


@register("NDE-09", "Weld report note is not a recognised NDE report")
def unrecognised_reference(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """NOTES entries that look like an id but match no NDE series on this job.

    Crews use the NOTES column for bore crossings (``BORE 2``), drawing numbers
    (``PG-327``) and spool references (``FG-515``) as well as NDE reports.
    Those are not defects, but they do mean the note cannot serve as the NDE
    link - so they are reported once per pattern rather than being silently
    counted as citations or silently ignored.
    """
    known = {
        r["prefix"]
        for r in db.q(
            "SELECT DISTINCT prefix FROM nde_shot WHERE project_id=? AND prefix<>''",
            (project_id,),
        )
    }
    rows = db.q(
        """SELECT segment, note, COUNT(*) n FROM weld
           WHERE project_id=? AND nde_id='' AND note<>''
           GROUP BY segment, note""",
        (project_id,),
    )

    # Collapse "FG-502", "FG-515" ... into one finding per (segment, prefix).
    buckets: dict[tuple[str, str], dict] = {}
    for r in rows:
        ids = re.findall(r"\b([A-Z]{2,5})-\d{1,4}\b", (r["note"] or "").upper())
        prefix = ids[0] if ids else re.sub(r"[\d\s].*$", "", (r["note"] or "").upper()) or "(text)"
        if prefix in known:
            continue
        key = (r["segment"], prefix)
        b = buckets.setdefault(key, {"n": 0, "examples": set()})
        b["n"] += r["n"]
        b["examples"].add(r["note"])

    return [
        {
            "project_id": project_id, "run_id": run_id, "rule": "NDE-09",
            "severity": "info", "segment": segment,
            "subject": prefix,
            "message": (
                f"{b['n']} welds carry a NOTES entry beginning '{prefix}' "
                f"(e.g. {', '.join(sorted(b['examples'])[:3])}) which matches no NDE "
                f"series on this job. Treated as a drawing or bore reference, not an "
                f"NDE citation."
            ),
            "detail": _detail(count=b["n"], examples="; ".join(sorted(b["examples"])[:8])),
            "document_id": None, "page_no": None,
        }
        for (segment, prefix), b in sorted(buckets.items())
        if b["n"] >= 2
    ]


# ---------------------------------------------------------------------------


def coverage_summary(db: Database, project_id: int) -> list[dict]:
    """Per-segment NDE coverage - the headline number for a turnover meeting.

    A segment can be described by more than one weld register, and adding them
    up counts the same physical weld twice.  Two registers cannot generally be
    matched joint by joint either (see :mod:`weldaudit.rules.registers`), so
    there is no exact deduplicated count to compute.  The headline figure is
    therefore a documented estimate:

        welds = max(distinct NDE ids across all registers,
                    the largest single register's weld count)

    Numbered welds deduplicate exactly by id.  Unnumbered ones cannot be
    attributed to anything, so the largest single register is the best view of
    the segment that is certainly not double counted.  Where registers are
    genuinely complementary rather than overlapping this under-counts, which is
    why the per-register breakdown travels with it and REG-03 reports the
    discrepancy outright.
    """
    from .registers import REGISTER_OF

    per_register: dict[str, dict[str, dict]] = defaultdict(dict)
    ids_by_segment: dict[str, set[str]] = defaultdict(set)

    for r in db.q(
        """SELECT segment, source, nde_id, nde_report FROM weld
           WHERE project_id=?""",
        (project_id,),
    ):
        segment = r["segment"] or ""
        name = REGISTER_OF.get(r["source"], r["source"] or "welds")
        entry = per_register[segment].setdefault(
            name, {"register": name, "welds": 0, "welds_with_nde": 0})
        entry["welds"] += 1
        # A weld cites its examination in `nde_id` where the reference is an
        # NdeId and in `nde_report` where it is not - a WeldTrace register
        # cites `NX-20260331RT01`, which has neither series nor sequence and
        # is deliberately kept out of `nde_id`. Counting only the ids would
        # report a fully examined test pack at 0% referenced, which is the one
        # kind of wrong answer this figure must never give.
        if r["nde_id"] or r["nde_report"]:
            entry["welds_with_nde"] += 1
        # Only NdeIds go in the deduplication set. Report numbers from a
        # different scheme are not comparable with them and would inflate a
        # distinct-weld count that exists to avoid double counting.
        if r["nde_id"]:
            ids_by_segment[segment].add(r["nde_id"])

    shots: dict[str, int] = defaultdict(int)
    for r in db.q(
        "SELECT segments, COUNT(DISTINCT nde_id) n FROM nde_shot WHERE project_id=? "
        "GROUP BY segments",
        (project_id,),
    ):
        for seg in (r["segments"] or "").split("; "):
            if seg:
                shots[seg] += r["n"]

    out = []
    for segment, registers in per_register.items():
        breakdown = sorted(registers.values(), key=lambda d: -d["welds"])
        if len(breakdown) == 1:
            # Nothing to deduplicate: report the register as it stands. The
            # numerator and denominator must come from the same place, or a
            # segment's percentage moves for reasons that have nothing to do
            # with registers.
            welds = breakdown[0]["welds"]
            with_nde = breakdown[0]["welds_with_nde"]
        else:
            welds = max(len(ids_by_segment[segment]),
                        max(b["welds"] for b in breakdown))
            # Same shape as the line above, and for the same reason: a
            # register whose citations are not NdeIds contributes nothing to
            # the deduplicated set, so the largest single register is the
            # floor below which this estimate is certainly wrong.
            with_nde = max(len(ids_by_segment[segment]),
                           max(b["welds_with_nde"] for b in breakdown))
        out.append(
            {
                "segment": segment,
                "welds": welds,
                "welds_with_nde": with_nde,
                "sheets_on_file": shots.get(segment, 0),
                "pct_referenced": round(100 * with_nde / welds) if welds else 0,
                "registers": breakdown,
                # True when the headline is an estimate rather than a count.
                "multiple_registers": len(breakdown) > 1,
            }
        )
    return sorted(out, key=lambda d: d["pct_referenced"])
