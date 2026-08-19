"""Reconciling the coating record against the coating specification.

Coating is the only corrosion barrier a buried line has, and unlike a weld it
cannot be re-examined once the ditch is backfilled — the daily field coating
inspection report *is* the evidence, permanently.  That makes two different
kinds of failure worth separating, and the rules here do:

* the report records a value outside what the specification allows
  (COAT-01..COAT-05);
* the report leaves the field blank, so nothing was recorded either way
  (COAT-06..COAT-08);
* the report and the weld register disagree about which joints were coated,
  or about the order the work was done in (COAT-11..COAT-14).

The second is the common one. On the Kestrel 8 reports the blast media,
cleanliness standard, required profile, dry film thickness and jeeped stations
are all empty on a form that is signed by both the inspector and the
contractor — the paperwork is complete and says almost nothing.

Every threshold below is quoted from a document in the corpus rather than from
general practice, because a coating rule with an invented limit produces
confident findings an auditor cannot defend:

* GPPB-0140 Rev 3 §4.C — "No coatings shall be applied during fog, mist or
  rain, when relative humidity is greater than 85% or on wet surfaces, and no
  epoxy coating shall be applied when the temperature is below 40°F."
* GPPB-0140 Rev 3 §4.E — FBE by flocking, "12 - 14 mils minimum ... on pipe
  sizes 16" or greater"; and before lowering in, "the coating shall be
  'jeeped' or inspected by another high voltage Holiday Detector".
* GPPB-0140 Rev 3 Table I footnotes 3 and 4 — coal slag, aka "Black Beauty",
  and Dolen sand are not to be used in field application.
* Sherwin-Williams Hi-Solids Polyurethane product data sheet — "At least 5°F
  (2.8°C) above dew point", "Relative humidity: 85% maximum".
"""

from __future__ import annotations

import json
import re
from collections import defaultdict

from ..db import Database
from ..instruments import LABELS, nearest_serials
from . import Finding, register

#: GPPB-0140 §4.C and every product data sheet in the corpus.
MAX_HUMIDITY = 85.0

#: The margin steel temperature must clear the dew point by, per the product
#: data sheets. Condensation on the steel under a coat is invisible once the
#: coat is on and delaminates it later.
DEW_POINT_MARGIN_F = 5.0

#: GPPB-0140 §4.C, for epoxy specifically.
MIN_EPOXY_TEMP_F = 40.0

#: GPPB-0140 §4.E: pipe this size and larger shall be flocked, 12-14 mils min.
FLOCKING_NPS = 16.0
FBE_MIN_MILS = 12.0

#: GPPB-0140 Table I footnotes 3 and 4.
_PROHIBITED_MEDIA = re.compile(r"black beauty|coal slag|dolen", re.IGNORECASE)

#: Minimum DFT by coat, GPPB-0140 Table I (Mild Corrosive Area). The footnote
#: reads "THIS IS THE MINIMUM ACCEPTABLE DFT".
MIN_DFT = {"epoxy": 4.0, "polyurethane": 3.0, "silicone": 5.0}

#: Which of those a product name belongs to. Kept small and literal: guessing
#: a generic type from an unfamiliar trade name would invent a limit.
_PRODUCT_TYPE = (
    ("epoxy", re.compile(r"macropoxy|recoatable epoxy|dura.?plate|epoxy", re.I)),
    ("polyurethane", re.compile(r"acrolon|hi.?solids|polyurethane|urethane", re.I)),
    ("silicone", re.compile(r"heat.?flex|hi.?temp 1200|silicone", re.I)),
)

#: Fields the form asks for that decide whether the coating can be judged at
#: all.  Reported together rather than one finding each: a report with five
#: blanks is one incomplete report, not five problems.
_REQUIRED_FIELDS = (
    ("blast_media", "blast media"),
    ("cleanliness", "cleanliness standard"),
    ("profile_reqd", "required profile"),
)


def _detail(**kw) -> str:
    return json.dumps({k: v for k, v in kw.items() if v not in (None, "")})


def _reports(db: Database, project_id: int) -> list:
    return db.q(
        """SELECT r.*, d.filename FROM coating_report r
           LEFT JOIN document d ON d.id = r.document_id
           WHERE r.project_id=? ORDER BY r.segment, r.report_date, r.page_no""",
        (project_id,),
    )


