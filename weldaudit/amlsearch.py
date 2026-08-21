"""Look a manufacturer up on the approved list, from the app.

The audit already answers this question a thousand times a run, but only ever
about names it read off a certificate, and only in the past tense: a finding
says a mill was not on the list, and the auditor's next move is to go and look
for themselves. Today that means opening a 57-page PDF and reading down it.

So the same matcher the rules use is exposed directly. Two answers come back
for one query, because there are two different questions hiding in "is this
mill approved":

**The verdict** is what ``match`` would say — the audit's own answer, with the
score and the reason it settled on. That is the one that explains a finding.
It is deliberately the *same call* the rules make rather than a second
implementation, so what the search box says and what the report says cannot
drift apart.

**The rows** are a plain text search over the list. Substring, not fuzzy: a
verdict of "not listed" is worth very little unless you can also see for
yourself what the list does hold near that name, and a fuzzy browse would just
be the verdict again with extra steps.

An optional NPS answers the other half of an approval. A mill cleared "Up to
NPS 20" supplying 24" pipe passes on name and fails on size, and the name is
the half people check.
"""

from __future__ import annotations

from .aml import Aml, AmlEntry, parse_nps


def _flag(e: AmlEntry) -> str:
    """The one caveat worth showing in a list, worst first."""
    if e.superseded:
        return "superseded"
    if e.on_hold:
        return "hold"
    if e.provisional:
        return "provisional"
    return ""


def _row(e: AmlEntry, matched: bool, nps: float | None) -> dict:
    size = ""
    if nps is not None and e.size_limit is not None:
        size = "allows" if e.size_limit.allows(nps) else "excludes"
    return {
        "category": e.category,
        "manufacturer": e.manufacturer,
        "location": e.location,
        "limits": e.limits_raw,
        "conditions": e.conditions,
        "flag": _flag(e),
        "matched": matched,
        "size": size,
    }


def categories(aml: Aml) -> list[str]:
    """Every sheet in the list, in the list's own order."""
    seen: list[str] = []
    for e in aml.entries:
        if e.category not in seen:
            seen.append(e.category)
    return seen


def search(aml: Aml, query: str, nps: str | float | None = None,
           category: str = "", limit: int = 200) -> dict:
    """Answer one lookup. See the module docstring for the two halves."""
    query = (query or "").strip()
    want = parse_nps(nps) if nps not in (None, "") else None
    rows: list[dict] = []
    verdict: dict | None = None
    seen: set[int] = set()
    # Identical rows are collapsed. 40% of the issued list is duplicates,
    # because each category is sub-divided by a heading the parser does not
    # keep — a valve sheet runs "Top Entry", then "Two Piece", then "Three
    # Piece", and a manufacturer in all of them lands twelve times. Nothing is
    # hidden by collapsing: once the sub-heading is gone the copies differ in
    # nothing, and twelve identical lines only bury the entries around them.
    shown: set[tuple] = set()

    def add(entries, matched: bool) -> None:
        for e in entries:
            if id(e) in seen:
                continue
            if category and e.category != category:
                continue
            seen.add(id(e))
            same = (e.category, e.manufacturer, e.location, e.limits_raw)
            if same in shown:
                continue
            shown.add(same)
            rows.append(_row(e, matched, want))

    if query:
        # The audit's own answer, so the box and the report cannot disagree.
        result = aml.match(query)
        # A verdict of "not listed" still carries an entry — the nearest name
        # the matcher could find. That is worth showing as "the closest thing
        # on the list is X", and it is emphatically not a match: highlighting
        # it, or floating it above the real text hits, would put an unrelated
        # company under a heading saying this one is approved.
        found = result.status != "not_listed"
        verdict = {
            "status": result.status,
            "score": result.score,
            "reason": result.reason,
            "names": sorted({e.manufacturer for e in result.entries}) if found else [],
            "nearest": result.entries[0].manufacturer
                       if not found and result.entries else "",
        }
        if want is not None and found:
            allowing, forbidding = aml.check_size(result.entries, want)
            # Named on the list but not for this size is its own answer, and
            # the one an approval check exists to catch.
            if forbidding and not allowing and result.status == "approved":
                verdict["status"] = "size"
                verdict["reason"] = "on the AML, but not for this size"
        if found:
            add(result.entries, True)

        needle = query.lower()
        add((e for e in aml.entries
             if needle in e.manufacturer.lower()
             or needle in e.location.lower()
             or needle in e.limits_raw.lower()), False)
    else:
        add(aml.entries, False)

    return {
        "verdict": verdict,
        "rows": rows[:limit],
        "shown": min(len(rows), limit),
        "total": len(rows),
        "nps": want,
    }
