"""Reconciling the as-built against the rest of the package.

The as-built is the record of what was actually put in the ground, joint by
joint: where each one sits on the line, how long it is, what heat it was
rolled from, and the NDE report on the weld at its end.  Nineteen hundred
joints across three jobs, which makes it the largest single register in the
corpus — larger than the reader sheets, larger than the daily reports.

Most of what it can settle, other rules already ask of other documents, and
those are not repeated here.  Two things are new.

**It is the only document that places a weld on the line.**  Every other
record identifies a joint by number; the as-built gives it a survey station.
The release for backfill states the length it covers in exactly those terms —
`130+00 to 135+00` — so this is what closes the gap BF-01 could not: a weld
inside the release *dates* but outside every released *length*.

**It is a third opinion on which welds exist.**  The weld map, the weld log
and the as-built are drawn up by different people at different times, and
where the as-built names an NDE report the other two have never heard of, one
of them is wrong.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict

from ..asbuilt import format_station
from ..db import Database
from . import Finding, register

#: A joint whose station falls this far outside every released stretch is
#: worth reporting. Survey stations are recorded to the foot and a release is
#: written to the nearest round station, so the ends need a little slack.
STATION_SLACK_FT = 25.0


def _detail(**kw) -> str:
    return json.dumps({k: v for k, v in kw.items() if v not in (None, "")})


def _joints(db: Database, project_id: int) -> list:
    return db.q(
        """SELECT a.*, d.filename FROM asbuilt_joint a
           LEFT JOIN document d ON d.id = a.document_id
           WHERE a.project_id=? ORDER BY a.segment, a.station_ft, a.seq""",
        (project_id,),
    )


def _by_segment(db: Database, project_id: int) -> dict[str, list]:
    out: dict[str, list] = defaultdict(list)
    for joint in _joints(db, project_id):
        out[joint["segment"] or ""].append(joint)
    return out


# ---------------------------------------------------------------------------


@register("AB-01", "As-built joint outside every released stretch of ditch")
def joint_outside_release(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A joint whose station no release for backfill covers.

    This is the check BF-01 could not make. The release states the length it
    covers as a station range and the as-built states where each joint sits,
    so together they say whether the ditch over a given joint was ever
    released — which the dates alone cannot, because a release signed after a
    weld says nothing about whether it covered that stretch of line.
    """
    from ..asbuilt import parse_station
    from .backfill import fully_read

    # Same guard as BF-01, for the same reason: a bundle read one page deep
    # knows one released stretch and would report the rest of the line as
    # never cleared. Bluewater files 27 releases in a single PDF.
    complete = fully_read(db, project_id)
    if not complete:
        return []

    ranges: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for r in db.q(
        """SELECT segment, from_station, to_station FROM backfill_release
           WHERE project_id=? AND IFNULL(from_station,'')<>''
             AND IFNULL(to_station,'')<>''""",
        (project_id,),
    ):
        low, high = parse_station(r["from_station"]), parse_station(r["to_station"])
        if low is not None and high is not None:
            ranges[r["segment"] or ""].append((min(low, high), max(low, high)))
    if not ranges:
        return []

    findings: list[Finding] = []
    for segment, joints in sorted(_by_segment(db, project_id).items()):
        covered = ranges.get(segment)
        if not covered or segment not in complete:
            continue                    # BF-06 reports a segment with none
        outside = [
            j for j in joints
            if j["station_ft"] is not None
            and not any(low - STATION_SLACK_FT <= j["station_ft"] <= high + STATION_SLACK_FT
                        for low, high in covered)
        ]
        if not outside:
            continue
        stations = [j["station"] for j in outside]
        spans = ", ".join(f"{format_station(low)}–{format_station(high)}"
                          for low, high in sorted(covered)[:6])
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "AB-01",
            "severity": "critical", "segment": segment,
            "subject": f"{len(outside)} joint{'s' if len(outside) != 1 else ''}",
            "message": (
                f"{len(outside)} joint{'s sit' if len(outside) != 1 else ' sits'} "
                f"at stations no release for backfill covers — "
                f"{', '.join(stations[:8])}{'...' if len(stations) > 8 else ''}. "
                f"The releases on this segment cover {spans}. Pipe was buried "
                f"over ground the hold point was never cleared for."
            ),
            "detail": _detail(joints=len(outside), stations=", ".join(stations[:40]),
                              released=spans),
            "document_id": outside[0]["document_id"], "page_no": None,
        })
    return findings


