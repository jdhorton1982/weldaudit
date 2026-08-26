"""How far along the job is, and what that changes about the report.

A turnover audit and a preliminary one ask different questions of the same
package. At turnover everything is meant to be there, and a weld in no test
pack is a hole in the hand-over. Before the hydrotest, with construction still
running, that same weld is simply a weld whose pack has not been built yet --
and reporting it as an exception buries the findings that actually gate the
test. On one real job twenty-eight of sixty-four findings were of that kind.

**Nothing is suppressed.** The findings are still made, still stored and still
in the report; what changes is that they drop to ``info`` and say why they are
quiet. A rule that goes silent because of a setting somebody forgot to change
is a worse failure than a noisy report: the noise is visible and the silence
is not. The severity counts lead with what gates the test, and the rest is a
click away rather than gone.

**What stays loud.** Only the rules about assembly are softened. Anything that
would let an unqualified weld or an unapproved material into a line under
pressure keeps its severity whatever the stage: procedures, welder
certifications, material certificates, mistyped report numbers. Those are not
"late paperwork" before a hydrotest, they are the reason for the audit.
"""

from __future__ import annotations

#: The stage a project is audited at. ``turnover`` is the default, and is what
#: every project audited before this existed is treated as -- the stricter
#: reading, so an old project cannot quietly become lenient.
TURNOVER = "turnover"
PRELIMINARY = "preliminary"
STAGES: tuple[str, ...] = (PRELIMINARY, TURNOVER)

LABELS: dict[str, str] = {
    PRELIMINARY: "Preliminary - under construction",
    TURNOVER: "Turnover - package should be complete",
}

#: Rules that report the package being incomplete rather than wrong. Every one
#: of these is a normal condition of a job still being built: the test packs,
#: the as-built and the NDE records are assembled last.
#:
#: A rule belongs here only if a *correct* job in progress would trip it. WT-17
#: does not: a mistyped report number is wrong on the day it is typed, and no
#: amount of construction left to run makes it right.
ASSEMBLED_LAST: frozenset[str] = frozenset({
    "WT-13",    # a heat in the register that is on no weld yet
    "WT-19",    # a weld in the register that is stamped on no isometric yet
    "WT-21",    # a weld stamped on a drawing that is in no test pack yet
    "NDE-00",   # welds not yet carrying their NDE report numbers
})

#: What a softened finding is dropped to, and what it is dropped from. Nothing
#: is ever raised: a stage cannot make a finding worse than the rule made it.
SOFTENED = "info"


def stage_of(row) -> str:
    """The stage a project row records, defaulting to the stricter reading."""
    try:
        said = (row["stage"] or "").strip().lower()
    except (KeyError, IndexError, TypeError):
        return TURNOVER
    return said if said in STAGES else TURNOVER


def is_softened(rule: str, stage: str) -> bool:
    return stage == PRELIMINARY and rule in ASSEMBLED_LAST


def soften(findings: list[dict], stage: str) -> list[dict]:
    """Drop the assembly findings to ``info`` and say why, in place.

    Returns the same list so this reads as a step in the pipeline. The message
    is appended to rather than replaced: the finding still says exactly what it
    found, and the sentence after it says why it is not being counted against
    the job yet.
    """
    if stage != PRELIMINARY:
        return findings
    for f in findings:
        if f.get("rule") in ASSEMBLED_LAST:
            f["severity"] = SOFTENED
            f["message"] = (
                f"{f.get('message', '').rstrip()} "
                f"(This job is marked preliminary, so the parts of the package "
                f"assembled last are not yet late. It is recorded rather than "
                f"counted; mark the job as turnover to have it weighed.)"
            )
    return findings
