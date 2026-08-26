"""What a WeldTrace download says against itself.

Three registers arrive in a download and every rule here is a comparison
across two of them: the weld register against the heat register, the weld
register against the stamps on the isometrics, a test pack against the report
numbers its welds cite.  Nothing needs a fourth document, which is the point -
these findings are available the moment the folder is indexed, with no OCR
pass and no button to press.

The severities say what a finding costs the package rather than how hard it is
to fix.  Critical means the package cannot be signed as it stands: a weld with
no procedure, a heat that is in no register, an examination that was asked for
and never reported.  Major means a contradiction between two documents that a
person has to resolve.  Minor is a data-entry artefact worth listing and
nothing more.

Every rule reads the ``weldtrace_*`` tables and never an export, so the whole
family re-runs in seconds.  The register-against-drawings match is not redone
here either; :mod:`weldaudit.extract.weldtrace` makes it once, when it has both
sides in memory, and writes down which weld each stamp was matched to.
"""

from __future__ import annotations

import json
from collections import defaultdict

from ..db import Database
from ..extract.weldtrace import PARSED_KINDS
from ..weldtrace import HeatRow, split_trailing_revision
from . import Finding, register


def _detail(**kw) -> str:
    return json.dumps({k: v for k, v in kw.items() if v not in (None, "")})


def _welds(db: Database, project_id: int) -> list:
    """Every WeldTrace weld in the project, register order."""
    return db.q(
        "SELECT * FROM weldtrace_weld WHERE project_id=? ORDER BY id",
        (project_id,))


def _heats(db: Database, project_id: int) -> list:
    return db.q(
        "SELECT * FROM weldtrace_heat WHERE project_id=? ORDER BY heat",
        (project_id,))


def _exams(db: Database, project_id: int) -> dict[int, list]:
    """``{weldtrace_weld id: examinations}``, requested ones only.

    A method the export does not ask for and did not report is the ordinary
    case - six of the eight blocks on most rows - and carrying those into the
    rules would mean every rule below starting with the same filter.
    """
    out: dict[int, list] = defaultdict(list)
    for r in db.q(
        """SELECT * FROM weldtrace_exam
           WHERE project_id=? AND (requested=1 OR verdict<>'' OR report<>'')
           ORDER BY id""",
        (project_id,),
    ):
        out[r["weldtrace_weld_id"]].append(r)
    return out


def _key(weld) -> str:
    """How a weld is named in a finding: ``TP-1-1/W-22``."""
    return (f"{weld['test_pack']}/{weld['weld_no']}"
            if weld["test_pack"] else weld["weld_no"])


def _finding(weld, run_id: str, rule: str, severity: str, message: str,
             detail: str = "") -> Finding:
    """One weld-scoped finding, filled in from the row it came off."""
    return {
        "project_id": weld["project_id"], "run_id": run_id, "rule": rule,
        "severity": severity, "segment": weld["segment"] or "",
        "subject": _key(weld), "message": message, "detail": detail,
        "document_id": weld["document_id"], "page_no": None,
    }


def _opening(text: str) -> str:
    """The first character upper-cased, and nothing else touched.

    ``str.capitalize`` lower-cases everything after it, which turns a heat
    status, a product form or a drawing number into something the register
    does not say - and these messages quote the register.
    """
    return text[:1].upper() + text[1:]


_PASSED = ("pass", "accept", "sat")
_FAILED = ("fail", "reject", "unsat")


def _verdict_of(exam) -> str:
    return (exam["verdict"] or "").strip().lower()


# ---------------------------------------------------------------------------
# The download itself
# ---------------------------------------------------------------------------


