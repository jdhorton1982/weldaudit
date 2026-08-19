"""Reading weld and heat callouts off an isometric's text layer.

Most of these drawings were plotted from CAD rather than scanned, so the
annotations survive as text and the weld register can be recovered without
sending a page to a model.  What does *not* survive is the layout: a callout
balloon becomes one or more text spans at known coordinates, and which spans
belong together has to be worked out from where they sit.

Three shapes appear in the corpus, and all three are handled here:

    AFB-19 ARO/ARV 8-01-25      one span - Kestrel 8
    AFB-10 / 6/4/26 / EM93      three spans stacked at the same x - GL 31
    AFB / 092                   the id itself split over two lines - GL 31

Parsing this with a general identifier regex does not work, and the two ways
it fails are worth naming because both produce welds that do not exist:

* run over a whole callout, ``AFB-19 ARO/ARV 8-01-25`` yields AFB-019 *and*
  AFB-001 and AFB-025, because the date's ``-01`` and ``-25`` read as bare
  continuations of the series;
* run over a title block, ``DTD22-LP-16-1A`` yields LP-016.

So the grammar here is anchored instead: a callout is a span whose *first*
token is an identifier and nothing else, and everything after that token is
read as welders and a date rather than as more identifiers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: A weld identifier as a callout writes it: a letter prefix, an optional
#: separator, a number, and an optional suffix.  Anchored at both ends - the
#: whole token has to be the identifier, which is what keeps line numbers and
#: dates out.
#: Four letters is the longest real prefix in the corpus (CAFB, GCFB, DCXR).
#: Allowing five let `ELBOW 90` become weld ELBOW-090.
_ID_TOKEN = re.compile(r"^([A-Z]{2,4})[-\s]?0*(\d{1,4})([A-Z]{0,2})$", re.IGNORECASE)

#: Prefixes that are really something else. Month abbreviations turn
#: `12-Mar-2025` into a weld; the service codes appear in line numbers.
_NOT_A_PREFIX = frozenset({
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "SEPT",
    "OCT", "NOV", "DEC", "SHT", "REV", "PG", "LP", "FG", "GL", "BD", "PO",
    "PW", "HP", "WI", "DWG", "ISO", "NPS", "SCH", "TYP", "EL", "BOP", "TOS",
    # Notes printed on the drawing read as callouts otherwise: "TO BE
    # INCLUDED IN THE 150 SERIES TEST" becomes weld THE-150.
    "THE", "AND", "FOR", "SEE", "PER", "ALL", "NOT", "USE", "NEW", "OLD",
    "SERIES", "PAGE", "NOTE", "TEST", "LINE", "TYPE", "SIZE", "AFE", "NO",
    # The job code, which prefixes every line number on Kestrel 8.
    "DTD", "DTDMP",
})

#: A weld map numbers its welds in one or two series — a single rig's prefix,
#: sometimes two where crews met.  Where the identifiers on a sheet are spread
#: much wider than that, the text is not a weld register: Bluewater's combined
#: map is a scan whose OCR yields sixteen "identifiers" across six prefixes,
#: none of them a real series.  Real maps in the corpus run 87-100%.
TOP_PREFIX_SHARE = 0.70

#: How many times an unfamiliar prefix has to appear on a job before it is
#: believed. A real series repeats; noise does not.
MIN_UNKNOWN_PREFIX = 3

#: A callout is terse. A span with more words than this is a note printed on
#: the drawing, and reading an identifier out of the middle of it invents a
#: weld — which is exactly how "TO BE INCLUDED IN THE 150 SERIES TEST" became
#: one before this cap existed.
MAX_CALLOUT_WORDS = 8

#: `HT: 651234` and `HT# 651234` - a heat callout on a heat map.
_HEAT = re.compile(r"^HT\s*[#:]?\s*(.+)$", re.IGNORECASE)

#: A valve figure number, which the draughtsmen write under an HT callout
#: because it is how a valve is identified — `HT: 2F-F33N-RF`, matching the
#: valve dossier `2F-F33N-RF BAYLOR 2IN 300 RF FP BV.pdf`. It is not a heat and
#: there will never be a mill certificate for it, so reporting one as
#: uncertified is a permanent false critical.
_VALVE_FIGURE = re.compile(
    r"^\d+(?:\.\d+)?[A-Z]{1,2}-[A-Z]\d{2}[A-Z](?:-[A-Z]{2})?$", re.IGNORECASE)

#: Welder stencils as the callouts write them: `ARO/ARV`, `AO28/AM53`, `EM93`.
_STENCILS = re.compile(r"^[A-Z]{2,4}\d{0,3}(?:\s*/\s*[A-Z]{2,4}\d{0,3})*$")

_DATE = re.compile(r"^\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}$")


@dataclass
class Callout:
    """One weld balloon: the identifier, and whatever was printed with it."""

    weld_id: str = ""
    prefix: str = ""
    number: int = 0
    suffix: str = ""
    welders: str = ""
    date: str = ""
    x: float = 0.0
    y: float = 0.0
    parts: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return self.weld_id


def parse_id_token(token: str, *, allow_short: frozenset[str] = frozenset()) -> tuple[str, int, str] | None:
    """``(prefix, number, suffix)`` if this token is a weld identifier alone.

    Two-letter prefixes are only accepted when the project's reader sheets
    already use them: `TI` and `FB` are real series on Bluewater, but accepting
    every two-letter prefix everywhere would let a stray `EL 8` elevation mark
    become a weld.
    """
    m = _ID_TOKEN.match((token or "").strip())
    if not m:
        return None
    prefix = m.group(1).upper()
    if prefix in _NOT_A_PREFIX:
        return None
    if len(prefix) < 3 and prefix not in allow_short:
        return None
    return prefix, int(m.group(2)), m.group(3).upper()


def format_id(prefix: str, number: int, suffix: str) -> str:
    """The project-wide spelling, matching what ``ids.py`` produces."""
    return f"{prefix}-{number:03d}{suffix}"


def is_concentrated(prefixes) -> bool:
    """Whether a sheet's identifiers cluster into one or two series."""
    from collections import Counter

    counts = Counter(prefixes)
    total = sum(counts.values())
    if not total:
        return False
    return sum(n for _p, n in counts.most_common(2)) / total >= TOP_PREFIX_SHARE


