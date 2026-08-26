"""Heat number -> material certificate -> approved materials list.

Three links, each of which an auditor checks by hand today:

1. every heat welded into the line has a certificate in the book,
2. the manufacturer on that material is on the AML,
3. the material is within whatever the AML approved that manufacturer *for* -
   a mill cleared "Up to NPS 20" supplying 24" pipe is a non-conformance that
   reading the name alone will never catch.

Link 2 needs a manufacturer, which only the pipe/heat export reliably supplies.
Where no manufacturer is known the tool says so (MTR-08) instead of passing the
heat silently; that list is exactly what a vision pass over the scanned
certificates would resolve.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from ..aml import Aml, SizeLimit
from ..db import Database
from ..mtrname import _SPEC as _SPEC_PATTERN
from ..mtrname import normalise_heat, same_heat_differently_read
from . import Finding, register


def _detail(**kw) -> str:
    return json.dumps({k: v for k, v in kw.items() if v not in (None, "")})


def _aml_from_db(db: Database, project_id: int) -> Aml | None:
    """Rebuild the matcher from the stored AML rows."""
    from ..aml import AmlEntry

    rows = db.q("SELECT * FROM aml_entry WHERE project_id=?", (project_id,))
    if not rows:
        return None
    entries = [
        AmlEntry(
            category=r["category"], manufacturer=r["manufacturer"],
            location=r["location"], limits_raw=r["limits_raw"] or "",
            size_limit=(SizeLimit(r["min_nps"], r["max_nps"])
                        if r["min_nps"] is not None or r["max_nps"] is not None else None),
            conditions=r["conditions"] or "", key=r["norm_name"] or "",
        )
        for r in rows
    ]
    from ..aliases import load as load_aliases

    return Aml(entries, aliases=load_aliases())


#: Words that mean the as-built's HT# cell held a cross-reference rather than
#: a heat.  The sheets open with "SEE ISO DRAWING" spread across two blocks.
_NOT_A_HEAT = re.compile(r"\b(SEE|ISO|DRAWING|DWG|PAD|SHEET|N/?A|TBD|NONE|RISER)\b",
                         re.IGNORECASE)
_HAS_DIGIT = re.compile(r"\d")


def _is_a_heat(heat: str) -> bool:
    """Whether an as-built HT# cell holds a heat number at all."""
    text = (heat or "").strip()
    return (len(text) >= 4 and bool(_HAS_DIGIT.search(text))
            and not _NOT_A_HEAT.search(text))


def asbuilt_heats(db: Database, project_id: int) -> dict[str, dict]:
    """Heats named by the as-built drawings, per segment.

    The largest material register in the corpus and, until now, the only one no
    rule consulted: 1,624 rows on Bluewater naming 577 heats against the 32 its
    heat maps know.  It is also the only one that says *where* on the line each
    heat sits, since every joint carries a station.
    """
    out: dict[str, dict] = {}
    for r in db.q(
        """SELECT heat, heat_key, segment, description, size, grade,
                  MIN(station) station, COUNT(*) n
           FROM asbuilt_joint
           WHERE project_id=? AND IFNULL(heat_key,'') <> ''
           GROUP BY heat_key""",
        (project_id,),
    ):
        if not _is_a_heat(r["heat"]):
            continue
        out[r["heat_key"]] = {
            "heat": r["heat"], "segment": r["segment"] or "",
            "description": r["description"] or "", "size": r["size"] or "",
            "grade": r["grade"] or "", "station": r["station"] or "",
            "joints": r["n"],
        }
    return out


def _welded_heats(db: Database, project_id: int) -> dict[str, dict]:
    """Every heat the package says went into the ground, keyed for joining.

    Three kinds of evidence, deliberately kept distinct in the wording of any
    finding.  A weld log names the heat on each end of a specific joint.  An
    as-built names the heat of each joint and where it sits.  A heat-map
    isometric only says a heat is installed somewhere on a line - which is
    weaker, but is the only material record some jobs have.
    """
    out: dict[str, dict] = {}

    for r in db.q(
        """SELECT segment, line, weld_no, heat_us, heat_ds, weld_size
           FROM weld WHERE project_id=? AND (heat_us<>'' OR heat_ds<>'')""",
        (project_id,),
    ):
        for side, heat in (("upstream", r["heat_us"]), ("downstream", r["heat_ds"])):
            key = normalise_heat(heat)
            if not key:
                continue
            rec = out.setdefault(
                key, {"heat": heat, "welds": [], "segment": r["segment"],
                      "line": r["line"], "nps": r["weld_size"], "where": "welds"},
            )
            if len(rec["welds"]) < 8:
                rec["welds"].append(f"{r['weld_no']} ({side})")

    for r in db.q(
        """SELECT segment, line, drawing_no, heat, heat_key, COUNT(*) n
           FROM installed_heat WHERE project_id=? AND heat_key<>''
           GROUP BY heat_key""",
        (project_id,),
    ):
        rec = out.setdefault(
            r["heat_key"], {"heat": r["heat"], "welds": [], "segment": r["segment"],
                            "line": r["line"], "nps": None, "where": "heat map"},
        )
        rec["drawing"] = r["drawing_no"]
        rec["callouts"] = r["n"]

    for key, r in asbuilt_heats(db, project_id).items():
        rec = out.setdefault(
            key, {"heat": r["heat"], "welds": [], "segment": r["segment"],
                  "line": "", "nps": None, "where": "as-built"},
        )
        rec.setdefault("joints", r["joints"])
        rec.setdefault("station", r["station"])
        rec.setdefault("description", r["description"])

    return out


