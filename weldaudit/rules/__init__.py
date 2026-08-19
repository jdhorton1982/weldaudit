"""Audit rules.

A rule is a callable ``(db, project_id, run_id) -> list[finding]``.  Rules read
only from the extracted tables, never from disk, so a rule change can be
re-run in seconds without touching the documents again.
"""

from __future__ import annotations

from typing import Callable

from ..db import Database

Finding = dict
Rule = Callable[[Database, int, str], list[Finding]]

_REGISTRY: dict[str, tuple[str, Rule]] = {}


def register(code: str, title: str) -> Callable[[Rule], Rule]:
    def deco(fn: Rule) -> Rule:
        _REGISTRY[code] = (title, fn)
        return fn
    return deco


def registry() -> dict[str, tuple[str, Rule]]:
    return dict(_REGISTRY)


def run_all(db: Database, project_id: int, run_id: str, only: list[str] | None = None) -> list[Finding]:
    findings: list[Finding] = []
    for code, (_title, fn) in _REGISTRY.items():
        if only and code not in only:
            continue
        findings.extend(fn(db, project_id, run_id))
    return findings


SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2, "info": 3}


def sort_findings(findings: list[Finding]) -> list[Finding]:
    """Severity order, except that scope warnings always come first.

    A SCOPE finding says the rest of the list may be unreliable, so it has to
    be read before the criticals it is qualifying - burying it at the bottom
    with the other info items would defeat the point.
    """
    return sorted(
        findings,
        key=lambda f: (
            0 if (f.get("rule") or "").startswith("SCOPE") else 1,
            SEVERITY_ORDER.get(f.get("severity", "info"), 9),
            f.get("segment") or "",
            f.get("rule") or "",
            f.get("subject") or "",
        ),
    )


from . import (  # noqa: E402,F401  (importing registers the rules)
    asbuilt, backfill, coating, flanges, hydrotest, materials, nde_coverage,
    ndetech, registers, review, roster, scope, welders, wps,
)

__all__ = ["register", "registry", "run_all", "sort_findings", "Finding", "Rule"]