def parse_heat_token(text: str) -> str:
    """The heat number out of an `HT# 651234` callout, or ''.

    The separator between `HT` and the number is written every way there is —
    `HT: 1234`, `HT#1234`, `HT - 1234` — so it is stripped rather than
    matched, which is what stops a heat coming back as `-071B33`.
    """
    m = _HEAT.match((text or "").strip())
    if not m:
        return ""
    heat = m.group(1).strip().strip("#:-–—.").strip()
    if not heat or not any(c.isdigit() for c in heat):
        return ""
    if _VALVE_FIGURE.match(heat):
        return ""
    # `HT# 652583/3` is one heat with a plate suffix, not two heats.
    return heat


def _split_ids(tokens: list[str], *, allow_short: frozenset[str] = frozenset()
               ) -> tuple[list[tuple[str, int, str]], list[str]]:
    """Every identifier in the tokens, and whatever is left over."""
    ids: list[tuple[str, int, str]] = []
    rest: list[str] = []
    i = 0
    while i < len(tokens):
        if parsed := parse_id_token(tokens[i], allow_short=allow_short):
            ids.append(parsed)
            i += 1
            continue
        # An identifier split across two tokens: `AFB 094P`, `AFB 215`.
        if i + 1 < len(tokens):
            joined = parse_id_token(f"{tokens[i]}-{tokens[i + 1]}",
                                    allow_short=allow_short)
            if joined:
                ids.append(joined)
                i += 2
                continue
        rest.append(tokens[i])
        i += 1
    return ids, rest


