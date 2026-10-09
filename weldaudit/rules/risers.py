"""The riser checklist, and what it is supposed to capture.

GPPB-0105 requires one in the field package, item (h). The inspector fills it
in at post-hydro restoration and again at the system PSRR walk, and it is the
only document in the book that records where a riser physically stands and
what is bolted to it.

These rules read the form's own questions back. Each of them exists because
the template asks something and the answer can be left blank: a riser with no
coordinates cannot be found again, a valve with no torque date has no record
of having been made up, and an unsigned checklist is a form nobody stands
behind.

**"NA" is an answer and is treated as one.** A riser with no drain plug
answers NA to the drain plug question, which is not the same as leaving it
empty, and reporting it would teach an inspector to write Yes instead.
"""

from __future__ import annotations

from ..db import Database
from . import Finding, register

#: How many to name in a message before it becomes a wall.
_SHOWN = 6


def _detail(**kw) -> str:
    import json

    return json.dumps({k: v for k, v in kw.items() if v not in (None, "")})


def _answered(value: str) -> bool:
    """Whether the inspector wrote something, NA included."""
    return bool((value or "").strip())


@register("RSR-01", "Riser valve has no torque date")
def valve_not_dated(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A valve on a riser checklist with the "Date Torqued" column empty.

    The riser is the one place on the line where the flange log does not
    reach: its valves are made up in the field at restoration, after the
    torque log has been closed out. The checklist's date column is the only
    record that the joint was torqued at all, and on the package this was
    written against it was blank on all seventeen valves across all nine
    risers - the column has simply never been used.

    Reported once per riser rather than once per valve. An inspector filling
    in a form wants the form named, not each of its rows.
    """
    rows = db.q(
        """SELECT r.id, r.segment, r.description, r.document_id,
                  v.serial, v.valve_type, v.size
           FROM riser r JOIN riser_valve v ON v.riser_id = r.id
           WHERE r.project_id=? AND IFNULL(v.torqued_on,'')=''
           ORDER BY r.segment, r.description, v.row_no""",
        (project_id,),
    )
    if not rows:
        return []

    by_riser: dict[int, list] = {}
    for r in rows:
        by_riser.setdefault(r["id"], []).append(r)

    findings: list[Finding] = []
    for items in by_riser.values():
        first = items[0]
        n = len(items)
        named = ", ".join(i["serial"] or "(no serial)" for i in items[:_SHOWN])
        more = f", and {n - _SHOWN} more" if n > _SHOWN else ""
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "RSR-01",
            "severity": "major", "segment": first["segment"] or "",
            "subject": first["description"] or "Riser",
            "message": (
                f"{n} valve{'s' if n != 1 else ''} on the riser checklist for "
                f"{first['description'] or 'this riser'} "
                f"{'have' if n != 1 else 'has'} no date in the 'Date Torqued' "
                f"column: {named}{more}. The riser valves are made up in the "
                f"field after the torque log is closed, so this column is the "
                f"only record that they were torqued at all."
            ),
            "detail": _detail(riser=first["description"], valves=named),
            "document_id": first["document_id"], "page_no": None,
        })
    return findings


@register("RSR-02", "Riser checklist is not signed")
def not_signed(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A checklist with no inspector name or signature on it.

    The form is a statement by a named person that they walked the riser. An
    unsigned one records that somebody opened the template.
    """
    rows = db.q(
        """SELECT segment, description, document_id, inspector, signature
           FROM riser WHERE project_id=?
             AND IFNULL(inspector,'')='' AND IFNULL(signature,'')=''
           ORDER BY segment, description""",
        (project_id,),
    )
    return [{
        "project_id": project_id, "run_id": run_id, "rule": "RSR-02",
        "severity": "major", "segment": r["segment"] or "",
        "subject": r["description"] or "Riser",
        "message": (
            f"The riser checklist for {r['description'] or 'this riser'} "
            f"carries neither an inspector's printed name nor a signature. "
            f"The form is a statement that somebody walked the riser."
        ),
        "detail": _detail(riser=r["description"]),
        "document_id": r["document_id"], "page_no": None,
    } for r in rows]


@register("RSR-03", "Riser checklist records no coordinates")
def no_coordinates(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A checklist with the latitude and longitude boxes empty.

    The coordinates are why this form outlives the job. A riser is buried
    furniture with no station on the as-built; without a position, finding it
    again means walking the line.
    """
    rows = db.q(
        """SELECT segment, description, document_id
           FROM riser WHERE project_id=?
             AND (latitude IS NULL OR longitude IS NULL)
           ORDER BY segment, description""",
        (project_id,),
    )
    return [{
        "project_id": project_id, "run_id": run_id, "rule": "RSR-03",
        "severity": "major", "segment": r["segment"] or "",
        "subject": r["description"] or "Riser",
        "message": (
            f"The riser checklist for {r['description'] or 'this riser'} "
            f"records no latitude and longitude. The coordinates are the only "
            f"thing in the package that says where this riser is."
        ),
        "detail": _detail(riser=r["description"]),
        "document_id": r["document_id"], "page_no": None,
    } for r in rows]


@register("RSR-04", "Riser checklist lists no valves")
def no_valves(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A checklist whose valve table is empty.

    "List all valves on riser" with nothing under it. A riser with genuinely
    no valves is unusual enough to be worth confirming, and a table left
    empty because the walk was rushed looks exactly the same on paper.
    """
    rows = db.q(
        """SELECT r.segment, r.description, r.document_id
           FROM riser r
           WHERE r.project_id=?
             AND NOT EXISTS (SELECT 1 FROM riser_valve v WHERE v.riser_id = r.id)
           ORDER BY r.segment, r.description""",
        (project_id,),
    )
    return [{
        "project_id": project_id, "run_id": run_id, "rule": "RSR-04",
        "severity": "minor", "segment": r["segment"] or "",
        "subject": r["description"] or "Riser",
        "message": (
            f"The riser checklist for {r['description'] or 'this riser'} lists "
            f"no valves. Either the riser carries none, which is worth "
            f"confirming, or the table was not filled in."
        ),
        "detail": _detail(riser=r["description"]),
        "document_id": r["document_id"], "page_no": None,
    } for r in rows]


@register("RSR-05", "Riser verification box left empty")
def box_not_answered(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """The drain plug or photograph box, left blank rather than answered.

    Both are yes/no/NA questions the inspector answers at the riser. "NA" is
    a real answer - a riser with no drain plug has nothing to verify - and is
    accepted as one. Empty is not an answer.
    """
    findings: list[Finding] = []
    for r in db.q("""SELECT segment, description, document_id, drain_plugs, photo
                     FROM riser WHERE project_id=? ORDER BY segment, description""",
                  (project_id,)):
        blank = [name for name, value in (
            ("drain plugs verified", r["drain_plugs"]),
            ("picture of the riser taken", r["photo"]),
        ) if not _answered(value)]
        if not blank:
            continue
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "RSR-05",
            "severity": "minor", "segment": r["segment"] or "",
            "subject": r["description"] or "Riser",
            "message": (
                f"The riser checklist for {r['description'] or 'this riser'} "
                f"leaves {' and '.join(blank)} unanswered. NA is an answer; "
                f"blank is not."
            ),
            "detail": _detail(riser=r["description"], unanswered="; ".join(blank)),
            "document_id": r["document_id"], "page_no": None,
        })
    return findings