def every_report_read(db: Database, project_id: int) -> bool:
    """Whether every coating document in the project has been read through.

    COAT-10 reasons from absence — a segment with welds and no coating report —
    so it is only sound once the reading is finished. Seed a single page of
    Bluewater's nineteen documents and it reports sixteen segments as uncoated,
    every one of them an artefact of stopping early rather than a gap in the
    package.

    Counted by pages read rather than by reports produced, for the same reason
    the backfill guard is: a page that is not a report — a cover sheet, a
    product data sheet bound into the same PDF — must not make the bundle
    unreadable for ever.
    """
    from ..vision import page_count

    for r in db.q(
        """SELECT MIN(id) id, path, fingerprint FROM document
           WHERE project_id=? AND kind='coating' AND ext IN ('.pdf','.PDF')
           GROUP BY IFNULL(fingerprint, id)""",
        (project_id,),
    ):
        pages = page_count(r["path"])
        if not pages:
            return False            # cannot open it, so cannot know
        fingerprint = r["fingerprint"] or str(r["id"])
        for page_no in range(pages):
            payload = db.ocr_any(fingerprint, "coating", page_no)
            if payload is None or payload.get("_error"):
                return False
    return True


def _label(report) -> str:
    """How to name a report: its date, else the file it came from."""
    date = report["report_date"] or ""
    where = report["service"] or report["segment"] or ""
    if date and where:
        return f"the {date} report for {where}"
    return f"the {date} coating report" if date else (
        f"the coating report in {report['filename']}" if report["filename"]
        else "the coating report")


def _sentence(text: str) -> str:
    """Capitalise the first letter and leave the rest alone.

    ``str.capitalize`` lower-cases everything after it, which turns a service
    named "GL" into "gl" and a weld id into nonsense.
    """
    return text[:1].upper() + text[1:]


def product_type(product: str, manufacturer: str = "") -> str:
    """Which generic coating type a trade name is, or '' when unrecognised."""
    text = f"{product or ''} {manufacturer or ''}"
    for name, pat in _PRODUCT_TYPE:
        if pat.search(text):
            return name
    return ""


def _children(db: Database, table: str, report_id: int, order: str = "seq") -> list:
    return db.q(f"SELECT * FROM {table} WHERE report_id=? ORDER BY {order}",
                (report_id,))


def _applied(coats: list) -> list:
    """The rows that represent coating actually put on the pipe.

    Which column holds the product depends on who filled the form in: Kestrel 8's
    reports write "Macropoxy 646" under Coating Mfr and leave Product
    ID/Description empty, while Bluewater's split it across both. Testing only
    the product column reads those days as "no coating applied" and silences
    every rule that matters on them.
    """
    return [c for c in coats
            if c["product"] or c["manufacturer"] or c["batch_a"]
            or c["dft"] is not None or c["wft"] is not None]


def _names(coats: list) -> str:
    return ", ".join(sorted({
        (c["product"] or c["manufacturer"]) for c in coats
        if c["product"] or c["manufacturer"]
    }))


# ---------------------------------------------------------------------------
# Tying the coating record to individual welds
# ---------------------------------------------------------------------------
#
# The later revision of the coating form carries an NDE Weld # column, which
# is the only direct link between the coating record and the weld register.
# Three things make it usable, and one makes it dangerous.
#
# Usable: the column is a real NDE identifier once normalised — the form
# writes `GXR 048` where everything else on the job writes `GXR-048`; the
# reader sheets know far more welds than the weld register does (2,141 shots
# against 80 linked welds on Bluewater); and both carry dates.
#
# Dangerous: the coating report's *segment* is where the file was put, not
# what it covers. Bluewater's 8-21-25 report is filed under `6 IN FUEL GAS SEG A`
# and its header says 20" Low Pressure — and the weld it names, GXR-048, sits
# in the `20 LP` reader sheets. So the weld id is the only safe join, and any
# rule that reasons by segment here will be wrong.


def _coated(db: Database, project_id: int) -> list:
    """Every coating row that names a weld, with its report's date."""
    return db.q(
        """SELECT c.nde_id, c.nde_weld_no, c.dft, c.product, c.manufacturer,
                  r.id AS report_id, r.report_date, r.segment, r.service,
                  r.document_id, r.page_no, d.filename
           FROM coating_coat c
           JOIN coating_report r ON r.id = c.report_id
           LEFT JOIN document d ON d.id = r.document_id
           WHERE r.project_id=? AND c.nde_id<>''
           ORDER BY c.nde_id""",
        (project_id,),
    )


