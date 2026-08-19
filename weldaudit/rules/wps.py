"""Reconciling the procedures welds were made to against the procedures on file.

A welding procedure specification is the recipe: process, filler, preheat,
diameter and wall range, how many welders on the root.  Two separate questions
follow from that, and the rules here keep them apart.

**Is the procedure in the package?**  GPPB-0110 is explicit — company
procedures are provided as references, contractor procedures may be used once
"reviewed and approved by Construction and Project Manager", and either way
"Procedures and approval must be part of the documentation hand-over to
Operations".  A weld log citing a procedure whose specification is nowhere in
the book leaves the operator with a joint and no recipe.

**Was the weld inside what the procedure covers?**  Where the register has
been read, the essential variables come with it, and the one this corpus can
actually test is the crew size: every API 1104 procedure in GPPB-0110 requires
two or more welders on the root and hot pass of pipe 12.750" and over.

The procedure identifiers themselves are the usual mess.  ``XTO-X60-6010/8010
Rev.1`` on a weld log is ``XTO-X60-6010-8010 Rev.1`` in a certificate filename
because a slash cannot go in a filename, and ``XTO-ASME PI HYP NACE`` is
``XTO-ASME-P1-HYP-NACE`` with a letter I typed for the digit 1.  The first is
normalised away; the second is reported as the near miss it is, because
guessing at more normalisation rules is how a tool starts silently merging
procedures that really are different.
"""

from __future__ import annotations

import json
from collections import defaultdict

from ..aml import parse_nps
from ..db import Database
from ..welders import parse_field
from ..wps import base_key, nearest_procedures, resolve, split_revision
from . import Finding, register


def _detail(**kw) -> str:
    return json.dumps({k: v for k, v in kw.items() if v not in (None, "")})


def _procedures(db: Database, project_id: int) -> dict[str, dict]:
    """The approved register, by base key."""
    return {r["wps_key"]: dict(r) for r in db.q(
        "SELECT * FROM procedure WHERE project_id=? AND IFNULL(wps_key,'')<>''",
        (project_id,))}


def _references(db: Database, project_id: int) -> dict[str, dict]:
    """Every procedure the job refers to, and where from.

    Keyed on the base so a certificate that omits the revision still lands on
    the same procedure as a weld log that states one.
    """
    out: dict[str, dict] = {}

    def note(raw: str, where: str, count: int = 1, revision: str = "") -> None:
        key = base_key(raw)
        if not key:
            return
        entry = out.setdefault(key, {"key": key, "spellings": {}, "welds": 0,
                                     "certs": 0, "revisions": set()})
        entry["spellings"][raw] = entry["spellings"].get(raw, 0) + count
        entry[where] += count
        if revision:
            entry["revisions"].add(revision)

    for r in db.q(
        """SELECT wps, COUNT(*) n FROM weld
           WHERE project_id=? AND IFNULL(wps,'')<>'' GROUP BY wps""",
        (project_id,),
    ):
        note(r["wps"], "welds", r["n"], split_revision(r["wps"])[1])
    for r in db.q(
        """SELECT wps, COUNT(*) n FROM welder_cert
           WHERE project_id=? AND IFNULL(wps,'')<>'' GROUP BY wps""",
        (project_id,),
    ):
        note(r["wps"], "certs", r["n"], split_revision(r["wps"])[1])
    return out


def _headline(entry: dict) -> str:
    """The spelling used most often, for talking about the procedure."""
    return max(entry["spellings"], key=lambda s: entry["spellings"][s])


def _used_on(entry: dict) -> str:
    parts = []
    if entry["welds"]:
        parts.append(f"{entry['welds']} weld{'s' if entry['welds'] != 1 else ''}")
    if entry["certs"]:
        parts.append(f"{entry['certs']} welder certificate"
                     f"{'s' if entry['certs'] != 1 else ''}")
    return " and ".join(parts)


# ---------------------------------------------------------------------------


