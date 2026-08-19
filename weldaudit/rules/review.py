"""Pages a person has to look at, because the machine could not settle them.

Every other rule in this package reports something wrong with the work. This
one reports something wrong with the *evidence*: a value that was read twice
and came back two different ways, on a field the audit depends on.

That distinction matters for how the finding reads. "The heat number could not
be read" is not an accusation about the pipe; it is a statement that this
particular cross-check did not happen, and that nobody would otherwise know it
had not. Left unsaid, the null looks exactly like an empty box.
"""

from __future__ import annotations

import json

from ..db import Database
from . import Finding, register

#: Pages listed in the message before it starts saying "and N more". Enough to
#: go and look, short enough to read.
_PAGES_SHOWN = 6


def _readings(raw: str | None) -> list[str]:
    try:
        return [str(v) for v in json.loads(raw or "[]")]
    except (TypeError, ValueError):
        return []


@register("VIS-01", "Scanned value could not be read the same way twice")
def unsettled_readings(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A decisive field where overlapping close-ups of one box disagreed.

    Reading a page as four overlapping quarters means most values are seen
    twice. Usually the two agree, or a majority settles it. When they cannot be
    reconciled the merge deliberately stores nothing rather than picking a
    winner — the alternative is a coin toss recorded as fact. This surfaces
    those, so the gap is closed by someone who can open the PDF.
    """
    rows = db.q(
        """SELECT filename, segment, kind, field, page_no, readings, document_id
           FROM vision_conflict
           WHERE project_id=? AND decisive=1
           ORDER BY filename, field, page_no""",
        (project_id,),
    )

    # Grouped by document and field: a sixteen-page bundle whose ticket number
    # is unreadable is one thing to go and check, not sixteen.
    grouped: dict[tuple[str, str, str, str], list] = {}
    for r in rows:
        key = (r["filename"], r["segment"] or "", r["kind"], r["field"])
        grouped.setdefault(key, []).append(r)

    findings: list[Finding] = []
    for (filename, segment, kind, field_name), group in grouped.items():
        pages = [str(r["page_no"]) for r in group]
        shown = ", ".join(pages[:_PAGES_SHOWN])
        if len(pages) > _PAGES_SHOWN:
            shown += f" and {len(pages) - _PAGES_SHOWN} more"

        variants: list[str] = []
        for r in group:
            for value in _readings(r["readings"]):
                if value not in variants:
                    variants.append(value)
        as_read = " or ".join(f"'{v}'" for v in variants[:4]) or "differently"

        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "VIS-01",
            "severity": "major", "segment": segment,
            "subject": f"{filename} {field_name}",
            # Every finding that says "open the page" has to say which page.
            # The reports turn this into a clickable path; without it the
            # instruction is unfollowable.
            "document_id": group[0]["document_id"],
            "page_no": group[0]["page_no"],
            "message": (
                f"On {filename} (page {shown}), the {field_name.replace('_', ' ')} "
                f"was read as {as_read} on separate close-ups of the same box, "
                f"so nothing was recorded for it. This is a {kind.replace('_', ' ')} "
                f"field the audit cross-checks, so the check did not run rather "
                f"than failing — open the page and enter the value."
            ),
        })
    return findings


# ---------------------------------------------------------------------------
# Manufacturer names that came off a scan
# ---------------------------------------------------------------------------
#
# A manufacturer read by a model is not the same evidence as one exported from
# a purchase system, and the AML lookup it feeds is deliberately fuzzy. Those
# two tolerances multiply: a misread name lands a short edit-distance from a
# real entry and is approved on its own merits.
#
# Measured on Bluewater 14. Three scans of one Kandal letterhead read as
# 'Kandal Pipe USA, Inc.', 'Modal Pipe USA, Inc' and 'Besteel Pipe USA, Inc'.
# The first two are not on that AML and are reported as such. The third scores
# 72 against Tiancheng Steel Pipe — a different mill on another continent — and
# turns a "not listed" into a "confirm". 'BQN Forgings Private Limited', one
# letter out from BQN, is approved outright at 91.
#
# The AML's own thresholds cannot catch this, because they measure how alike
# two strings are and the strings really are alike. What distinguishes these
# cases is where the name came from, which only this side knows.

#: Below this, an approval resting on a scanned name is not evidence enough.
#: An exact match scores 100 and a leading-word prefix 95; the band underneath
#: is where "close enough for a person who knows the trade name" meets "close
#: enough because a scanner blurred a letter", and those are not the same
#: thing when nobody looked at the page.
SCANNED_NAME_NEEDS_EYES = 95


@register("VIS-02", "Manufacturer name was read inconsistently off the page")
def disputed_manufacturer_name(db: Database, project_id: int,
                               run_id: str) -> list[Finding]:
    """Close-ups of one letterhead that did not read the same way.

    Unlike VIS-01 these were settled — a majority agreed, or the whole-page
    reading broke the tie — so a name *was* recorded. It is reported anyway
    because the thing in doubt is a company name feeding an approval, and a
    letterhead legible enough to read two ways is not legible.
    """
    rows = db.q(
        """SELECT vc.filename, vc.segment, vc.field, vc.readings, vc.chosen,
                  vc.document_id, m.heat, m.manufacturer
           FROM vision_conflict vc
           JOIN material m ON m.document_id = vc.document_id
                          AND m.project_id = vc.project_id
           WHERE vc.project_id=? AND vc.chosen IS NOT NULL
             AND vc.field IN ('issuing_company', 'mill_name')""",
        (project_id,),
    )
    findings: list[Finding] = []
    seen: set[tuple] = set()
    for r in rows:
        key = (r["filename"], r["field"])
        if key in seen:
            continue
        seen.add(key)
        variants = _readings(r["readings"])
        if len(set(variants)) < 2:
            continue
        as_read = " or ".join(f"'{v}'" for v in variants[:4])
        heat = f"heat {r['heat']}" if r["heat"] else "the material on it"
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "VIS-02",
            "severity": "major", "segment": r["segment"] or "",
            "subject": f"{r['filename']} {r['field']}",
            "document_id": r["document_id"],
            "message": (
                f"On {r['filename']}, separate close-ups of the letterhead read "
                f"the {r['field'].replace('_', ' ')} as {as_read}. "
                f"'{r['chosen']}' was recorded and {heat} is credited to it, but "
                f"a name that reads two ways cannot settle whether the material "
                f"is from an approved manufacturer, so no approval check was "
                f"made against it. Read the letterhead and enter the company."
            ),
        })
    return findings


@register("VIS-03", "Scanned manufacturer approved on an approximate name match")
def scanned_name_approved_loosely(db: Database, project_id: int,
                                  run_id: str) -> list[Finding]:
    """An AML approval that rests on a fuzzy match of an OCR'd company name.

    The approval is not overturned here — this rule cannot tell a misread from
    a trade name — but it stops the material passing silently. A wrong
    manufacturer that the AML approves is the one failure of this tool that
    leaves no trace anywhere for anyone to notice.
    """
    from ..rules.materials import _aml_from_db, _categories

    aml = _aml_from_db(db, project_id)
    if aml is None:
        return []

    findings: list[Finding] = []
    for r in db.q(
        """SELECT * FROM material
           WHERE project_id=? AND IFNULL(manufacturer,'')<>''
             AND confidence='vision'""",
        (project_id,),
    ):
        result = aml.match(r["manufacturer"], _categories(r))
        if result.status != "approved" or result.score >= SCANNED_NAME_NEEDS_EYES:
            continue
        entry = result.entries[0].manufacturer if result.entries else "an AML entry"
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "VIS-03",
            "severity": "major", "segment": r["segment"] or "",
            "subject": r["manufacturer"],
            "document_id": r["document_id"],
            "message": (
                f"'{r['manufacturer']}' was read off a scanned certificate for "
                f"heat {r['heat'] or '(unknown)'} and approved by matching "
                f"'{entry}' at {result.score}%. Both tolerances are in play at "
                f"once here — the name came from a model reading small print, "
                f"and the lookup that cleared it allows for spelling. Confirm "
                f"the letterhead before relying on this approval."
            ),
        })
    return findings