def _known_welds(db: Database, project_id: int) -> dict[str, dict]:
    """Every weld the project knows of, by NDE id, with the earliest dates.

    Reader sheets and the weld register are merged rather than chosen
    between: on a job with a CSV weld log the register is authoritative, and
    on a scanned job like Bluewater the reader sheets are all there is.
    """
    known: dict[str, dict] = {}

    def note(nde_id: str, welded: str, examined: str) -> None:
        if not nde_id:
            return
        row = known.setdefault(nde_id, {"welded": "", "examined": ""})
        for field, value in (("welded", welded), ("examined", examined)):
            if value and (not row[field] or value < row[field]):
                row[field] = value

    for r in db.q(
        "SELECT nde_id, date_welded FROM weld WHERE project_id=? AND nde_id<>''",
        (project_id,),
    ):
        note(r["nde_id"], r["date_welded"] or "", "")
    for r in db.q(
        "SELECT nde_id, sheet_date FROM nde_shot WHERE project_id=? AND nde_id<>''",
        (project_id,),
    ):
        note(r["nde_id"], "", r["sheet_date"] or "")
    return known


def _split_id(nde_id: str) -> tuple[str, int] | None:
    from ..weldmap import parse_id_token

    parsed = parse_id_token(nde_id)
    return (parsed[0], parsed[1]) if parsed else None


@register("COAT-11", "Coating report names a weld nothing else knows")
def coated_weld_unknown(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A weld coated according to the coating report and on no other record.

    Either the weld number was written down wrong or a joint was made and
    coated without ever being examined. Both are worth the same question.
    """
    known = _known_welds(db, project_id)
    if not known:
        return []                       # nothing to check against

    findings: list[Finding] = []
    for row in _coated(db, project_id):
        if row["nde_id"] in known:
            continue
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "COAT-11",
            "severity": "major", "segment": row["segment"],
            "subject": row["nde_id"],
            "message": (
                f"{_sentence(_label(row))} records coating on weld "
                f"{row['nde_weld_no']}, and no weld map, weld log or reader "
                f"sheet on this job mentions it. Either the number is wrong or "
                f"a joint was coated that was never examined."
            ),
            "detail": _detail(weld=row["nde_id"], as_written=row["nde_weld_no"],
                              report_date=row["report_date"]),
            "document_id": row["document_id"], "page_no": row["page_no"],
        })
    return findings


@register("COAT-12", "Weld coated before it was examined")
def coated_before_nde(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """Coating applied to a weld before the NDE that accepts it.

    A coated weld cannot be radiographed, and a reject found afterwards means
    stripping the coating back off — so the sequence is the control, not a
    formality. Same-day is normal and passes: shot in the morning, coated in
    the afternoon.
    """
    known = _known_welds(db, project_id)
    findings: list[Finding] = []
    for row in _coated(db, project_id):
        coated_on = row["report_date"]
        examined = (known.get(row["nde_id"]) or {}).get("examined")
        if not coated_on or not examined or coated_on >= examined:
            continue
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "COAT-12",
            "severity": "major", "segment": row["segment"],
            "subject": row["nde_id"],
            "message": (
                f"Weld {row['nde_id']} was coated on {coated_on} and examined "
                f"on {examined} — the coating went on first. A coated weld "
                f"cannot be radiographed, and a reject found afterwards means "
                f"stripping the coating back off to repair it."
            ),
            "detail": _detail(weld=row["nde_id"], coated=coated_on,
                              examined=examined),
            "document_id": row["document_id"], "page_no": row["page_no"],
        })
    return findings


@register("COAT-13", "Weld coated before it was welded")
def coated_before_welded(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A coating date earlier than the date the joint was made.

    Impossible rather than merely wrong, so whichever date is bad, one of the
    two records cannot be relied on for anything else either.
    """
    known = _known_welds(db, project_id)
    findings: list[Finding] = []
    for row in _coated(db, project_id):
        coated_on = row["report_date"]
        welded = (known.get(row["nde_id"]) or {}).get("welded")
        if not coated_on or not welded or coated_on >= welded:
            continue
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "COAT-13",
            "severity": "critical", "segment": row["segment"],
            "subject": row["nde_id"],
            "message": (
                f"Weld {row['nde_id']} is recorded as coated on {coated_on} "
                f"and welded on {welded}. The joint cannot have been coated "
                f"before it existed, so one of the two dates is wrong and "
                f"neither record can be relied on for this weld."
            ),
            "detail": _detail(weld=row["nde_id"], coated=coated_on, welded=welded),
            "document_id": row["document_id"], "page_no": row["page_no"],
        })
    return findings


