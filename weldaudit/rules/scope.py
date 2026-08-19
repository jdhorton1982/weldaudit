"""Is the audit pointed at one job, or at a folder full of them?

Several rules reason across a whole audit - "no reader sheet for this report
exists *anywhere in this project*" - because crews deliberately file the same
sheet into every book that shares a spread.  That is right within one job and
wrong across several: point the tool at a folder containing five unrelated
jobs and one job's weld map gets checked against another's NDE package.

There is no reliable way to infer job boundaries from folder names, so this
does not try.  It detects the situation and says so, once.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from ..db import Database
from . import Finding, register

#: Kinds that indicate a job keeps its own weld map, and its own evidence.
_WELD_KINDS = ("daily_weld_report", "weld_log_csv", "weld_map_csv", "as_built")
_EVIDENCE_KINDS = ("nde_reader_sheet", "mtr")


@register("SCOPE-01", "Audit root contains several separate jobs")
def multiple_jobs(db: Database, project_id: int, run_id: str) -> list[Finding]:
    project = db.one("SELECT root FROM project WHERE id=?", (project_id,))
    if not project:
        return []
    root = Path(project["root"])

    rows = db.q(
        "SELECT path, kind, fingerprint FROM document WHERE project_id=? AND kind IN "
        f"({','.join('?' * (len(_WELD_KINDS) + len(_EVIDENCE_KINDS)))})",
        (project_id, *_WELD_KINDS, *_EVIDENCE_KINDS),
    )

    # Group by the immediate child of the audit root.
    by_branch: dict[str, set[str]] = defaultdict(set)
    evidence_branches: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        try:
            rel = Path(r["path"]).relative_to(root)
        except ValueError:
            continue
        branch = rel.parts[0] if len(rel.parts) > 1 else "(root)"
        by_branch[branch].add(r["kind"])
        if r["kind"] in _EVIDENCE_KINDS and r["fingerprint"]:
            evidence_branches[r["fingerprint"]].add(branch)

    # A self-contained job has both a weld map and its own evidence beneath it.
    jobs = sorted(
        b for b, kinds in by_branch.items()
        if any(k in kinds for k in _WELD_KINDS) and any(k in kinds for k in _EVIDENCE_KINDS)
    )
    if len(jobs) < 2:
        return []

    # One job's segment books share evidence: the same reader sheet is filed
    # into every book covering that spread. Separate jobs never do. Substantial
    # sharing therefore means these branches are segments, not jobs.
    if evidence_branches:
        shared = sum(1 for branches in evidence_branches.values() if len(branches) > 1)
        if shared / len(evidence_branches) > 0.10:
            return []

    return [
        {
            "project_id": project_id, "run_id": run_id, "rule": "SCOPE-01",
            "severity": "info", "segment": "(project)",
            "subject": f"{len(jobs)} jobs",
            "message": (
                f"This folder holds {len(jobs)} self-contained jobs "
                f"({', '.join(jobs[:5])}{'...' if len(jobs) > 5 else ''}). Checks that "
                f"look for evidence \"anywhere in the project\" are being run across all "
                f"of them at once, which reports gaps that do not exist and hides ones "
                f"that do. Audit each job folder separately for a result you can act on."
            ),
            "detail": f'{{"jobs": "{"; ".join(jobs[:20])}"}}',
            "document_id": None, "page_no": None,
        }
    ]
