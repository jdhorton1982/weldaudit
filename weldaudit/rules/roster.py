"""Reconciling the project welder log against who actually welded.

The welder rules in ``welders.py`` work from certificates: does a ticket exist
for this stencil, does it cover this procedure, this process, this diameter.
The roster answers a different question — **was this person on the job at all,
on the day the weld is dated** — and it answers it with the contractor's own
record rather than by inference.

Three things here exist only because the roster does:

* a weld dated outside the welder's recorded time on site (ROS-01);
* a weld after the requalification date the contractor itself wrote down,
  where ``WLD-03`` can only infer a lapse from a 183-day gap between welds
  (ROS-02);
* the welder's **name**, which turns "stencil ADP-1" into "Alton Morgan" —
  the difference between a finding an auditor can chase and one they have to
  research first.

Care is taken not to restate what the certificate rules already say. ROS-04
reports a stencil missing from the roster **only when a certificate for it
does exist**: a qualified welder absent from the contractor's own log is a gap
in the log, whereas a stencil with neither is already WLD-01's critical, and
raising it twice under two headings helps nobody.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date

from ..db import Database
from ..welders import nearest_stencils
from . import Finding, register

#: A weld a few days outside the recorded window is a mobilisation date
#: written down loosely; a month outside is a different question. The split
#: keeps the large discrepancies visible instead of averaged into a long list.
SLOPPY_DAYS = 7


def _detail(**kw) -> str:
    return json.dumps({k: v for k, v in kw.items() if v not in (None, "")})


def _as_date(text) -> date | None:
    try:
        return date.fromisoformat(str(text or "")[:10])
    except ValueError:
        return None


def roster(db: Database, project_id: int) -> dict[str, dict]:
    """One entry per stencil, merged across every segment's welder log.

    A welder who moves between spreads appears on several logs with different
    arrival dates, so the window is the widest of them: earliest arrival to
    latest departure. Taking one log's dates in isolation would report the
    other spread's welds as out of window.
    """
    out: dict[str, dict] = {}
    for r in db.q(
        "SELECT * FROM welder_roster WHERE project_id=? AND stencil<>''",
        (project_id,),
    ):
        entry = out.setdefault(r["stencil"], {
            "stencil": r["stencil"], "names": set(), "materials": set(),
            "arrived": "", "left_job": "", "cert_date": "", "requal_date": "",
            "next_requal": "", "reasons": set(), "segments": set(),
            "document_id": r["document_id"], "still_on_job": False,
        })
        entry["names"].add(r["name"])
        if r["material"]:
            entry["materials"].add(r["material"])
        if r["reason"]:
            entry["reasons"].add(r["reason"])
        if r["segment"]:
            entry["segments"].add(r["segment"])
        for field in ("arrived", "cert_date"):
            value = r[field]
            if value and (not entry[field] or value < entry[field]):
                entry[field] = value
        for field in ("left_job", "requal_date", "next_requal"):
            value = r[field]
            if value and (not entry[field] or value > entry[field]):
                entry[field] = value
        # A blank leaving date on any log means the welder had not left.
        if not r["left_job"]:
            entry["still_on_job"] = True
    return out


def _who(entry: dict) -> str:
    """'ADP-1 (Alton Morgan)', or just the stencil when the log has no name."""
    names = sorted(n for n in entry["names"] if n)
    if not names:
        return entry["stencil"]
    return f"{entry['stencil']} ({' / '.join(names)})"


def _welds(db: Database, project_id: int) -> dict[str, list]:
    out: dict[str, list] = defaultdict(list)
    for r in db.q(
        """SELECT stencil, date_welded, segment, weld_no, document_id
           FROM welder_pass
           WHERE project_id=? AND stencil<>'' AND IFNULL(date_welded,'')<>''
           ORDER BY date_welded""",
        (project_id,),
    ):
        out[r["stencil"]].append(r)
    return out


# ---------------------------------------------------------------------------


@register("ROS-01", "Weld dated outside the welder's time on the job")
def welded_off_roster_dates(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """Welds before a welder arrived or after they left.

    Whichever record is wrong — the weld report naming the wrong stencil, or
    the log recording the wrong dates — the pair cannot both be right, and a
    weld attributed to someone who was not on site is attributed to nobody.
    """
    entries = roster(db, project_id)
    if not entries:
        return []

    findings: list[Finding] = []
    for stencil, welds in sorted(_welds(db, project_id).items()):
        entry = entries.get(stencil)
        if not entry:
            continue                    # ROS-04's business
        arrived, left = _as_date(entry["arrived"]), _as_date(entry["left_job"])
        early = [w for w in welds
                 if arrived and (d := _as_date(w["date_welded"])) and d < arrived]
        late = [w for w in welds
                if left and not entry["still_on_job"]
                and (d := _as_date(w["date_welded"])) and d > left]
        if not early and not late:
            continue

        parts, worst = [], 0
        if early:
            gap = (arrived - _as_date(early[0]["date_welded"])).days
            worst = max(worst, gap)
            parts.append(
                f"{len(early)} weld{'s' if len(early) != 1 else ''} dated from "
                f"{early[0]['date_welded']}, up to {gap} days before they "
                f"arrived on {entry['arrived']}")
        if late:
            gap = (_as_date(late[-1]["date_welded"]) - left).days
            worst = max(worst, gap)
            parts.append(
                f"{len(late)} weld{'s' if len(late) != 1 else ''} dated up to "
                f"{late[-1]['date_welded']}, {gap} days after they left on "
                f"{entry['left_job']}")

        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "ROS-01",
            "severity": "major" if worst > SLOPPY_DAYS else "minor",
            "segment": (early or late)[0]["segment"],
            "subject": stencil,
            "message": (
                f"{_who(entry)} has {' and '.join(parts)}. Either the weld "
                f"report names the wrong welder or the log records the wrong "
                f"dates; as filed, these joints are attributed to someone the "
                f"contractor says was not on site."
            ),
            "detail": _detail(stencil=stencil, name=" / ".join(sorted(entry["names"])),
                              arrived=entry["arrived"], left=entry["left_job"],
                              before=len(early), after=len(late),
                              welds=", ".join(w["weld_no"] for w in (early + late)[:20])),
            "document_id": entry["document_id"], "page_no": None,
        })
    return findings


@register("ROS-02", "Weld after the requalification the roster itself sets")
def welded_after_requal_due(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """Welds dated after the log's own "Next Requal / Annual Required" date.

    Stronger than WLD-03, which can only infer a lapse from a long gap between
    welds. Here the contractor has written down the date the ticket expires,
    so a weld after it is measured against the job's own statement rather than
    against a rule of thumb.
    """
    entries = roster(db, project_id)
    findings: list[Finding] = []
    for stencil, welds in sorted(_welds(db, project_id).items()):
        entry = entries.get(stencil)
        due = _as_date((entry or {}).get("next_requal"))
        if not due:
            continue
        after = [w for w in welds
                 if (d := _as_date(w["date_welded"])) and d > due]
        if not after:
            continue
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "ROS-02",
            "severity": "major", "segment": after[0]["segment"],
            "subject": stencil,
            "message": (
                f"{_who(entry)} welded {len(after)} pass"
                f"{'es' if len(after) != 1 else ''} after "
                f"{entry['next_requal']}, the requalification date the "
                f"project welder log itself records for them — the last on "
                f"{after[-1]['date_welded']}. Either a later ticket is missing "
                f"from the package or the welding continued past it."
            ),
            "detail": _detail(stencil=stencil, due=entry["next_requal"],
                              passes=len(after),
                              last_weld=after[-1]["date_welded"]),
            "document_id": entry["document_id"], "page_no": None,
        })
    return findings


@register("ROS-03", "Welder log dates contradict each other")
def roster_dates_impossible(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """Dates within one roster entry that cannot all be true.

    The columns describe a sequence — qualified, requalified, requalification
    due, arrived, left — and the log is filled in by hand, so the sequence is
    worth checking before anything is measured against it.
    """
    findings: list[Finding] = []
    for stencil, entry in sorted(roster(db, project_id).items()):
        cert = _as_date(entry["cert_date"])
        requal = _as_date(entry["requal_date"])
        due = _as_date(entry["next_requal"])
        left = _as_date(entry["left_job"])

        problems = []
        if cert and left and cert > left:
            problems.append(
                f"qualified on {entry['cert_date']}, after leaving the job on "
                f"{entry['left_job']}")
        if cert and requal and requal < cert:
            problems.append(
                f"requalified on {entry['requal_date']}, before qualifying on "
                f"{entry['cert_date']}")
        if requal and due and due < requal:
            problems.append(
                f"next requalification due {entry['next_requal']}, before the "
                f"requalification on {entry['requal_date']}")
        if not problems:
            continue

        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "ROS-03",
            "severity": "minor", "segment": ", ".join(sorted(entry["segments"]))[:60],
            "subject": stencil,
            "message": (
                f"The project welder log records {_who(entry)} as "
                f"{'; and '.join(problems)}. The dates cannot all be right, so "
                f"none of them can be relied on to show the ticket was current "
                f"when this welder worked."
            ),
            "detail": _detail(stencil=stencil, cert=entry["cert_date"],
                              requal=entry["requal_date"],
                              next_requal=entry["next_requal"],
                              left=entry["left_job"]),
            "document_id": entry["document_id"], "page_no": None,
        })
    return findings


@register("ROS-04", "A certified welder is missing from the welder log")
def missing_from_roster(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A stencil that welded, holds a certificate, and is on no roster.

    Restricted to certified stencils on purpose. A stencil with neither a
    certificate nor a roster entry is already WLD-01's critical; what this
    adds is the case where the welder is demonstrably qualified and the
    contractor's own log does not list them, which is a defect in the log.
    """
    entries = roster(db, project_id)
    if not entries:
        return []
    certified = {r["stencil"] for r in db.q(
        "SELECT DISTINCT stencil FROM welder_cert WHERE project_id=? AND stencil<>''",
        (project_id,))}
    if not certified:
        return []

    findings: list[Finding] = []
    for stencil, welds in sorted(_welds(db, project_id).items()):
        if stencil in entries or stencil not in certified:
            continue
        near = nearest_stencils(stencil, set(entries))
        hint = (f" The log lists {', '.join(sorted(near))}, one keystroke away."
                if near else "")
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "ROS-04",
            "severity": "minor", "segment": welds[0]["segment"],
            "subject": stencil,
            "message": (
                f"Stencil {stencil} welded {len(welds)} pass"
                f"{'es' if len(welds) != 1 else ''} and holds a certificate, "
                f"but appears on no project welder log.{hint} The log is the "
                f"contractor's record of who was on site and it is incomplete."
            ),
            "detail": _detail(stencil=stencil, passes=len(welds),
                              near=", ".join(sorted(near))),
            "document_id": welds[0]["document_id"], "page_no": None,
        })
    return findings


