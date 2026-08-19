"""What a welder qualification test actually qualifies the welder to do.

Deriving qualification ranges from a test coupon is genuinely hard — API 1104
and ASME IX each have their own tables for diameter groups, thickness limits,
position coverage and filler-metal groupings, and getting one wrong would
produce confident, wrong findings about people's tickets.

This module does not derive them.  XTO's own Welder Performance Qualification
form carries a **Qualification Ranges** block that the certifying CWI filled in
at test time — process, position, progression, pipe diameter, P/S numbers, F
number.  That is an authoritative statement by a qualified person, and it is
what gets read and compared.  Everything here is therefore parsing and
comparison, not code-rule inference.

Where a range cannot be interpreted unambiguously, the answer is "unknown", and
the rules stay silent rather than guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .aml import parse_nps

# ---------------------------------------------------------------------------
# Welding processes
# ---------------------------------------------------------------------------

#: Canonical process names and the spellings seen on these forms and reports.
_PROCESS_ALIASES: dict[str, str] = {
    "SMAW": "SMAW", "STICK": "SMAW", "MMA": "SMAW",
    "GTAW": "GTAW", "TIG": "GTAW",
    "GMAW": "GMAW", "MIG": "GMAW", "STT": "GMAW", "RMD": "GMAW",
    "FCAW": "FCAW", "FLUXCORE": "FCAW", "FCAWG": "FCAW", "FCAWS": "FCAW",
    "SAW": "SAW",
}

#: Tokens that appear in a weld report's PROCESS column but name a joint type
#: or an NDE method rather than a welding process.  The column is used
#: inconsistently across crews, so a value being present does not make it a
#: process — treating "BW" as one would flag every butt weld on the job.
_NOT_A_PROCESS = {
    "BW", "SW", "FW", "ML", "TI", "TIE-IN", "TIEIN", "FAB", "FB", "XR", "PT",
    "MT", "UT", "RT", "VT", "FILLET", "BUTT", "SOCKET", "N/A", "NA", "-",
}


def parse_processes(text: str | None) -> set[str]:
    """Canonical welding processes named in a free-text field.

    Returns an empty set when the text names none — which is different from
    naming an unrecognised one, and is why callers check emptiness rather than
    assuming a process was recorded.
    """
    out: set[str] = set()
    for token in re.split(r"[^A-Za-z]+", (text or "").upper()):
        if not token or token in _NOT_A_PROCESS:
            continue
        if canonical := _PROCESS_ALIASES.get(token):
            out.add(canonical)
    return out


def is_process_field(text: str | None) -> bool:
    """Whether a weld report's PROCESS cell actually names a welding process."""
    return bool(parse_processes(text))


# ---------------------------------------------------------------------------
# Diameter ranges
# ---------------------------------------------------------------------------

_UNLIMITED = re.compile(r"\b(all|any|unlimited|n/?a)\b", re.IGNORECASE)
_AND_ABOVE = re.compile(
    r"([\d./\s]+?)\s*(?:\"|in\b|inch)?\s*(?:and|or)\s+(?:above|larger|greater|up)",
    re.IGNORECASE)
_AND_BELOW = re.compile(
    r"([\d./\s]+?)\s*(?:\"|in\b|inch)?\s*(?:and|or)\s+(?:below|smaller|less|under)",
    re.IGNORECASE)
_RANGE = re.compile(
    r"([\d./]+)\s*(?:\"|in\b|inch)?\s*(?:to|thru|through|-|–)\s*([\d./]+)",
    re.IGNORECASE)
_UP_TO = re.compile(r"\bup to\s*([\d./]+)", re.IGNORECASE)