@register("AB-02", "As-built names an NDE report nothing else knows")
def unknown_nde_report(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """An X-ray number on the as-built that no reader sheet or register has.

    The as-built is drawn up from the field marks after the fact, so a report
    number here that nothing else recognises is either a sheet that was never
    filed or a number transcribed wrong onto the permanent record of the line.
    """
    # An X-ray number's counterpart is a reader sheet, so this rule needs the
    # sheets to have been read. PLU files 65 of them and none yields an id its
    # filename grammar recognises, which would make every X-ray on its
    # as-built look unfiled — a finding about the NDE package wearing the
    # as-built's name.
    sheets = {r["nde_id"] for r in db.q(
        "SELECT DISTINCT nde_id FROM nde_shot WHERE project_id=? AND nde_id<>''",
        (project_id,))}
    if not sheets:
        return []
    known = sheets | {r["nde_id"] for r in db.q(
        "SELECT DISTINCT nde_id FROM weld WHERE project_id=? AND nde_id<>''",
        (project_id,))}

    missing: dict[str, list] = defaultdict(list)
    for joint in _joints(db, project_id):
        if joint["nde_id"] and joint["nde_id"] not in known:
            missing[joint["segment"] or ""].append(joint)

    findings: list[Finding] = []
    for segment, joints in sorted(missing.items()):
        ids = sorted({j["nde_id"] for j in joints})
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "AB-02",
            "severity": "major", "segment": segment,
            "subject": f"{len(ids)} report{'s' if len(ids) != 1 else ''}",
            "message": (
                f"The as-built names {len(ids)} NDE report"
                f"{'s' if len(ids) != 1 else ''} that no reader sheet and no "
                f"weld register mentions — {', '.join(ids[:8])}"
                f"{'...' if len(ids) > 8 else ''}. The as-built is the "
                f"permanent record of the line, so either those sheets were "
                f"never filed or the numbers on it are wrong."
            ),
            "detail": _detail(reports=", ".join(ids[:40]), joints=len(joints)),
            "document_id": joints[0]["document_id"], "page_no": None,
        })
    return findings


