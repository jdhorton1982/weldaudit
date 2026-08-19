"""Welder stencils: reading them, and reading certification filenames.

A weld report records who welded each pass in four columns - root, hot pass,
fill, cap - and a pass is often shared, so a cell holds more than one stencil::

    AEA            one welder
    ARB/AMG        two welders on that pass
    ANR-AMG        the same, dash separated
    AM53, OM64     the digital projects use letter-digit stencils
    ADP-1          and some crews suffix a number

Not everything in those columns is a welder.  Where a weld report has no NOTES
column the NDE report drifts into the cap column instead, so ``GXR-89`` shows
up looking like a stencil.  Those are recognised and handed back separately
rather than being counted as an uncertified welder.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

#: A welder stencil: two to four letters, optionally with trailing digits, and
#: optionally a "-1" style suffix.  AEA, ABF, AM53, OM64, ADP-1.
_STENCIL = re.compile(r"^[A-Z]{2,4}\d{0,2}(-\d{1,2})?$")

#: An NDE report id that has drifted into a welder column.
_NDE_ID = re.compile(r"^[A-Z]{2,5}-\d{1,4}(P|R|RR|CO)?$", re.IGNORECASE)

#: Values that mean "nothing recorded".
_BLANK = {"", "-", "--", "N/A", "NA", "NONE", "N\\A", "TBD", "X"}

#: Separators between two welders sharing a pass.
_SPLIT = re.compile(r"\s*(?:[/,;+&]|\band\b)\s*", re.IGNORECASE)


@dataclass
class WelderField:
    """What a single root/hp/fill/cap cell actually contained."""

    stencils: list[str] = field(default_factory=list)
    nde_ids: list[str] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)


def parse_field(text: str, nde_prefixes: set[str] | None = None) -> WelderField:
    """Split one welder cell into stencils, stray NDE ids and leftovers.

    ``ADP-1`` and ``GXR-89`` are the same shape, and only the project's own
    data can tell them apart: pass the NDE series that actually exist on this
    job and anything else with that shape is treated as a welder stencil.
    """
    out = WelderField()
    raw = (text or "").strip()
    if raw.upper() in _BLANK:
        return out

    for part in _SPLIT.split(raw):
        token = part.strip().upper().rstrip(".")
        if not token or token in _BLANK:
            continue
        if m := _NDE_ID.match(token):
            prefix = token.split("-")[0]
            if nde_prefixes is None or prefix in nde_prefixes:
                out.nde_ids.append(token)
                continue
        if _STENCIL.match(token):
            out.stencils.append(token)
            continue
        # "ANR-AMG" is a dash-separated pair of stencils, but "ADP-1" is one
        # stencil with a numeric suffix - the dash only splits when both sides
        # are themselves stencils.
        if "-" in token:
            halves = [h.strip() for h in token.split("-")]
            if len(halves) == 2 and all(_STENCIL.match(h) and h.isalpha() for h in halves):
                out.stencils.extend(halves)
                continue
        out.unparsed.append(token)
    return out


def stencils_of(*fields: str, nde_prefixes: set[str] | None = None) -> WelderField:
    """Combine several welder cells, keeping each stencil once."""
    merged = WelderField()
    seen: set[str] = set()
    for text in fields:
        part = parse_field(text, nde_prefixes)
        for s in part.stencils:
            if s not in seen:
                seen.add(s)
                merged.stencils.append(s)
        merged.nde_ids.extend(part.nde_ids)
        merged.unparsed.extend(part.unparsed)
    return merged


# ---------------------------------------------------------------------------
# Certification filenames
# ---------------------------------------------------------------------------

_PROCESS = re.compile(r"\b(GTAW|SMAW|GMAW|FCAW|SAW|TIG|MIG|STT|RMD)\b", re.IGNORECASE)
_MATERIAL = re.compile(r"\b(SS|CS|DSS|316L?|304L?|CRA)\b", re.IGNORECASE)
#: No \b here - these filenames glue words together with underscores, and an
#: underscore is a word character, so "_REQUAL" has no boundary before it.
_REQUAL = re.compile(r"REQUAL|RE-?QUALIF", re.IGNORECASE)

#: Dates as written on cert filenames: 4-8-25, 05.06.2025, 050224, 10.22.26.
#: The run form is likewise underscore-delimited, so digit lookarounds are used
#: rather than word boundaries.
_DATE_SEP = re.compile(r"(?<!\d)(\d{1,2})[.\-_](\d{1,2})[.\-_](\d{2,4})(?!\d)")
_DATE_RUN = re.compile(r"(?<!\d)(\d{2})(\d{2})(\d{2})(?!\d)")
_EXPIRY = re.compile(r"\bexp\.?\s*[:.]?\s*(\d{1,2})[.\-_/](\d{1,2})[.\-_/](\d{2,4})", re.IGNORECASE)

#: Noise tokens that are never a stencil or a name.
_CERT_NOISE = re.compile(
    r"^(XTO|SEC|IX|REQUAL|WPS|WPQ|API|1104|ASME|B31\.?3|CERT|CERTS?|"
    r"CONTINUITY|WELDER|QUALIFICATION|TEST|COUPON|COPY|SCAN|PDF)$", re.IGNORECASE
)


#: The scoped-cert convention: "Javier Vazquez_XTO-X60-6010-8010 Rev.1_042425_ARS.pdf".
#: A welder holds one certificate per procedure, so the WPS in the name is the
#: scope of that ticket - not decoration.
_SCOPED_CERT = re.compile(
    r"^(?P<name>[A-Za-z][A-Za-z .'-]+?)_(?P<wps>[A-Z0-9][A-Za-z0-9\-. ]*?)"
    r"_(?P<date>\d{6})_(?P<stencil>[A-Z]{2,4}\d{0,2})(?:_(?P<note>[A-Za-z]+))?$"
)


@dataclass
class WelderCert:
    stencil: str = ""
    name: str = ""
    process: str = ""
    material: str = ""
    cert_date: str = ""
    expiry: str = ""
    wps: str = ""
    requalification: bool = False


def _mkdate(mm: int, dd: int, yy: int) -> str | None:
    if yy < 100:
        yy += 2000
    if not (1 <= mm <= 12 and 1 <= dd <= 31 and 2000 <= yy <= 2100):
        return None
    try:
        return date(yy, mm, dd).isoformat()
    except ValueError:
        return None


def parse_cert_filename(filename: str, known_stencils: set[str] | None = None) -> WelderCert:
    """Read what a welder certification filename declares.

    Handles the three conventions in the corpus::

        ABF.pdf                                     stencil only
        4-8-25 ANDREW MORGAN SS GTAW ABJ.pdf        date, name, material, process, stencil
        Craig Lunsford_XTO-SS_050224_ADL_REQUAL.pdf name, material, date, stencil, requal

    A short surname is shaped exactly like a stencil - "Kody Babb" would yield
    ``BABB``.  Pass the stencils the weld reports actually use and only those
    are accepted, so a name is never mistaken for a welder code.
    """
    stem = re.sub(r"\.(pdf|jpg|png)$", "", filename, flags=re.IGNORECASE)
    cert = WelderCert(requalification=bool(_REQUAL.search(stem)))

    # The scoped convention states every field outright; parse it whole rather
    # than letting the generic token scan guess at the pieces.
    if m := _SCOPED_CERT.match(stem):
        cert.name = m.group("name").replace("_", " ").strip().title()
        cert.wps = m.group("wps").strip()
        cert.stencil = m.group("stencil").upper()
        mm, dd, yy = (int(m.group("date")[i:i + 2]) for i in (0, 2, 4))
        cert.cert_date = _mkdate(mm, dd, yy) or ""
        if p := _PROCESS.search(stem):
            cert.process = p.group(1).upper()
        if mat := _MATERIAL.search(m.group("wps")):
            cert.material = mat.group(1).upper()
        return cert

    if m := _EXPIRY.search(stem):
        cert.expiry = _mkdate(int(m.group(1)), int(m.group(2)), int(m.group(3))) or ""
        stem = stem[: m.start()] + " " + stem[m.end():]

    if m := _DATE_SEP.search(stem):
        cert.cert_date = _mkdate(int(m.group(1)), int(m.group(2)), int(m.group(3))) or ""
        stem = stem[: m.start()] + " " + stem[m.end():]
    elif m := _DATE_RUN.search(stem):
        # Written MMDDYY on these files: 050224 is 2 May 2024.
        cert.cert_date = _mkdate(int(m.group(1)), int(m.group(2)), int(m.group(3))) or ""
        if cert.cert_date:
            stem = stem[: m.start()] + " " + stem[m.end():]

    if m := _PROCESS.search(stem):
        cert.process = m.group(1).upper()
    if m := _MATERIAL.search(stem):
        cert.material = m.group(1).upper()

    tokens = [t for t in re.split(r"[\s_,\-]+", stem) if t]
    candidates = [
        t.upper() for t in tokens
        if _STENCIL.match(t.upper())
        and not _CERT_NOISE.match(t)
        and not _PROCESS.match(t)
        and not _MATERIAL.match(t)
    ]
    if known_stencils is not None:
        # Only accept a code the weld reports actually use, unless the whole
        # filename is a bare stencil (the "ABF.pdf" convention).
        recognised = [c for c in candidates if c in known_stencils]
        if recognised:
            candidates = recognised
        elif len(tokens) > 1:
            candidates = []
    if candidates:
        # A bare "ABF.pdf" has exactly one; a descriptive name puts the stencil
        # last, after the welder's name and the process.
        cert.stencil = candidates[-1] if len(tokens) > 1 else candidates[0]

    name_tokens = [
        t for t in tokens
        if t.isalpha() and len(t) > 2 and t.upper() != cert.stencil
        and not _CERT_NOISE.match(t) and not _PROCESS.match(t) and not _MATERIAL.match(t)
    ]
    cert.name = " ".join(name_tokens[:3]).title()
    return cert


# ---------------------------------------------------------------------------
# Continuity
# ---------------------------------------------------------------------------

def nearest_stencils(stencil: str, known: set[str]) -> list[str]:
    """Certified stencils one keystroke away from this one.

    Weld reports are filled in by hand on a tailgate, and the errors are
    exactly what you would expect: ``AFB`` for ``ABF``, ``AGR`` for ``ARG``
    (transpositions), ``AREA`` for ``AEA`` (a doubled keystroke).  Damerau
    distance counts a transposition as one edit, which plain Levenshtein does
    not - and transpositions are the common case here.

    Reporting these as uncertified welders would be wrong and alarming; they
    are clerical errors on the report, which is a different thing to fix.
    """
    from rapidfuzz.distance import DamerauLevenshtein

    if not stencil or not known:
        return []
    return sorted(
        k for k in known
        if k != stencil and DamerauLevenshtein.distance(stencil, k, score_cutoff=1) == 1
    )


#: API 1104 disqualifies a welder who has not used the process for six months.
CONTINUITY_DAYS = 183


def continuity_gaps(dates: list[str], limit_days: int = CONTINUITY_DAYS) -> list[tuple[str, str, int]]:
    """Gaps longer than ``limit_days`` between consecutive welding dates.

    Returns ``(previous, next, days)`` for each gap.  This only ever sees welds
    on the audited job, so a gap means "no evidence of continuity here", not
    proof the welder was idle - the finding is worded accordingly.
    """
    parsed = sorted({d for d in dates if d})
    out: list[tuple[str, str, int]] = []
    for a, b in zip(parsed, parsed[1:]):
        try:
            days = (datetime.fromisoformat(b) - datetime.fromisoformat(a)).days
        except ValueError:
            continue
        if days > limit_days:
            out.append((a, b, days))
    return out