@register("WPS-01", "No welding procedure is filed for this job")
def no_procedures_filed(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """The package contains no procedure specification at all.

    GPPB-0110: "Procedures and approval must be part of the documentation
    hand-over to Operations." One finding for the job rather than one per
    procedure — the gap is the missing document set, not each reference into
    it, and listing the procedures inside the message is what an auditor needs
    to go and ask for.
    """
    if _procedures(db, project_id):
        return []                       # the register is filed; WPS-02's job
    references = _references(db, project_id)
    if not references:
        return []

    names = sorted(_headline(e) for e in references.values())
    welds = sum(e["welds"] for e in references.values())
    certs = sum(e["certs"] for e in references.values())
    cited = " and ".join(
        p for p in (f"{welds} weld{'s' if welds != 1 else ''}" if welds else "",
                    f"{certs} welder certificate{'s' if certs != 1 else ''}"
                    if certs else "") if p)

    return [{
        "project_id": project_id, "run_id": run_id, "rule": "WPS-01",
        "severity": "major", "segment": "",
        "subject": f"{len(names)} procedure{'s' if len(names) != 1 else ''}",
        "message": (
            f"No welding procedure specification is filed anywhere in this "
            f"package, yet {cited} cite "
            + (f"one: {names[0]}" if len(names) == 1
               else f"{len(names)}: {', '.join(names)}")
            + f". GPPB-0110 requires procedures and their approval to be part "
              f"of the hand-over to Operations; without "
              f"{'it' if len(names) == 1 else 'them'} the line is inherited "
              f"with no record of how it was welded."
        ),
        "detail": _detail(procedures=", ".join(names), welds=welds,
                          certificates=certs),
        "document_id": None, "page_no": None,
    }]


@register("WPS-02", "Procedure used is not on the approved register")
def procedure_not_approved(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A procedure used that the job's own standard does not list.

    Only runs where the register has been read. With no standard in the book
    the answer is "not known", which WPS-01 reports once for the job; firing
    here as well would raise one gap twice under two headings.
    """
    approved = _procedures(db, project_id)
    if not approved:
        return []

    findings: list[Finding] = []
    for key, entry in sorted(_references(db, project_id).items()):
        matched, _how = resolve(_headline(entry), set(approved))
        if matched:
            continue                    # WPS-03 reports how it was written
        name = _headline(entry)
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "WPS-02",
            "severity": "major", "segment": "",
            "subject": name,
            "message": (
                f"{_used_on(entry)} cite procedure {name}, which is not one of "
                f"the {len(approved)} procedures the job's welding standard "
                f"approves. A contractor procedure may be used, but GPPB-0110 "
                f"requires it to be reviewed and approved by Construction and "
                f"the Project Manager, with the approval kept on file."
            ),
            "detail": _detail(procedure=name, welds=entry["welds"],
                              certificates=entry["certs"],
                              approved=", ".join(
                                  sorted(p["wps"] for p in approved.values()))),
            "document_id": None, "page_no": None,
        })
    return findings


@register("WPS-03", "The same procedure is written more than one way")
def procedure_spelling(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """Two references one character apart, or one spelled several ways.

    `XTO-ASME PI HYP NACE` against `XTO-ASME-P1-HYP-NACE` is an I typed for a
    1 in an ASME P-number. Reporting it as an unknown procedure would send
    someone looking for a document that exists; reporting it as a spelling
    keeps the paperwork honest without inventing a normalisation rule.
    """
    references = _references(db, project_id)
    approved = _procedures(db, project_id)
    known = set(references) | set(approved)

    findings: list[Finding] = []
    reported: set[frozenset] = set()
    for key, entry in sorted(references.items()):
        name = _headline(entry)

        # An unambiguous abbreviation of a filed procedure: a certificate
        # saying `XTO-SS` where the register says `XTO-SS-Sec. IX`.
        matched, how = resolve(name, set(approved))
        if how == "abbreviated":
            full = approved[matched]["wps"]
            findings.append({
                "project_id": project_id, "run_id": run_id, "rule": "WPS-03",
                "severity": "minor", "segment": "",
                "subject": name,
                "message": (
                    f"{_used_on(entry)} cite procedure {name}, which is how "
                    f"{full} is written short. Only one procedure in the "
                    f"standard extends it, so the reference resolves — but a "
                    f"shortened procedure number is one revision away from "
                    f"being ambiguous."
                ),
                "detail": _detail(written=name, procedure=full,
                                  welds=entry["welds"], certificates=entry["certs"]),
                "document_id": None, "page_no": None,
            })
            continue

        near = [k for k in nearest_procedures(name, known) if k != key]
        if near:
            pair = frozenset([key, *near])
            if pair in reported:
                continue
            reported.add(pair)
            others = ", ".join(
                sorted({(approved[k]["wps"] if k in approved
                         else _headline(references[k])) for k in near}))
            findings.append({
                "project_id": project_id, "run_id": run_id, "rule": "WPS-03",
                "severity": "minor", "segment": "",
                "subject": name,
                "message": (
                    f"This job refers to procedure {name} and to {others}, "
                    f"which differ by one character. They are almost certainly "
                    f"the same procedure written two ways; whichever is right, "
                    f"the records do not agree on how to name it."
                ),
                "detail": _detail(procedure=name, other=others,
                                  welds=entry["welds"], certificates=entry["certs"]),
                "document_id": None, "page_no": None,
            })
            continue

        # The same base written several ways within the job's own records.
        if len(entry["spellings"]) > 1:
            listed = ", ".join(f"{s} ({n})" for s, n in
                               sorted(entry["spellings"].items(),
                                      key=lambda kv: -kv[1]))
            findings.append({
                "project_id": project_id, "run_id": run_id, "rule": "WPS-03",
                "severity": "minor", "segment": "",
                "subject": name,
                "message": (
                    f"One procedure is written {len(entry['spellings'])} "
                    f"different ways across this job's records: {listed}. The "
                    f"references reconcile, but an auditor matching them by "
                    f"eye would have to know that a slash and a dash mean the "
                    f"same thing here."
                ),
                "detail": _detail(procedure=name, spellings=listed),
                "document_id": None, "page_no": None,
            })
    return findings


@register("WPS-04", "Approved procedure has no supporting PQR")
def procedure_without_pqr(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A procedure in the register that names no qualification record.

    API 1104 and ASME IX both require a WPS to be supported by a PQR — the
    record of the test coupon that proved the recipe works. A specification
    citing none has not been shown to be qualified.
    """
    findings: list[Finding] = []
    for key, p in sorted(_procedures(db, project_id).items()):
        if p["pqr"]:
            continue
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "WPS-04",
            "severity": "minor", "segment": "",
            "subject": p["wps"],
            "message": (
                f"Procedure {p['wps']} is in the job's welding standard and "
                f"names no supporting PQR. A specification has to be backed by "
                f"the qualification record of the coupon that proved it; "
                f"without one there is nothing showing the recipe was ever "
                f"tested."
            ),
            "detail": _detail(procedure=p["wps"], code=p["code"],
                              page=p["page_no"]),
            "document_id": p["document_id"], "page_no": p["page_no"],
        })
    return findings