@register("COAT-14", "Weld inside a coated run with no coating record")
def uncoated_weld(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """Welds skipped between the first and last weld a coating report names.

    Scoped to the range the coating reports actually cover, per NDE series.
    If the reports name GXR-040 and GXR-048, then GXR-041 to GXR-047 are welds
    the coating crew worked past and did not write down; GXR-001 and GXR-089
    are outside anything the reports claim, and reporting them would turn
    "these reports have not all been read yet" into a finding about the pipe.
    """
    coated_ids = {row["nde_id"] for row in _coated(db, project_id)}
    if not coated_ids:
        return []

    known = _known_welds(db, project_id)

    # The run is bounded by coated welds the project also *knows*. A weld id
    # nothing else recognises is the one most likely to be a slip of the pen,
    # and letting it set the boundary is how a single mistyped `GXR 900`
    # turns eight uncoated joints into a hundred and twenty-two. COAT-11
    # reports the unknown id itself; it does not get to define coverage.
    covered: dict[str, list[int]] = defaultdict(list)
    for nde_id in coated_ids & set(known):
        if split := _split_id(nde_id):
            covered[split[0]].append(split[1])
    if not covered:
        return []
    gaps: dict[str, list[str]] = defaultdict(list)
    for nde_id in known:
        if nde_id in coated_ids:
            continue
        split = _split_id(nde_id)
        if not split:
            continue
        prefix, number = split
        numbers = covered.get(prefix)
        if not numbers or not (min(numbers) < number < max(numbers)):
            continue
        gaps[prefix].append(nde_id)

    findings: list[Finding] = []
    for prefix, missing in sorted(gaps.items()):
        numbers = covered[prefix]
        listed = sorted(missing)
        sample = ", ".join(listed[:8])
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "COAT-14",
            "severity": "major", "segment": "",
            "subject": f"{len(listed)} welds",
            "message": (
                f"The coating reports cover {prefix}-{min(numbers):03d} to "
                f"{prefix}-{max(numbers):03d} and never name {len(listed)} weld"
                f"{'s' if len(listed) != 1 else ''} inside that run "
                f"({sample}{'...' if len(listed) > 8 else ''}). A field girth "
                f"weld is bare where the mill coating stops, so a joint the "
                f"coating crew worked past is uncoated pipe in the ditch."
            ),
            "detail": _detail(prefix=prefix, welds=", ".join(listed[:40]),
                              covered_from=f"{prefix}-{min(numbers):03d}",
                              covered_to=f"{prefix}-{max(numbers):03d}"),
            "document_id": None, "page_no": None,
        })
    return findings


@register("COAT-15", "Total welds coated disagrees with the rows")
def welds_coated_mismatch(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """The count box against the number of joints the report actually lists."""
    findings: list[Finding] = []
    for report in _reports(db, project_id):
        stated = report["welds_coated"]
        if stated is None:
            continue
        listed = len([c for c in _children(db, "coating_coat", report["id"])
                      if c["nde_id"]])
        if not listed or abs(stated - listed) < 0.5:
            continue
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "COAT-15",
            "severity": "minor", "segment": report["segment"],
            "subject": _label(report),
            "message": (
                f"{_sentence(_label(report))} says {stated:g} welds were "
                f"coated but lists {listed}. The count and the rows should "
                f"agree; whichever is right, the other is a gap in the record."
            ),
            "detail": _detail(stated=stated, listed=listed,
                              report_date=report["report_date"]),
            "document_id": report["document_id"], "page_no": report["page_no"],
        })
    return findings


