"""Reading a material certificate's identity out of its filename.

MTRs in this corpus are named by whoever filed them, and there are at least
four conventions in active use::

    071B33 - 16IN 150 RFWN FLANGE STD A105N XTO-414.pdf   heat first
    377314022-003 ~ 16IN 150 TRUNNION BV CS NACE.pdf      heat first, tilde
    4F214-FLG-WN-2IN-CL600-SCH160-A105N.pdf               heat first, dash-packed
    FLANGE 3 INCH SCH 80- 4F318P.pdf                      heat last
    45° 8 INCH 3R SEGM-5J17DK.pdf                         heat last
    2IN_600_FLG_WN_SCH160_A105N_CS.pdf                    no heat at all

Nothing here guesses.  A token becomes the heat number only once everything
that is recognisably *not* a heat - a size, a schedule, a pressure class, a
material spec, an internal XTO tag - has been ruled out.  Filenames that yield
no heat are reported rather than silently skipped, because "we could not tell
which heat this certificate covers" is itself an audit finding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .aml import categories_for, parse_nps

# ---------------------------------------------------------------------------
# Vocabulary of things that are definitely not heat numbers
# ---------------------------------------------------------------------------

#: ASTM/ASME material specifications.
_SPEC = re.compile(
    r"^(S?A\d{2,4}[A-Z]{0,3}|WPL?\d[A-Z]?|WPB|F\d{1,3}[A-Z]?|"
    r"TP\d{3}[A-Z]{0,2}|\d{3}[A-Z]?L?)$", re.IGNORECASE
)
#: Recognisable spec strings we want to capture rather than discard.
_SPEC_STRICT = re.compile(r"^(S?A\d{2,4}[A-Z]{0,2}N?|WPL\d|WPB|TP\d{3}L?)$", re.IGNORECASE)

_SIZE = re.compile(r"^\d{1,2}(\.\d+)?\s*(IN|INCH|\")$", re.IGNORECASE)
_SCHEDULE = re.compile(r"^(SCH\s*\d{1,3}[A-Z]?|SCH|STD|XS|XH|XXS|XXH|S\d{2,3})$", re.IGNORECASE)
#: Flanged classes, and the API 602 threaded/socket-weld classes that read as
#: bare four-digit numbers — "BAYLOR BV THRD 3000" put 3000 in as a heat.
_CLASS = re.compile(
    r"^(CL)?\s*(150|300|400|600|800|900|1500|2500|3000|6000|9000|3M|6M|10M)$",
    re.IGNORECASE)
_GRADE = re.compile(r"^B?X?\d{2}[A-Z]?$", re.IGNORECASE)          # X42, X52, B, 52
_MATERIAL = re.compile(r"^(CS|SS|316|316L|304|304L|LTCS|DSS|GALV|NACE|FBE|ARO)$", re.IGNORECASE)
_TAG = re.compile(r"^(XTO|XTTO|XT0|TAG|PO|REV)\b", re.IGNORECASE)
_DESCRIPTOR = re.compile(
    r"^(FLANGE|FLG|PIPE|TEE|ELBOW|ELL?|EL|REDUCER|RED|CAP|BLIND|BLD|PLUG|UNION|"
    r"COUPLING|CPLG|NIPPLE|OLET|WELDOLET|SOCKOLET|THREDOLET|FLEXOLET|BW|SW|NPT|"
    r"RF|FF|RTJ|WN|SO|LR|SR|CON|ECC|SEGM|VALVE|BV|GV|PV|CV|TRUNNION|BALL|GATE|"
    r"CHECK|GLOBE|BUTTERFLY|SPACER|SPADE|GASKET|STUD|BOLT|NUT|TAP|MXL|HT|EF|LF|"
    r"DUPLICATE|ASSY|CONC|R|P|D|A|B|C)$", re.IGNORECASE
)
#: Bare fraction/degree noise: "45°", "90", "3R".
_MISC = re.compile(r"^(\d{1,3}°|\d{1,2}R|\d{1,2}/\d{1,2})$", re.IGNORECASE)

#: A date. Bills of lading are filed by the day they arrived — `8-13-25
#: PIPE.pdf`, `6-24-25 FITTINGS & VALVES.pdf` — and the digits sit exactly
#: where a heat usually does. Worse, the trimmer used to eat one: 13 and 25
#: both read as grades, so `8-13-25` was peeled back to heat "8", which then
#: matched nothing on any as-built and counted as a certificate on file.
_DATE = re.compile(r"^\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}$")

#: What the document is, not what it certifies. `MTR - 2 Inch Pipe - HT
#: 318652.pdf` led with "MTR" and the leading-heat rule took it, while the
#: real heat sat further along the name behind an explicit label.
_DOCUMENT = re.compile(
    r"^(MTRS?|CERTS?|CERTIFICATE|COC|COA|TEST|REPORT|BOL|PACKING|SLIP)$",
    re.IGNORECASE)

#: A catalogue figure number: "YARROW CAP FIG 500". It is what the part is, not
#: what melt it came from, and it reads as a bare number at the end of a name.
_FIGURE = re.compile(r"\bFIG\.?\s*#?\s*\d+[A-Z]?\b", re.IGNORECASE)

#: A valve model code, which is how the valve certificates are filed:
#: `5F-F03N-SE BAYLOR 3000 THRD BALL VALVE`, `8F-T63SN-RF`, `6F-F13N-RF15.5`,
#: `F-AE 2IN 600 YARROW CAP`. Size, then the maker's figure, then the facing —
#: no heat anywhere in it. Left alone, each piece in turn looks heat-shaped:
#: these files were filed under 3000, then F03N, then RF15.5, none of which is
#: a melt. A certificate whose filename does not name a heat is meant to yield
#: none; that is a finding of its own, and a truthful one.
_VALVE_FIGURE = re.compile(
    r"^\s*\d{0,2}F-[A-Z0-9][A-Z0-9.]*(?:-[A-Z0-9.]+)*", re.IGNORECASE)

#: A pipe dimension written as outside diameter by wall: "4x0.337", "16X.375".
#: It leads the filename where a heat usually does, so without this
#: `4x0.337 PIPE Gr.B LL0731.pdf` registers its dimensions as the heat and the
#: real heat as uncertified.
_DIMENSION = re.compile(r"^\d{1,2}(\.\d+)?\s*[xX]\s*\.?\d+(\.\d+)?$")

#: A plausible heat: has a digit, is not absurdly short or long.  Three
#: characters is the floor - "FLANGE 16 INCH STD EL8" really is heat EL8.
_HEATISH = re.compile(r"^(?=.*\d)[A-Z0-9][A-Z0-9./-]{2,17}$", re.IGNORECASE)

#: The strongest convention in the corpus: a heat, then a spaced separator,
#: then the description - "071B33 - 16IN 150 RFWN FLANGE", "CCDS - 2IN 300 RF".
#: The trailing whitespace is what distinguishes this from a dash-packed name
#: such as "4F214-FLG-WN-2IN", where the dashes are joining description parts.
_LEADING_HEAT = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9./-]{1,17}?)\s*[-~]\s+")


#: A heat somebody labelled: "MTR - 2 Inch Pipe - HT 318652", "HEAT# 24913".
#: The label has to be a whole word, or the HT in "1IN TAP A105N" qualifies.
_LABELLED_HEAT = re.compile(
    r"\b(?:HEAT|HT)\s*(?:NO|NUMBER|#)?\s*[.:#-]?\s+"
    r"([A-Za-z0-9][A-Za-z0-9./-]{2,17})\b", re.IGNORECASE)


#: Words that join two heat numbers in a filename rather than ending the run.
_HEAT_CONNECTOR = re.compile(r"^(and|&|\+|,)$", re.IGNORECASE)


@dataclass
class MtrIdentity:
    heat: str = ""
    #: Every heat the certificate covers.  One mill certificate routinely
    #: covers a whole rolling — `3651447 3653602 3754167 3756253.pdf` is four
    #: heats and `F37B6 F45B6.pdf` is two — and taking only the first left the
    #: rest of the rolling looking uncertified.  ``heat`` stays as the first
    #: for every existing caller.
    heats: list[str] = field(default_factory=list)
    nps: float | None = None
    schedule: str = ""
    spec: str = ""
    material: str = ""
    pressure_class: str = ""
    description: str = ""
    categories: list[str] = field(default_factory=list)
    confidence: str = "none"      # 'high' | 'medium' | 'none'

    @property
    def has_heat(self) -> bool:
        return bool(self.heat)


def _tokens(stem: str) -> list[str]:
    """Split a filename stem into candidate tokens, keeping order."""
    # Windows copy markers and revision suffixes carry no identity.
    stem = re.sub(r"\s*\(\d+\)\s*$", "", stem)
    stem = re.sub(r"\bREV\s*\d+\b", " ", stem, flags=re.IGNORECASE)
    # A tilde or a long dash run is a separator between heat and description.
    stem = stem.replace("~", " ")
    # An internal tag and its number are one thing. Splitting "XTO-421" left
    # "421" standing alone at the end of the name, where the trailing-heat
    # rule adopted it: two gaskets were filed under heats 421 and 484, which
    # are their XTO tags.
    stem = re.sub(r"\b(XTO|XTTO|XT0|TAG|PO)\s*-?\s*\d+\b", " ", stem,
                  flags=re.IGNORECASE)
    # A valve model code leads the name where a heat would, and every piece of
    # it in turn looks heat-shaped. Removed whole rather than picked apart.
    stem = _VALVE_FIGURE.sub(" ", stem)
    stem = _FIGURE.sub(" ", stem)
    parts = re.split(r"[\s_,]+|(?<=[A-Za-z0-9])-(?=[A-Za-z])|(?<=[A-Za-z])-(?=\d)", stem)
    return [p.strip(" -.") for p in parts if p and p.strip(" -.")]


def _is_not_heat(token: str) -> bool:
    return bool(
        _SIZE.match(token) or _SCHEDULE.match(token) or _CLASS.match(token)
        or _MATERIAL.match(token) or _TAG.match(token) or _DESCRIPTOR.match(token)
        or _MISC.match(token) or _SPEC_STRICT.match(token) or _GRADE.match(token)
        or _DIMENSION.match(token) or _DATE.match(token) or _DOCUMENT.match(token)
    )


def parse(filename: str) -> MtrIdentity:
    """Pull heat number and material attributes out of an MTR filename."""
    stem = re.sub(r"\.(pdf|PDF)$", "", filename)
    toks = _tokens(stem)
    ident = MtrIdentity(description=re.sub(r"\s+", " ", stem).strip())

    # -- attributes ---------------------------------------------------------
    for t in toks:
        if ident.nps is None and _SIZE.match(t):
            ident.nps = parse_nps(t)
        if not ident.schedule and _SCHEDULE.match(t) and t.upper() != "SCH":
            ident.schedule = t.upper()
        if not ident.spec and _SPEC_STRICT.match(t):
            ident.spec = t.upper()
        if not ident.material and _MATERIAL.match(t):
            ident.material = t.upper()
        if not ident.pressure_class and _CLASS.match(t):
            ident.pressure_class = t.upper().replace("CL", "").strip()

    # "SCH 80" and "SCH160" both occur; stitch a split schedule back together.
    for a, b in zip(toks, toks[1:]):
        if a.upper() == "SCH" and re.fullmatch(r"\d{1,3}[A-Z]?", b):
            ident.schedule = f"SCH{b.upper()}"
    # A size written "3 INCH" splits into two tokens.
    if ident.nps is None:
        for a, b in zip(toks, toks[1:]):
            if re.fullmatch(r"\d{1,2}(\.\d+)?", a) and re.fullmatch(r"IN|INCH", b, re.IGNORECASE):
                ident.nps = parse_nps(a)
                break

    ident.categories = categories_for(stem)

    # -- heat ---------------------------------------------------------------
    # A labelled heat outranks every positional rule. Nothing else in the name
    # is stated; this is, so where somebody wrote it down it is taken.
    if m := _LABELLED_HEAT.search(stem):
        said = _trim_heat(m.group(1))
        if said and not _is_not_heat(said):
            ident.heat = said
            ident.heats = [said]
            ident.confidence = "high"
            return ident

    # "<heat> - <description>" is unambiguous, and is the only form that can
    # carry a letters-only heat such as CCDS without risking a descriptor.
    if m := _LEADING_HEAT.match(stem):
        lead = m.group(1)
        if not _is_not_heat(lead) and len(lead) >= 3:
            ident.heat = _trim_heat(lead)
            ident.heats = [ident.heat]
            ident.confidence = "high"
            return ident

    candidates = [t for t in toks if _HEATISH.match(t) and not _is_not_heat(t)]
    if not candidates:
        return ident

    # The first token wins when the name leads with the heat, which is the
    # dominant convention; otherwise fall back to the last candidate, which
    # covers "FLANGE 3 INCH SCH 80- 4F318P".
    first_tok = toks[0]
    if first_tok in candidates:
        ident.heats = _leading_run(toks, candidates)
        ident.heat = ident.heats[0]
        ident.confidence = "high"
    else:
        ident.heat = _trim_heat(candidates[-1])
        ident.heats = [ident.heat]
        # Trailing heats are more easily confused with a stray number, so they
        # are only trusted when the rest of the name looks like a description.
        ident.confidence = "high" if len(toks) > 2 else "medium"
    return ident


def _leading_run(toks: list[str], candidates: list[str]) -> list[str]:
    """The unbroken run of heats a filename opens with.

    Stops at the first token that is not a heat, so a description ends the
    run: `T98481 4 X45 XS` is one heat, and `951486 and 951488 16in pipe` is
    two because `and` only joins.
    """
    heats: list[str] = []
    for token in toks:
        if token in candidates:
            trimmed = _trim_heat(token)
            if trimmed not in heats:
                heats.append(trimmed)
        elif not _HEAT_CONNECTOR.match(token):
            break
    return heats


def _trim_heat(token: str) -> str:
    """Drop description fragments a dash glued onto the heat.

    ``A241114AB24-4IN-PIPE`` tokenises with the size still attached because the
    dash sits between two digits; peel off any trailing part that is itself a
    recognisable non-heat.
    """
    parts = token.split("-")
    while len(parts) > 1 and (_is_not_heat(parts[-1]) or not parts[-1]):
        parts.pop()
    kept = "-".join(parts).upper()
    # What is left has to still look like a heat. Peeling a token down to one
    # or two characters means the token was never a heat with a description
    # stuck to it — it was something else entirely, and the stub is noise.
    return kept if len(kept) >= 3 else ""


def normalise_heat(heat: str) -> str:
    """Canonical form for joining heats across sources.

    Heats are transcribed inconsistently - leading zeros come and go, and
    separators drift - so comparison is done on an upper-cased, punctuation-free
    form.
    """
    return re.sub(r"[^A-Z0-9]", "", (heat or "").upper())