def same_person(a: str, b: str) -> bool:
    """Whether two roster names are one welder spelled two ways.

    `MICHAEL MUNOZ` and `MICHEAL MUNOZ` are transposed vowels in a name typed
    into sixteen separate spreadsheets, not two men sharing a stencil.
    """
    from rapidfuzz.distance import DamerauLevenshtein

    left, right = (re.sub(r"[^A-Z]", "", (n or "").upper()) for n in (a, b))
    if not left or not right:
        return False
    return DamerauLevenshtein.distance(left, right) <= 1


def _tenures(db: Database, project_id: int) -> dict[str, list[dict]]:
    """Each stencil's holders, with the window each was on the job."""
    out: dict[str, list[dict]] = defaultdict(list)
    for r in db.q(
        """SELECT stencil, name, MIN(arrived) arrived, MAX(left_job) left_job,
                  MAX(IFNULL(left_job,'')='') open_ended,
                  GROUP_CONCAT(DISTINCT segment) segments
           FROM welder_roster WHERE project_id=? AND stencil<>'' AND name<>''
           GROUP BY stencil, name""",
        (project_id,),
    ):
        holders = out[r["stencil"]]
        segments = {s for s in (r["segments"] or "").split(",") if s}
        for existing in holders:
            if same_person(existing["name"], r["name"]):
                existing["aliases"].add(r["name"])
                existing["segments"] |= segments
                break
        else:
            holders.append({"name": r["name"], "aliases": {r["name"]},
                            "arrived": r["arrived"] or "",
                            "left_job": r["left_job"] or "",
                            "open_ended": bool(r["open_ended"]),
                            "segments": segments})
    return out


