"""Welder qualification: was the person who welded it qualified to, on that day?

Three questions, in the order an auditor asks them: is there a certification on
file for this stencil at all, was it in force when the weld was made, and is
there evidence the welder stayed current.

The continuity rule is the one to read carefully.  API 1104 disqualifies a
welder who has gone six months without using the process, but this tool only
ever sees welds on the job being audited - a welder may well have been welding
elsewhere.  So a gap is reported as *no evidence of continuity in this package*,
which is a request for the continuity record, not an accusation.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime

from ..aml import parse_nps
from ..db import Database
from ..qualification import (
    DiameterRange, normalise_wps, parse_diameter_range, parse_processes,
)
from ..welders import CONTINUITY_DAYS, continuity_gaps, nearest_stencils
from . import Finding, register


def _detail(**kw) -> str:
    return json.dumps({k: v for k, v in kw.items() if v not in (None, "")})


def _certs_by_stencil(db: Database, project_id: int) -> dict[str, list]:
    rows = db.q(
        """SELECT c.*, d.filename FROM welder_cert c
           LEFT JOIN document d ON d.id = c.document_id
           WHERE c.project_id=? AND c.stencil<>''""",
        (project_id,),
    )
    out: dict[str, list] = defaultdict(list)
    for r in rows:
        out[r["stencil"]].append(r)
    return out


def _passes_by_stencil(db: Database, project_id: int) -> dict[str, list]:
    rows = db.q(
        "SELECT * FROM welder_pass WHERE project_id=? ORDER BY date_welded",
        (project_id,),
    )
    out: dict[str, list] = defaultdict(list)
    for r in rows:
        out[r["stencil"]].append(r)
    return out


# ---------------------------------------------------------------------------


@register("WLD-01", "Welder has no certification on file")
def welder_without_cert(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A stencil that welded on this job with no certification document filed.

    Before calling a welder uncertified, the certified stencils one keystroke
    away are checked.  A tailgate-written ``AFB`` where the cert says ``ABF``
    is a clerical error on the report, not an unqualified welder - a different
    finding, with a different urgency and a different fix.
    """
    certs = _certs_by_stencil(db, project_id)
    if not certs:
        return []          # no welder certs filed at all - see WLD-06
    certified = set(certs)

    findings: list[Finding] = []
    for stencil, passes in sorted(_passes_by_stencil(db, project_id).items()):
        if stencil in certified:
            continue
        segments = sorted({p["segment"] for p in passes if p["segment"]})
        dates = sorted({p["date_welded"] for p in passes if p["date_welded"]})
        span = f" ({dates[0]} to {dates[-1]})" if dates else ""
        near = nearest_stencils(stencil, certified)

        if near:
            options = " or ".join(near[:3])
            findings.append(
                {
                    "project_id": project_id, "run_id": run_id, "rule": "WLD-01",
                    "severity": "major", "segment": segments[0] if segments else "",
                    "subject": stencil,
                    "message": (
                        f"Stencil {stencil} welded {len(passes)} pass"
                        f"{'es' if len(passes) != 1 else ''}{span} with no certification "
                        f"on file, but {options} is certified and is one keystroke away. "
                        f"Probably a transcription error on the weld report; confirm who "
                        f"welded these."
                    ),
                    "detail": _detail(
                        passes=len(passes), certified_alternatives=", ".join(near),
                        first_weld=dates[0] if dates else "",
                        last_weld=dates[-1] if dates else "",
                        segments="; ".join(segments[:6]),
                    ),
                    "document_id": passes[0]["document_id"], "page_no": None,
                }
            )
            continue

        findings.append(
            {
                "project_id": project_id, "run_id": run_id, "rule": "WLD-01",
                "severity": "critical", "segment": segments[0] if segments else "",
                "subject": stencil,
                "message": (
                    f"Stencil {stencil} welded {len(passes)} pass"
                    f"{'es' if len(passes) != 1 else ''} on this job{span} but no welder "
                    f"certification for that stencil is filed, and no certified stencil "
                    f"resembles it."
                ),
                "detail": _detail(
                    passes=len(passes), first_weld=dates[0] if dates else "",
                    last_weld=dates[-1] if dates else "",
                    segments="; ".join(segments[:6]),
                ),
                "document_id": passes[0]["document_id"], "page_no": None,
            }
        )
    return findings