@register("AB-03", "As-built heat has no material certificate")
def uncertified_heat(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A heat named on the as-built with no certificate filed for it.

    MTR-01 asks this of heats on the weld log and the heat map. The as-built
    is a third source and a more definite one — it names the heat of each
    individual joint in the ground — so the heats it adds are heats nothing
    else in the audit was checking.
    """
    certified = {r["heat_key"] for r in db.q(
        """SELECT DISTINCT heat_key FROM material
           WHERE project_id=? AND heat_key<>'' AND source='mtr_file'""",
        (project_id,))}
    if not certified:
        return []                       # MTR-10 reports a job with no certs

    # Heats the material rules already know about from another register.
    elsewhere = {r["heat_key"] for r in db.q(
        "SELECT DISTINCT heat_key FROM installed_heat WHERE project_id=? AND heat_key<>''",
        (project_id,))}
    for column in ("heat_us", "heat_ds"):
        elsewhere |= {r[column] for r in db.q(
            f"SELECT DISTINCT {column} FROM weld WHERE project_id=? AND {column}<>''",
            (project_id,)) if r[column]}

    unknown: dict[str, list] = defaultdict(list)
    for joint in _joints(db, project_id):
        key = joint["heat_key"]
        if key and key not in certified:
            unknown[joint["segment"] or ""].append(joint)

    findings: list[Finding] = []
    for segment, joints in sorted(unknown.items()):
        heats = sorted({j["heat"] for j in joints})
        # Heats no other register mentions are the ones this rule adds; say
        # how many, so the overlap with MTR-01 is visible rather than implied.
        fresh = sorted({j["heat"] for j in joints
                        if j["heat_key"] not in elsewhere})
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "AB-03",
            "severity": "major", "segment": segment,
            "subject": f"{len(heats)} heat{'s' if len(heats) != 1 else ''}",
            "message": (
                f"The as-built puts {len(heats)} heat"
                f"{'s' if len(heats) != 1 else ''} in the ground on this "
                f"segment with no material certificate on file — "
                f"{', '.join(heats[:8])}{'...' if len(heats) > 8 else ''}"
                + (f", of which {len(fresh)} "
                   f"{'appear' if len(fresh) != 1 else 'appears'} on no other "
                   f"register." if fresh
                   else ", all of which other registers also name.")
            ),
            "detail": _detail(heats=", ".join(heats[:40]), joints=len(joints),
                              only_on_asbuilt=", ".join(fresh[:20])),
            "document_id": joints[0]["document_id"], "page_no": None,
        })
    return findings


@register("AB-04", "As-built joint records no heat")
def joint_without_heat(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """Joints on the as-built with the heat column empty.

    The as-built is where the heat of a specific length of pipe in the ground
    is recorded permanently. A blank there cannot be reconstructed later —
    the pipe is buried and the mill mark with it.
    """
    findings: list[Finding] = []
    for segment, joints in sorted(_by_segment(db, project_id).items()):
        blank = [j for j in joints if not j["heat"]]
        if not blank or len(blank) == len(joints):
            continue                    # all blank means the column is unused
        stations = [j["station"] for j in blank if j["station_ft"] is not None]
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "AB-04",
            "severity": "minor", "segment": segment,
            "subject": f"{len(blank)} joint{'s' if len(blank) != 1 else ''}",
            "message": (
                f"{len(blank)} of {len(joints)} as-built joints record no heat "
                f"number"
                + (f" ({', '.join(stations[:8])}"
                   f"{'...' if len(stations) > 8 else ''})" if stations else "")
                + f". The pipe is in the ground; the mill mark went with it."
            ),
            "detail": _detail(blank=len(blank), joints=len(joints),
                              stations=", ".join(stations[:30])),
            "document_id": blank[0]["document_id"], "page_no": None,
        })
    return findings


@register("AB-05", "As-built length disagrees with the pressure test")
def length_disagrees(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """The line the as-built measures against the length the hydrotest states.

    Both are measurements of the same run of pipe, taken by different people
    for different purposes. Where they disagree materially, one of the two
    documents is describing a different length of line than the other.
    """
    tested = {}
    for r in db.q(
        """SELECT segment, started_raw, completed_raw, req_min_press
           FROM hydrotest WHERE project_id=? AND segment<>''""",
        (project_id,),
    ):
        tested.setdefault(r["segment"], r)
    if not tested:
        return []

    findings: list[Finding] = []
    for segment, joints in sorted(_by_segment(db, project_id).items()):
        if segment not in tested:
            continue
        lengths = [j["length"] for j in joints if j["length"]]
        placed = [j for j in joints if j["station_ft"] is not None]
        if len(lengths) < 2 or len(placed) < 2:
            continue
        stations = [j["station_ft"] for j in placed]
        by_length = sum(lengths)
        # A station marks where a joint *starts*, so the line runs from the
        # first station to the end of the last joint — not to its start.
        # Without the final joint's length the two measures differ by one
        # joint, which on a short run is a quarter of the line.
        last = max(placed, key=lambda j: j["station_ft"])
        by_station = max(stations) - min(stations) + (last["length"] or 0)
        if not by_station:
            continue
        drift = abs(by_length - by_station) / by_station
        if drift < 0.10:
            continue
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "AB-05",
            "severity": "minor", "segment": segment,
            "subject": f"{drift:.0%}",
            "message": (
                f"The as-built joint lengths on this segment add up to "
                f"{by_length:,.0f} ft, but its stationing runs from "
                f"{format_station(min(stations))} to "
                f"{format_station(max(stations))} — {by_station:,.0f} ft. The "
                f"two differ by {drift:.0%}; the same sheet is measuring the "
                f"line two ways and getting two answers."
            ),
            "detail": _detail(by_length=round(by_length, 1),
                              by_station=round(by_station, 1),
                              joints=len(joints)),
            "document_id": joints[0]["document_id"], "page_no": None,
        })
    return findings


#: A fitting's LENGTH cell does not hold its own length. Couplings, elbows and
#: pups all come out at 39.0 ft — the length of the pipe joint beside them —
#: so only run-of-pipe rows can be measured against the survey.
_NOT_PIPE = re.compile(
    r"\b(COUPLING|ELL|ELBOW|TEE|FLANGE|VALVE|CAP|REDUCER|WELDOLET|OLET|RISER|"
    r"BEND|SPOOL|FITTING|STUB|DEG|BORE|PAD|PUP)\b|^[\d.]+$", re.IGNORECASE)


def _feet(value) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _runs(joints: list) -> dict[tuple, list]:
    """Joints grouped into the runs they were drawn in, in sheet order.

    Keyed by band as well as sheet, because a band's last joint is repeated as
    the next band's first and the two are not consecutive on the ground.
    """
    out: dict[tuple, list] = defaultdict(list)
    for j in joints:
        if j["station_ft"] is not None:
            out[(j["segment"] or "", j["sheet"] or "", j["band"])].append(j)
    for key in out:
        out[key].sort(key=lambda j: j["seq"] if j["seq"] is not None else 0)
    return out



#: How closely a station step and the pipe between two joints agree when the
#: drawing is right. Measured across a job: 96% of steps land within two feet
#: and 82% within one. Five is loose enough for a survey and nowhere near the
#: fifteen to forty feet a mistyped station produces.
_AGREES_WITHIN = 5.0

#: Steps that must actually be measurable before a run is excused as merely
#: out of order.
_ENOUGH_TO_JUDGE = 2


def _only_written_out_of_order(joints: list[dict]) -> bool:
    """Is this run consistent once the joints are read in station order?

    The drawing is a column per joint, and the columns are usually laid out in
    the order the pipe was laid. Usually. A pup welded in beside a coupling
    gets written in the next free column, which can put it after the joint it
    physically precedes — and reading the columns in order then shows the
    survey running backwards three feet where thirty-nine feet of pipe sits.
    That is a drawing written out of sequence, not a drawing that disagrees
    with itself, and reporting it sends somebody to check a stretch that is
    perfectly correct.

    A mistyped station does not survive this test. Sorting by station puts the
    wrong value in the wrong place, and the gaps it opens either side stay
    wide: on one real sheet a station typed 42+17 for 42+72 still left
    twenty-four and thirty-eight foot holes after sorting. Only a run where
    every step agrees closely is treated as merely out of order.
    """
    ordered = sorted(joints, key=lambda j: j["station_ft"] or 0)
    if [id(j) for j in ordered] == [id(j) for j in joints]:
        return False                       # already in station order

    measured = 0
    for a, b in zip(ordered, ordered[1:]):
        length = _feet(a["length"])
        described = (a["description"] or "").strip()
        if not length or length <= 0 or not described or _NOT_PIPE.search(described):
            continue
        step = (b["station_ft"] or 0) - (a["station_ft"] or 0)
        if abs(step - length) > _AGREES_WITHIN:
            return False
        measured += 1

    # An excuse has to be earned. The LENGTH cell is filled on some joints and
    # not others, so a run with one measurable step "agrees" in any order at
    # all — including the order that hides a station typed 221+68 for 121+68.
    # Two steps is the least that can distinguish a drawing written out of
    # sequence from one that simply has little to check.
    return measured >= _ENOUGH_TO_JUDGE


@register("AB-07", "As-built station disagrees with the pipe between the joints")
def station_length_conflict(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """The surveyed distance between two joints against the pipe laid between.

    The as-built states both, independently: a station for each joint and a
    length for the pipe. They are two measurements of the same geometry and
    they agree closely — 96% of Bluewater's eleven hundred pipe-to-pipe steps
    land within two feet, 82% within one — which is what makes a disagreement
    worth reporting.

    **Reported when the discrepancy is at least as large as the joint itself**,
    which needs no tolerance chosen out of the air: the survey is then claiming
    a whole joint's worth of pipe more or less than the drawing says was laid.
    The clearest case is a run of mainline reading 120+91, 121+29, **221+68**,
    122+06, 122+44 — every neighbour in the 12,000s and 121+29 to 121+68
    exactly one 39-foot joint, so the station was typed with the wrong leading
    digit. Nothing outside the as-built is needed to know that.

    Fittings are excluded because their LENGTH cell does not hold their own
    length — a coupling comes out at 39.0 ft, the pipe beside it — and a run's
    direction is taken from its own majority, since some lines are stationed
    descending.
    """
    findings: list[Finding] = []
    for (segment, sheet, band), joints in sorted(_runs(_joints(db, project_id)).items()):
        steps = [b["station_ft"] - a["station_ft"] for a, b in zip(joints, joints[1:])]
        if not steps:
            continue
        if _only_written_out_of_order(joints):
            continue
        forward = 1 if sum(1 for s in steps if s > 0) * 2 >= len(steps) else -1
        for a, b in zip(joints, joints[1:]):
            # Only joints that stand side by side on the drawing. A band can
            # carry blank columns — `5+93` then four empty placeholders then
            # `7+32` — and those two are neighbours in this list without being
            # neighbours on the sheet. Comparing them charges the one joint of
            # pipe recorded at 5+93 with the whole 139 ft to 7+32, and reports
            # a drawing that says nothing of the sort as contradicting itself.
            if a["seq"] is not None and b["seq"] is not None                     and b["seq"] - a["seq"] != 1:
                continue
            length = _feet(a["length"])
            described = (a["description"] or "").strip()
            if not length or length <= 0 or not described or _NOT_PIPE.search(described):
                continue
            step = (b["station_ft"] - a["station_ft"]) * forward
            off = abs(step - length)
            if off < length:
                continue
            findings.append(
                {
                    "project_id": project_id, "run_id": run_id, "rule": "AB-07",
                    "severity": "major", "segment": segment,
                    "subject": f"{a['station']} to {b['station']}",
                    "message": (
                        f"The as-built puts {off:,.0f} ft between stations "
                        f"{a['station']} and {b['station']} that the pipe does not "
                        f"account for: the survey steps {step:,.0f} ft while the "
                        f"{described.lower()} between them is {length:g} ft. One of "
                        f"the two was written down wrong, and the station is what "
                        f"every other record of this joint is filed against."
                    ),
                    "detail": _detail(
                        sheet=sheet, band=band, from_station=a["station"],
                        to_station=b["station"], surveyed_ft=round(step, 1),
                        pipe_ft=length, unaccounted_ft=round(off, 1),
                        described_as=described, heat=a["heat"],
                    ),
                    "document_id": a["document_id"], "page_no": None,
                }
            )
    return findings


@register("AB-06", "Segment welded with no as-built")
def segment_without_asbuilt(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A welded segment with no as-built, where other segments have one."""
    drawn = {s for s in _by_segment(db, project_id) if s}
    if not drawn:
        return []

    findings: list[Finding] = []
    for row in db.q(
        """SELECT segment, COUNT(*) n FROM weld
           WHERE project_id=? AND segment<>'' GROUP BY segment ORDER BY segment""",
        (project_id,),
    ):
        if row["segment"] in drawn:
            continue
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "AB-06",
            "severity": "major", "segment": row["segment"],
            "subject": row["segment"],
            "message": (
                f"{row['n']} weld{'s are' if row['n'] != 1 else ' is'} recorded "
                f"on this segment and no as-built is filed against it, while "
                f"other segments on this job have one. Nothing records where "
                f"on the line this pipe was laid, or what heat went where."
            ),
            "detail": _detail(welds=row["n"], drawn=", ".join(sorted(drawn))),
            "document_id": None, "page_no": None,
        })
    return findings


