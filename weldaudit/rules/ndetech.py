"""NDE technician qualification: was the person who shot it qualified, that day?

The join that makes this possible is a convention in the numbering: the first
letter of an NDE report prefix is the crew's rig letter.  ``GFB-037`` was shot
by rig G, and the rig log names rig G as JD Williams - which is exactly the
technician printed on that reader sheet.  So every shot in the package can be
tied to a named technician without opening a single scan.

Rig letters are reused as crews rotate, so the technician for a shot is the one
whose arrival on the job most recently precedes the sheet date.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime

from ..db import Database
from . import Finding, register

#: How long a technician certification is treated as current before it should
#: be re-checked.  ASNT Level II recertification is commonly three years; this
#: is a prompt to verify, not an assertion that the cert has lapsed.
CERT_VALID_YEARS = 3

_YES = re.compile(r"^(y|yes|true|1|x)$", re.IGNORECASE)
_NO = re.compile(r"^(n|no|false|0)$", re.IGNORECASE)

#: Bare NDE method codes.  When a prefix is only a method - "TI-1", "FB-8-9" -
#: its first letter is the method, not a rig, and the rig is named separately
#: in the filename ("9-12-25 RIG C TI-1.pdf", "10-8-25 D FB-1-2.pdf").
_METHOD_CODES = {"XR", "FB", "TI", "PT", "MT", "UT", "ML", "BR", "TW"}

_RIG_WORD = re.compile(r"\bRIG\s*([A-Z])\b", re.IGNORECASE)
#: A standalone capital letter sitting between the date and the report id.
_LOOSE_RIG = re.compile(r"(?:^|\s)([A-Z])\s+[A-Z]{2,5}-\d")


def rig_letter_for(prefix: str, filename: str | None) -> str:
    """Which rig shot this sheet.

    Two conventions are in use.  Usually the rig letter is the first letter of
    the report prefix - ``GFB-037`` is rig G, and the rig log names rig G as
    the technician printed on that sheet.  But where the prefix is a bare
    method code the rig is written out in the filename instead, and taking the
    first letter would invent a rig that does not exist.
    """
    name = filename or ""
    if m := _RIG_WORD.search(name):
        return m.group(1).upper()
    if (prefix or "").upper() in _METHOD_CODES:
        if m := _LOOSE_RIG.search(name):
            return m.group(1).upper()
        return ""          # method-only prefix with no rig named: unknown
    return (prefix or "")[:1].upper()


def _detail(**kw) -> str:
    return json.dumps({k: v for k, v in kw.items() if v not in (None, "")})


def _techs(db: Database, project_id: int) -> dict[str, list]:
    """Rig letter -> technician records, earliest arrival first."""
    rows = db.q(
        "SELECT * FROM nde_tech WHERE project_id=? AND rig_letter<>''", (project_id,)
    )
    out: dict[str, list] = defaultdict(list)
    for r in rows:
        out[(r["rig_letter"] or "").strip().upper()[:1]].append(r)
    for letter in out:
        out[letter].sort(key=lambda r: r["arrived"] or "")
    return out


def _tech_for(records: list, when: str | None):
    """The technician on that rig when the shot was taken."""
    if not records:
        return None
    if not when:
        return records[-1]
    arrived_before = [r for r in records if r["arrived"] and r["arrived"] <= when]
    return arrived_before[-1] if arrived_before else None


def _shots(db: Database, project_id: int) -> list:
    return db.q(
        """SELECT s.nde_id, s.prefix, s.segment, s.segments, s.sheet_date,
                  s.document_id, d.filename
           FROM nde_shot s LEFT JOIN document d ON d.id = s.document_id
           WHERE s.project_id=?""",
        (project_id,),
    )


def _collapse(findings: list[Finding]) -> list[Finding]:
    """One finding per (rule, subject), with the shot count attached."""
    grouped: dict[tuple, list[Finding]] = defaultdict(list)
    for f in findings:
        grouped[(f["rule"], f["subject"], f["segment"])].append(f)
    out: list[Finding] = []
    for group in grouped.values():
        head = dict(group[0])
        if len(group) > 1:
            shots = [json.loads(f["detail"]).get("shot", "") for f in group]
            shots = [s for s in shots if s]
            head["message"] += (
                f" Affects {len(group)} shots: {', '.join(shots[:8])}"
                + (f" and {len(shots) - 8} more." if len(shots) > 8 else ".")
            )
            detail = json.loads(head["detail"]) if head["detail"] else {}
            detail["shots"] = ", ".join(shots[:40])
            detail["shot_count"] = len(group)
            head["detail"] = json.dumps(detail)
        out.append(head)
    return out


# ---------------------------------------------------------------------------


@register("NDT-01", "Shot taken by a rig with no technician in the rig log")
def rig_not_in_log(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A reader sheet whose rig letter appears nowhere in the NDE rig log."""
    techs = _techs(db, project_id)
    if not techs:
        return []          # no rig log parsed - see NDT-06

    findings: list[Finding] = []
    for s in _shots(db, project_id):
        letter = rig_letter_for(s["prefix"], s["filename"])
        if not letter or letter in techs:
            continue
        findings.append(
            {
                "project_id": project_id, "run_id": run_id, "rule": "NDT-01",
                "severity": "critical", "segment": s["segment"],
                "subject": f"Rig {letter}",
                "message": (
                    f"Reader sheets in the {s['prefix']} series were shot by rig "
                    f"{letter}, which is not listed in the NDE rig log. The "
                    f"technician's certification and visual acuity cannot be verified."
                ),
                "detail": _detail(shot=s["nde_id"], series=s["prefix"],
                                  filename=s["filename"]),
                "document_id": s["document_id"], "page_no": None,
            }
        )
    return _collapse(findings)


