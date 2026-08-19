"""Reconciling the flange logs against themselves.

A bolted joint has no non-destructive examination and no pressure record of
its own — the torque log is the only evidence it was made up correctly, and it
is a record the crew writes about its own work.  So the question is not "does
this match the specification" but "does this record hold together".

That distinction is forced by the corpus rather than chosen. **No document on
any of these jobs states a target torque.** There is no equivalent of
GPPB-0140 for bolting, and a ft-lb figure depends on stud diameter and
material, gasket type, lubricant and the stress the designer wanted — so a
rule carrying a made-up number would produce confident findings an auditor
cannot defend, on the one part of the audit with no independent check.

What the logs *can* be asked is whether they agree with themselves, and the
answer is often no. Across Bluewater's 27 logs, a 2" class 300 flange with a CGI
gasket and eight bolts is torqued to 90 ft-lb on 116 joints and to 320 on one;
a 6" class 600 joint gets six different final torques. Whichever figure is
right, the others are wrong, and no external standard is needed to say so.

The other half is sign-off: the log's own "Copy of Torque Wrench Cert (Verify
calibration)" box, the inspector's initials, and whether the wrench named has
a certificate on the job at all.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import date

from ..db import Database
from ..instruments import nearest_serials
from . import Finding, register

#: Two final torques for the same joint configuration are the same target if
#: they are this close: crews round differently and 165 against 168 ft-lb is
#: not a disagreement worth an auditor's time.
TORQUE_TOLERANCE = 0.05

#: Above this spread the configuration has no single target at all, rather
#: than one target recorded with slips.
WIDE_SPREAD = 0.25

#: A bolt-up cannot predate the job by more than this and be a real date.
#: The 16" PW log carries a joint dated 2006 on a job that started in 2025.
GRACE_DAYS = 30


def _detail(**kw) -> str:
    return json.dumps({k: v for k, v in kw.items() if v not in (None, "")})


def _joints(db: Database, project_id: int) -> list:
    return db.q(
        """SELECT f.*, d.filename FROM flange f
           LEFT JOIN document d ON d.id = f.document_id
           WHERE f.project_id=? ORDER BY f.segment, f.sheet, f.row_no""",
        (project_id,),
    )


def configuration(joint) -> tuple | None:
    """What makes two flange joints the same job, for torque purposes.

    Size, class, gasket and bolt count together determine the target: change
    any one and the correct torque changes with it. Lubricant matters too, but
    it is recorded as `KK` on essentially every row in the corpus, so keying on
    it would only split groups without separating anything.
    """
    if joint["nps"] is None or joint["pressure_class"] is None:
        return None
    if joint["round3"] is None:
        return None
    return (joint["nps"], joint["pressure_class"],
            joint["gasket"] or "", joint["bolts"])


def _describe(key: tuple) -> str:
    nps, cls, gasket, bolts = key
    bolt_text = f", {bolts:g} bolts" if bolts else ""
    return f'{nps:g}" class {cls:g} {gasket or "flange"}{bolt_text}'.replace("  ", " ")


def _where(joints: list) -> str:
    """The logs a set of joints came from, for the detail column."""
    return ", ".join(sorted({j["filename"] for j in joints if j["filename"]})[:6])


def _verb(n: int, singular: str) -> str:
    """'1 joint records' / '2 joints record'."""
    return singular if n != 1 else singular + ("es" if singular.endswith("ss")
                                               else "s")


# ---------------------------------------------------------------------------


@register("FLG-01", "Identical flanges torqued to different values")
def torque_disagreement(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """One flange configuration with more than one final torque on the job.

    Reported once per configuration, not once per joint: 116 joints at 90
    ft-lb and one at 320 is a single question — which is the target — and
    raising it 117 times would bury it.
    """
    groups: dict[tuple, list] = defaultdict(list)
    for joint in _joints(db, project_id):
        if key := configuration(joint):
            groups[key].append(joint)

    findings: list[Finding] = []
    for key, joints in sorted(groups.items()):
        torques = Counter(j["round3"] for j in joints)
        if len(torques) < 2:
            continue
        low, high = min(torques), max(torques)
        spread = (high - low) / high
        if spread <= TORQUE_TOLERANCE:
            continue

        common, common_n = torques.most_common(1)[0]
        odd = sorted(t for t in torques if t != common)
        listed = ", ".join(
            f"{t:g} ft-lb on {torques[t]} joint{'s' if torques[t] != 1 else ''}"
            for t in sorted(torques, key=lambda t: -torques[t]))

        if spread >= WIDE_SPREAD and len(torques) > 2:
            severity = "major"
            verdict = (f"No single target was used: the log carries "
                       f"{len(torques)} different final torques for this joint.")
        else:
            severity = "major" if spread >= WIDE_SPREAD else "minor"
            verdict = (f"{common:g} ft-lb is the figure used on {common_n} of "
                       f"{len(joints)} joints, so "
                       f"{', '.join(f'{t:g}' for t in odd)} "
                       f"{'is' if len(odd) == 1 else 'are'} the exception.")

        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "FLG-01",
            "severity": severity, "segment": joints[0]["segment"],
            "subject": _describe(key),
            "message": (
                f"{_describe(key)} is torqued to {listed}. {verdict} Whichever "
                f"figure is correct, the others are not — and no document on "
                f"this job states a target torque to settle it."
            ),
            "detail": _detail(configuration=_describe(key), values=listed,
                              spread=f"{spread:.0%}", logs=_where(joints)),
            "document_id": joints[0]["document_id"], "page_no": None,
        })
    return findings


@register("FLG-02", "Torque rounds are inconsistent")
def rounds_inconsistent(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """Rounds that do not step up, or a final pass under the 100% round.

    Deliberately structural rather than proportional. The form labels its
    columns 30% / 60% / 100%, but the crews work in thirds as often as not and
    both are accepted bolting practice, so holding them to the printed
    percentages would report a third of the corpus. That the rounds rise, and
    that the last pass matches the 100% round, is true either way.
    """
    findings: list[Finding] = []
    for joint in _joints(db, project_id):
        r1, r2, r3, r4 = (joint["round1"], joint["round2"],
                          joint["round3"], joint["round4"])
        problems, because = [], []
        if None not in (r1, r2, r3) and not (r1 < r2 <= r3):
            problems.append(
                f"the rounds run {r1:g}, {r2:g}, {r3:g} rather than rising")
            because.append("a joint pulled up out of sequence loads its gasket "
                           "unevenly, and the record cannot show it was not")
        # Only a final pass *below* the 100% round matters. Coming back round
        # at slightly more than the target is a check pass, not a defect, and
        # firing on 168 against 165 would report ordinary practice.
        if None not in (r3, r4) and r4 < r3:
            problems.append(
                f"the final pass is {r4:g} ft-lb against a 100% round of {r3:g}")
            because.append("the joint was left below the torque the log itself "
                           "sets as the target")
        if not problems:
            continue
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "FLG-02",
            "severity": "major", "segment": joint["segment"],
            "subject": f"flange {joint['flange_no']}",
            "message": (
                f"Flange {joint['flange_no']} on {joint['filename']}: "
                f"{' and '.join(problems)} — {'; '.join(because)}."
            ),
            "detail": _detail(flange=joint["flange_no"], round1=r1, round2=r2,
                              round3=r3, round4=r4, sheet=joint["sheet"]),
            "document_id": joint["document_id"], "page_no": None,
        })
    return findings


@register("FLG-03", "Torque wrench has no calibration certificate")
def wrench_uncalibrated(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A wrench serial on the log with no certificate filed for it.

    Grouped by serial rather than by joint, and a serial one character from
    one on file is reported as the transcription it almost certainly is: the
    logs write `0322600192` on 45 joints and `0323600192` on seven, and only
    one wrench exists.
    """
    certs = {
        r["serial_key"]: r["serial"]
        for r in db.q(
            "SELECT serial_key, serial FROM instrument_cal "
            "WHERE project_id=? AND serial_key<>''", (project_id,),
        )
    }
    if not certs:
        return []

    used: dict[str, list] = defaultdict(list)
    for joint in _joints(db, project_id):
        if joint["wrench_key"]:
            used[joint["wrench_key"]].append(joint)

    findings: list[Finding] = []
    for key, joints in sorted(used.items()):
        if key in certs:
            continue
        serial = joints[0]["wrench"]
        near = [certs[k] for k in nearest_serials(serial, set(certs))]
        count = len(joints)
        joint_text = f"{count} joint{'s' if count != 1 else ''}"
        if near:
            severity = "minor"
            message = (
                f"The flange logs record torque wrench {serial} on "
                f"{joint_text}, and no certificate for it is on the job — but "
                f"{' or '.join(near)} is certified and differs by one "
                f"character. Almost certainly the same wrench written down "
                f"wrong; worth correcting the log rather than chasing a wrench."
            )
        else:
            severity = "major"
            message = (
                f"The flange logs record torque wrench {serial} on "
                f"{joint_text}, and no calibration certificate for it is filed "
                f"on this job. An uncalibrated wrench cannot evidence the "
                f"torque it applied."
            )
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "FLG-03",
            "severity": severity, "segment": joints[0]["segment"],
            "subject": serial,
            "message": message,
            "detail": _detail(serial=serial, joints=count,
                              near_match=", ".join(near), logs=_where(joints)),
            "document_id": joints[0]["document_id"], "page_no": None,
        })
    return findings