@register("WT-01", "WeldTrace download is missing one of its exports")
def download_incomplete(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A download that arrived without one of the three registers.

    Reported once, against the project, and only where at least one WeldTrace
    export *is* present: a job that keeps paper registers has none of the three
    and is not an incomplete download.

    The consequence is specific to which one is absent, and saying so is the
    whole value of the finding - without the heat register every weld reports
    an unknown heat, and a reader who does not know the file is missing will
    read that as a hundred material failures rather than as one absent export.
    """
    have = {
        kind: db.one(
            "SELECT COUNT(*) AS n FROM document WHERE project_id=? AND kind=?",
            (project_id, kind))["n"]
        for kind, _name in PARSED_KINDS
    }
    if not any(have.values()):
        return []
    missing = [name for kind, name in PARSED_KINDS if not have[kind]]
    if not missing:
        return []

    consequence = {
        "weldtrace_welds": "there is no register of what was welded, so every "
                           "stamp on the drawings reports as an orphan",
        "weldtrace_materials": "no heat on any weld can be resolved, so every "
                               "joint reports an unknown heat",
        "weldtrace_stamps": "the register cannot be checked against the "
                            "isometrics at all",
    }
    absent = [kind for kind, _n in PARSED_KINDS if not have[kind]]
    return [{
        "project_id": project_id, "run_id": run_id, "rule": "WT-01",
        "severity": "critical", "segment": "(project)",
        "subject": f"{len(missing)} export{'s' if len(missing) != 1 else ''}",
        "message": (
            f"This WeldTrace download is missing {' and '.join(missing)}. "
            + _opening("; ".join(consequence[k] for k in absent))
            + ". Re-export the download with all three registers rather than "
              "reading the findings below as the state of the package."
        ),
        "detail": _detail(missing="; ".join(missing)),
        "document_id": None, "page_no": None,
    }]


# ---------------------------------------------------------------------------
# The weld register on its own
# ---------------------------------------------------------------------------


@register("WT-02", "Weld has no welding procedure")
def wps_missing(db: Database, project_id: int, run_id: str) -> list[Finding]:
    return [
        _finding(w, run_id, "WT-02", "critical",
                 "No welding procedure is recorded against this weld, so "
                 "there is nothing to check the welder's qualification or the "
                 "essential variables against.")
        for w in _welds(db, project_id) if not w["wps"]
    ]


@register("WT-03", "Pass has no welder")
def welder_missing(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A root, fill or cap pass with no stencil against it.

    One finding per weld rather than per pass: three findings on one joint say
    the same thing three times, and the passes are named in the message.
    """
    findings: list[Finding] = []
    for w in _welds(db, project_id):
        passes = [p for p in (w["passes_unmanned"] or "").split("; ") if p]
        if not passes:
            continue
        findings.append(_finding(
            w, run_id, "WT-03", "critical",
            f"No welder ID is recorded for the "
            f"{', '.join(passes)} pass{'es' if len(passes) != 1 else ''}. "
            f"The joint cannot be tied to a qualified welder.",
            _detail(passes=", ".join(passes), welders=w["welders"])))
    return findings


@register("WT-04", "Weld has no date")
def date_welded_missing(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """No weld date, or one no parser accepts.

    The two are one finding because the export gives no way to tell them
    apart once read - a date the parser rejected arrives blank, and the sample
    download contains ``8/222/2026`` to prove the case is real - so the
    message names both possibilities rather than asserting the wrong one.
    """
    return [
        _finding(w, run_id, "WT-04", "critical",
                 "No weld date is recorded, or the date recorded is not one "
                 "any format reads. Nothing can be sequenced against this "
                 "joint: not the examination, not the pressure test, not the "
                 "welder's continuity.")
        for w in _welds(db, project_id) if not w["date_welded"]
    ]


@register("WT-05", "Welded before the planned date")
def date_before_plan(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """Usually an order-of-operations artefact, and still worth listing.

    A weld dated before the plan that called for it is nearly always the plan
    date being entered after the fact rather than a joint made early. It is
    reported as minor for that reason, and reported at all because the one
    case in a hundred where it is real is a joint made against a superseded
    drawing.
    """
    return [
        _finding(w, run_id, "WT-05", "minor",
                 f"Welded {w['date_welded']} against a planned date of "
                 f"{w['date_planned']}. Normally the plan date was entered "
                 f"after the joint was made; check that it was not welded to "
                 f"a superseded revision.",
                 _detail(planned=w["date_planned"], welded=w["date_welded"]))
        for w in _welds(db, project_id)
        if w["date_planned"] and w["date_welded"]
        and w["date_welded"] < w["date_planned"]
    ]


@register("WT-06", "Test pack carries more than one reference number")
def pack_reference_split(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """One pack, two reference numbers.

    The pack reference is what every examination report on those welds is
    supposed to cite, so a pack issued under two of them makes WT-14
    unanswerable: half the welds will disagree with whichever number is
    treated as correct, and there is no way to tell from the export which half.
    """
    packs: dict[tuple[str, str], set[str]] = defaultdict(set)
    rows: dict[tuple[str, str], object] = {}
    for w in _welds(db, project_id):
        if not w["test_pack"] or not w["pack_reference"]:
            continue
        key = (w["segment"] or "", w["test_pack"])
        packs[key].add(w["pack_reference"])
        rows.setdefault(key, w)

    findings: list[Finding] = []
    for (segment, pack), refs in sorted(packs.items()):
        if len(refs) < 2:
            continue
        w = rows[(segment, pack)]
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "WT-06",
            "severity": "major", "segment": segment, "subject": pack,
            "message": (
                f"Test pack {pack} carries {len(refs)} different reference "
                f"numbers ({', '.join(sorted(refs))}). Every examination "
                f"report on these welds is checked against the pack "
                f"reference, and with two of them on one pack there is no "
                f"way to say which report numbers are wrong."
            ),
            "detail": _detail(references="; ".join(sorted(refs))),
            "document_id": w["document_id"], "page_no": None,
        })
    return findings


# ---------------------------------------------------------------------------
# The weld register against the heat register
# ---------------------------------------------------------------------------


def _sides(weld) -> tuple[tuple[int, str, str], ...]:
    """``(side, heat, material code)`` for each end of the joint."""
    return ((1, weld["heat_1"], weld["material_1"]),
            (2, weld["heat_2"], weld["material_2"]))


@register("WT-07", "Joint records no heat on one side")
def heat_missing(db: Database, project_id: int, run_id: str) -> list[Finding]:
    findings: list[Finding] = []
    for w in _welds(db, project_id):
        blank = [str(side) for side, heat, _m in _sides(w) if not heat]
        if not blank:
            continue
        findings.append(_finding(
            w, run_id, "WT-07", "critical",
            f"Material {' and '.join(blank)} on this joint has no heat "
            f"number, so that end of the weld cannot be traced to a "
            f"certificate or checked against the approved list.",
            _detail(material_1=w["material_1"], material_2=w["material_2"])))
    return findings


@register("WT-08", "Heat on a weld is in no material register")
def heat_unknown(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A heat welded into the line that the heat register never received.

    Distinct from MTR-01, which asks whether a certificate is filed. This asks
    the earlier question: whether the material was ever booked onto the job at
    all. A heat that is in no register has no certificate, no supplier and no
    specification, so nothing downstream can be checked about it.
    """
    known = {r["heat"] for r in _heats(db, project_id)}
    if not known:
        return []          # no heat register at all - that is WT-01, once
    findings: list[Finding] = []
    for w in _welds(db, project_id):
        absent = [heat for _side, heat, _m in _sides(w)
                  if heat and heat not in known]
        if not absent:
            continue
        findings.append(_finding(
            w, run_id, "WT-08", "critical",
            f"Heat {' and '.join(sorted(set(absent)))} is welded into this "
            f"joint but is in no material register on the job. Nothing can be "
            f"traced about it: no certificate, no supplier, no specification.",
            _detail(heats=", ".join(sorted(set(absent))))))
    return findings


@register("WT-09", "Product form disagrees with the material register")
def heat_form_mismatch(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """The weld calls a heat one thing and the register calls it another.

    Compared case-insensitively and only where both sides say something: the
    two columns are free text filled in by different people, and a register
    that leaves the form blank is a gap rather than a disagreement.
    """
    form = {r["heat"]: (r["product_form"] or r["fitting_type"] or "")
            for r in _heats(db, project_id)}
    findings: list[Finding] = []
    for w in _welds(db, project_id):
        # Keyed by the disagreement rather than by the side: both ends of a
        # joint are frequently the same heat out of the same length of pipe,
        # and saying so twice makes the report longer without making it say
        # anything more.
        wrong: dict[tuple[str, str, str], list[str]] = {}
        for side, heat, material in _sides(w):
            theirs = form.get(heat, "")
            if not heat or not material or not theirs:
                continue
            if material.strip().lower() == theirs.strip().lower():
                continue
            wrong.setdefault((heat, material, theirs), []).append(str(side))
        if not wrong:
            continue
        findings.append(_finding(
            w, run_id, "WT-09", "major",
            _opening("; ".join(
                f"material {' and '.join(sides)} is recorded on the weld as "
                f"{material} and in the register as {theirs}, both against "
                f"heat {heat}"
                for (heat, material, theirs), sides in wrong.items()))
            + ". One of the two is describing a different part.",
            _detail(**{heat: f"weld {material}, register {theirs}"
                       for heat, material, theirs in wrong})))
    return findings


@register("WT-10", "Heat used on a weld is not active")
def heat_inactive(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A heat welded in that the register has since marked something else.

    Quarantined, rejected, superseded - the register says only that it is not
    Active, so the finding says that and names the status rather than guessing
    at what it means.
    """
    status = {r["heat"]: (r["status"] or "") for r in _heats(db, project_id)}
    findings: list[Finding] = []
    for w in _welds(db, project_id):
        # One finding per heat, not per end: a joint with the same heat on
        # both sides is one piece of material, and one problem.
        stale: dict[str, str] = {}
        for _side, heat, _material in _sides(w):
            state = status.get(heat, "")
            if state and state.strip().lower() != "active":
                stale[heat] = state
        if not stale:
            continue
        findings.append(_finding(
            w, run_id, "WT-10", "major",
            _opening("; ".join(f"heat {heat} is welded into this joint and the "
                      f"material register records it as {state} rather than "
                      f"Active" for heat, state in stale.items()))
            + ". Either the material was used after it was set aside or the "
              "register is out of date.",
            _detail(**stale)))
    return findings


# ---------------------------------------------------------------------------
# The heat register on its own
# ---------------------------------------------------------------------------


def _heat_finding(heat, run_id: str, rule: str, severity: str, message: str,
                  detail: str = "") -> Finding:
    return {
        "project_id": heat["project_id"], "run_id": run_id, "rule": rule,
        "severity": severity, "segment": heat["segment"] or "",
        "subject": heat["heat"], "message": message, "detail": detail,
        "document_id": heat["document_id"], "page_no": None,
    }


@register("WT-11", "Heat has no MTR attached in the register")
def mtr_missing(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """The register points at no certificate for a heat.

    This is the register's own account of itself, and it is a stronger
    statement than MTR-01: that rule looks for a certificate anywhere in the
    package and can be satisfied by one filed under a name nothing links to
    the heat, while this one says the register was never told which document
    belongs to this material.
    """
    return [
        _heat_finding(h, run_id, "WT-11", "critical",
                      f"The material register attaches no MTR to heat "
                      f"{h['heat']}. Whatever is filed in section 7, the "
                      f"register cannot say which certificate covers this "
                      f"material.")
        for h in _heats(db, project_id) if not h["mtr_file"]
    ]


@register("WT-12", "Heat cannot be checked against the approved list")
def mtr_fields_blank(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """Supplier, spec, grade or P-number missing from the register.

    This is the rule that matters most on a WeldTrace job, and it is the one
    an empty report hides. The approved-manufacturer check needs four fields;
    they are optional in WeldTrace, and on the first download seen all four
    were blank on all nineteen heats. The AML check then runs, finds nothing
    to evaluate, and reports nothing - a shorter report that reads as a
    cleaner package.

    So the absence is the finding. It is reported per heat rather than once
    per project because which heats are unevaluable is what an auditor has to
    act on, and because a register that is half filled in is the common case
    once somebody starts fixing the export.
    """
    findings: list[Finding] = []
    for h in _heats(db, project_id):
        missing = [name for name, value in zip(
            HeatRow.AML_FIELDS,
            (h["supplier"], h["spec_no"], h["grade"], h["p_no"])) if not value]
        if not missing:
            continue
        findings.append(_heat_finding(
            h, run_id, "WT-12", "major",
            f"Heat {h['heat']} has no "
            f"{', '.join(missing[:-1])}{' or ' if len(missing) > 1 else ''}"
            f"{missing[-1]} in the material register, so approval of its "
            f"manufacturer cannot be evaluated at all. This is a WeldTrace "
            f"export setting, not a missing document: the fields are optional "
            f"and were left out of the download.",
            _detail(blank=", ".join(missing), mtr=h["mtr_file"])))
    return findings


@register("WT-13", "Heat is registered but welded into nothing")
def heat_unused(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """Material booked onto the job that no weld uses.

    Ordinary on a live job - it is stock - and reported as minor for that
    reason. It earns its place because the other reading is a weld register
    that is short of the joints this material went into.
    """
    used = set()
    for w in _welds(db, project_id):
        used.update(heat for _s, heat, _m in _sides(w) if heat)
    if not used:
        return []          # no weld register at all - that is WT-01, once
    return [
        _heat_finding(h, run_id, "WT-13", "minor",
                      f"Heat {h['heat']} is in the material register but is "
                      f"on no weld. Either it is stock that was never used, "
                      f"or the weld register is short of the joints it went "
                      f"into.")
        for h in _heats(db, project_id) if h["heat"] not in used
    ]


# ---------------------------------------------------------------------------
# Examinations
# ---------------------------------------------------------------------------


@register("WT-14", "Examination requested and never reported")
def result_missing(db: Database, project_id: int, run_id: str) -> list[Finding]:
    exams = _exams(db, project_id)
    findings: list[Finding] = []
    for w in _welds(db, project_id):
        absent = [e["method"] for e in exams.get(w["id"], [])
                  if e["requested"] == 1 and not (e["verdict"] or e["report"])]
        if not absent:
            continue
        findings.append(_finding(
            w, run_id, "WT-14", "critical",
            f"{', '.join(absent)} {'were' if len(absent) != 1 else 'was'} "
            f"requested against this weld and no result is recorded. The "
            f"joint is unexamined as far as the package can show.",
            _detail(methods=", ".join(absent))))
    return findings


@register("WT-15", "Examination failed with no retest")
def fail_no_retest(db: Database, project_id: int, run_id: str) -> list[Finding]:
    exams = _exams(db, project_id)
    findings: list[Finding] = []
    for w in _welds(db, project_id):
        failed = [e for e in exams.get(w["id"], [])
                  if _verdict_of(e).startswith(_FAILED)
                  and not (e["retest_verdict"] or e["retest_requested"] == 1)]
        if not failed:
            continue
        findings.append(_finding(
            w, run_id, "WT-15", "critical",
            f"{', '.join(e['method'] for e in failed)} rejected this weld and "
            f"no retest follows. The joint is in the line with an open "
            f"rejection against it.",
            _detail(**{e["method"]: e["verdict"] for e in failed})))
    return findings


@register("WT-16", "Examination result is not a verdict")
def result_unclear(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A result that is neither an acceptance nor a rejection.

    Reported rather than assumed either way. A blank is WT-14; this is a cell
    with something in it that does not decide the joint - ``In progress``,
    ``See report``, a date typed into the wrong column - and reading it as a
    pass is exactly the error that lets a rejected weld through.
    """
    exams = _exams(db, project_id)
    findings: list[Finding] = []
    for w in _welds(db, project_id):
        unclear = [e for e in exams.get(w["id"], [])
                   if _verdict_of(e)
                   and not _verdict_of(e).startswith(_PASSED)
                   and not _verdict_of(e).startswith(_FAILED)]
        if not unclear:
            continue
        findings.append(_finding(
            w, run_id, "WT-16", "major",
            "; ".join(f"{e['method']} reads '{e['verdict']}'" for e in unclear)
            + ". Neither an acceptance nor a rejection, so this weld is not "
              "shown as examined and is not shown as rejected either.",
            _detail(**{e["method"]: e["verdict"] for e in unclear})))
    return findings


@register("WT-17", "Report number disagrees with the test pack reference")
def report_reference_mismatch(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """An examination citing a report the pack was not issued under.

    This is the class of defect a page-by-page review does not catch. On the
    sample download every weld in one pack cited a report number one digit
    short of the pack's own reference - the same typo propagated across
    twenty-four welds, each of which looks right beside the last.

    The pack reference is compared with its own trailing revision split off,
    because a report is cited with a revision (``...-0``) and the pack is not.
    """
    exams = _exams(db, project_id)
    findings: list[Finding] = []
    for w in _welds(db, project_id):
        reference = w["pack_reference"]
        if not reference:
            continue
        wrong = [e for e in exams.get(w["id"], [])
                 if e["report"] and e["report"] != reference
                 and split_trailing_revision(e["report"])[0] != reference]
        if not wrong:
            continue
        findings.append(_finding(
            w, run_id, "WT-17", "major",
            "; ".join(f"{e['method']} cites report {e['report']}"
                      for e in wrong)
            + f", but this weld's test pack was issued under {reference}. "
              f"One of the two numbers is mistyped, and the report cannot be "
              f"found from the pack until it is settled.",
            _detail(pack_reference=reference,
                    **{e["method"]: e["report"] for e in wrong})))
    return findings


@register("WT-18", "No examination of any kind was requested")
def nde_none_requested(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A weld the export asks for nothing against.

    Every one of the eight method blocks is either explicitly not requested or
    silent, and nothing was reported. Sometimes correct - a joint outside the
    examination scope - and always worth stating, because the alternative
    reading is a weld that was left out of the NDE plan.
    """
    exams = _exams(db, project_id)
    return [
        _finding(w, run_id, "WT-18", "major",
                 "No examination of any method is requested against this weld "
                 "and none is reported. Either the joint is outside the "
                 "examination scope or it was left out of the plan; the "
                 "export does not say which.")
        for w in _welds(db, project_id) if not exams.get(w["id"])
    ]


# ---------------------------------------------------------------------------
# The register against the drawings
# ---------------------------------------------------------------------------


def _has_stamps(db: Database, project_id: int) -> bool:
    return bool(db.one("SELECT 1 AS n FROM weldtrace_stamp WHERE project_id=? "
                       "LIMIT 1", (project_id,)))


@register("WT-19", "Registered weld is stamped on no drawing")
def stamp_missing(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A weld in the register that appears on no isometric.

    Silent where the download carries no annotation export at all - that is
    WT-01, said once, rather than a finding against every weld on the job.
    """
    if not _has_stamps(db, project_id):
        return []
    return [
        _finding(w, run_id, "WT-19", "major",
                 "This weld is in the register but is stamped on no "
                 "isometric. The as-built does not show it, so there is no "
                 "drawing that says where in the line it is.")
        for w in _welds(db, project_id) if not w["stamps"]
    ]


@register("WT-20", "Weld is stamped on a different drawing than the register names")
def stamp_drawing_mismatch(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """Register and as-built disagree about which sheet a weld is on.

    The register fuses drawing and revision into one field and the annotation
    export keeps them apart, so the comparison is made on the drawing alone.
    Revision is not compared: a weld stamped on revision 0 of the sheet the
    register now calls revision 1 is the ordinary way a drawing is reissued,
    and reporting it would bury the case this rule is for.
    """
    if not _has_stamps(db, project_id):
        return []
    findings: list[Finding] = []
    for w in _welds(db, project_id):
        if not w["stamps"] or not w["drawing"]:
            continue
        stamped = [d for d in (w["stamped_on"] or "").split("; ") if d]
        if not stamped or w["drawing"] in stamped:
            continue
        findings.append(_finding(
            w, run_id, "WT-20", "major",
            f"The register puts this weld on {w['drawing']} and it is "
            f"stamped on {', '.join(stamped)}. Whichever is right, one "
            f"isometric shows a joint that is not there and another is "
            f"missing one that is.",
            _detail(register=w["drawing"], stamped_on="; ".join(stamped))))
    return findings


@register("WT-21", "Weld is stamped on a drawing but is in no test pack")
def stamp_orphan(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A stamp on an isometric that matched no weld in the register.

    Reported one per tag, and deliberately not resolved against the near
    misses. On the sample download ``W-81`` in the register and ``BFW-81`` on
    the drawing are almost certainly the same joint mistyped - but almost is
    not a thing to write into a turnover package, so both sides are reported:
    this rule for the stamp nothing claims, WT-19 for the weld nothing stamps.
    An auditor pairs them in a second; a tool that paired them automatically
    would also pair the ones that are genuinely two different welds.
    """
    rows = db.q(
        """SELECT weld_tag, segment, document_id,
                  GROUP_CONCAT(DISTINCT raw_tag) AS tags,
                  GROUP_CONCAT(DISTINCT drawing) AS drawings
           FROM weldtrace_stamp
           WHERE project_id=? AND (matched_weld_no IS NULL OR matched_weld_no='')
           GROUP BY weld_tag ORDER BY weld_tag""",
        (project_id,))
    if not rows:
        return []
    # With no register at all every stamp is an orphan, which is WT-01's
    # finding to make and not a hundred of these.
    if not db.one("SELECT 1 AS n FROM weldtrace_weld WHERE project_id=? LIMIT 1",
                  (project_id,)):
        return []

    findings: list[Finding] = []
    for r in rows:
        tags = (r["tags"] or "").replace(",", ", ")
        drawings = (r["drawings"] or "").replace(",", ", ")
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "WT-21",
            "severity": "critical", "segment": r["segment"] or "",
            "subject": r["weld_tag"],
            "message": (
                f"{r['weld_tag']} is stamped on {drawings} (as {tags}) and is "
                f"in no test pack. A weld is in the line that the register "
                f"does not have, so nothing checks its procedure, its welder "
                f"or its examination."
            ),
            "detail": _detail(tags=tags, drawings=drawings),
            "document_id": r["document_id"], "page_no": None,
        })
    return findings