def parse_callout(text: str, *, allow_short: frozenset[str] = frozenset()) -> Callout | None:
    """A whole one-span callout: identifier first, then welders and date.

    Everything after the identifier is classified by shape rather than
    position, because the two orders both occur — `AFB-19 ARO/ARV 8-01-25` and
    `ARV/AFM AFB-20 7/31/25`.
    """
    tokens = (text or "").split()
    if not tokens or len(tokens) > MAX_CALLOUT_WORDS:
        return None

    # Consume the identifiers before classifying anything else. A bare prefix
    # left in the pool matches the stencil pattern — `AFB 215 AFB 216` read
    # the second `AFB` as the welder, which then suppressed the second weld.
    ids, rest = _split_ids(tokens, allow_short=allow_short)
    if not ids:
        return None

    prefix, number, suffix = ids[0]
    welders = [t for t in rest if _STENCILS.match(t.upper())]
    dates = [t for t in rest if _DATE.match(t)]

    return Callout(
        weld_id=format_id(prefix, number, suffix), prefix=prefix, number=number,
        suffix=suffix,
        # Only one welder group belongs to a callout. More than one means the
        # spans have run together and the pairing is not safe to guess.
        welders=welders[0].upper().replace(" ", "") if len(welders) == 1 else "",
        date=dates[0] if len(dates) == 1 else "",
        parts=[text],
    )


#: How far two spans can sit apart and still belong to the same balloon.
#: Deliberately applied to both axes rather than to lines beneath: these
#: drawings are plotted at whatever rotation suited the sheet, so what reads
#: as three stacked lines is three spans side by side in PDF coordinates on
#: Kestrel 8 and three spans stacked on GL 31.  Balloons themselves sit more than
#: a hundred points apart, so there is a wide margin either way.
CALLOUT_GAP = 14.0


def _gap(a: tuple[float, float, float, float],
         b: tuple[float, float, float, float]) -> tuple[float, float]:
    """The horizontal and vertical space between two bounding boxes."""
    return (max(0.0, max(a[0] - b[2], b[0] - a[2])),
            max(0.0, max(a[1] - b[3], b[1] - a[3])))


def cluster_spans(spans: list[tuple[float, float, float, float, str]],
                  gap: float = CALLOUT_GAP) -> list[list[str]]:
    """Group spans that touch into one callout each, by single linkage.

    Returns each cluster's texts in reading order — left to right, top to
    bottom — which is enough for ``parse_callout`` to sort out, because it
    classifies the parts by shape rather than by position.
    """
    n = len(spans)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    # The spans on one sheet number in the hundreds, so the quadratic pass is
    # not worth indexing away.
    for i in range(n):
        for j in range(i + 1, n):
            dx, dy = _gap(spans[i][:4], spans[j][:4])
            if dx <= gap and dy <= gap:
                parent[find(i)] = find(j)

    groups: dict[int, list[tuple[float, float, str]]] = {}
    for i, span in enumerate(spans):
        groups.setdefault(find(i), []).append((span[1], span[0], span[4]))
    return [[t for _y, _x, t in sorted(v)] for v in groups.values()]


def all_ids(tokens: list[str], *, allow_short: frozenset[str] = frozenset()) -> list[str]:
    """Every identifier in a token list, including ones split over two tokens."""
    ids, _rest = _split_ids(tokens, allow_short=allow_short)
    return [format_id(*i) for i in ids]


def group_spans(spans: list[tuple[float, float, float, float, str]], *,
                allow_short: frozenset[str] = frozenset()) -> list[Callout]:
    """Every callout on a sheet, from its positioned spans.

    A cluster usually holds one balloon, but on a crowded sheet two balloons
    sit close enough to link.  Where that happens and the cluster carries no
    welders or date, every identifier in it is emitted: they are all really
    on the drawing, and with nothing to pair them to there is nothing to get
    wrong.  Where the cluster *does* carry a welder or a date, only the first
    identifier is taken — guessing which of two welds a date belongs to is
    exactly the mistake that puts the wrong crew on a joint.
    """
    out: list[Callout] = []
    for texts in cluster_spans(spans):
        callout = parse_callout(" ".join(texts), allow_short=allow_short)
        if not callout:
            continue
        callout.parts = texts
        out.append(callout)

        if callout.welders or callout.date:
            continue
        for extra in all_ids(" ".join(texts).split(), allow_short=allow_short):
            if extra == callout.weld_id:
                continue
            out.append(Callout(weld_id=extra, prefix=extra.split("-")[0],
                               parts=texts))
    return out