def _logs(holder: dict) -> str:
    """'the 20 LP log', or 'seven logs' — enough to find them, short enough to read."""
    segments = sorted(holder["segments"])
    if not segments:
        return "an unnamed log"
    if len(segments) == 1:
        return f"the {segments[0]} log"
    return f"{len(segments)} logs including {segments[0]}"


def _overlaps(a: dict, b: dict) -> bool:
    """Whether two tenures were current at the same time."""
    a_end = "9999" if a["open_ended"] or not a["left_job"] else a["left_job"]
    b_end = "9999" if b["open_ended"] or not b["left_job"] else b["left_job"]
    return a["arrived"] <= b_end and b["arrived"] <= a_end


@register("ROS-05", "One stencil used by two welders at the same time")
def stencil_shared(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A stencil held by two people whose time on the job overlaps.

    Stencils get reissued when a welder leaves, and on Bluewater that is routine
    — eight of seventeen pass between crews. Reissue is not a defect and is
    reported once for the job as context. What is a defect is two welders
    holding one stencil *simultaneously*, because then no weld stamped with it
    can be attributed to either.
    """
    tenures = _tenures(db, project_id)
    findings: list[Finding] = []
    reissued: list[str] = []

    for stencil, holders in sorted(tenures.items()):
        if len(holders) < 2:
            continue
        clashing = [(a, b) for i, a in enumerate(holders)
                    for b in holders[i + 1:] if _overlaps(a, b)]
        if not clashing:
            reissued.append(
                f"{stencil} ({' then '.join(h['name'] for h in sorted(holders, key=lambda h: h['arrived']))})")
            continue
        a, b = clashing[0]
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "ROS-05",
            "severity": "major", "segment": "", "subject": stencil,
            "message": (
                f"Stencil {stencil} is recorded against {a['name']} "
                f"({a['arrived']} to {a['left_job'] or 'still on job'}) and "
                f"{b['name']} ({b['arrived']} to "
                f"{b['left_job'] or 'still on job'}) — overlapping periods, on "
                f"{_logs(a)} and {_logs(b)} respectively. Unless stencils run "
                f"per spread rather than per job, no weld stamped {stencil} "
                f"can be attributed to either while both held it."
            ),
            "detail": _detail(stencil=stencil,
                              holders=" | ".join(
                                  f"{h['name']} {h['arrived']}..{h['left_job']}"
                                  f" [{', '.join(sorted(h['segments']))}]"
                                  for h in holders)),
            "document_id": None, "page_no": None,
        })

    if reissued:
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "ROS-05",
            "severity": "info", "segment": "",
            "subject": f"{len(reissued)} stencils",
            "message": (
                f"{len(reissued)} stencil{'s were' if len(reissued) != 1 else ' was'} "
                f"passed from one welder to another during this job, without "
                f"their times on site overlapping: {'; '.join(sorted(reissued)[:8])}"
                f"{'...' if len(reissued) > 8 else ''}. Normal practice, but it "
                f"means a stencil alone does not identify the welder — the "
                f"weld date is needed too, and continuity for these welders is "
                f"split across more than one stencil."
            ),
            "detail": _detail(stencils="; ".join(sorted(reissued))),
            "document_id": None, "page_no": None,
        })
    return findings


@register("ROS-06", "No project welder log filed for this job")
def no_roster(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """Welding recorded and no welder log filed at all."""
    if roster(db, project_id):
        return []
    welders = db.one(
        "SELECT COUNT(DISTINCT stencil) n FROM welder_pass "
        "WHERE project_id=? AND stencil<>''", (project_id,))["n"]
    if not welders:
        return []
    return [{
        "project_id": project_id, "run_id": run_id, "rule": "ROS-06",
        "severity": "major", "segment": "", "subject": f"{welders} stencils",
        "message": (
            f"{welders} stencils welded on this job and no project welder log "
            f"is filed. Nothing records who those stencils belong to, when "
            f"they were on site, or when their tickets fall due — so the "
            f"welder checks rest on the certificates alone."
        ),
        "detail": _detail(stencils=welders),
        "document_id": None, "page_no": None,
    }]


def roster_summary(db: Database, project_id: int) -> list[dict]:
    """Every welder on the log, with what they actually welded."""
    welds = _welds(db, project_id)
    certified = {r["stencil"] for r in db.q(
        "SELECT DISTINCT stencil FROM welder_cert WHERE project_id=? AND stencil<>''",
        (project_id,))}

    out: list[dict] = []
    for stencil, entry in sorted(roster(db, project_id).items()):
        mine = welds.get(stencil, [])
        out.append({
            "stencil": stencil,
            "name": " / ".join(sorted(entry["names"])),
            "material": ", ".join(sorted(entry["materials"])),
            "cert_date": entry["cert_date"],
            "requal_date": entry["requal_date"],
            "next_requal": entry["next_requal"],
            "arrived": entry["arrived"],
            "left_job": entry["left_job"] if not entry["still_on_job"] else "",
            "reason": " / ".join(sorted(entry["reasons"])),
            "passes": len(mine),
            "first_weld": mine[0]["date_welded"] if mine else "",
            "last_weld": mine[-1]["date_welded"] if mine else "",
            "certificate": stencil in certified,
            "segments": ", ".join(sorted(entry["segments"])),
        })
    return out