@register("FLG-04", "Flange joint records no torque wrench")
def no_wrench(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """Joints torqued with no wrench serial written down.

    Grouped per log, because a blank column is a habit rather than an event.
    """
    return _blank_column_rule(
        db, project_id, run_id, "FLG-04",
        predicate=lambda j: not j["wrench"] and j["round3"] is not None,
        summary=lambda n, total: (
            f"{n} of {total} joints {_verb(n, 'record')} no torque wrench "
            f"serial, so the torque applied to {'them' if n != 1 else 'it'} "
            f"cannot be tied to a calibrated tool."),
    )


@register("FLG-05", "Wrench calibration not verified on the log")
def calibration_not_verified(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """The log's own verification box, left empty.

    The template's column is headed "Copy of Torque Wrench Cert (Verify
    calibration)" — the form asks the inspector to confirm the certificate at
    the joint, which is a stronger statement than a certificate merely
    existing somewhere in the book.
    """
    return _blank_column_rule(
        db, project_id, run_id, "FLG-05",
        predicate=lambda j: not j["cert_checked"] and j["round3"] is not None,
        summary=lambda n, total: (
            f"{n} of {total} joints {_verb(n, 'leave')} the \"Copy of Torque "
            f"Wrench Cert (Verify calibration)\" box empty. The form asks the "
            f"inspector to confirm the wrench was in calibration at the joint, "
            f"and on {'these rows' if n != 1 else 'this row'} nobody did."),
    )


@register("FLG-06", "Flange joint has no inspector sign-off")
def no_signoff(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """Joints with no initials in the inspector column.

    A torque log is a record of the contractor's own work; the inspector's
    initials are what make it witnessed.
    """
    return _blank_column_rule(
        db, project_id, run_id, "FLG-06",
        predicate=lambda j: not j["inspector"] and j["round3"] is not None,
        summary=lambda n, total: (
            f"{n} of {total} joints {_verb(n, 'carry')} no inspector initials. "
            f"Nothing shows {'these bolt-ups were' if n != 1 else 'this bolt-up was'} "
            f"witnessed rather than self-certified."),
    )


def _blank_column_rule(db: Database, project_id: int, run_id: str, code: str,
                       predicate, summary) -> list[Finding]:
    """One finding per log for a column left blank on some of its rows."""
    by_log: dict[tuple, list] = defaultdict(list)
    for joint in _joints(db, project_id):
        by_log[(joint["document_id"], joint["sheet"])].append(joint)

    findings: list[Finding] = []
    for (document_id, sheet), joints in by_log.items():
        torqued = [j for j in joints if j["round3"] is not None]
        blank = [j for j in joints if predicate(j)]
        if not blank or not torqued:
            continue
        first = joints[0]
        where = first["filename"] or first["segment"] or "this log"
        tab = f" ({sheet})" if sheet and sheet.lower() != "flange log" else ""
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": code,
            "severity": "major", "segment": first["segment"],
            "subject": where,
            "message": f"{where}{tab}: {summary(len(blank), len(torqued))}",
            "detail": _detail(log=where, sheet=sheet, blank=len(blank),
                              joints=len(torqued),
                              flanges=", ".join(j["flange_no"] for j in blank[:30])),
            "document_id": document_id, "page_no": None,
        })
    return findings


@register("FLG-07", "Flange bolt-up dated before the job")
def impossible_date(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A bolt-up dated before the job start the same log declares.

    The 16" PW log dates a joint to January 2006 on a job that started in June
    2025, and the rows below it run 1-9-26 through 1-9-30 — a column dragged
    down in Excel rather than dates anyone wrote.
    """
    findings: list[Finding] = []
    by_log: dict[int, list] = defaultdict(list)
    for joint in _joints(db, project_id):
        if joint["bolted_on"] and joint["job_start"]:
            by_log[joint["document_id"]].append(joint)

    for document_id, joints in by_log.items():
        start = _as_date(joints[0]["job_start"])
        if not start:
            continue
        early = [j for j in joints
                 if (when := _as_date(j["bolted_on"]))
                 and (start - when).days > GRACE_DAYS]
        if not early:
            continue
        first = joints[0]
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "FLG-07",
            "severity": "minor", "segment": first["segment"],
            "subject": f"{len(early)} joints",
            "message": (
                f"{first['filename'] or 'This flange log'} dates "
                f"{len(early)} joint{'s' if len(early) != 1 else ''} before the "
                f"job started on {first['job_start']} — the earliest is "
                f"{min(j['bolted_on'] for j in early)}. The bolt-up date is "
                f"the only record of when the joint was made up, so a wrong "
                f"one cannot be reconciled against anything later."
            ),
            "detail": _detail(job_start=first["job_start"],
                              dates=", ".join(sorted(
                                  {j["bolted_on"] for j in early})[:12]),
                              flanges=", ".join(j["flange_no"] for j in early[:20])),
            "document_id": document_id, "page_no": None,
        })
    return findings


def _as_date(text) -> date | None:
    try:
        return date.fromisoformat(str(text or "")[:10])
    except ValueError:
        return None


@register("FLG-08", "Flange map with no torque log")
def map_without_log(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A segment whose drawings show flanges and which has no torque log.

    This is deliberately a segment-level statement and not a count. The maps
    number their flanges and so do the logs, but the two cite different
    drawing series — logs reference the isometric, maps reference the flange
    mapping drawing — and of thirty drawings named across Bluewater's maps and
    logs exactly one appears in both. There is no reliable way to say *which*
    flanges went untorqued, only that a segment drawn with flanges has no
    record of any being made up.
    """
    logged = {r["segment"] for r in db.q(
        "SELECT DISTINCT segment FROM flange WHERE project_id=? AND segment<>''",
        (project_id,))}
    if not logged:
        return []

    findings: list[Finding] = []
    for row in db.q(
        """SELECT segment, SUM(balloons) balloons, COUNT(*) maps
           FROM flange_map WHERE project_id=? AND segment<>''
           GROUP BY segment ORDER BY segment""",
        (project_id,),
    ):
        if row["segment"] in logged:
            continue
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "FLG-08",
            "severity": "major", "segment": row["segment"],
            "subject": row["segment"],
            "message": (
                f"This segment has {row['maps']} flange map"
                f"{'s' if row['maps'] != 1 else ''} ballooning "
                f"{row['balloons']} flanges and no torque log filed against "
                f"it, while other segments on this job have one. Nothing "
                f"records these joints being made up."
            ),
            "detail": _detail(maps=row["maps"], balloons=row["balloons"],
                              logged_segments=", ".join(sorted(logged))),
            "document_id": None, "page_no": None,
        })
    return findings


