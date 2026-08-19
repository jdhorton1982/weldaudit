"""Parsing and normalisation of NDE report / weld identifiers.

Field paperwork writes the same identifier a dozen ways.  A reader sheet is
filed as ``20IN LP 09.09.25 GFB-037-040.pdf`` while the daily weld report
records the very same shot as ``GFB-37``.  Everything downstream joins on the
normalised triple ``(prefix, number, suffix)`` produced here.

Suffixes carry meaning on this job:
    ``P``   procedure shot (the qualifying shot for a technique)
    ``R``   repair shot (a re-shoot after a rejected weld)
    ``CO``  cut out (the weld was removed rather than repaired)

They compose: ``GFFB-001PCO`` is the procedure shot for a weld that was later
cut out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Iterator

#: Suffixes whose meaning is known, longest first so ``PCO`` beats ``P`` and
#: ``CO`` beats a bare ``C``.
#: A single unrecognised letter is still accepted as a suffix: crews use
#: designators this list does not cover (AFB-16C on the Kestrel 8 weld maps), and
#: refusing to parse the id at all is worse than carrying a suffix we cannot
#: interpret - it drops a real weld's NDE reference on the floor.
_SUFFIX = r"PCO|RCO|CO|RR|P|R|[A-Z]"

# A single identifier: letters, a dash, digits, an optional suffix.
_ID = re.compile(rf"\b([A-Z]{{2,5}})-(\d{{1,4}})\s*({_SUFFIX})?\b", re.IGNORECASE)

# A range written as PREFIX-start(-|to)end.  Both conventions in this corpus
# are covered: the compact "GFB-037-040" / "FXR-001P-006", and the spelt-out
# "AXR-03 to AXR-15" where the prefix is repeated on the far end (sometimes
# without a space, as in "AFB-01P toAFB-05").
_RANGE = re.compile(
    rf"\b([A-Z]{{2,5}})-(\d{{1,4}})\s*({_SUFFIX})?\s*(?:-|–|to|thru|through)\s*"
    rf"(?:\1-)?(\d{{1,4}})\s*({_SUFFIX})?\b",
    re.IGNORECASE,
)

# A bare continuation that inherits the preceding prefix, after either a comma
# or a dash: "GFB-58-62,24CO" ends with GFB-24CO, and the "-19" left over from
# the list "GFB-4P-15P-19" is GFB-19.  Only unconsumed text is considered, so
# this never re-reads the interior of a range.
_BARE = re.compile(r"[,\-]\s*(\d{1,4})\s*(P|R|CO|RR)?\b", re.IGNORECASE)

VALID_SUFFIXES = {"", "P", "R", "RR", "CO", "PCO", "RCO"}


@dataclass(frozen=True, order=True)
class NdeId:
    """A normalised NDE report identifier."""

    prefix: str
    number: int
    suffix: str = ""

    def __str__(self) -> str:  # canonical rendering, zero padded to 3
        return f"{self.prefix}-{self.number:03d}{self.suffix}"

    @property
    def base(self) -> "NdeId":
        """The same shot without its suffix - used to tie GFB-45R back to GFB-45."""
        return NdeId(self.prefix, self.number, "")

    @property
    def is_procedure(self) -> bool:
        return self.suffix.startswith("P")

    @property
    def is_repair(self) -> bool:
        return self.suffix in ("R", "RR", "RCO")

    @property
    def is_cutout(self) -> bool:
        return self.suffix.endswith("CO")

    @property
    def borrows_its_number(self) -> bool:
        """Whether this shot reuses a weld's number instead of extending a run.

        Shots are numbered as they are taken, so an ordinary shot's number is
        one step along the sequence.  A repair or a cut-out is not: it is named
        for the weld it re-examines or removes, and could carry any number in
        the line.  Such a shot is still *on file* - the sheet exists - but it
        cannot be used to say how far the series ran.
        """
        return self.is_repair or self.is_cutout


def _mk(prefix: str, number: str | int, suffix: str | None) -> NdeId:
    return NdeId(prefix.upper(), int(number), (suffix or "").upper())


def parse_ids(text: str, *, max_span: int = 200) -> list[NdeId]:
    """Pull every NDE identifier out of a free-text string, expanding ranges.

    ``max_span`` guards against a mis-read range such as ``GFB-1-9999``
    silently generating ten thousand phantom welds.
    """
    if not text:
        return []

    found: list[NdeId] = []
    consumed: list[tuple[int, int]] = []
    last_prefix: str | None = None

    for m in _RANGE.finditer(text):
        prefix, start, start_sfx, end, end_sfx = m.groups()
        lo, hi = int(start), int(end)
        # "GFB-037-040" is a range; "GFB-58-62" likewise.  But a mis-parse like
        # a date fragment can invert or explode the span, so sanity-check it.
        if hi < lo or (hi - lo) > max_span:
            continue
        # Both endpoints carrying a suffix means a list, not a range:
        # "GFB-4P-15P-19" is shots 4P, 15P and 19, not shots 4 through 15.
        # A range only ever marks its opening shot, as in "FXR-001P-006".
        if start_sfx and end_sfx:
            found.append(_mk(prefix, lo, start_sfx))
            found.append(_mk(prefix, hi, end_sfx))
            consumed.append(m.span())
            last_prefix = prefix.upper()
            continue
        # Only the endpoints keep their suffix; the interior shots are plain.
        for n in range(lo, hi + 1):
            if n == lo:
                found.append(_mk(prefix, n, start_sfx))
            elif n == hi:
                found.append(_mk(prefix, n, end_sfx))
            else:
                found.append(_mk(prefix, n, None))
        consumed.append(m.span())
        last_prefix = prefix.upper()

    def overlaps(span: tuple[int, int]) -> bool:
        return any(span[0] < c[1] and c[0] < span[1] for c in consumed)

    for m in _ID.finditer(text):
        if overlaps(m.span()):
            continue
        prefix, number, sfx = m.groups()
        found.append(_mk(prefix, number, sfx))
        consumed.append(m.span())
        last_prefix = prefix.upper()

    # Comma continuations inherit the most recent prefix.
    if last_prefix:
        for m in _BARE.finditer(text):
            if overlaps(m.span()):
                continue
            number, sfx = m.groups()
            found.append(_mk(last_prefix, number, sfx))

    # Preserve first-seen order while removing duplicates.
    seen: set[NdeId] = set()
    ordered: list[NdeId] = []
    for i in found:
        if i not in seen:
            seen.add(i)
            ordered.append(i)
    return ordered


def parse_one(text: str) -> NdeId | None:
    """Parse a field expected to hold exactly one identifier (e.g. a DWR note)."""
    ids = parse_ids(text)
    return ids[0] if len(ids) >= 1 else None


def _runs(ids: Iterable[NdeId]) -> dict[str, tuple[set[int], set[int]]]:
    """Per prefix, ``(numbers on file, numbers that anchor the run)``.

    Two different questions, and conflating them is what made the gap rule
    misread cut-outs.  *On file* is every number with a sheet, whatever the
    suffix.  *Anchoring* is the subset that says how far the series ran, which
    excludes anything borrowing its number from the weld it replaced.

    The verdict is reached per number rather than per shot, because the same
    shot arrives from two places and only one of them says ``CO``.  The
    filename is ``GCFB-31 CO ,GCFB-37 CO PO FLEX STEEL.pdf`` while the sheet's
    own table prints a bare ``GCFB-31`` - so evidence of a cut-out has to beat
    a bare sighting of the same number, or the bare copy quietly re-anchors it.
    """
    on_file: dict[str, set[int]] = {}
    borrowed: dict[str, set[int]] = {}
    anchors: dict[str, set[int]] = {}
    for i in ids:
        on_file.setdefault(i.prefix, set()).add(i.number)
        target = borrowed if i.borrows_its_number else anchors
        target.setdefault(i.prefix, set()).add(i.number)
    for prefix, numbers in borrowed.items():
        if prefix in anchors:
            anchors[prefix] -= numbers
    return {p: (nums, anchors.get(p, set())) for p, nums in sorted(on_file.items())}


def gaps(ids: Iterable[NdeId]) -> list[NdeId]:
    """Missing numbers in each prefix's sequence.

    A pipeline's shots are numbered consecutively as they are taken, so a hole
    in the sequence means a reader sheet was never filed.

    The run is measured between the lowest and highest *anchoring* shot, and a
    number inside it counts as filed if any sheet at all bears it.  Both halves
    matter.  Anchoring only on ordinary shots is what keeps a cut-out series
    quiet: ``GCFB`` holds four sheets, numbered 31, 37, 39 and 114 after the
    welds they removed, and every one is a cut-out.  There is no run there to
    have a hole in - measuring 31 to 114 reported eighty missing sheets that
    were never taken.  Counting cut-outs as filed matters for the other
    direction: ``GFB-64CO`` is the only sheet bearing 64 in a series that runs
    to 129, and dropping it would invent a gap in the middle of a complete run.
    """
    missing: list[NdeId] = []
    for prefix, (on_file, anchors) in _runs(ids).items():
        # A series with no ordinary shot has no run; one with a single shot has
        # no interior.  Neither can be missing anything.
        if len(anchors) < 2:
            continue
        for n in range(min(anchors), max(anchors) + 1):
            if n not in on_file:
                missing.append(NdeId(prefix, n, ""))
    return missing


def cutout_series(ids: Iterable[NdeId]) -> set[str]:
    """Prefixes that exist only to carry cut-outs.

    On the Flexsteel spread a cut-out is filed under its own prefix - ``GFB``
    becomes ``GCFB``, ``GDFB`` or ``GFFB``, ``GTI`` becomes ``GDTI``, ``CXR``
    becomes ``DCXR`` - and every sheet ever filed under one of those carries a
    ``CO``.  They are not lines with missing paperwork; they are the removals
    from a line that is filed elsewhere in full.
    """
    return {prefix for prefix, (_on_file, anchors) in _runs(ids).items()
            if not anchors}


def sequences(ids: Iterable[NdeId]) -> dict[str, tuple[int, int, int]]:
    """Per-prefix ``(low, high, count)`` summary, for reporting.

    Bounded by the anchoring shots, like :func:`gaps`, but counting every sheet
    on file - so a series reads as complete when it is.
    """
    out: dict[str, tuple[int, int, int]] = {}
    for prefix, (on_file, anchors) in _runs(ids).items():
        span = anchors or on_file
        out[prefix] = (min(span), max(span), len(on_file))
    return out


def iter_bases(ids: Iterable[NdeId]) -> Iterator[NdeId]:
    for i in ids:
        yield i.base