@register("NDT-02", "Technician recorded without certification or visual acuity")
def tech_not_qualified(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """The rig log itself says the technician's certs or acuity are not in hand."""
    findings: list[Finding] = []
    for letter, records in sorted(_techs(db, project_id).items()):
        for r in records:
            missing = []
            if _NO.match((r["certs"] or "").strip()):
                missing.append("certifications")
            if _NO.match((r["acuity"] or "").strip()):
                missing.append("visual acuity")
            if not missing:
                continue
            findings.append(
                {
                    "project_id": project_id, "run_id": run_id, "rule": "NDT-02",
                    "severity": "critical", "segment": r["segment"],
                    "subject": f"{r['name']} (rig {letter})",
                    "message": (
                        f"The NDE rig log records {r['name']} of {r['company']} on rig "
                        f"{letter} with no {' and no '.join(missing)}. Any shot taken by "
                        f"rig {letter} is unverified."
                    ),
                    "detail": _detail(company=r["company"], certs=r["certs"],
                                      acuity=r["acuity"], arrived=r["arrived"]),
                    "document_id": None, "page_no": None,
                }
            )
    return findings


@register("NDT-03", "Shot taken before the technician arrived on the job")
def shot_before_arrival(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A reader sheet dated before any technician on that rig had arrived."""
    techs = _techs(db, project_id)
    if not techs:
        return []

    findings: list[Finding] = []
    for s in _shots(db, project_id):
        letter = rig_letter_for(s["prefix"], s["filename"])
        records = techs.get(letter)
        if not records or not s["sheet_date"]:
            continue
        if _tech_for(records, s["sheet_date"]):
            continue
        earliest = min((r["arrived"] for r in records if r["arrived"]), default="")
        if not earliest:
            continue
        findings.append(
            {
                "project_id": project_id, "run_id": run_id, "rule": "NDT-03",
                "severity": "major", "segment": s["segment"],
                "subject": f"Rig {letter}",
                "message": (
                    f"Reader sheet {s['nde_id']} is dated {s['sheet_date']}, but the "
                    f"earliest arrival recorded for rig {letter} in the NDE rig log is "
                    f"{earliest}. Either the sheet is misdated or the rig log is "
                    f"incomplete."
                ),
                "detail": _detail(shot=s["nde_id"], sheet_date=s["sheet_date"],
                                  earliest_arrival=earliest, filename=s["filename"]),
                "document_id": s["document_id"], "page_no": None,
            }
        )
    return _collapse(findings)


@register("NDT-04", "Technician certification may have lapsed before the shot")
def cert_possibly_lapsed(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A shot taken more than the recertification interval after the cert date."""
    techs = _techs(db, project_id)
    if not techs:
        return []

    findings: list[Finding] = []
    for s in _shots(db, project_id):
        letter = rig_letter_for(s["prefix"], s["filename"])
        records = techs.get(letter)
        if not records or not s["sheet_date"]:
            continue
        tech = _tech_for(records, s["sheet_date"])
        if not tech or not tech["cert_date"]:
            continue
        try:
            certified = date.fromisoformat(tech["cert_date"][:10])
            shot = date.fromisoformat(s["sheet_date"][:10])
        except ValueError:
            continue
        age_days = (shot - certified).days
        if age_days <= CERT_VALID_YEARS * 365:
            continue
        findings.append(
            {
                "project_id": project_id, "run_id": run_id, "rule": "NDT-04",
                "severity": "major", "segment": s["segment"],
                "subject": f"{tech['name']} (rig {letter})",
                "message": (
                    f"{tech['name']} shot {s['nde_id']} on {s['sheet_date']}, "
                    f"{age_days // 365} years after the certification date recorded in "
                    f"the rig log ({tech['cert_date'][:10]}). Confirm a current "
                    f"recertification is on file."
                ),
                "detail": _detail(shot=s["nde_id"], cert_date=tech["cert_date"][:10],
                                  sheet_date=s["sheet_date"], age_days=age_days,
                                  company=tech["company"]),
                "document_id": s["document_id"], "page_no": None,
            }
        )
    return _collapse(findings)


@register("NDT-05", "Technician in the rig log has no certification on file")
def tech_without_cert_document(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A named technician with no certification or vision document in the book."""
    techs = _techs(db, project_id)
    if not techs:
        return []

    docs = db.q(
        """SELECT id, filename, segment FROM document
           WHERE project_id=? AND kind IN ('nde_tech_cert','welder_cert')""",
        (project_id,),
    )
    filenames = [(d["filename"] or "").upper() for d in docs]

    findings: list[Finding] = []
    seen: set[str] = set()
    for letter, records in sorted(techs.items()):
        for r in records:
            name = (r["name"] or "").strip()
            if not name or name.upper() in seen:
                continue
            seen.add(name.upper())
            # Match on surname, which survives the spelling drift between the
            # rig log and the certificate ("CAMBELL" vs "CAMPBELL" both appear).
            parts = [p for p in re.split(r"[^A-Z]+", name.upper()) if len(p) > 2]
            if not parts:
                continue
            if any(any(p in fn for fn in filenames) for p in parts):
                continue
            findings.append(
                {
                    "project_id": project_id, "run_id": run_id, "rule": "NDT-05",
                    "severity": "major", "segment": r["segment"],
                    "subject": f"{name} (rig {letter})",
                    "message": (
                        f"{name} of {r['company']} is listed on rig {letter} in the NDE "
                        f"rig log, but no certification or visual acuity document for "
                        f"them is filed in this job."
                    ),
                    "detail": _detail(company=r["company"], rig=letter,
                                      cert_date=r["cert_date"], arrived=r["arrived"]),
                    "document_id": None, "page_no": None,
                }
            )
    return findings


@register("NDT-07", "Technician on the sheet is not the one the rig log names")
def sheet_technician_mismatch(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """The name printed on a read sheet doesn't match the rig log for that rig.

    The rig-letter join is a convention, not a guarantee. Once a sheet has
    actually been read, the printed technician name is the ground truth — and
    where the two disagree, either the rig log is wrong or the shot is
    attributed to the wrong crew, and neither can be left unresolved.

    Surname matching, because the rig logs spell names inconsistently
    (``CAMBELL`` and ``CAMPBELL`` both appear for the same person).

    Any evidence will do. This was written when only a vision pass could read a
    name off a sheet and was restricted to that, but the IIA forms carry the
    technician in their text layer and 1,929 shots now have one without a model
    being involved. Who signed the sheet is ground truth whoever transcribed
    it, so the restriction only kept the check asleep.
    """
    techs = _techs(db, project_id)
    if not techs:
        return []

    rows = db.q(
        """SELECT s.nde_id, s.prefix, s.segment, s.sheet_date, s.technician,
                  s.document_id, s.page_no, d.filename
           FROM nde_shot s LEFT JOIN document d ON d.id = s.document_id
           WHERE s.project_id=? AND IFNULL(s.technician,'') <> ''""",
        (project_id,),
    )

    findings: list[Finding] = []
    for s in rows:
        letter = rig_letter_for(s["prefix"], s["filename"])
        records = techs.get(letter)
        if not records:
            continue
        expected = _tech_for(records, s["sheet_date"]) or records[-1]
        if _same_person(s["technician"], expected["name"]):
            continue
        findings.append(
            {
                "project_id": project_id, "run_id": run_id, "rule": "NDT-07",
                "severity": "major", "segment": s["segment"],
                "subject": s["nde_id"],
                "message": (
                    f"The reader sheet for {s['nde_id']} is signed by "
                    f"'{s['technician']}', but the NDE rig log names "
                    f"'{expected['name']}' on rig {letter}. Either the rig log is "
                    f"out of date or this shot is attributed to the wrong crew — "
                    f"the technician's certification cannot be confirmed until "
                    f"that is resolved."
                ),
                "detail": _detail(
                    sheet_technician=s["technician"], rig_log_technician=expected["name"],
                    rig=letter, sheet_date=s["sheet_date"], filename=s["filename"],
                ),
                "document_id": s["document_id"], "page_no": s["page_no"],
            }
        )
    return _collapse(findings)


def _same_person(a: str, b: str) -> bool:
    """Whether two spellings plausibly name the same technician."""
    from rapidfuzz.distance import DamerauLevenshtein

    tokens_a = {t for t in re.split(r"[^A-Z]+", (a or "").upper()) if len(t) > 2}
    tokens_b = {t for t in re.split(r"[^A-Z]+", (b or "").upper()) if len(t) > 2}
    if not tokens_a or not tokens_b:
        return True          # nothing to compare - do not manufacture a finding
    if tokens_a & tokens_b:
        return True
    # Allow one keystroke per surname pair: CAMBELL vs CAMPBELL.
    return any(
        DamerauLevenshtein.distance(x, y, score_cutoff=1) <= 1
        for x in tokens_a for y in tokens_b
    )


def _people(rows: list) -> list[list]:
    """Rig-log rows grouped into people, collapsing spellings of one name."""
    groups: list[list] = []
    for r in rows:
        for g in groups:
            if _same_person(g[0]["name"], r["name"]):
                g.append(r)
                break
        else:
            groups.append([r])
    return groups


@register("NDT-08", "Rig log disagrees with itself about a technician's certification")
def conflicting_cert_date(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """The same technician recorded with more than one certification date.

    Every segment book carries its own copy of the NDE rig log — seventy-one
    rows across fifteen books on Bluewater for nine people — and the copies do not
    always agree. A certification date is a fact about the person, not about
    the segment, so two of them cannot both be right.

    This matters because NDT-04 reads that date to decide whether a shot was
    taken on a lapsed ticket, and the recertification interval is three years.
    Jimmy Hanks is recorded as certified on 2024-12-30 in six books, 2025-12-30
    in four and 2025-08-18 in one; whichever is true, ten of those entries are
    wrong, and a check that silently picks one of them is not a check.

    Arrival dates are deliberately *not* compared. A technician genuinely
    arrives on different segments on different days, so the copies differing
    there is the register working correctly rather than contradicting itself.
    """
    rows = db.q(
        """SELECT segment, company, name, rig_letter, cert_date, arrived
           FROM nde_tech
           WHERE project_id=? AND IFNULL(cert_date,'') <> ''""",
        (project_id,),
    )
    if not rows:
        return []

    findings: list[Finding] = []
    for group in _people(rows):
        dates = Counter(r["cert_date"][:10] for r in group)
        if len(dates) < 2:
            continue
        (agreed, agreed_n), *rest = dates.most_common()
        spellings = sorted({r["name"] for r in group})
        rigs = sorted({(r["rig_letter"] or "").upper() for r in group if r["rig_letter"]})
        odd = [
            f"{when} in {n} ({'; '.join(sorted(s['segment'] for s in group if (s['cert_date'] or '')[:10] == when)[:3])})"
            for when, n in rest
        ]
        findings.append(
            {
                "project_id": project_id, "run_id": run_id, "rule": "NDT-08",
                "severity": "major", "segment": "",
                "subject": spellings[0],
                "message": (
                    f"The NDE rig log records {spellings[0]} "
                    + (f"(also spelled {', '.join(spellings[1:])}) "
                       if len(spellings) > 1 else "")
                    + f"on rig {'/'.join(rigs) or '?'} with "
                      f"{len(dates)} different certification dates: {agreed} in "
                      f"{agreed_n} segment book{'s' if agreed_n != 1 else ''}, and "
                    + "; ".join(odd)
                    + ". A certification date belongs to the person, not the "
                      "segment, so the lapse check is reading one of these at "
                      "random."
                ),
                "detail": _detail(
                    technician=spellings[0], rigs="/".join(rigs),
                    cert_dates="; ".join(f"{d} x{n}" for d, n in dates.most_common()),
                    books=len(group),
                ),
                "document_id": None, "page_no": None,
            }
        )
    return findings


@register("NDT-06", "No NDE rig log in this job")
def no_rig_log(db: Database, project_id: int, run_id: str) -> list[Finding]:
    if db.one("SELECT 1 FROM nde_tech WHERE project_id=? LIMIT 1", (project_id,)):
        return []
    row = db.one("SELECT COUNT(DISTINCT prefix) n FROM nde_shot WHERE project_id=?",
                 (project_id,))
    if not row or not row["n"]:
        return []
    return [
        {
            "project_id": project_id, "run_id": run_id, "rule": "NDT-06",
            "severity": "major", "segment": "(project)",
            "subject": f"{row['n']} NDE series",
            "message": (
                f"Reader sheets from {row['n']} NDE series are filed, but this job has "
                f"no NDE rig log, so no technician's certification, visual acuity or "
                f"arrival date can be checked."
            ),
            "detail": "", "document_id": None, "page_no": None,
        }
    ]