@dataclass(frozen=True)
class DiameterRange:
    """A qualified pipe-diameter range, in NPS inches."""

    minimum: float | None = None
    maximum: float | None = None
    unlimited: bool = False
    understood: bool = True

    def allows(self, nps: float) -> bool:
        if not self.understood or self.unlimited:
            return True
        if self.minimum is not None and nps < self.minimum - 1e-9:
            return False
        if self.maximum is not None and nps > self.maximum + 1e-9:
            return False
        return True

    def describe(self) -> str:
        if not self.understood:
            return "an unclear range"
        if self.unlimited:
            return "all diameters"
        if self.minimum is not None and self.maximum is not None:
            return f"{_n(self.minimum)}\" to {_n(self.maximum)}\""
        if self.minimum is not None:
            return f"{_n(self.minimum)}\" and larger"
        if self.maximum is not None:
            return f"up to {_n(self.maximum)}\""
        return "an unstated range"


def _n(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


def parse_diameter_range(text: str | None) -> DiameterRange:
    """Read the form's *Qualification Ranges → Pipe Diameter* value.

    A bare number is deliberately treated as *not understood*.  On an API 1104
    form it usually implies a diameter group rather than an exact size, and
    resolving which group is exactly the code inference this module refuses to
    do — a wrong reading here would disqualify a welder who is in fact
    qualified.
    """
    raw = (text or "").strip()
    if not raw:
        return DiameterRange(understood=False)
    if _UNLIMITED.search(raw):
        return DiameterRange(unlimited=True)
    if m := _RANGE.search(raw):
        lo, hi = parse_nps(m.group(1)), parse_nps(m.group(2))
        if lo is not None and hi is not None and lo <= hi:
            return DiameterRange(minimum=lo, maximum=hi)
    if m := _AND_ABOVE.search(raw):
        if (lo := parse_nps(m.group(1))) is not None:
            return DiameterRange(minimum=lo)
    if m := _AND_BELOW.search(raw):
        if (hi := parse_nps(m.group(1))) is not None:
            return DiameterRange(maximum=hi)
    if m := _UP_TO.search(raw):
        if (hi := parse_nps(m.group(1))) is not None:
            return DiameterRange(maximum=hi)
    return DiameterRange(understood=False)


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

#: Pipe and plate test positions, and what each covers for pipe groove welds.
#: 6G is the all-position test; 5G covers everything except horizontal-fixed.
_POSITION_COVERAGE: dict[str, set[str]] = {
    "1G": {"1G"},
    "2G": {"1G", "2G"},
    "5G": {"1G", "3G", "4G", "5G"},
    "6G": {"1G", "2G", "3G", "4G", "5G", "6G"},
    "6GR": {"1G", "2G", "3G", "4G", "5G", "6G", "6GR"},
}

_POSITION = re.compile(r"\b([1-6]G R?|[1-6]GR?)\b", re.IGNORECASE)


def parse_positions(text: str | None) -> tuple[set[str], bool]:
    """Positions named in a field.  Returns ``(positions, is_all)``."""
    raw = (text or "").strip()
    if not raw:
        return set(), False
    if _UNLIMITED.search(raw):
        return set(), True
    found = {m.group(1).upper().replace(" ", "") for m in _POSITION.finditer(raw)}
    return found, False


def positions_covered(tested: str | None) -> set[str]:
    """Everything a given test position qualifies, per the standard coverage."""
    positions, is_all = parse_positions(tested)
    if is_all:
        return set(_POSITION_COVERAGE["6GR"])
    covered: set[str] = set()
    for p in positions:
        covered |= _POSITION_COVERAGE.get(p, {p})
    return covered


# ---------------------------------------------------------------------------
# Welding procedure specifications
# ---------------------------------------------------------------------------

def normalise_wps(text: str | None) -> str:
    """Canonical form of a WPS reference, for joining across sources.

    The same procedure is written ``XTO-X60-6010/8010 Rev.1`` on a weld log and
    ``XTO-X60-6010-8010 Rev.1`` in a certificate filename, because a slash is
    not legal in a filename.
    """
    text = (text or "").upper()
    text = re.sub(r"\bREV\.?\s*", "REV", text)
    return re.sub(r"[^A-Z0-9]", "", text)
