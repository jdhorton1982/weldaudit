"""The turnover-book taxonomy and the rules that map a file path onto it.

Every line segment on these projects is filed as the same 22-section book.
Folder names drift between projects (``11 NDE`` vs ``11-NDE``, ``7 MTRS`` vs
``07-MTRS``), so sections are matched on a normalised form of the path rather
than on an exact string.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# The 22 sections of a turnover book
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Section:
    number: int
    name: str
    patterns: tuple[str, ...]
    #: Sections a complete book cannot be signed off without.
    required: bool = True


SECTIONS: tuple[Section, ...] = (
    Section(1, "Scope of Work", ("scope of work", "rfq")),
    Section(2, "Alignment Sheets", ("alignment sheet",)),
    Section(3, "As-Built", ("as.?built",)),
    Section(4, "Job Specs", ("job spec",)),
    Section(5, "Permits", ("permit",)),
    Section(6, "Bill of Ladings", ("bill of lading", r"\bbols?\b")),
    Section(7, "MTRs", (r"\bmtrs?\b", "material test")),
    Section(8, "Valve Documents", ("valve doc", "valve document")),
    Section(9, "Inspector Documents", ("inspector doc",)),
    Section(10, "Welding", ("welding", "weld report")),
    Section(11, "NDE", (r"\bnde\b",)),
    Section(12, "Shop Fab Package", ("shop fab",)),
    Section(13, "Corrosion Requirement", ("corrosion",)),
    Section(14, "Bore Profiles", ("bore profile",), required=False),
    Section(15, "Foreign Line Crossing", ("foreign line",), required=False),
    Section(16, "One Calls", ("one call",), required=False),
    Section(17, "Hydro Test Packet", ("hydro",)),
    Section(18, "Flange Map", ("flange map",)),
    Section(19, "Coating", ("coating",)),
    Section(20, "Backfill Release Form", ("backfill",)),
    Section(21, "Safety", ("safety",)),
    Section(22, "Miscellaneous", ("miscellaneous", "misc"), required=False),
)

SECTION_BY_NUMBER = {s.number: s for s in SECTIONS}
REQUIRED_SECTIONS = tuple(s for s in SECTIONS if s.required)

# A leading section number in the folder name is the strongest signal we have.
_LEADING_NUMBER = re.compile(r"(?:^|[\\/])0*(\d{1,2})[\s\-_]+([a-z])")


# ---------------------------------------------------------------------------
# Document kinds - finer grained than sections, and what the extractors key on
# ---------------------------------------------------------------------------

#: ``(kind, regex)`` tested against the normalised path, most specific first.
KIND_RULES: tuple[tuple[str, str], ...] = (
    ("nde_reader_sheet", r"reader sheet"),
    ("nde_tech_cert", r"nde certs?|tech certs?"),
    ("nde_procedure", r"\bnde\b.*procedures?|procedures?.*\bnde\b|individual procedures?|company procedures?"),
    ("nde_rig_log", r"nde rig log|nde log"),
    ("weld_log_csv", r"weld log summary|master_weld_log"),
    ("pipes_csv", r"pipes_export"),
    # Named for the content, not the format: on some jobs the weld and heat
    # maps are CSV exports, on others they are scanned drawings. The CSV
    # extractor filters on the extension, so both share a kind.
    ("weld_map", r"weld\s*maps?|maps?\s*weld|weldsexport|heat\s*maps?|maps?\s*heat"),
    ("daily_weld_report", r"weld reports?|\bdwr\b|daily weld"),
    # "Welder log" is the contractor's roster of who is on the job; "weld
    # log" is the register of joints. One letter apart, entirely different
    # documents, so the roster is matched first.
    ("welder_roster", r"welder logs?|welder roster"),
    ("welder_cert", r"welder certs?|continuity"),
    ("wps", r"\bwps\b|welding procedure"),
    ("mtr", r"\bmtrs?\b|material test"),
    ("as_built", r"as.?built"),
    ("hydrotest", r"hydro"),
    ("bill_of_lading", r"bill of lading|\bbols?\b"),
    ("valve_doc", r"valve docs?|(ball|check|gate|globe|plug|butterfly) valve"),
    ("flange_map", r"flange map"),
    # Instrument certificates live in the coating folder and are named for the
    # gauge, so they are separated by name before `coating` claims them.
    ("instrument_cal", r"holiday detector|jeep ?meter|posi ?tector|\bdpm\b|"
                       r"environmental gauge|profile gauge|testex micrometer|"
                       r"thickness ga(u)?ge|thickness instrument|torque wrench"),
    ("product_data_sheet", r"\bpds\b|product data sheet|technical data sheet"),
    ("safety_data_sheet", r"\bsds\b|\bmsds\b|safety data sheet"),
    ("coating", r"coating"),
    ("alignment_sheet", r"alignment sheet"),
    ("permit", r"permit|one call"),
    ("job_spec", r"job spec|scope of work|\brfq\b"),
    ("inspector_doc", r"inspector doc"),
    ("safety", r"safety"),
    ("backfill", r"backfill"),
    ("shop_fab", r"shop fab"),
)

_COMPILED_KINDS = tuple((k, re.compile(p, re.IGNORECASE)) for k, p in KIND_RULES)
_COMPILED_SECTIONS = tuple(
    (s, tuple(re.compile(p, re.IGNORECASE) for p in s.patterns)) for s in SECTIONS
)


def normalise(path: str) -> str:
    """Lower-case, collapse separators, so folder-name drift stops mattering."""
    text = path.replace("\\", "/").lower()
    text = re.sub(r"[_]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text


def section_for(path: str) -> Section | None:
    """Which of the 22 sections does this path sit under?"""
    norm = normalise(path)

    # Prefer an explicit leading section number on a folder ("11 nde/...").
    for m in _LEADING_NUMBER.finditer(norm):
        num = int(m.group(1))
        if num in SECTION_BY_NUMBER:
            sec = SECTION_BY_NUMBER[num]
            # Guard against a coincidental number by requiring the name to agree.
            tail = norm[m.start(2) : m.start(2) + 40]
            for pat in sec.patterns:
                if re.search(pat, tail, re.IGNORECASE):
                    return sec

    for sec, pats in _COMPILED_SECTIONS:
        for pat in pats:
            if pat.search(norm):
                return sec
    return None


def kind_for(path: str) -> str:
    """The document kind, used to pick an extractor.

    The filename beats the folder it is filed under.  Contractors file whole
    document sets inside one section folder — GL 31 keeps its pipe
    certificates and its heat maps under ``HYDRO TEST DOCUMENTS`` — and the
    containing folder says where a document was put, while its own name says
    what it is.
    """
    norm = normalise(path)
    name = normalise(re.split(r"[\\/]", path)[-1])

    by_name = next((k for k, pat in _COMPILED_KINDS if pat.search(name)), "")
    if by_name:
        return by_name
    return next((k for k, pat in _COMPILED_KINDS if pat.search(norm)), "unknown")


# ---------------------------------------------------------------------------
# Segment identification
# ---------------------------------------------------------------------------

#: Folder names that are containers, not line segments.
_NOT_A_SEGMENT = re.compile(
    r"^(book|all files|documents|projects|new folder|trash|personal.*|weldaudit|"
    r"\.claude|reader sheets|procedures|weld reports|nde certs?|fittings|"
    r"company procedures|individual procedures|nde log)$",
    re.IGNORECASE,
)

#: A segment folder almost always names a pipe size, e.g. "20 LP", "4IN FG SEG A".
_SEGMENT_HINT = re.compile(r"\b\d{1,2}\s*(in\b|\"|-?inch)|\b\d{1,2}\s+[a-z]{2,}", re.IGNORECASE)


def segment_for(root: str, path: str) -> str:
    """Best guess at the line segment a file belongs to.

    Walks the path from the project root downwards and takes the deepest folder
    that looks like a line segment rather than a book section or a container.
    """
    rel = path[len(root) :].strip("\\/") if path.startswith(root) else path
    parts = [p for p in re.split(r"[\\/]+", rel) if p]
    parts = parts[:-1]  # drop the filename

    best = ""
    for part in parts:
        if _NOT_A_SEGMENT.match(part.strip()):
            continue
        if section_for(part) is not None:
            break  # we have descended into the book itself
        if _SEGMENT_HINT.search(part):
            best = part.strip()
    if best:
        return best
    # Fall back to the shallowest meaningful folder.
    for part in parts:
        if not _NOT_A_SEGMENT.match(part.strip()) and section_for(part) is None:
            return part.strip()
    return "(unassigned)"