def flange_summary(db: Database, project_id: int) -> list[dict]:
    """One row per torque log: what it recorded and what it left blank."""
    by_log: dict[tuple, list] = defaultdict(list)
    for joint in _joints(db, project_id):
        by_log[(joint["document_id"], joint["sheet"])].append(joint)

    out: list[dict] = []
    for (document_id, sheet), joints in by_log.items():
        torqued = [j for j in joints if j["round3"] is not None]
        sizes = sorted({j["nps"] for j in joints if j["nps"] is not None})
        out.append({
            "segment": joints[0]["segment"] or "",
            "log": joints[0]["filename"] or "",
            "sheet": sheet or "",
            "service": joints[0]["service"] or "",
            "joints": len(joints),
            "torqued": len(torqued),
            "sizes": ", ".join(f"{s:g}\"" for s in sizes),
            "wrenches": ", ".join(sorted({j["wrench"] for j in joints if j["wrench"]})),
            "no_wrench": sum(1 for j in torqued if not j["wrench"]),
            "not_verified": sum(1 for j in torqued if not j["cert_checked"]),
            "no_signoff": sum(1 for j in torqued if not j["inspector"]),
            "document_id": document_id,
        })
    out.sort(key=lambda r: (r["segment"], r["log"], r["sheet"]))
    return out