def asbuilt_summary(db: Database, project_id: int) -> list[dict]:
    """One row per as-built sheet: what it covers and how complete it is."""
    by_sheet: dict[tuple, list] = defaultdict(list)
    for joint in _joints(db, project_id):
        by_sheet[(joint["document_id"], joint["filename"], joint["sheet"],
                  joint["segment"])].append(joint)

    out: list[dict] = []
    for (document_id, filename, sheet, segment), joints in by_sheet.items():
        stations = [j["station_ft"] for j in joints if j["station_ft"] is not None]
        out.append({
            "segment": segment or "",
            "document": filename or "",
            "sheet": sheet or "",
            "service": next((j["service"] for j in joints if j["service"]), ""),
            "pipe_size": next((j["pipe_size"] for j in joints if j["pipe_size"]), ""),
            "joints": len(joints),
            "from_station": format_station(min(stations)) if stations else "",
            "to_station": format_station(max(stations)) if stations else "",
            "length": round(sum(j["length"] for j in joints if j["length"]), 1),
            "with_heat": sum(1 for j in joints if j["heat"]),
            "with_nde": sum(1 for j in joints if j["nde_id"]),
            "document_id": document_id,
        })
    out.sort(key=lambda r: (r["segment"], r["document"], r["sheet"]))
    return out