@register("WLD-02", "Weld predates the welder's certification")
def weld_before_cert(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A weld made before the earliest certification on file for that stencil."""
    certs = _certs_by_stencil(db, project_id)
    findings: list[Finding] = []

    for stencil, passes in sorted(_passes_by_stencil(db, project_id).items()):
        dated_certs = [c["cert_date"] for c in certs.get(stencil, []) if c["cert_date"]]
        if not dated_certs:
            continue          # no dated certificate to compare against
        earliest = min(dated_certs)
        before = [p for p in passes if p["date_welded"] and p["date_welded"] < earliest]
        if not before:
            continue
        first = before[0]
        findings.append(
            {
                "project_id": project_id, "run_id": run_id, "rule": "WLD-02",
                "severity": "major", "segment": first["segment"],
                "subject": stencil,
                "message": (
                    f"Stencil {stencil} welded {len(before)} pass"
                    f"{'es' if len(before) != 1 else ''} from {before[0]['date_welded']}, "
                    f"before the earliest certification on file for that stencil "
                    f"({earliest}). Either an earlier certification is missing from the "
                    f"book or the welder was not yet qualified."
                ),
                "detail": _detail(
                    earliest_cert=earliest, passes_before=len(before),
                    first_weld=before[0]["date_welded"],
                    welds=", ".join(f"{p['weld_no']}" for p in before[:8]),
                ),
                "document_id": first["document_id"], "page_no": None,
            }
        )
    return findings


@register("WLD-03", "No evidence of welder continuity")
def continuity_gap(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """More than six months between a welder's welds, with no requalification.

    Only counts when a requalification certificate does not sit inside the gap,
    since that is exactly what closes it.
    """
    certs = _certs_by_stencil(db, project_id)
    findings: list[Finding] = []

    for stencil, passes in sorted(_passes_by_stencil(db, project_id).items()):
        dates = [p["date_welded"] for p in passes if p["date_welded"]]
        gaps = continuity_gaps(dates)
        if not gaps:
            continue
        cert_dates = sorted(c["cert_date"] for c in certs.get(stencil, []) if c["cert_date"])
        open_gaps = [
            (a, b, days) for a, b, days in gaps
            if not any(a < cd < b for cd in cert_dates)
        ]
        if not open_gaps:
            continue
        a, b, days = max(open_gaps, key=lambda g: g[2])
        segment = next((p["segment"] for p in passes if p["date_welded"] == b), "")
        findings.append(
            {
                "project_id": project_id, "run_id": run_id, "rule": "WLD-03",
                "severity": "major", "segment": segment,
                "subject": stencil,
                "message": (
                    f"Stencil {stencil} has a {days}-day gap between welds "
                    f"({a} then {b}) with no requalification filed in between. "
                    f"API 1104 requires continuity within {CONTINUITY_DAYS} days; the "
                    f"welder may have been welding on another job, so obtain the "
                    f"continuity record."
                ),
                "detail": _detail(
                    gap_days=days, previous_weld=a, next_weld=b,
                    other_gaps=len(open_gaps) - 1,
                    certs_on_file=", ".join(cert_dates) or "none dated",
                ),
                "document_id": None, "page_no": None,
            }
        )
    return findings


@register("WLD-04", "Certification on file for a welder who never welded here")
def unused_cert(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A stencil certified for this job but absent from every weld report."""
    passes = _passes_by_stencil(db, project_id)
    if not passes:
        return []
    findings: list[Finding] = []
    for stencil, certs in sorted(_certs_by_stencil(db, project_id).items()):
        if stencil in passes:
            continue
        c = certs[0]
        findings.append(
            {
                "project_id": project_id, "run_id": run_id, "rule": "WLD-04",
                "severity": "info", "segment": c["segment"],
                "subject": stencil,
                "message": (
                    f"A certification for stencil {stencil}"
                    + (f" ({c['name']})" if c["name"] else "")
                    + " is filed but that stencil appears on no weld report in this job."
                ),
                "detail": _detail(filename=c["filename"], name=c["name"]),
                "document_id": c["document_id"], "page_no": None,
            }
        )
    return findings


@register("WLD-05", "Welder column holds something that is not a stencil")
def unparsed_welder_field(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """Weld reports whose welder columns contain NDE ids or free text.

    Usually means the report has no NOTES column and the NDE reference has
    drifted into the cap column. Worth fixing: those passes have no recorded
    welder at all.
    """
    from ..welders import stencils_of
    from ..extract.welders import known_nde_prefixes

    prefixes = known_nde_prefixes(db, project_id) or None
    rows = db.q(
        """SELECT w.segment, w.document_id, d.filename,
                  w.welder_root, w.welder_hp, w.welder_fill, w.welder_cap
           FROM weld w LEFT JOIN document d ON d.id = w.document_id
           WHERE w.project_id=?""",
        (project_id,),
    )

    buckets: dict[tuple[str, str], dict] = {}
    for r in rows:
        field = stencils_of(
            r["welder_root"] or "", r["welder_hp"] or "",
            r["welder_fill"] or "", r["welder_cap"] or "",
            nde_prefixes=prefixes,
        )
        stray = field.nde_ids + field.unparsed
        if not stray:
            continue
        key = (r["segment"], r["filename"] or "")
        b = buckets.setdefault(key, {"n": 0, "examples": set(), "doc": r["document_id"]})
        b["n"] += 1
        b["examples"].update(stray[:3])

    return [
        {
            "project_id": project_id, "run_id": run_id, "rule": "WLD-05",
            "severity": "minor", "segment": segment,
            "subject": filename or "(weld report)",
            "message": (
                f"{b['n']} welds in '{filename}' have a welder column holding "
                f"something that is not a stencil ({', '.join(sorted(b['examples'])[:4])}). "
                f"Those passes have no recorded welder."
            ),
            "detail": _detail(count=b["n"], examples="; ".join(sorted(b["examples"])[:10])),
            "document_id": b["doc"], "page_no": None,
        }
        for (segment, filename), b in sorted(buckets.items())
        if b["n"] >= 2
    ]


@register("WLD-07", "Welded under a procedure the welder is not certified for")
def wps_not_certified(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A weld made under a WPS the welder holds no ticket for.

    A qualification is scoped to one procedure — that is why a welder here
    holds three separate certificates, one per WPS. Needs no vision: both
    sides of the join are in filenames and the weld log.
    """
    certified: dict[str, set[str]] = defaultdict(set)
    for c in db.q(
        "SELECT stencil, wps FROM welder_cert WHERE project_id=? AND stencil<>'' "
        "AND IFNULL(wps,'')<>''",
        (project_id,),
    ):
        certified[c["stencil"]].add(normalise_wps(c["wps"]))
    if not certified:
        return []          # no procedure-scoped tickets here to check against

    rows = db.q(
        """SELECT p.stencil, p.segment, p.weld_no, p.line, p.document_id,
                  w.wps, COUNT(*) n
           FROM welder_pass p JOIN weld w ON w.id = p.weld_id
           WHERE p.project_id=? AND IFNULL(w.wps,'')<>''
           GROUP BY p.stencil, w.wps""",
        (project_id,),
    )

    findings: list[Finding] = []
    for r in rows:
        held = certified.get(r["stencil"])
        # Only judge welders who hold procedure-scoped tickets at all; someone
        # whose certificate is a bare stencil file is WLD-01's business.
        if not held or normalise_wps(r["wps"]) in held:
            continue
        findings.append(
            {
                "project_id": project_id, "run_id": run_id, "rule": "WLD-07",
                "severity": "critical", "segment": r["segment"],
                "subject": f"{r['stencil']} on {r['wps']}",
                "message": (
                    f"Stencil {r['stencil']} welded {r['n']} pass"
                    f"{'es' if r['n'] != 1 else ''} under procedure {r['wps']}, but "
                    f"holds no certification for it. Certifications on file cover "
                    f"only: {', '.join(sorted(_wps_names(db, project_id, r['stencil'])))}."
                ),
                "detail": _detail(stencil=r["stencil"], wps_used=r["wps"],
                                  passes=r["n"], line=r["line"]),
                "document_id": r["document_id"], "page_no": None,
            }
        )
    return findings


def _wps_names(db: Database, project_id: int, stencil: str) -> set[str]:
    return {
        c["wps"] for c in db.q(
            "SELECT DISTINCT wps FROM welder_cert WHERE project_id=? AND stencil=? "
            "AND IFNULL(wps,'')<>''",
            (project_id, stencil),
        )
    } or {"none"}


@register("WLD-08", "Welded a process the certification does not qualify")
def process_not_qualified(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A welder used a process outside their certificate's qualified range.

    Compared against the *Qualification Ranges* block on the record — what the
    certifying inspector determined the test covers — not against the process
    the coupon happened to be welded with.
    """
    qualified: dict[str, set[str]] = defaultdict(set)
    for c in db.q(
        """SELECT stencil, qual_process, process FROM welder_cert
           WHERE project_id=? AND stencil<>'' AND evidence='vision'""",
        (project_id,),
    ):
        # The range block is authoritative; fall back to the tested process
        # only when the range was left blank on the form.
        qualified[c["stencil"]] |= parse_processes(c["qual_process"] or c["process"])
    if not qualified:
        return []

    rows = db.q(
        """SELECT p.stencil, p.segment, p.line, p.document_id, w.process, COUNT(*) n
           FROM welder_pass p JOIN weld w ON w.id = p.weld_id
           WHERE p.project_id=? AND IFNULL(w.process,'')<>''
           GROUP BY p.stencil, w.process""",
        (project_id,),
    )

    findings: list[Finding] = []
    for r in rows:
        holds = qualified.get(r["stencil"])
        used = parse_processes(r["process"])
        # An unrecognised PROCESS cell is a data-quality issue, not a
        # violation - crews put joint types like "BW" in that column.
        if not holds or not used or (used & holds):
            continue
        findings.append(
            {
                "project_id": project_id, "run_id": run_id, "rule": "WLD-08",
                "severity": "critical", "segment": r["segment"],
                "subject": f"{r['stencil']} using {'/'.join(sorted(used))}",
                "message": (
                    f"Stencil {r['stencil']} welded {r['n']} pass"
                    f"{'es' if r['n'] != 1 else ''} using "
                    f"{'/'.join(sorted(used))}, but the qualification record "
                    f"qualifies only {'/'.join(sorted(holds))}."
                ),
                "detail": _detail(stencil=r["stencil"], used="/".join(sorted(used)),
                                  qualified="/".join(sorted(holds)), passes=r["n"],
                                  line=r["line"]),
                "document_id": r["document_id"], "page_no": None,
            }
        )
    return findings


@register("WLD-09", "Qualification record is not marked as passed")
def cert_not_passed(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A certificate on file whose own result box says FAIL."""
    rows = db.q(
        """SELECT c.stencil, c.name, c.segment, c.result, c.cert_date,
                  c.document_id, d.filename
           FROM welder_cert c LEFT JOIN document d ON d.id = c.document_id
           WHERE c.project_id=? AND c.evidence='vision' AND UPPER(c.result)='FAIL'""",
        (project_id,),
    )
    return [
        {
            "project_id": project_id, "run_id": run_id, "rule": "WLD-09",
            "severity": "critical", "segment": r["segment"],
            "subject": r["stencil"] or r["filename"],
            "message": (
                f"The qualification record filed for stencil {r['stencil']}"
                + (f" ({r['name']})" if r["name"] else "")
                + " is marked FAIL. A failed test does not qualify the welder; "
                  "the passing record it was superseded by should be on file instead."
            ),
            "detail": _detail(filename=r["filename"], test_date=r["cert_date"]),
            "document_id": r["document_id"], "page_no": None,
        }
        for r in rows
    ]


@register("WLD-10", "Welded a diameter outside the qualified range")
def diameter_not_qualified(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A weld larger or smaller than the certificate's qualified pipe diameter.

    A range that cannot be read unambiguously is skipped rather than guessed —
    most of these records say "ALL", and a misread of the rest would
    disqualify a welder who is in fact qualified.
    """
    ranges: dict[str, tuple[DiameterRange, str]] = {}
    for c in db.q(
        """SELECT stencil, qual_diameter FROM welder_cert
           WHERE project_id=? AND stencil<>'' AND evidence='vision'
             AND IFNULL(qual_diameter,'')<>''""",
        (project_id,),
    ):
        rng = parse_diameter_range(c["qual_diameter"])
        if not rng.understood or rng.unlimited:
            continue
        # A welder with several tickets is covered by the widest of them.
        existing = ranges.get(c["stencil"])
        if existing is None or _wider(rng, existing[0]):
            ranges[c["stencil"]] = (rng, c["qual_diameter"])
    if not ranges:
        return []

    rows = db.q(
        """SELECT p.stencil, p.segment, p.line, p.weld_no, p.document_id,
                  w.weld_size
           FROM welder_pass p JOIN weld w ON w.id = p.weld_id
           WHERE p.project_id=? AND IFNULL(w.weld_size,'')<>''""",
        (project_id,),
    )

    worst: dict[tuple[str, float], dict] = {}
    for r in rows:
        entry = ranges.get(r["stencil"])
        nps = parse_nps(r["weld_size"])
        if not entry or nps is None or entry[0].allows(nps):
            continue
        key = (r["stencil"], nps)
        worst.setdefault(key, {"row": r, "range": entry, "n": 0})["n"] += 1

    findings: list[Finding] = []
    for (stencil, nps), hit in sorted(worst.items()):
        r, (rng, raw) = hit["row"], hit["range"]
        findings.append(
            {
                "project_id": project_id, "run_id": run_id, "rule": "WLD-10",
                "severity": "critical", "segment": r["segment"],
                "subject": f"{stencil} at {r['weld_size']}",
                "message": (
                    f"Stencil {stencil} welded {hit['n']} pass"
                    f"{'es' if hit['n'] != 1 else ''} on {r['weld_size']} pipe, but "
                    f"the qualification record covers {rng.describe()} "
                    f"(recorded as '{raw}')."
                ),
                "detail": _detail(stencil=stencil, welded_nps=nps,
                                  qualified_range=raw, passes=hit["n"], line=r["line"]),
                "document_id": r["document_id"], "page_no": None,
            }
        )
    return findings


def _wider(a: DiameterRange, b: DiameterRange) -> bool:
    lo_a = a.minimum if a.minimum is not None else float("-inf")
    lo_b = b.minimum if b.minimum is not None else float("-inf")
    hi_a = a.maximum if a.maximum is not None else float("inf")
    hi_b = b.maximum if b.maximum is not None else float("inf")
    return (hi_a - lo_a) > (hi_b - lo_b)


@register("WLD-11", "Qualification test witnessed by a lapsed inspector")
def qualifier_lapsed(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """The CWI who signed the test had an expired certification that day.

    A qualification is only as good as the inspector who witnessed it, and the
    record carries their number and expiry precisely so this can be checked.
    """
    rows = db.q(
        """SELECT c.stencil, c.name, c.segment, c.cert_date, c.qualifier_name,
                  c.qualifier_cwi, c.qualifier_expiry, c.document_id, d.filename
           FROM welder_cert c LEFT JOIN document d ON d.id = c.document_id
           WHERE c.project_id=? AND c.evidence='vision'
             AND IFNULL(c.cert_date,'')<>'' AND IFNULL(c.qualifier_expiry,'')<>''
             AND c.qualifier_expiry < c.cert_date""",
        (project_id,),
    )
    return [
        {
            "project_id": project_id, "run_id": run_id, "rule": "WLD-11",
            "severity": "major", "segment": r["segment"],
            "subject": r["stencil"] or r["filename"],
            "message": (
                f"The qualification test for stencil {r['stencil']} was witnessed on "
                f"{r['cert_date']} by {r['qualifier_name'] or 'the signing inspector'}"
                + (f" (CWI {r['qualifier_cwi']})" if r["qualifier_cwi"] else "")
                + f", whose own certification expired {r['qualifier_expiry']}. "
                  "Confirm the inspector was current, or the qualification may not "
                  "stand."
            ),
            "detail": _detail(filename=r["filename"], test_date=r["cert_date"],
                              qualifier_expiry=r["qualifier_expiry"]),
            "document_id": r["document_id"], "page_no": None,
        }
        for r in rows
    ]


@register("WLD-12", "Welding position is qualified but never recorded")
def position_not_recorded(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """Position can be checked on paper but not against the work.

    Every qualification record states a position range, but no weld report in
    this corpus records the position a joint was welded in — so the one
    essential variable that cannot be inferred from pipe size or process is
    unverifiable. Reported once, because it is a gap in the records rather
    than a defect in any one weld.
    """
    qualified = db.one(
        """SELECT COUNT(*) n FROM welder_cert
           WHERE project_id=? AND evidence='vision' AND IFNULL(qual_position,'')<>''""",
        (project_id,),
    )
    if not qualified or not qualified["n"]:
        return []
    if db.one("SELECT 1 FROM weld WHERE project_id=? AND IFNULL(position,'')<>'' LIMIT 1",
              (project_id,)):
        return []
    return [
        {
            "project_id": project_id, "run_id": run_id, "rule": "WLD-12",
            "severity": "info", "segment": "(project)",
            "subject": f"{qualified['n']} certifications",
            "message": (
                f"{qualified['n']} qualification records state a qualified welding "
                f"position, but no weld report records the position any joint was "
                f"welded in. Process and diameter are checked; position cannot be, "
                f"until the weld reports capture it."
            ),
            "detail": "", "document_id": None, "page_no": None,
        }
    ]


@register("WLD-13", "Roster and certificate disagree on when a welder qualified")
def cert_date_conflict(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """The welder log's certification date against the certificate's own.

    Two records of the same fact, and the certificate is the one signed by the
    CWI who witnessed the test — the welder log is a contractor spreadsheet
    kept alongside it. Where they disagree, the log is the likelier to be
    wrong, and it is the log that WLD-02 would otherwise read.

    Worth its own rule because of how the error usually looks. Clinton Wilson's
    record on Bluewater is stencil AEA, tested by Lee Vermillion on **11/14/2024**
    and stamped PASS; the welder log carries the same day and month against
    **2025**. A year out in that direction would put every one of his 538
    passes before his own qualification.
    """
    certs: dict[str, list] = defaultdict(list)
    for r in db.q(
        """SELECT stencil, name, cert_date, wps FROM welder_cert
           WHERE project_id=? AND stencil<>'' AND IFNULL(cert_date,'')<>''
             AND evidence='vision'""",
        (project_id,),
    ):
        certs[r["stencil"].upper()].append(r)
    if not certs:
        return []          # only the certificate itself can settle this

    findings: list[Finding] = []
    for r in db.q(
        """SELECT stencil, MIN(name) name, cert_date, COUNT(*) copies
           FROM welder_roster
           WHERE project_id=? AND stencil<>'' AND IFNULL(cert_date,'')<>''
           GROUP BY stencil, cert_date""",
        (project_id,),
    ):
        held = certs.get((r["stencil"] or "").upper())
        if not held:
            continue
        on_paper = sorted({c["cert_date"] for c in held})
        if r["cert_date"] in on_paper:
            continue
        findings.append(
            {
                "project_id": project_id, "run_id": run_id, "rule": "WLD-13",
                "severity": "major", "segment": "",
                "subject": f"{r['name']} ({r['stencil']})",
                "message": (
                    f"The welder log dates {r['name']}'s qualification "
                    f"{r['cert_date']}, but the certification record filed for "
                    f"stencil {r['stencil']} is dated "
                    f"{' and '.join(on_paper)}. The certificate is the document "
                    f"the witnessing inspector signed, so the log is the likelier "
                    f"to be wrong — and the log's date is what decides whether a "
                    f"weld predates its welder's ticket."
                ),
                "detail": _detail(stencil=r["stencil"], roster_date=r["cert_date"],
                                  certificate_date=", ".join(on_paper),
                                  roster_copies=r["copies"],
                                  certified_for=held[0]["wps"]),
                "document_id": None, "page_no": None,
            }
        )
    return findings


@register("WLD-06", "No welder certifications filed in this job")
def no_welder_certs(db: Database, project_id: int, run_id: str) -> list[Finding]:
    if db.one("SELECT 1 FROM welder_cert WHERE project_id=? AND stencil<>'' LIMIT 1",
              (project_id,)):
        return []
    row = db.one(
        "SELECT COUNT(DISTINCT stencil) n FROM welder_pass WHERE project_id=?",
        (project_id,),
    )
    if not row or not row["n"]:
        return []
    return [
        {
            "project_id": project_id, "run_id": run_id, "rule": "WLD-06",
            "severity": "major", "segment": "(project)",
            "subject": f"{row['n']} stencils",
            "message": (
                f"{row['n']} welder stencils appear on the weld reports but this job "
                f"holds no welder certifications, so none of them can be verified. "
                f"Section 10 Welding should carry a certification per stencil."
            ),
            "detail": "", "document_id": None, "page_no": None,
        }
    ]
