"""Reconciling the several registers that can describe the same welds.

A job can carry more than one record of what was welded: the daily weld
reports the crew filled in, the weld map the as-built drawing carries, and on
digital jobs a weld log export.  They are produced by different people at
different times, and where they disagree one of them is wrong.

Matching them is only possible where both sides name the weld.  On these jobs
that is often not true — Kestrel 8's daily reports leave the WELD # column blank
entirely, so its weld map is the only register that numbers anything.  Rather
than invent a pairing, the rules here do two different things:

* where both registers name a weld, compare them weld by weld (REG-01, REG-02);
* where they do not, compare how many welds each records and say plainly how
  much of the overlap could be matched at all (REG-03).

The second is the honest answer to "do these two documents agree?" when the
documents cannot be lined up joint by joint.
"""

from __future__ import annotations

import json
from collections import defaultdict

from ..db import Database
from ..welders import parse_field
from . import Finding, register

#: Weld sources, grouped into the register a reader would name. The two daily
#: report sources are one register recorded two ways - a spreadsheet on some
#: jobs, a scan on others - and must not be reconciled against each other.
REGISTER_OF: dict[str, str] = {
    "daily_weld_report": "the daily weld reports",
    "daily_weld_report_vision": "the daily weld reports",
    # As with the daily reports, the two weld-map sources are one register
    # read two ways - off the drawing's text layer where it has one, and off
    # a scan where it does not - and must not be reconciled against each other.
    "weld_map_text": "the weld map",
    "weld_map_vision": "the weld map",
    "weld_log_csv": "the weld log export",
}


def _detail(**kw) -> str:
    return json.dumps({k: v for k, v in kw.items() if v not in (None, "")})


def _by_segment(db: Database, project_id: int) -> dict[str, dict[str, list]]:
    """``{segment: {register: [weld rows]}}`` for every weld in the project."""
    rows = db.q(
        """SELECT id, segment, line, weld_no, nde_id, date_welded, source,
                  document_id, welder_root, welder_hp, welder_fill, welder_cap
           FROM weld WHERE project_id=?""",
        (project_id,),
    )
    out: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        name = REGISTER_OF.get(r["source"])
        if name:
            out[r["segment"] or ""][name].append(r)
    return out


def _pairs(registers: dict[str, list]) -> list[tuple[str, str]]:
    names = sorted(registers)
    return [(a, b) for i, a in enumerate(names) for b in names[i + 1:]]


def _plural(register: str) -> bool:
    """Whether a register's name takes a plural verb ('the reports' vs 'the map')."""
    return register.rstrip().endswith("s")


def _verb(register: str) -> str:
    """'the reports name' / 'the map names'."""
    return "name" if _plural(register) else "names"


def _records(register: str) -> str:
    """'the reports record' / 'the map records'."""
    return "record" if _plural(register) else "records"


def _welders(row) -> set[str]:
    out: set[str] = set()
    for col in ("welder_root", "welder_hp", "welder_fill", "welder_cap"):
        out |= set(parse_field(row[col] or "").stencils)
    return out


def _indexed(rows: list) -> dict[str, list]:
    """Welds keyed by NDE id, for the ones that carry one."""
    out: dict[str, list] = defaultdict(list)
    for r in rows:
        if r["nde_id"]:
            out[r["nde_id"]].append(r)
    return out


# ---------------------------------------------------------------------------