def report_summary(db: Database, project_id: int) -> list[dict]:
    """Every coating report, with what it recorded and what it left blank.

    The blank count travels with each row because that is the story of these
    reports: the numbers that are present almost always pass, and what an
    auditor needs to see at a glance is how much was never written down.
    """
    out: list[dict] = []
    for report in _reports(db, project_id):
        coats = _children(db, "coating_coat", report["id"])
        applied = _applied(coats)
        profiles = [r["mils"] for r in _children(db, "coating_profile", report["id"])
                    if r["mils"] is not None]
        environment = _children(db, "coating_environment", report["id"])
        dfts = [c["dft"] for c in coats if c["dft"] is not None]
        margins = [r["steel_temp"] - r["dew_point"] for r in environment
                   if r["steel_temp"] is not None and r["dew_point"] is not None]
        humidities = [r["humidity"] for r in environment if r["humidity"] is not None]

        missing = [label for column, label in _REQUIRED_FIELDS if not report[column]]
        if applied and not dfts:
            missing.append("dry film thickness")
        if not report["jeep_from"] and not report["jeep_to"]:
            missing.append("jeeped stations")

        out.append({
            "segment": report["segment"] or "",
            "report_date": report["report_date"] or "",
            "service": report["service"] or "",
            "line_size": report["line_size"] or "",
            "products": _names(applied),
            "method": ", ".join(sorted({c["method"] for c in coats if c["method"]})),
            "blast_media": report["blast_media"] or "",
            "cleanliness": report["cleanliness"] or "",
            "profile_required": report["profile_reqd"],
            "profile_low": min(profiles) if profiles else None,
            "dft_low": min(dfts) if dfts else None,
            "humidity_high": max(humidities) if humidities else None,
            "dew_point_margin": min(margins) if margins else None,
            "jeeped": bool(report["jeep_from"] or report["jeep_to"]),
            "welds_coated": report["welds_coated"],
            # How many joints the report ties itself to. Zero means the older
            # form revision, which has no NDE Weld # column at all.
            "welds_named": len([c for c in coats if c["nde_id"]]),
            "missing": missing,
            "inspector": report["inspector"] or "",
            "document_id": report["document_id"],
            "filename": report["filename"] or "",
            "page_no": report["page_no"],
        })
    return out


# ---------------------------------------------------------------------------


@register("COAT-01", "Coating applied outside the allowed conditions")
def conditions_out_of_range(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """Humidity above 85%, or steel within 5°F of the dew point.

    Both come off the same ambient reading and both cause the same failure —
    moisture under the coat — so they are reported together against the
    reading that recorded them.
    """
    findings: list[Finding] = []
    for report in _reports(db, project_id):
        for row in _children(db, "coating_environment", report["id"]):
            reasons, detail = [], {}
            if row["humidity"] is not None and row["humidity"] > MAX_HUMIDITY:
                reasons.append(
                    f"relative humidity was {row['humidity']:g}%, above the "
                    f"{MAX_HUMIDITY:g}% maximum")
                detail["humidity"] = row["humidity"]
            if row["steel_temp"] is not None and row["dew_point"] is not None:
                margin = row["steel_temp"] - row["dew_point"]
                if margin < DEW_POINT_MARGIN_F:
                    reasons.append(
                        f"steel was {row['steel_temp']:g}°F against a "
                        f"{row['dew_point']:g}°F dew point, a margin of "
                        f"{margin:g}°F where {DEW_POINT_MARGIN_F:g}°F is required")
                    detail.update(steel_temp=row["steel_temp"],
                                  dew_point=row["dew_point"], margin=margin)
            if not reasons:
                continue
            when = f" at {row['reading_time']}" if row["reading_time"] else ""
            findings.append({
                "project_id": project_id, "run_id": run_id, "rule": "COAT-01",
                "severity": "critical", "segment": report["segment"],
                "subject": row["reading_time"] or _label(report),
                "message": (
                    f"On {_label(report)}{when}, {' and '.join(reasons)}. "
                    f"Coating applied in these conditions traps moisture under "
                    f"the film and disbonds later, with nothing visible at "
                    f"the time."
                ),
                "detail": _detail(**detail, reading=row["reading_time"]),
                "document_id": report["document_id"], "page_no": report["page_no"],
            })
    return findings


@register("COAT-02", "Epoxy applied below the minimum temperature")
def epoxy_too_cold(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """An epoxy coat recorded on a day the steel was below 40°F.

    Only fires where the report names an epoxy: the 40°F floor in GPPB-0140
    §4.C is specific to epoxy, and applying it to every product would invent a
    limit for coatings that do not have one.
    """
    findings: list[Finding] = []
    for report in _reports(db, project_id):
        coats = _children(db, "coating_coat", report["id"])
        epoxies = [c for c in coats
                   if product_type(c["product"], c["manufacturer"]) == "epoxy"]
        if not epoxies:
            continue
        cold = [r for r in _children(db, "coating_environment", report["id"])
                if r["steel_temp"] is not None and r["steel_temp"] < MIN_EPOXY_TEMP_F]
        if not cold:
            continue
        lowest = min(r["steel_temp"] for r in cold)
        product = epoxies[0]["product"] or "an epoxy"
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "COAT-02",
            "severity": "critical", "segment": report["segment"],
            "subject": product,
            "message": (
                f"{_sentence(_label(report))} records {product} applied with "
                f"the steel at {lowest:g}°F. No epoxy may be applied below "
                f"{MIN_EPOXY_TEMP_F:g}°F without prior XTO approval and the "
                f"manufacturer's advice on preheating."
            ),
            "detail": _detail(product=product, lowest_steel_temp=lowest),
            "document_id": report["document_id"], "page_no": report["page_no"],
        })
    return findings