@register("WPS-05", "Welds recorded with no procedure")
def weld_without_procedure(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """Welds on a register that carries a WPS column and leaves it empty.

    Guarded on the register using the column at all: the daily weld report
    form has no WPS field, so a job recorded only that way is not missing
    anything it was ever asked for.
    """
    findings: list[Finding] = []
    for row in db.q(
        """SELECT source, COUNT(*) n, SUM(IFNULL(wps,'')='') blank
           FROM weld WHERE project_id=? GROUP BY source""",
        (project_id,),
    ):
        if not row["blank"] or row["blank"] == row["n"]:
            continue                    # the source has no WPS column at all
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "WPS-05",
            "severity": "major", "segment": "",
            "subject": f"{row['blank']} welds",
            "message": (
                f"{row['blank']} of {row['n']} welds on the weld log record no "
                f"welding procedure, on a log that names one for the other "
                f"{row['n'] - row['blank']}. Nothing says which recipe those "
                f"joints were made to."
            ),
            "detail": _detail(source=row["source"], blank=row["blank"],
                              welds=row["n"]),
            "document_id": None, "page_no": None,
        })
    return findings


@register("WPS-06", "Approved procedure that nothing on the job uses")
def procedure_unused(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """Procedures filed and never referenced.

    Informational: a standard carries every procedure the company has, and a
    job uses two or three of them. Worth stating once so the count in the
    package is not mistaken for the count in use.
    """
    approved = _procedures(db, project_id)
    references = _references(db, project_id)
    if not approved or not references:
        return []

    # Resolve first, so a certificate abbreviating `XTO-SS-Sec. IX` to
    # `XTO-SS` does not leave that procedure counted as unused.
    referenced = {matched for e in references.values()
                  if (matched := resolve(_headline(e), set(approved))[0])}
    unused = sorted(p["wps"] for key, p in approved.items() if key not in referenced)
    if not unused or len(unused) == len(approved):
        return []
    return [{
        "project_id": project_id, "run_id": run_id, "rule": "WPS-06",
        "severity": "info", "segment": "",
        "subject": f"{len(unused)} procedures",
        "message": (
            f"{len(unused)} of the {len(approved)} procedures in the welding "
            f"standard are not referenced by any weld or certificate on this "
            f"job ({', '.join(unused)}). Normal for a company standard, and "
            f"worth knowing before the count of filed procedures is read as "
            f"the count in use."
        ),
        "detail": _detail(unused=", ".join(unused), approved=len(approved)),
        "document_id": None, "page_no": None,
    }]


@register("WPS-07", "Large-bore weld made by fewer welders than the procedure requires")
def too_few_welders(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A root pass on big pipe with fewer welders than the WPS demands.

    Every API 1104 procedure in GPPB-0110 says the same thing: "For Pipe
    >= 12.750" O.D., 2 or more welders are REQUIRED for Root and Hot Pass; 1
    welder may Fill and Cap." The threshold and the count are read off the
    procedure rather than assumed, so a standard that says something different
    is followed instead.
    """
    approved = _procedures(db, project_id)
    if not approved:
        return []

    by_procedure: dict[str, list] = defaultdict(list)
    for weld in db.q(
        """SELECT weld_no, segment, wps, weld_size, welder_root, document_id
           FROM weld WHERE project_id=? AND IFNULL(wps,'')<>''
             AND IFNULL(welder_root,'')<>''""",
        (project_id,),
    ):
        matched, _how = resolve(weld["wps"], set(approved))
        procedure = approved.get(matched)
        threshold = (procedure or {}).get("two_welder_over")
        if not threshold:
            continue
        nps = parse_nps(weld["weld_size"])
        if nps is None or nps < threshold:
            continue
        if len(parse_field(weld["welder_root"] or "").stencils) >= 2:
            continue
        by_procedure[procedure["wps"]].append(weld)

    findings: list[Finding] = []
    for wps, welds in sorted(by_procedure.items()):
        threshold = approved[base_key(wps)]["two_welder_over"]
        names = [w["weld_no"] for w in welds]
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "WPS-07",
            "severity": "major", "segment": welds[0]["segment"],
            "subject": f"{len(welds)} welds",
            "message": (
                f"{len(welds)} weld{'s' if len(welds) != 1 else ''} of "
                f"{threshold:g}\" and over record a single welder on the root "
                f"({', '.join(names[:8])}{'...' if len(names) > 8 else ''}). "
                f"Procedure {wps} requires two or more welders on the root and "
                f"hot pass at that diameter, so either the joint was made "
                f"outside the procedure or the log understates who was on it."
            ),
            "detail": _detail(procedure=wps, threshold=threshold,
                              welds=", ".join(names[:40])),
            "document_id": welds[0]["document_id"], "page_no": None,
        })
    return findings


def procedure_summary(db: Database, project_id: int) -> list[dict]:
    """Every procedure the job approves or refers to, and how it is used."""
    approved = _procedures(db, project_id)
    references = _references(db, project_id)

    # Fold each reference onto the procedure it resolves to, so an
    # abbreviation does not appear as a second, unfiled procedure.
    folded: dict[str, dict] = {}
    for key, entry in references.items():
        matched, _how = resolve(_headline(entry), set(approved))
        target = folded.setdefault(matched or key,
                                   {"welds": 0, "certs": 0, "spellings": {}})
        target["welds"] += entry["welds"]
        target["certs"] += entry["certs"]
        target["spellings"].update(entry["spellings"])

    out: list[dict] = []
    for key in sorted(set(approved) | set(folded)):
        p = approved.get(key, {})
        entry = folded.get(key, {})
        out.append({
            "wps": p.get("wps") or (_headline(entry) if entry else ""),
            "revision": p.get("revision", ""),
            "pqr": p.get("pqr", ""),
            "code": p.get("code", ""),
            "filed": bool(p),
            "welds": entry.get("welds", 0),
            "certs": entry.get("certs", 0),
            "spellings": ", ".join(sorted(entry.get("spellings", {}))),
            "min_diameter": p.get("min_diameter"),
            "two_welder_over": p.get("two_welder_over"),
        })
    return out