@register("REG-01", "A weld appears on one register but not the other")
def missing_from_register(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A named weld recorded on one register and absent from the other.

    Only welds both registers *could* have named are compared: if a register
    numbers none of its welds, its absence from the other proves nothing and
    the count comparison in REG-03 is the right instrument instead.
    """
    findings: list[Finding] = []
    for segment, registers in sorted(_by_segment(db, project_id).items()):
        for left, right in _pairs(registers):
            a, b = _indexed(registers[left]), _indexed(registers[right])
            if not a or not b:
                continue          # one side names nothing - see REG-03
            for name, ours, theirs in ((left, a, b), (right, b, a)):
                other = right if name == left else left
                missing = sorted(set(ours) - set(theirs))
                if not missing:
                    continue
                sample = ", ".join(missing[:8])
                findings.append(
                    {
                        "project_id": project_id, "run_id": run_id, "rule": "REG-01",
                        "severity": "major", "segment": segment,
                        "subject": f"{len(missing)} welds",
                        "message": (
                            f"{len(missing)} weld"
                            f"{'s' if len(missing) != 1 else ''} on {name} "
                            f"({sample}{'...' if len(missing) > 8 else ''}) "
                            f"{'are' if len(missing) != 1 else 'is'} not on {other}. "
                            f"The two records of what was welded on this line "
                            f"disagree; one of them is incomplete."
                        ),
                        "detail": _detail(welds=", ".join(missing[:40]),
                                          present_on=name, absent_from=other),
                        "document_id": ours[missing[0]][0]["document_id"],
                        "page_no": None,
                    }
                )
    return findings


@register("REG-02", "Registers disagree on who welded a joint")
def welder_disagreement(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """Two registers naming the same weld, with no welder in common.

    Whichever is wrong, the weld cannot be tied to a qualified welder - the
    same consequence as NDE-11, reached from a different pair of documents.
    """
    findings: list[Finding] = []
    for segment, registers in sorted(_by_segment(db, project_id).items()):
        for left, right in _pairs(registers):
            a, b = _indexed(registers[left]), _indexed(registers[right])
            for nde_id in sorted(set(a) & set(b)):
                ours = set().union(*(_welders(r) for r in a[nde_id]))
                theirs = set().union(*(_welders(r) for r in b[nde_id]))
                if not ours or not theirs or (ours & theirs):
                    continue
                findings.append(
                    {
                        "project_id": project_id, "run_id": run_id, "rule": "REG-02",
                        "severity": "major", "segment": segment,
                        "subject": nde_id,
                        "message": (
                            f"{left.capitalize()} {_verb(left)} "
                            f"{'/'.join(sorted(ours))} as the welder on {nde_id}, "
                            f"but {right} {_verb(right)} {'/'.join(sorted(theirs))}. "
                            f"Whichever is wrong, this weld cannot be tied to a "
                            f"qualified welder."
                        ),
                        "detail": _detail(**{
                            left.replace("the ", "").replace(" ", "_"):
                                "/".join(sorted(ours)),
                            right.replace("the ", "").replace(" ", "_"):
                                "/".join(sorted(theirs)),
                        }),
                        "document_id": a[nde_id][0]["document_id"], "page_no": None,
                    }
                )
    return findings


#: Weld sources that are a record of work done, as opposed to the drawing.
_WORK_RECORDS = ("daily_weld_report", "daily_weld_report_vision", "weld_log_csv")

#: Below this share of a series appearing only on the drawing, the map is
#: simply ahead of the paperwork on a few joints.
SERIES_UNLOGGED_SHARE = 0.25


def _log_keeps_nde_ids(db: Database, project_id: int) -> bool:
    """Whether the work records number their welds well enough to compare.

    Bluewater's daily reports carry an NDE number on seventy of nineteen hundred
    welds, because the NOTES column that would hold it is free text the crews
    mostly leave empty.  Saying "the weld log records none of the CFB series"
    against a register that records almost no series at all is true and
    meaningless, and it produced four findings that were really one fact about
    the NOTES column — which NDE-00 already reports.
    """
    from .nde_coverage import LINK_THRESHOLD

    marks = ", ".join("?" * len(_WORK_RECORDS))
    row = db.one(
        f"""SELECT COUNT(*) n, SUM(CASE WHEN nde_id<>'' THEN 1 ELSE 0 END) linked
            FROM weld WHERE project_id=? AND source IN ({marks})""",
        (project_id, *_WORK_RECORDS),
    )
    return bool(row and row["n"] and (row["linked"] or 0) / row["n"] >= LINK_THRESHOLD)


@register("REG-04", "Welds ballooned on the map that no weld log records")
def map_welds_not_logged(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """Welds the as-built drawing shows and the work records do not.

    Compared by NDE series rather than by segment, which is the only axis the
    two share on these jobs: the maps are filed at drawing level and the logs
    at line level, so GL 31's isometrics sit under "(unassigned)" while its
    weld log sits under a segment folder, and a segment-keyed comparison — the
    one REG-01 and REG-03 do — never pairs them at all.

    The direction matters. A weld map is the as-built drawing and is
    authoritative for which joints exist; a weld log is the crew's record of
    making them. A joint on the drawing with no log entry means the work was
    never written up. The converse usually means only that the drawing set in
    the book is partial, so it is reported quietly.
    """
    maps: dict[str, set[str]] = defaultdict(set)
    logs: dict[str, set[str]] = defaultdict(set)
    where: dict[str, str] = {}
    doc: dict[str, int] = {}

    for r in db.q(
        """SELECT nde_id, segment, source, document_id FROM weld
           WHERE project_id=? AND nde_id<>''""",
        (project_id,),
    ):
        prefix = r["nde_id"].split("-")[0]
        if r["source"] in ("weld_map_text", "weld_map_vision"):
            maps[prefix].add(r["nde_id"])
            where.setdefault(prefix, r["segment"] or "")
            doc.setdefault(prefix, r["document_id"])
        elif r["source"] in _WORK_RECORDS:
            logs[prefix].add(r["nde_id"])

    # With no work record anywhere there is nothing to compare against, and
    # the coverage table already reports that the map stands alone.
    if not any(logs.values()) or not _log_keeps_nde_ids(db, project_id):
        return []

    shots = {
        r["nde_id"] for r in db.q(
            "SELECT DISTINCT nde_id FROM nde_shot WHERE project_id=?", (project_id,))
    }

    findings: list[Finding] = []
    for prefix, balloons in sorted(maps.items()):
        logged = logs.get(prefix, set())
        unlogged = sorted(balloons - logged)
        if not unlogged or len(unlogged) / len(balloons) < SERIES_UNLOGGED_SHARE:
            continue

        examined = sorted(set(unlogged) & shots)
        many = len(unlogged) != 1
        if examined:
            evidence = f"reader sheets cover {len(examined)} of them"
        elif many:
            evidence = "none of them has a reader sheet either"
        else:
            evidence = "it has no reader sheet either"
        logged_text = (
            f"the weld log records {len(logged)} of them"
            if logged else "the weld log records none of the series")

        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "REG-04",
            "severity": "critical" if not examined else "major",
            "segment": where.get(prefix, ""),
            "subject": f"{prefix} series",
            "message": (
                f"The weld maps balloon {len(balloons)} {prefix} weld"
                f"{'s' if len(balloons) != 1 else ''} "
                f"({unlogged[0]} to {unlogged[-1]}) and {logged_text}. "
                f"{len(unlogged)} joint{'s' if many else ''} "
                f"{'are' if many else 'is'} drawn as built with "
                f"no record of being welded, and {evidence}."
            ),
            "detail": _detail(series=prefix, balloons=len(balloons),
                              logged=len(logged), unlogged=len(unlogged),
                              with_sheets=len(examined),
                              welds=", ".join(unlogged[:40])),
            "document_id": doc.get(prefix), "page_no": None,
        })
    return findings


@register("REG-05", "Welds in the log that no weld map shows")
def logged_welds_not_mapped(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """Welds the crew recorded that appear on no drawing in the book.

    Reported as information rather than a defect: on every job here the book
    holds a partial set of isometrics, so the usual explanation is a missing
    drawing rather than a weld that should not exist. It is still worth
    stating, because the alternative explanation — a joint made that the
    as-built never picked up — is one an auditor would want to rule out.
    """
    maps: set[str] = set()
    logs: dict[str, set[str]] = defaultdict(set)
    for r in db.q(
        """SELECT nde_id, source FROM weld WHERE project_id=? AND nde_id<>''""",
        (project_id,),
    ):
        if r["source"] in ("weld_map_text", "weld_map_vision"):
            maps.add(r["nde_id"])
        elif r["source"] in _WORK_RECORDS:
            logs[r["nde_id"].split("-")[0]].add(r["nde_id"])

    if not maps:
        return []

    findings: list[Finding] = []
    for prefix, logged in sorted(logs.items()):
        # Only series the drawings cover at all; a series with no map in the
        # book says nothing about the welds in it.
        if not any(i.startswith(f"{prefix}-") for i in maps):
            continue
        missing = sorted(logged - maps)
        if not missing:
            continue
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "REG-05",
            "severity": "info", "segment": "",
            "subject": f"{prefix} series",
            "message": (
                f"{len(missing)} {prefix} weld"
                f"{'s' if len(missing) != 1 else ''} recorded on the weld log "
                f"({missing[0]} to {missing[-1]}) appear on no weld map in the "
                f"book. Most likely the isometric for that run was never filed; "
                f"the alternative is a joint the as-built does not show."
            ),
            "detail": _detail(series=prefix, logged=len(logged),
                              unmapped=len(missing),
                              welds=", ".join(missing[:40])),
            "document_id": None, "page_no": None,
        })
    return findings


@register("REG-03", "Two weld registers cover this segment")
def register_overlap(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """How two registers compare, and how much of them could be matched.

    Reported wherever a segment carries more than one register, because it is
    also the caveat on every weld count in the audit: a job with two registers
    counts the same physical weld twice in the coverage table, and an auditor
    reading that number needs to know.
    """
    findings: list[Finding] = []
    for segment, registers in sorted(_by_segment(db, project_id).items()):
        if len(registers) < 2:
            continue
        for left, right in _pairs(registers):
            a, b = _indexed(registers[left]), _indexed(registers[right])
            matched = len(set(a) & set(b))
            n_left, n_right = len(registers[left]), len(registers[right])

            if matched:
                how = (f"{matched} weld{'s' if matched != 1 else ''} could be "
                       f"matched by NDE report number")
                severity = "info"
            else:
                # Nothing to line up joint by joint: usually because one
                # register leaves the weld number blank.
                unnamed = [n for n, reg in ((left, a), (right, b)) if not reg]
                if len(unnamed) == 1:
                    because = f"{unnamed[0]} {_verb(unnamed[0])} no weld numbers"
                else:
                    because = "neither names any weld numbers"
                how = f"no weld could be matched, because {because}"
                severity = "major" if abs(n_left - n_right) else "info"

            # Mirrors coverage_summary's rule, so the two never disagree.
            deduped = max(matched, n_left, n_right)

            gap = ""
            if n_left != n_right:
                gap = (f" The counts differ by {abs(n_left - n_right)}, which is "
                       f"worth reconciling before turnover.")

            findings.append(
                {
                    "project_id": project_id, "run_id": run_id, "rule": "REG-03",
                    "severity": severity, "segment": segment,
                    "subject": f"{left} vs {right}",
                    "message": (
                        f"This segment has two weld registers: {left} "
                        f"{_records(left)} {n_left} weld"
                        f"{'s' if n_left != 1 else ''}, and {right} "
                        f"{_records(right)} {n_right}. Between them {how}.{gap} "
                        f"The coverage table shows {deduped} weld"
                        f"{'s' if deduped != 1 else ''} for this segment — a "
                        f"deduplicated estimate, not a count, because the two "
                        f"cannot be matched exactly."
                    ),
                    "detail": _detail(left=left, left_welds=n_left, right=right,
                                      right_welds=n_right, matched_by_id=matched),
                    "document_id": None, "page_no": None,
                }
            )
    return findings