@register("COAT-03", "Prohibited blast media")
def prohibited_media(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """Coal slag or Dolen sand recorded as the abrasive.

    Worth reading the finding carefully before acting on it: the older form's
    own printed instruction names Dolen Sand #3 as acceptable, while Table I
    footnote 4 of the current specification prohibits it. The specification
    governs, but a crew following the form was not being careless.
    """
    findings: list[Finding] = []
    for report in _reports(db, project_id):
        media = report["blast_media"] or ""
        if not _PROHIBITED_MEDIA.search(media):
            continue
        dolen = "dolen" in media.lower()
        note = (" The report form itself lists Dolen Sand #3 as acceptable, "
                "which contradicts the current specification — the form is out "
                "of date, not the crew." if dolen else "")
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "COAT-03",
            "severity": "major", "segment": report["segment"],
            "subject": media,
            "message": (
                f"{_sentence(_label(report))} records the blast media as "
                f"\"{media}\", which GPPB-0140 Table I prohibits in field "
                f"application.{note}"
            ),
            "detail": _detail(blast_media=media, report_date=report["report_date"]),
            "document_id": report["document_id"], "page_no": report["page_no"],
        })
    return findings


@register("COAT-04", "Dry film thickness below the specified minimum")
def thickness_below_minimum(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A measured DFT under the minimum for that coating.

    The minimum depends on what was applied: 12 mils for flocked FBE on 16"
    and larger, and otherwise the Table I figure for the generic type. A
    product the tables do not name is left alone rather than measured against
    a guess.
    """
    findings: list[Finding] = []
    for report in _reports(db, project_id):
        for coat in _children(db, "coating_coat", report["id"]):
            if coat["dft"] is None:
                continue
            minimum, why = _minimum_dft(report, coat)
            if minimum is None or coat["dft"] >= minimum:
                continue
            product = coat["product"] or coat["manufacturer"] or "the coating"
            weld = f" on weld {coat['nde_weld_no']}" if coat["nde_weld_no"] else ""
            findings.append({
                "project_id": project_id, "run_id": run_id, "rule": "COAT-04",
                "severity": "major", "segment": report["segment"],
                "subject": coat["nde_weld_no"] or product,
                "message": (
                    f"{_sentence(_label(report))} measures {product}{weld} at "
                    f"{coat['dft']:g} mils dry, against a minimum of "
                    f"{minimum:g} mils ({why}). Thin film is the usual cause "
                    f"of early coating failure on buried line."
                ),
                "detail": _detail(product=product, dft=coat["dft"], minimum=minimum,
                                  basis=why, weld=coat["nde_weld_no"],
                                  layer=coat["layer"]),
                "document_id": report["document_id"], "page_no": report["page_no"],
            })
    return findings


def _minimum_dft(report, coat) -> tuple[float | None, str]:
    # The method decides, not the diameter: a 20" line's field joints are
    # taped even though its body is flocked, and holding the tape to the FBE
    # minimum would be wrong.
    if "flock" in (coat["method"] or "").lower():
        return FBE_MIN_MILS, "flocked FBE, GPPB-0140 §4.E"
    generic = product_type(coat["product"], coat["manufacturer"])
    if generic in MIN_DFT:
        return MIN_DFT[generic], f"{generic}, GPPB-0140 Table I"
    return None, ""


@register("COAT-05", "Surface profile outside the required range")
def profile_out_of_range(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A Testex reading below the profile the same report calls for.

    Only compares against the figure written in "Profile Reqd" on that report:
    the required profile depends on the coating system, and the report is the
    only place on the job that states which one applied that day.
    """
    findings: list[Finding] = []
    for report in _reports(db, project_id):
        required = report["profile_reqd"]
        if required is None:
            continue
        readings = [r["mils"] for r in _children(db, "coating_profile", report["id"])
                    if r["mils"] is not None]
        low = [m for m in readings if m < required]
        if not low:
            continue
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "COAT-05",
            "severity": "major", "segment": report["segment"],
            "subject": f"{len(low)} of {len(readings)} profiles",
            "message": (
                f"{_sentence(_label(report))} calls for a {required:g} mil "
                f"anchor profile and records {len(low)} reading"
                f"{'s' if len(low) != 1 else ''} below it "
                f"({', '.join(f'{m:g}' for m in sorted(low))}). Coating over an "
                f"under-profiled surface has nothing to key into."
            ),
            "detail": _detail(required=required, low=", ".join(f"{m:g}" for m in low),
                              all_readings=", ".join(f"{m:g}" for m in readings)),
            "document_id": report["document_id"], "page_no": report["page_no"],
        })
    return findings