def _certified_heats(db: Database, project_id: int) -> dict[str, list]:
    """Heats with an actual certificate document on file.

    Only ``mtr_file`` rows count.  A pipe export tells us who made a heat, but
    it is a system record, not the mill certificate the turnover package has to
    contain - treating it as one would let a book with no MTRs at all pass.
    """
    rows = db.q(
        """SELECT m.*, d.filename FROM material m
           LEFT JOIN document d ON d.id = m.document_id
           WHERE m.project_id=? AND m.heat_key<>'' AND m.source='mtr_file'""",
        (project_id,),
    )
    out: dict[str, list] = defaultdict(list)
    for r in rows:
        out[r["heat_key"]].append(r)
    return out


# ---------------------------------------------------------------------------


@register("MTR-01", "Welded heat has no material certificate on file")
def heat_without_certificate(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A heat number on the weld map with no certificate anywhere in the book.

    Before calling material uncertified, the nearest certified heat is checked.
    Heat numbers are transcribed by hand and a single wrong digit is far more
    common than genuinely missing paperwork - ``2345438`` on the weld log
    against ``2245438`` on the certificate, for instance.  Those are reported
    as probable transcription errors, which is a different job to do.
    """
    certified = _certified_heats(db, project_id)
    if not certified:
        return []          # no certificates filed here at all - see MTR-10

    wholesale = _uncertified_lines(db, project_id, certified)

    findings: list[Finding] = []
    for key, rec in sorted(_welded_heats(db, project_id).items()):
        if key in certified:
            continue
        if rec.get("where") == "as-built" and rec["segment"] in wholesale:
            continue       # the whole line is reported once, by MTR-11
        near_key, near = _nearest_heat(key, certified)
        if near is not None:
            findings.append(
                {
                    "project_id": project_id, "run_id": run_id, "rule": "MTR-01",
                    "severity": "major", "segment": rec["segment"],
                    "subject": f"Heat {rec['heat']}",
                    "message": (
                        f"No certificate for heat {rec['heat']} "
                        f"({_where(rec)}), but a certificate for "
                        f"{near['heat']} is on file - one character different. "
                        f"Probably a transcription error; confirm which heat was "
                        f"actually installed."
                    ),
                    "detail": _detail(
                        welds=", ".join(rec["welds"]), line=rec["line"],
                        certificate_on_file=near["heat"], certificate=near["filename"],
                    ),
                    "document_id": near["document_id"], "page_no": None,
                }
            )
            continue
        findings.append(
            {
                "project_id": project_id, "run_id": run_id, "rule": "MTR-01",
                "severity": "critical", "segment": rec["segment"],
                "subject": f"Heat {rec['heat']}",
                "message": (
                    f"Heat {rec['heat']} is {_where(rec)} but no material "
                    f"certificate for it is filed in this project."
                ),
                "detail": _detail(welds=", ".join(rec["welds"]), line=rec["line"],
                                  evidence=rec.get("where"),
                                  drawing=rec.get("drawing")),
                "document_id": None, "page_no": None,
            }
        )
    return findings


def _where(rec: dict) -> str:
    """How this heat is known to be in the line — the three differ in strength."""
    if rec.get("where") == "heat map":
        where = f"the heat map for {rec['line'] or rec['segment']}"
        n = rec.get("callouts") or 1
        return f"shown on {where}" + (f" ({n} callouts)" if n > 1 else "")
    if rec.get("where") == "as-built":
        n = rec.get("joints") or 1
        at = f" at station {rec['station']}" if rec.get("station") else ""
        return (f"on the as-built for {rec['segment']} "
                f"({n} joint{'s' if n != 1 else ''}{at})")
    n = len(rec.get("welds") or ())
    return (f"welded into {rec['line'] or rec['segment']} "
            f"({n} weld end{'s' if n != 1 else ''})")


def _nearest_heat(key: str, certified: dict[str, list]) -> tuple[str, dict | None]:
    """The closest certified heat, if it is close enough to be a typo.

    Only same-length, single-character differences count.  Anything looser
    starts matching genuinely different heats from the same mill run, which
    would hide real missing certificates behind a plausible-looking excuse.
    """
    from rapidfuzz.distance import Levenshtein

    best_key, best_dist = "", 99
    for candidate in certified:
        if len(candidate) != len(key):
            continue
        d = Levenshtein.distance(key, candidate, score_cutoff=2)
        if d < best_dist:
            best_key, best_dist = candidate, d
    if best_dist == 1 and len(key) >= 5:
        return best_key, dict(certified[best_key][0])
    return "", None


def _uncertified_lines(db: Database, project_id: int,
                       certified: dict[str, list]) -> dict[str, list]:
    """Segments whose as-built heats are certified *nowhere* in the package.

    Not a threshold — none at all. Every other segment on this corpus has a
    healthy certified population alongside its gaps (Bluewater runs 14% to 60%
    uncertified segment by segment), and `16 PW BLUEWATER` has 466 heats and not
    one certificate. A line where nothing matches is a different fact from a
    line with holes in it, and worth saying once rather than 466 times.
    """
    by_segment: dict[str, list] = defaultdict(list)
    for key, rec in asbuilt_heats(db, project_id).items():
        by_segment[rec["segment"]].append((key, rec))
    return {
        segment: [rec for _key, rec in items]
        for segment, items in by_segment.items()
        if len(items) >= 2 and not any(key in certified for key, _rec in items)
    }


@register("MTR-11", "No certificate matches any heat on a line's as-built")
def line_without_certificates(db: Database, project_id: int,
                              run_id: str) -> list[Finding]:
    """A whole line's worth of installed material with nothing certifying it.

    The as-built is the largest material register in the corpus — 1,624 rows on
    Bluewater naming 577 heats, against the 32 its heat maps know — and until now
    no rule read it. Adding it surfaced one fact worth more than the rest: the
    `16 PW BLUEWATER` as-built names 466 heats of mainline pipe, couplings, pups
    and elbows, and **not one of them has a certificate anywhere in the
    project**. The twenty-two certificates filed under that segment are all
    stainless flanges and fittings; the carbon steel line pipe has none.

    Reported per line rather than per heat, because 466 findings would say the
    same thing 466 times.
    """
    certified = _certified_heats(db, project_id)
    if not certified:
        return []          # nothing certified anywhere - see MTR-10

    findings: list[Finding] = []
    for segment, heats in sorted(_uncertified_lines(db, project_id, certified).items()):
        kinds = Counter(h["description"].upper() for h in heats if h["description"])
        what = ", ".join(k.lower() for k, _n in kinds.most_common(4)) or "material"
        sample = ", ".join(sorted(h["heat"] for h in heats)[:5])
        joints = sum(h["joints"] for h in heats)
        findings.append(
            {
                "project_id": project_id, "run_id": run_id, "rule": "MTR-11",
                "severity": "critical", "segment": segment,
                "subject": segment or "(unassigned)",
                "message": (
                    f"The as-built for {segment} names {len(heats)} heats across "
                    f"{joints} joints ({what}), and no material certificate in "
                    f"this project matches any of them. Certificates are filed "
                    f"for this segment, but for other material — the heats the "
                    f"drawing says are in the ground are uncertified."
                ),
                "detail": _detail(
                    heats=len(heats), joints=joints, sample=sample,
                    described_as="; ".join(f"{k} x{n}" for k, n in kinds.most_common(5)),
                ),
                "document_id": None, "page_no": None,
            }
        )
    return findings


def _names_read_two_ways(db: Database, project_id: int) -> set[int]:
    """Documents whose company name different close-ups read differently.

    Tiling reads a page as four overlapping quarters, and where they disagree
    about a letterhead the merge still records the majority answer — but VIS-02
    has already published that the name is not reliably legible.

    Asserting on the same breath that the manufacturer is *not approved* is a
    contradiction: it is a critical non-conformance resting on evidence this
    tool has itself called into question. Hand-checking the 66 findings found
    seventeen of exactly that shape — seven spellings of Tex-Tubo and ten of
    Kandal, each misread becoming its own unapproved "company".

    The pages are not dropped. They are already reported, as VIS-02, which
    asks a person to read the letterhead — which is the only thing that can
    actually settle it.
    """
    return {r["document_id"] for r in db.q(
        """SELECT DISTINCT document_id FROM vision_conflict
           WHERE project_id=? AND chosen IS NOT NULL
             AND field IN ('issuing_company', 'mill_name')
             AND document_id IS NOT NULL""", (project_id,))}


@register("MTR-02", "Manufacturer is not on the approved materials list")
def manufacturer_not_approved(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A material whose manufacturer no AML entry covers."""
    aml = _aml_from_db(db, project_id)
    if not aml:
        return []

    disputed = _names_read_two_ways(db, project_id)

    findings: list[Finding] = []
    for r in _materials_with_manufacturer(db, project_id):
        if r["document_id"] in disputed:
            continue        # VIS-02 already has this page; see _names_read_two_ways
        result = aml.match(r["manufacturer"], _categories(r))
        if result.status != "not_listed":
            continue
        near = result.entries[0].manufacturer if result.entries else ""
        findings.append(
            {
                "project_id": project_id, "run_id": run_id, "rule": "MTR-02",
                "severity": "critical", "segment": r["segment"],
                "subject": r["manufacturer"],
                "message": (
                    f"'{r['manufacturer']}' supplied heat {r['heat']} "
                    f"({_describe(r)}) but does not appear on the approved "
                    f"materials list."
                    + (f" Closest AML entry is '{near}' ({result.score}% similar), "
                       f"which is not a match." if near else "")
                ),
                "detail": _detail(heat=r["heat"], line=r["line"], grade=r["grade"],
                                  nps=r["nps"], best_score=result.score),
                "document_id": r["document_id"], "page_no": None,
            }
        )
    return _collapse(findings, by="subject", per_segment=False)


@register("MTR-03", "Manufacturer needs confirmation against the AML")
def manufacturer_needs_confirmation(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A name that is close to an AML entry, ambiguous, on HOLD, or withdrawn."""
    aml = _aml_from_db(db, project_id)
    if not aml:
        return []

    findings: list[Finding] = []
    for r in _materials_with_manufacturer(db, project_id):
        result = aml.match(r["manufacturer"], _categories(r))
        if result.status != "confirm":
            continue
        options = "; ".join(
            f"{e.manufacturer} [{e.location}]" for e in result.entries[:4]
        )
        findings.append(
            {
                "project_id": project_id, "run_id": run_id, "rule": "MTR-03",
                "severity": "major", "segment": r["segment"],
                "subject": r["manufacturer"],
                "message": (
                    f"'{r['manufacturer']}' (heat {r['heat']}) cannot be cleared "
                    f"automatically: {result.reason}. Candidates: {options}."
                ),
                "detail": _detail(heat=r["heat"], score=result.score,
                                  candidates=options, line=r["line"]),
                "document_id": r["document_id"], "page_no": None,
            }
        )
    return _collapse(findings, by="subject", per_segment=False)


@register("MTR-04", "Material exceeds the AML size limit for that manufacturer")
def size_limit_violated(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """The manufacturer is approved, but not for this size.

    This is the check that cannot be done by eye across a whole project: 86 AML
    entries carry a size restriction, and the violation only shows up when the
    mill, the location and the actual pipe diameter are read together.
    """
    aml = _aml_from_db(db, project_id)
    if not aml:
        return []

    findings: list[Finding] = []
    for r in _materials_with_manufacturer(db, project_id):
        nps = r["nps"]
        if nps is None:
            continue
        result = aml.match(r["manufacturer"], _categories(r))
        if result.status != "approved":
            continue
        allowing, forbidding = aml.check_size(result.entries, nps)
        if allowing or not forbidding:
            continue
        blocked = forbidding[0]
        findings.append(
            {
                "project_id": project_id, "run_id": run_id, "rule": "MTR-04",
                "severity": "critical", "segment": r["segment"],
                "subject": f"{r['manufacturer']} @ NPS {_nps(nps)}",
                "message": (
                    f"{r['manufacturer']} is approved only {blocked.size_limit.describe()} "
                    f"at {blocked.location}, but heat {r['heat']} is NPS {_nps(nps)}."
                ),
                "detail": _detail(
                    heat=r["heat"], nps=nps, aml_limit=blocked.limits_raw,
                    aml_entry=blocked.manufacturer, location=blocked.location,
                    category=blocked.category, line=r["line"],
                ),
                "document_id": r["document_id"], "page_no": None,
            }
        )
    return _collapse(findings, by="subject")


@register("MTR-05", "AML approval carries a condition needing review")
def condition_needs_review(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """An approval qualified by something that is not a size rule.

    "SAW only", "Induction bends only", "Brand: Capitol Mfg." - the tool cannot
    verify these from the data it has, so it surfaces them rather than treating
    the approval as unconditional.
    """
    aml = _aml_from_db(db, project_id)
    if not aml:
        return []

    findings: list[Finding] = []
    for r in _materials_with_manufacturer(db, project_id):
        result = aml.match(r["manufacturer"], _categories(r))
        if result.status != "approved":
            continue
        conditioned = [e for e in result.entries if e.conditions]
        if not conditioned or len(conditioned) < len(result.entries):
            continue          # at least one unconditional entry covers it
        e = conditioned[0]
        findings.append(
            {
                "project_id": project_id, "run_id": run_id, "rule": "MTR-05",
                "severity": "info", "segment": r["segment"],
                "subject": r["manufacturer"],
                "message": (
                    f"{r['manufacturer']} is on the AML for heat {r['heat']}, but the "
                    f"approval is qualified: \"{e.conditions}\". Confirm the "
                    f"certificate meets that condition."
                ),
                "detail": _detail(heat=r["heat"], condition=e.conditions,
                                  aml_entry=e.manufacturer, location=e.location),
                "document_id": r["document_id"], "page_no": None,
            }
        )
    return _collapse(findings, by="subject")


#: Kinds that are not certificates and are not expected to name a heat. A
#: bill of lading is a delivery note: it says a load arrived, and asking it
#: which melt the steel came from is asking the wrong document. Named as an
#: exclusion rather than listing the kinds that *are* certificates, so a new
#: certificate kind is checked from the day it exists rather than silently
#: skipped until somebody remembers this list.
NOT_A_CERTIFICATE = ("bill_of_lading",)


@register("MTR-06", "Certificate filed with no readable heat number")
def certificate_without_heat(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A certificate that cannot be tied to a heat from its filename.

    Only documents that are supposed to certify a heat. Bills of lading were
    being reported here, and the reason they had not been before is worth
    recording: their filenames are dates — `8-13-25 PIPE.pdf` — and the
    extractor used to read `8` out of one and call it a heat. Correcting that
    left them with no heat at all, which is right, and this rule then
    complained that eight certificates had unreadable heats. They are not
    certificates.
    """
    holes = ",".join("?" * len(NOT_A_CERTIFICATE))
    rows = db.q(
        f"""SELECT m.segment, m.document_id, d.filename FROM material m
            JOIN document d ON d.id = m.document_id
            WHERE m.project_id=? AND m.source='mtr_file' AND m.heat_key=''
              AND IFNULL(d.kind,'') NOT IN ({holes})""",
        (project_id, *NOT_A_CERTIFICATE),
    )
    return [
        {
            "project_id": project_id, "run_id": run_id, "rule": "MTR-06",
            "severity": "minor", "segment": r["segment"],
            "subject": r["filename"],
            "message": (
                f"No heat number can be read from '{r['filename']}', so this "
                f"certificate cannot be tied to any weld. Rename it to lead with "
                f"the heat, or record the heat inside the book."
            ),
            "detail": _detail(filename=r["filename"]),
            "document_id": r["document_id"], "page_no": None,
        }
        for r in rows
    ]


@register("MTR-07", "Certificates on file for heats no weld map uses")
def orphan_certificate(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """Paperwork for material that does not appear on any weld map.

    Reported once per segment, not once per heat.  Usually this is not a
    defect: only some lines have a machine-readable weld map, so certificates
    for the rest have nothing to match against.  The number is what matters -
    it tells the auditor how much of the material package is unaccounted for.
    """
    welded = _welded_heats(db, project_id)
    if not welded:
        return []          # nothing to compare against; not a finding

    by_segment: dict[str, list] = defaultdict(list)
    for key, rows in sorted(_certified_heats(db, project_id).items()):
        if key not in welded:
            by_segment[rows[0]["segment"]].append(rows[0])

    findings: list[Finding] = []
    for segment, rows in sorted(by_segment.items()):
        heats = [r["heat"] for r in rows]
        findings.append(
            {
                "project_id": project_id, "run_id": run_id, "rule": "MTR-07",
                "severity": "minor", "segment": segment,
                "subject": f"{len(heats)} heats",
                "message": (
                    f"{len(heats)} certificates in this segment are for heats that no "
                    f"weld map in the project uses ({', '.join(heats[:6])}"
                    f"{'...' if len(heats) > 6 else ''}). Expected where a line's weld "
                    f"map is not machine-readable; otherwise the heat map is incomplete."
                ),
                "detail": _detail(heats=", ".join(heats[:60]), count=len(heats)),
                "document_id": rows[0]["document_id"], "page_no": None,
            }
        )
    return findings


#: Certificates named in the message before it says "and N more". Enough to
#: start on, short enough that the line stays readable; the rest are in the
#: report's own columns.
_UNREAD_SHOWN = 8


@register("MTR-08", "Heat has no determinable manufacturer")
def manufacturer_unknown(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """Certificates whose manufacturer is not recorded anywhere machine-readable.

    Not a defect in the package — it is the boundary of what can be checked
    without a person reading the certificates. Reported once per segment so
    the size of that gap is visible in one line rather than sixty.

    Which makes naming the certificates the whole job. "7 heats could not be
    checked" states that a gap exists and leaves the reader to find it: the
    only way to act on it was to open every MTR in the segment book and work
    out which seven were the unread ones. The heats and their file names go in
    the message, and every document id goes in the detail, which is what the
    report turns into full paths.
    """
    if not _aml_from_db(db, project_id):
        return []
    # A heat counts as unknown only when *no* source names its manufacturer -
    # a certificate on file plus a pipe export that names the mill is fine.
    rows = db.q(
        """SELECT c.segment, c.heat, c.heat_key, c.document_id, d.filename
           FROM material c
           LEFT JOIN document d ON d.id = c.document_id
           WHERE c.project_id=? AND c.heat_key<>'' AND c.source='mtr_file'
             AND NOT EXISTS (
               SELECT 1 FROM material k
               WHERE k.project_id = c.project_id AND k.heat_key = c.heat_key
                 AND IFNULL(k.manufacturer,'') <> ''
             )
           ORDER BY c.segment, d.filename, c.heat""",
        (project_id,),
    )

    by_segment: dict[str, dict] = {}
    for r in rows:
        seg = by_segment.setdefault(r["segment"] or "", {"heats": {}, "docs": []})
        seg["heats"].setdefault(r["heat_key"], (r["heat"], r["filename"]))
        if r["document_id"] and r["document_id"] not in seg["docs"]:
            seg["docs"].append(r["document_id"])

    findings: list[Finding] = []
    for segment, seen in by_segment.items():
        pairs = list(seen["heats"].values())
        if not pairs:
            continue
        shown = "; ".join(
            f"{heat} on {name}" if name else f"{heat} (certificate not identified)"
            for heat, name in pairs[:_UNREAD_SHOWN])
        if len(pairs) > _UNREAD_SHOWN:
            shown += f"; and {len(pairs) - _UNREAD_SHOWN} more"
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "MTR-08",
            "severity": "info", "segment": segment,
            "subject": f"{len(pairs)} heats",
            "message": (
                f"{len(pairs)} heats in this segment have a certificate on file "
                f"but no machine-readable manufacturer, so they could not be "
                f"checked against the AML. These are the certificates to read, "
                f"and the only ones — {shown}. Every one is listed with its full "
                f"path in the report; entering the manufacturer against any of "
                f"them closes that heat."
            ),
            "detail": _detail(heats=len(pairs),
                              unread=", ".join(h for h, _n in pairs),
                              document_ids=", ".join(str(d) for d in seen["docs"])),
            # The first certificate, so the row itself resolves to a path; the
            # rest reach the report through document_ids above.
            "document_id": seen["docs"][0] if seen["docs"] else None,
            "page_no": None,
        })
    return findings


@register("MTR-10", "No material certificates filed in this project")
def no_certificates(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """The weld maps name heats but the project folder holds no MTRs.

    Reported once, as a major, rather than firing MTR-01 for every heat: the
    certificates usually live in another book, and claiming a hundred missing
    MTRs when the folder simply does not hold them is noise.
    """
    if db.one("SELECT 1 FROM material WHERE project_id=? AND source='mtr_file' LIMIT 1",
              (project_id,)):
        return []
    welded = _welded_heats(db, project_id)
    if not welded:
        return []
    return [
        {
            "project_id": project_id, "run_id": run_id, "rule": "MTR-10",
            "severity": "major", "segment": "(project)",
            "subject": f"{len(welded)} heats",
            "message": (
                f"The package names {len(welded)} distinct heats as installed, but "
                f"this project folder contains no material certificates, so none of "
                f"them can be verified. Point the audit at the folder holding the "
                f"MTRs, or add them to this book."
            ),
            "detail": _detail(heats=", ".join(sorted(r["heat"] for r in welded.values())[:25])),
            "document_id": None, "page_no": None,
        }
    ]


@register("MTR-09", "No approved materials list available")
def no_aml(db: Database, project_id: int, run_id: str) -> list[Finding]:
    if _aml_from_db(db, project_id):
        return []
    if not db.one("SELECT 1 FROM material WHERE project_id=? LIMIT 1", (project_id,)):
        return []
    return [
        {
            "project_id": project_id, "run_id": run_id, "rule": "MTR-09",
            "severity": "major", "segment": "(project)",
            "subject": "AML",
            "message": (
                "No approved materials list workbook was found for this project, so "
                "no manufacturer could be checked. Place 'AML Search Spreadsheet.xlsx' "
                "in the project folder or a parent folder."
            ),
            "detail": "", "document_id": None, "page_no": None,
        }
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _materials_with_manufacturer(db: Database, project_id: int) -> list:
    return db.q(
        """SELECT * FROM material
           WHERE project_id=? AND IFNULL(manufacturer,'')<>''""",
        (project_id,),
    )


def _categories(row) -> list[str] | None:
    cats = [c for c in (row["categories"] or "").split("; ") if c]
    return cats or None


def _describe(row) -> str:
    bits = [b for b in (row["description"], row["grade"]) if b]
    return ", ".join(bits)[:70] or "material"


def _nps(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _collapse(findings: list[Finding], by: str,
              per_segment: bool = True) -> list[Finding]:
    """One finding per distinct subject, noting how many heats it covers.

    A single unapproved mill can supply hundreds of joints; the auditor needs
    to know about the mill once, with the heats attached.

    ``per_segment`` keeps a separate finding for each segment, which is right
    for most checks because a segment is a book somebody signs off. It is
    wrong for a question about a *company*: whether a mill is on the approved
    list has one answer for the whole job, and asking it once per segment
    turned 53 manufacturers into 66 criticals that say the same thing.
    """
    grouped: dict[tuple, list[Finding]] = defaultdict(list)
    for f in findings:
        grouped[((f["segment"] if per_segment else ""), f[by])].append(f)

    out: list[Finding] = []
    for (_segment, _subject), group in grouped.items():
        head = dict(group[0])
        if not per_segment:
            # The finding is about the company, so name every segment its
            # material reached rather than silently keeping the first.
            segments = sorted({f["segment"] for f in group if f["segment"]})
            head["segment"] = (segments[0] if len(segments) == 1
                               else f"{len(segments)} segments")
            if len(segments) > 1:
                head["message"] += (" Material from this manufacturer appears in: "
                                    + ", ".join(segments[:8])
                                    + (f" and {len(segments) - 8} more."
                                       if len(segments) > 8 else "."))
        if len(group) > 1:
            heats = []
            for f in group:
                try:
                    heats.append(json.loads(f["detail"]).get("heat", ""))
                except (TypeError, ValueError):
                    pass
            heats = [h for h in heats if h]
            head["message"] += f" Affects {len(group)} heats: {', '.join(heats[:10])}" + (
                f" and {len(heats) - 10} more." if len(heats) > 10 else "."
            )
            detail = json.loads(head["detail"]) if head["detail"] else {}
            detail["heats"] = ", ".join(heats[:40])
            detail["heat_count"] = len(group)
            # The certificates this covers, not just the first one. A finding
            # about a manufacturer can span a dozen documents, and the row
            # links to whichever happened to sort first — which is the wrong
            # one eleven times out of twelve for anybody going to correct it.
            docs = sorted({f["document_id"] for f in group if f.get("document_id")})
            if len(docs) > 1:
                detail["document_ids"] = ", ".join(str(d) for d in docs[:40])
                detail["document_count"] = len(docs)
            head["detail"] = json.dumps(detail)
        out.append(head)
    return out


# ---------------------------------------------------------------------------
# The list itself
# ---------------------------------------------------------------------------

#: Warn this far ahead of the validity date. Long enough to fetch the new
#: revision before a book is signed off against a list about to lapse.
EXPIRING_SOON = 45


@register("AML-01", "Approved manufacturer list is out of date")
def aml_out_of_date(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """The approved list used for this audit had run out, or nearly.

    Every MTR-01 and MTR-02 finding in this report rests on the list named
    here, and a list that expired months ago answers "is this mill approved?"
    for a world that has moved on. Between two consecutive revisions of one
    real Piping AML, 68 manufacturer/location pairs were dropped: material
    cleared under the older list would not have been cleared under the newer.

    Nothing else in the report can show this. A spreadsheet transcribed from
    an AML carries no validity date at all, so an audit run against a
    three-year-old copy looks exactly like one run this morning.
    """
    from datetime import date, datetime

    row = db.one("SELECT * FROM aml_source WHERE project_id=?", (project_id,))
    if row is None:
        return []

    name = Path(row["path"]).name if row["path"] else "the approved list"
    if not row["valid_thru"]:
        # A workbook states no date. Worth one quiet note, not an accusation:
        # it may well be current, and there is no way to tell from the file.
        if row["kind"] == "workbook":
            return [{
                "project_id": project_id, "run_id": run_id, "rule": "AML-01",
                "severity": "info", "segment": "", "subject": name,
                "message": (
                    f"Manufacturers were checked against '{name}', a workbook, "
                    f"which carries no validity date — so whether it is the "
                    f"current revision cannot be established from the audit. "
                    f"Auditing against the issued AML PDF instead lets this be "
                    f"checked, and states the revision in the report."
                ),
            }]
        return []

    lapsed = date.fromisoformat(row["valid_thru"])
    today = datetime.now().date()
    days = (today - lapsed).days
    printed = row["revision"] or lapsed.isoformat()

    if days > 0:
        return [{
            "project_id": project_id, "run_id": run_id, "rule": "AML-01",
            "severity": "critical", "segment": "", "subject": name,
            "message": (
                f"The approved manufacturer list used for this audit expired "
                f"{days} days ago: '{name}', valid thru {printed}. Every "
                f"manufacturer verdict in this report — approved and not "
                f"listed alike — was decided against a list that is no longer "
                f"in force. Fetch the current revision from the the supplier list "
                f"SharePoint site and re-run before signing anything off."
            ),
        }]

    if -days <= EXPIRING_SOON:
        return [{
            "project_id": project_id, "run_id": run_id, "rule": "AML-01",
            "severity": "minor", "segment": "", "subject": name,
            "message": (
                f"The approved manufacturer list expires in {-days} days "
                f"('{name}', valid thru {printed}). Worth fetching the next "
                f"revision before this book is signed off."
            ),
        }]
    return []


#: A heat read off a page that is really a material specification. The reader
#: puts "A/SA105-N" in the heat field often enough to matter, and a
#: specification compared against a heat is a finding about nothing.
def _spec_not_heat(text: str) -> bool:
    return any(_SPEC_PATTERN.match(part) for part in re.split(r"[/-]", text or "")
               if part)


#: How much of a shorter heat has to be present before a prefix is treated as
#: the same heat written two ways. Below this a coincidence is likely.
_ENOUGH_OF_A_PREFIX = 4


def _same_heat_two_ways(filename_heat: str, page_heat: str) -> bool:
    """Whether these two readings describe one heat.

    Three ways they legitimately differ, none of which is a filing error:

    * exactly equal once punctuation is dropped;
    * one character apart on a scan — the tolerance the rest of the audit
      already uses, because a scanned heat that comes back a character out is
      a statement about the scan, not about the steel;
    * one is a prefix of the other. A certificate often prints the melt,
      ``A11484``, while the filename carries the piece it was cut for,
      ``A11484-24``. Reporting that pair would be a finding on a naming
      convention rather than on the material.
    """
    left, right = normalise_heat(filename_heat), normalise_heat(page_heat)
    if not left or not right or left == right:
        return True
    if same_heat_differently_read(filename_heat, page_heat):
        return True
    short, long = sorted((left, right), key=len)
    return len(short) >= _ENOUGH_OF_A_PREFIX and long.startswith(short)


@register("MTR-12", "Certificate's filename names a different heat than the page")
def filename_heat_disagrees(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """The heat in the filename is not the heat printed on the certificate.

    Every heat in this corpus arrives from a filename — ``A11484-24 ~ 4IN 300
    RFWN FLANGE.pdf`` — because that is the only exact text there is; a heat
    on a scan has been through OCR. The filename is also typed by whoever
    filed the document, and nothing was checking it against the page.

    So a certificate filed under the wrong heat passed silently. The material
    is then credited to a melt it did not come from: the wrong mill is checked
    against the approved list, the heat that was really installed shows as
    having no certificate, and the heat named in the filename shows as
    certified when nothing certifies it. One typo, three wrong answers, and
    nothing anywhere to say so.

    Only fires where a page was actually read. A certificate nobody has put
    through the reader has no page heat, and silence there means unchecked,
    not agreed — which is what MTR-08 and the readings banner are for.
    """
    rows = db.q(
        """SELECT m.segment, m.heat, m.page_heat, m.document_id, d.filename
           FROM material m JOIN document d ON d.id = m.document_id
           WHERE m.project_id=? AND m.source='mtr_file'
             AND IFNULL(m.heat_key,'') <> '' AND IFNULL(m.page_heat,'') <> ''
           ORDER BY d.filename""",
        (project_id,),
    )

    # Grouped by what the page said, not by file. One heat covers many pieces,
    # and a works will certify all of them on one sheet that is then filed
    # once per piece under each piece's own number — five swing check valves,
    # five copies of one certificate, one heat AJ3550. Reported per file that
    # is five findings saying the same thing about the same page; reported per
    # page heat it is one finding that names all five.
    from collections import defaultdict

    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        if _spec_not_heat(r["page_heat"]):
            continue                      # the reader picked up A/SA105-N
        if _same_heat_two_ways(r["heat"], r["page_heat"]):
            continue
        groups[r["page_heat"]].append(r)

    out: list[Finding] = []
    for page_heat, found in sorted(groups.items()):
        first = found[0]
        named = ", ".join(sorted({r["heat"] for r in found}))
        if len(found) == 1:
            what = (f"'{first['filename']}' is filed under heat {first['heat']}, "
                    f"but the certificate itself reads {page_heat}")
        else:
            what = (f"{len(found)} certificates all read {page_heat} on the page "
                    f"but are filed under {named}")
        out.append({
            "project_id": project_id, "run_id": run_id, "rule": "MTR-12",
            "severity": "major", "segment": first["segment"],
            "subject": named[:80],
            "message": (
                f"{what}. The material is being credited to the filename rather "
                f"than to what the certificate says. Either the filenames carry "
                f"a piece or serial number instead of the heat -- in which case "
                f"heat {page_heat} is not recorded as certified anywhere -- or a "
                f"certificate is filed under the wrong heat, and whatever was "
                f"really installed under {named} has no certificate at all. "
                f"Open the page and settle which."
            ),
            "detail": _detail(page_heat=page_heat, filed_under=named,
                              certificates="; ".join(r["filename"] for r in found),
                              document_ids=",".join(str(r["document_id"]) for r in found)),
            "document_id": first["document_id"], "page_no": 1,
        })
    return out
