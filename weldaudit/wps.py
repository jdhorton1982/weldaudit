"""Welding procedure specifications: identifying them, and reading the register.

A WPS reference is written differently everywhere it appears.  The weld log
exports say ``XTO-X60-6010/8010 Rev.1``; a certificate filename cannot contain
a slash so it says ``XTO-X60-6010-8010 Rev.1``; and one job's log says
``XTO-ASME PI HYP NACE Rev.0`` where the certificate says
``XTO-ASME-P1-HYP-NACE`` — a letter I for the digit 1 in an ASME P-number.

Two ideas keep that manageable.  The **base** and the **revision** are split
apart, because "is this the same procedure" and "was it the right revision"
are different questions and only the first can be answered when a certificate
omits the revision entirely.  And the base is compared on a punctuation-free
key, which resolves slash-against-dash exactly; anything left over is handled
as a near miss rather than by inventing more normalisation rules, the same way
welder stencils and instrument serials are.

The other half of this module reads the **approved procedure register**.
XTO's welding procedures standard, GPPB-0110, carries the procedures
themselves — eight of them across sixty-five pages, each stating its number,
its supporting PQR and its essential variables.  That makes the list of valid
procedures something the corpus supplies rather than something this tool
asserts, which is the difference between a finding an auditor can defend and
one they cannot.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: `Rev. 1`, `Rev.1`, `REV 2`, `Revision 3` at the end of a reference.
_REVISION = re.compile(r"\s*\bREV(?:ISION)?\.?\s*([0-9]+[A-Z]?)\s*$", re.IGNORECASE)


def split_revision(text: str | None) -> tuple[str, str]:
    """``('XTO-X60-6010/8010', '1')`` from ``'XTO-X60-6010/8010 Rev. 1'``.

    A reference with no revision returns an empty one rather than a guess:
    the certificates routinely omit it, and treating "unstated" as revision 0
    would silently assert something the paperwork does not say.
    """
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    if m := _REVISION.search(raw):
        return raw[: m.start()].strip(" -,"), m.group(1).upper()
    return raw, ""


def base_key(text: str | None) -> str:
    """Punctuation-free key for the procedure, revision removed.

    Resolves ``XTO-X60-6010/8010`` against ``XTO-X60-6010-8010`` exactly.
    """
    base, _revision = split_revision(text)
    return re.sub(r"[^A-Z0-9]", "", base.upper())


def full_key(text: str | None) -> str:
    """Key including the revision, for "was it the same revision" questions."""
    base, revision = split_revision(text)
    key = re.sub(r"[^A-Z0-9]", "", base.upper())
    return f"{key}REV{revision}" if revision else key


def resolve(text: str, known: set[str]) -> tuple[str, str]:
    """Match a reference to a known procedure: ``(key, how)``.

    Three ways, in descending confidence.  Exact on the punctuation-free base.
    Then an unambiguous abbreviation — a certificate saying ``XTO-SS`` for the
    register's ``XTO-SS-Sec. IX`` — accepted only when exactly one known
    procedure extends it.  That last condition is the whole safety of it:
    ``XTO-X42-6010`` also prefixes ``XTO-X42-6010/7018`` and
    ``XTO-X42-6010/8010``, which are three genuinely different procedures, and
    a prefix match there would merge them.  Then a single-character near miss.
    """
    key = base_key(text)
    if not key:
        return "", ""
    if key in known:
        return key, "exact"
    extensions = [other for other in known if other.startswith(key)]
    if len(extensions) == 1:
        return extensions[0], "abbreviated"
    near = nearest_procedures(text, known)
    if len(near) == 1:
        return near[0], "near"
    return "", ""


def nearest_procedures(text: str, known: set[str]) -> list[str]:
    """Procedure keys within one edit of this one.

    `XTOASMEPIHYPNACE` against `XTOASMEP1HYPNACE` is a single substitution —
    an I typed for a 1 in an ASME P-number. Reporting that as an unknown
    procedure would send an auditor looking for a document that does exist.
    """
    from rapidfuzz.distance import DamerauLevenshtein

    key = base_key(text)
    if not key:
        return []
    return sorted(
        other for other in known
        if other != key and DamerauLevenshtein.distance(key, other) <= 1
    )


# ---------------------------------------------------------------------------
# The approved procedure register, as GPPB-0110 states it
# ---------------------------------------------------------------------------


@dataclass
class Procedure:
    """One procedure from the register, with the variables worth checking."""

    wps: str = ""
    revision: str = ""
    pqr: str = ""
    code: str = ""                       # 'API 1104'
    process: str = ""
    min_diameter: float | None = None    # inches OD
    min_wall: float | None = None        # inches
    two_welder_over: float | None = None  # OD at or above which 2 are required
    page_no: int = 0

    @property
    def base_key(self) -> str:
        return base_key(self.wps)

    @property
    def label(self) -> str:
        return f"{self.wps} Rev. {self.revision}" if self.revision else self.wps


_WPS_NO = re.compile(r"WPS\s*(?:NO|NUMBER)\.?\s*:?\s*([A-Z0-9][A-Z0-9\-/. ]{4,40}?)"
                     r"(?=\s+SUPPORTING|\s+API|\s+XTO Energy|\s{2,}|$)", re.IGNORECASE)
_PQR = re.compile(r"SUPPORTING\s+PQR\s*:?\s*([A-Z0-9][A-Z0-9\-/&. ]{4,60}?)"
                  r"(?=\s+API\b|\s+XTO Energy|\s+WPS\b|\s{2,}|$)", re.IGNORECASE)
_CODE = re.compile(
    r"QUALIFIED\s+TO\s*:?\s*(API\s*1104|ASME\s*(?:B31\.\d+|SEC(?:TION)?\.?\s*[IVX]+))",
    re.IGNORECASE,
)
_PROCESS = re.compile(r"WELDING\s+PROCESS\s*:?\s*([A-Z ()\-,]{4,70}?)"
                      r"(?=\s+MATERIAL|\s+PIPE|\s{2,}|$)", re.IGNORECASE)
#: `PIPE OUTSTIDE DIAMETER: >= 2.375" thru Unlimited` - the typo is the
#: source document's, and matching it exactly is cheaper than being clever.
_DIAMETER = re.compile(r"PIPE\s+OUTS?T?IDE\s+DIAMETER\s*:?\s*[≥>=\s]*([\d.]+)",
                       re.IGNORECASE)
_WALL = re.compile(r"WALL\s+THICKNESS\s+RANGE\s*:?\s*[≥>=\s]*([\d.]+)", re.IGNORECASE)
#: `NUMBER OF WELDERS: For Pipe >= 12.750" O.D., 2 or more welders are
#: REQUIRED for Root and Hot Pass`
_TWO_WELDERS = re.compile(
    r"NUMBER\s+OF\s+WELDERS.{0,60}?[≥>=]\s*([\d.]+)\s*[\"”]?\s*O\.?D\.?.{0,40}?"
    r"(\d+)\s+or\s+more\s+welders",
    re.IGNORECASE | re.DOTALL,
)


def _number(text: str | None) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def parse_page(text: str, page_no: int = 0) -> Procedure | None:
    """One procedure page of the register, or None if the page is not one."""
    flat = re.sub(r"\s+", " ", text or "")
    m = _WPS_NO.search(flat)
    if not m:
        return None
    wps, revision = split_revision(m.group(1))
    if not wps:
        return None

    procedure = Procedure(wps=wps, revision=revision, page_no=page_no)
    if p := _PQR.search(flat):
        procedure.pqr = re.sub(r"\s+", " ", p.group(1)).strip(" .&")
    if code := _CODE.search(flat):
        procedure.code = re.sub(r"\s+", " ", code.group(1)).strip()
    if process := _PROCESS.search(flat):
        procedure.process = re.sub(r"\s+", " ", process.group(1)).strip(" ,-")
    procedure.min_diameter = _number(m.group(1) if (m := _DIAMETER.search(flat)) else None)
    procedure.min_wall = _number(m.group(1) if (m := _WALL.search(flat)) else None)
    if welders := _TWO_WELDERS.search(flat):
        procedure.two_welder_over = _number(welders.group(1))
    return procedure


def parse_register(pages: list[tuple[int, str]]) -> list[Procedure]:
    """Every distinct procedure across a standard's pages.

    A procedure spans two pages — the variables on one, the parameters on the
    next — and both repeat the WPS number, so entries are merged on the
    procedure rather than returned twice.  Later pages fill in fields the
    first left blank.
    """
    found: dict[str, Procedure] = {}
    for page_no, text in pages:
        parsed = parse_page(text, page_no)
        if not parsed or not parsed.base_key:
            continue
        existing = found.get(parsed.base_key)
        if existing is None:
            found[parsed.base_key] = parsed
            continue
        for field in ("pqr", "code", "process", "revision"):
            if not getattr(existing, field):
                setattr(existing, field, getattr(parsed, field))
        for field in ("min_diameter", "min_wall", "two_welder_over"):
            if getattr(existing, field) is None:
                setattr(existing, field, getattr(parsed, field))
    return sorted(found.values(), key=lambda p: p.wps)