@register("COAT-06", "Coating not recorded as holiday tested")
def not_jeeped(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A report that applied coating and left the jeeped stations blank.

    GPPB-0140 §4.E requires the coating to be jeeped before the pipe is
    lowered in. Once the ditch is backfilled the test cannot be done, so a
    blank here is not a paperwork gap that can be closed later.
    """
    findings: list[Finding] = []
    for report in _reports(db, project_id):
        applied = _applied(_children(db, "coating_coat", report["id"]))
        if not applied or report["jeep_from"] or report["jeep_to"]:
            continue
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "COAT-06",
            "severity": "major", "segment": report["segment"],
            "subject": _label(report),
            "message": (
                f"{_sentence(_label(report))} records coating applied but "
                f"leaves \"Coating Jeeped From Stn\" blank, so nothing shows "
                f"the film was holiday tested before the pipe went in the "
                f"ditch. Once backfilled the test cannot be repeated."
            ),
            "detail": _detail(products=_names(applied),
                              report_date=report["report_date"]),
            "document_id": report["document_id"], "page_no": report["page_no"],
        })
    return findings


@register("COAT-07", "Coating report is missing fields the audit needs")
def incomplete_report(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """Blank fields that leave the day's work unjudgeable.

    Reported as one finding per report rather than per field. A report with no
    cleanliness standard, no required profile and no dry film thickness is one
    incomplete record; splitting it into three would triple the count without
    adding a fact.
    """
    findings: list[Finding] = []
    for report in _reports(db, project_id):
        missing = [label for column, label in _REQUIRED_FIELDS if not report[column]]

        applied = _applied(_children(db, "coating_coat", report["id"]))
        if applied and not any(c["dft"] is not None for c in applied):
            missing.append("dry film thickness")
        if not _children(db, "coating_environment", report["id"]):
            missing.append("ambient conditions")
        if not missing:
            continue

        # A report of a blast-only day has no coating to judge, so the fields
        # about the film are not yet expected.
        severity = "major" if applied else "minor"
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "COAT-07",
            "severity": severity, "segment": report["segment"],
            "subject": _label(report),
            "message": (
                f"{_sentence(_label(report))} is signed but leaves "
                f"{len(missing)} field{'s' if len(missing) != 1 else ''} blank: "
                f"{', '.join(missing)}. "
                + ("Without them the coating applied that day cannot be "
                   "checked against the specification."
                   if applied else
                   "This appears to be surface preparation only, with no "
                   "coating applied.")
            ),
            "detail": _detail(missing=", ".join(missing),
                              report_date=report["report_date"],
                              inspector=report["inspector"]),
            "document_id": report["document_id"], "page_no": report["page_no"],
        })
    return findings


@register("COAT-08", "Coating instrument has no calibration certificate")
def instrument_uncalibrated(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A gauge named on a report with no certificate filed for that serial.

    Runs only once certificates have been read: with none on file the answer
    is "not known". A serial one character from one on file is reported as the
    transcription it almost certainly is — `BTYG12` is filed and one report
    writes `BTYGL2` — rather than as an uncalibrated instrument.
    """
    certs = {
        r["serial_key"]: r
        for r in db.q(
            "SELECT serial_key, serial, kind, calibrated FROM instrument_cal "
            "WHERE project_id=? AND serial_key<>''",
            (project_id,),
        )
    }
    if not certs:
        return []

    findings: list[Finding] = []
    for report in _reports(db, project_id):
        for used in _children(db, "coating_instrument", report["id"], order="id"):
            if not used["serial_key"] or used["serial_key"] in certs:
                continue
            what = LABELS.get(used["kind"], "instrument")
            near = [certs[k]["serial"] for k in nearest_serials(used["serial"], set(certs))]
            if near:
                severity = "minor"
                message = (
                    f"{_sentence(_label(report))} names {what} serial "
                    f"{used['serial']}, which has no certificate, but "
                    f"{' or '.join(near)} does and differs by one character. "
                    f"Almost certainly the same instrument written down wrong; "
                    f"worth correcting on the record rather than chasing."
                )
            else:
                severity = "major"
                message = (
                    f"{_sentence(_label(report))} names {what} serial "
                    f"{used['serial']}, and no calibration certificate for it "
                    f"is filed on this job. An uncalibrated gauge cannot "
                    f"evidence the thickness or the holiday test it produced."
                )
            findings.append({
                "project_id": project_id, "run_id": run_id, "rule": "COAT-08",
                "severity": severity, "segment": report["segment"],
                "subject": used["serial"],
                "message": message,
                "detail": _detail(instrument=what, serial=used["serial"],
                                  near_match=", ".join(near),
                                  report_date=report["report_date"]),
                "document_id": report["document_id"], "page_no": report["page_no"],
            })
    return findings


@register("COAT-09", "Underground pipe 16\" or larger was not flocked")
def wrong_coating_system(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A large-diameter line coated by some method other than flocking.

    GPPB-0140 §4.E: 16" and greater underground piping shall be flocked; 12"
    and under may be flocked or wrapped with RD-6. Reported as major rather
    than critical because a report can describe above-ground work on a line
    whose buried run was flocked elsewhere.
    """
    findings: list[Finding] = []
    for report in _reports(db, project_id):
        nps = report["line_nps"]
        if nps is None or nps < FLOCKING_NPS:
            continue
        for coat in _children(db, "coating_coat", report["id"]):
            method = (coat["method"] or "").strip()
            if not method or "flock" in method.lower():
                continue
            if not re.search(r"rd.?6|tape|wrap|hand", method, re.IGNORECASE):
                continue
            findings.append({
                "project_id": project_id, "run_id": run_id, "rule": "COAT-09",
                "severity": "major", "segment": report["segment"],
                "subject": f'{nps:g}" by {method}',
                "message": (
                    f"{_sentence(_label(report))} coats a {nps:g}\" line by "
                    f"{method}. GPPB-0140 §4.E requires underground piping "
                    f"16\" and greater to be flocked; RD-6 and tape are for "
                    f"12\" and under, and for field joints."
                ),
                "detail": _detail(nps=nps, method=method, product=coat["product"]),
                "document_id": report["document_id"], "page_no": report["page_no"],
            })
    return findings


@register("COAT-10", "Segment has welds but no coating report")
def segment_uncoated(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A welded segment with no coating record, where other segments have one.

    Every girth weld on a buried line is coated in the field — the mill
    coating stops short of the weld — so a segment with welds and no coating
    report has either lost its paperwork or never been coated.

    Reasoning from absence, so it waits until the reading is finished: one page
    of Bluewater's nineteen documents is enough to make sixteen segments look
    uncoated.
    """
    covered = {r["segment"] for r in _reports(db, project_id) if r["segment"]}
    if not covered or not every_report_read(db, project_id):
        return []

    findings: list[Finding] = []
    for row in db.q(
        """SELECT segment, COUNT(*) n FROM weld
           WHERE project_id=? AND segment<>'' GROUP BY segment ORDER BY segment""",
        (project_id,),
    ):
        if row["segment"] in covered:
            continue
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "COAT-10",
            "severity": "major", "segment": row["segment"],
            "subject": row["segment"],
            "message": (
                f"{row['n']} weld{'s' if row['n'] != 1 else ''} "
                f"{'are' if row['n'] != 1 else 'is'} recorded on this segment "
                f"and no coating report is filed against it, while other "
                f"segments on this job have one. Every field girth weld needs "
                f"coating over the bare band the mill coating leaves."
            ),
            "detail": _detail(welds=row["n"],
                              covered_segments=", ".join(sorted(covered))),
            "document_id": None, "page_no": None,
        })
    return findings
