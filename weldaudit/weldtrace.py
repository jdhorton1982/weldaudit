"""Reading a WeldTrace project download.

WeldTrace is the register the crews on the digital jobs keep instead of a
clipboard, and a project download is that register exported whole.  It matters
to this tool for one reason: it arrives **typed instead of scanned**.  Every
table the audit builds from a photocopied weld log - who welded what, against
which procedure, out of which heat, examined by which report - is already
characters in a file here, so it costs nothing to read and needs no second
pass behind a button.  A job audited on a machine that never pressed that
button came back naming two manufacturers out of 495 instead of 28 of 37: a
shorter report that looks like a cleaner package.  This module is how that
stops being possible for the jobs that run on WeldTrace.

A download holds six artefacts, three of them machine-readable:

===============================  ==========================================
``*TestPackExport.csv``          the weld register, one row per weld
``*weldsExport.csv``             the same register, exported by drawing
``*projectMaterialsExport.csv``  the heat register, one row per heat
``AnnotationAttachments_*.pdf``  the as-built: Bluebeam stamps per drawing
``*annotationsExport.csv``       the same stamps, exported as a table
``TEST n - ISOS AND PIDS.pdf``   the signed drawing set - stored, not parsed
``TEST n - Test Plan.docx/.pdf`` QAQC-FRM-4347 - stored, not parsed
===============================  ==========================================

The joins are string equality once the format quirks are handled: weld to heat
by heat number, weld to stamp by weld number, stamp to drawing by drawing and
revision.

Nothing here touches the database.  :mod:`weldaudit.extract.weldtrace` does
that; this module is only the format.
"""

from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

#: Every empty cell in a WeldTrace export is written as a single hyphen rather
#: than left blank.  Without normalising, every "is this field filled in?"
#: check in the audit silently passes on the whole download.
NULL = {"", "-", "--", "---", "n/a", "na", "none", "null"}

#: The eight examination blocks a weld row carries.  VI is the visual sign-off
#: and is a column like the rest; heat treatment (HT, PWHT) is recorded the
#: same way but is not an examination, and is not read as one.
NDE_METHODS: tuple[str, ...] = ("VI", "RT", "UT", "MT", "PT", "FT", "PMI", "BT")

#: Verdicts, lower-cased, that mean the examination was accepted or rejected.
_PASSED = ("pass", "accept", "sat")
_FAILED = ("fail", "reject", "unsat")

#: Dates as WeldTrace writes them.  The CSVs use a month abbreviation; the
#: Bluebeam stamps use slashes, with a two-digit year as often as four.
_DATE_FORMATS = ("%b-%d-%Y", "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%d-%b-%Y")


def clean(value: object) -> str:
    """Normalise one exported cell, treating WeldTrace's ``-`` as empty."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).strip()
    return "" if text.lower() in NULL else text


def parse_date(value: object) -> str:
    """One exported date as ``YYYY-MM-DD``, or ``''``.

    An unparseable date is dropped rather than passed through: the sample
    download contains ``8/222/2026``, and a string that only looks like a date
    would sort and compare as though it were one.
    """
    text = clean(value)
    if not text:
        return ""
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def split_ids(value: object) -> list[str]:
    """``'AOO;'`` -> ``['AOO']``, ``'AOO;APT;'`` -> ``['AOO', 'APT']``.

    Welder columns are semicolon-*terminated*, not semicolon-separated, so a
    naive split leaves an empty stencil on the end of every populated cell.
    """
    return [part.strip() for part in clean(value).split(";") if part.strip()]


# ---------------------------------------------------------------------------
# Reading an export
# ---------------------------------------------------------------------------

#: WeldTrace writes the weld size header as ``"Weld Size ("in)`` - an
#: unescaped quote inside a quoted field.  The rows parse fine and some CSV
#: readers recover the header too, but not all of them and not in every
#: export, so the token is repaired on content rather than on position: the
#: column sits at index 19 in a test pack export and at index 9 in a welds
#: export, and keying off the index would fix one by breaking the other.
_MANGLED_HEADERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r'^"?weld\s*size\s*\("?in\)?"?$', re.IGNORECASE), "Weld Size (in)"),
)


def repair_header(header: list[str]) -> list[str]:
    """Put back the header tokens WeldTrace's own quoting breaks."""
    out = []
    for cell in header:
        name = cell.strip().lstrip("﻿")
        for pattern, fixed in _MANGLED_HEADERS:
            if pattern.match(name):
                name = fixed
                break
        out.append(name)
    return out


def read_export(path: str | Path) -> list[dict[str, str]]:
    """Read a WeldTrace CSV into row dicts, header repaired and rows rejoined.

    The annotations export writes a newline inside an *unquoted* field - the
    bubble text and the welder initials under it are one cell, exported with
    the line break intact - so a row arrives split across two or three lines
    of the file.  Any row shorter than the header is therefore continued into
    the next one rather than dropped, which is the only reading that does not
    silently lose a third of the stamps.
    """
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    rows = list(csv.reader(text.splitlines()))
    if not rows:
        return []

    header = repair_header(rows[0])
    width = len(header)

    out: list[dict[str, str]] = []
    pending: list[str] = []
    for row in rows[1:]:
        if pending:
            # The break fell inside the last field of the pending row.
            pending = pending[:-1] + [f"{pending[-1]}\n{row[0]}"] + list(row[1:])
        else:
            pending = list(row)
        if len(pending) < width:
            continue
        if any(cell.strip() for cell in pending):
            out.append(dict(zip(header, pending[:width])))
        pending = []
    if pending and any(cell.strip() for cell in pending):
        out.append(dict(zip(header, pending + [""] * (width - len(pending)))))
    return out


def _first(row: dict[str, str], *names: str) -> str:
    """The first of several column spellings that this export actually has.

    WeldTrace exports the same field under different headings depending on
    which report produced the file - a test pack export fuses drawing and
    revision into ``Drawing # - Revision`` where a welds export keeps
    ``Drawing Number`` and ``Drawing Revision`` apart - so every field is
    looked up by the spellings it is known to arrive under.
    """
    lower = {k.strip().lower(): v for k, v in row.items() if k}
    for name in names:
        if name.lower() in lower:
            value = clean(lower[name.lower()])
            if value:
                return value
    return ""


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------

#: A weld tag: ``W-1``, ``FW-104``, ``PT-2``, ``AFW-6PCO``.  One letter of
#: prefix is enough - the sample register numbers welds ``W-37`` - which is
#: why :mod:`weldaudit.ids` cannot be used here: an NDE report id needs two.
_TAG = re.compile(r"^(?P<prefix>[A-Z]{1,5})-0*(?P<number>\d{1,4})(?P<suffix>[A-Z]{0,3})$")

#: Suffixes a stamp adds to a tag that the register does not carry: ``P`` for
#: a repair or partial pass, ``CO`` for a weld cut out.  Same vocabulary as
#: :mod:`weldaudit.ids` keeps for NDE reports, for the same reason.
_TAG_SUFFIXES = ("PCO", "RCO", "CO", "RR", "P", "R")


@dataclass(frozen=True, order=True)
class WeldTag:
    """A weld number, comparable across the register and the drawings."""

    prefix: str
    number: int
    suffix: str = ""

    def __str__(self) -> str:
        return f"{self.prefix}-{self.number}{self.suffix}"

    @property
    def variants(self) -> tuple[WeldTag, ...]:
        """This tag, then the looser readings of it, most exact first.

        Two things differ between how a weld is numbered in the register and
        how it is stamped on the isometric, and neither is safe to normalise
        away outright:

        * an ``A``/``B`` layer letter in front of the prefix.  On the Merlin 3
          download stripping it lifts stamp matches from 19 of 107 to 103; on
          the Hot Pass downloads the register numbers its own welds ``BPT-2``
          and ``BFW-4``, so stripping it unconditionally would break every
          match rather than make one.
        * a ``P`` or ``CO`` suffix the stamp records and the register does not.

        So the exact tag is tried first and the looser readings only where the
        exact one is unknown.  That gets both jobs right, and leaves ``W-81``
        against ``BFW-81`` reported as the two-sided discrepancy it is rather
        than quietly resolved.
        """
        out = [self]
        if self.suffix:
            out.append(WeldTag(self.prefix, self.number))
        if len(self.prefix) > 1 and self.prefix[0] in "AB":
            out.append(WeldTag(self.prefix[1:], self.number, self.suffix))
            if self.suffix:
                out.append(WeldTag(self.prefix[1:], self.number))
        return tuple(out)


def parse_tag(text: object) -> WeldTag | None:
    """``'AFW-006P'`` -> ``WeldTag('AFW', 6, 'P')``, or ``None``."""
    token = clean(text).upper().replace(" ", "")
    m = _TAG.match(token)
    if not m:
        return None
    suffix = m.group("suffix")
    if suffix and suffix not in _TAG_SUFFIXES:
        return None
    return WeldTag(m.group("prefix"), int(m.group("number")), suffix)


def split_trailing_revision(value: str) -> tuple[str, str]:
    """``'2-D1-0-GL-4012-1-0'`` -> ``('2-D1-0-GL-4012-1', '0')``.

    Drawing and revision arrive fused in the test pack export and separate in
    the annotation export, and exactly one trailing group is the revision.
    Stripping both sides was the bug that turned four genuine mismatches into
    103 out of 107, so this is applied to the fused side only.
    """
    text = clean(value)
    parts = text.split("-")
    if len(parts) > 2 and parts[-1].isdigit():
        return "-".join(parts[:-1]), parts[-1]
    return text, ""


def heat_of(material: str) -> str:
    """The heat out of a WeldTrace material code.

    ``'PRC-2-FLG-WN-RF-S160-A105N'`` is heat ``PRC`` in a 2" weld neck flange.
    The leading group is the heat however short it is - three characters is
    common - and the rest describes the part it was rolled into.
    """
    text = clean(material)
    head = text.split("-", 1)[0]
    return head if head and head != text else text


# ---------------------------------------------------------------------------
# What a download says
# ---------------------------------------------------------------------------


@dataclass
class Examination:
    """One method's block on a weld row."""

    method: str
    requested: bool | None      # None where the export has no request column
    verdict: str = ""
    report: str = ""
    revision: str = ""
    date: str = ""
    retest_requested: bool | None = None
    retest_verdict: str = ""

    @property
    def recorded(self) -> bool:
        return bool(self.verdict or self.report or self.date)

    @property
    def passed(self) -> bool:
        return self.verdict.lower().startswith(_PASSED)

    @property
    def failed(self) -> bool:
        return self.verdict.lower().startswith(_FAILED)

    @property
    def retested(self) -> bool:
        return bool(self.retest_verdict) or self.retest_requested is True


@dataclass
class WeldRow:
    """One weld, as the register has it."""

    weld_number: str
    tag: WeldTag | None
    test_pack: str
    pack_reference: str
    drawing: str
    revision: str
    line: str
    line_class: str
    category: str               # Shop / Field
    joint_type: str
    size: str
    wps: str                    # base, with the revision split off
    wps_revision: str
    welders: dict[str, list[str]]
    date_planned: str
    date_welded: str
    materials: tuple[str, str]  # the material codes as written
    heats: tuple[str, str]
    exams: dict[str, Examination]
    result: str
    penalty: str
    row: dict[str, str] = field(repr=False, default_factory=dict)

    @property
    def key(self) -> str:
        """How a weld is named in a finding: ``TP-1-1/W-22``."""
        return f"{self.test_pack}/{self.weld_number}" if self.test_pack else self.weld_number

    @property
    def examined(self) -> list[Examination]:
        return [e for e in self.exams.values() if e.recorded]

    @property
    def requested(self) -> list[Examination]:
        return [e for e in self.exams.values() if e.requested]

    @property
    def asks_for_nde(self) -> bool:
        """Whether this export says anything at all about wanting NDE."""
        return any(e.requested is not False for e in self.exams.values())


@dataclass
class HeatRow:
    """One heat, as the material register has it."""

    #: The fields the approved-manufacturer check needs before it can run.
    AML_FIELDS = ("Supplier", "Spec No.", "Grade", "P-No.")

    heat: str
    name: str
    product_form: str
    fitting_type: str
    supplier: str
    spec_no: str
    grade: str
    p_no: str
    mtr_file: str
    status: str
    row: dict[str, str] = field(repr=False, default_factory=dict)

    @property
    def missing_aml_fields(self) -> list[str]:
        return [name for name, value in zip(
            self.AML_FIELDS,
            (self.supplier, self.spec_no, self.grade, self.p_no)) if not value]


@dataclass
class Stamp:
    """One as-built annotation lifted off an isometric."""

    drawing: str
    revision: str
    tag: WeldTag
    raw_tag: str
    welder: str = ""
    date: str = ""
    page_no: int | None = None


# ---------------------------------------------------------------------------
# The weld register
# ---------------------------------------------------------------------------

_RESULT_REPORT = re.compile(r"^(?P<report>.+?)-(?P<rev>\d+)$")


def parse_result(value: str) -> tuple[str, str, str, str]:
    """``'Passed;NX-20260331RT01-0;Mar-31-2026;'`` -> verdict, report, rev, date."""
    parts = [clean(p) for p in clean(value).split(";")]
    verdict = parts[0] if parts else ""
    reference = parts[1] if len(parts) > 1 else ""
    date = parse_date(parts[2]) if len(parts) > 2 else ""
    report, revision = reference, ""
    if reference and (m := _RESULT_REPORT.match(reference)):
        report, revision = m.group("report"), m.group("rev")
    return verdict, report, revision, date


_YES = {"yes", "y", "true", "1", "required", "requested"}


def _has_column(row: dict[str, str], *names: str) -> bool:
    """Whether this export has any of these columns, however it filled them in.

    A column that is absent and a column that is blank mean different things
    in these exports, and more than one field below turns on the difference.
    """
    lower = {k.strip().lower() for k in row if k}
    return any(n.lower() in lower for n in names)


def _requested(row: dict[str, str], *names: str) -> bool | None:
    """Whether a method was asked for, or ``None`` if the export never says.

    A welds export carries results and no request columns at all.  Reading a
    missing column as "not requested" would report every weld on such a
    download as never examined, so the difference is kept.
    """
    if not _has_column(row, *names):
        return None
    return _first(row, *names).lower() in _YES


def parse_weld_register(path: str | Path) -> list[WeldRow]:
    """Read a test pack export or a welds export into welds."""
    welds: list[WeldRow] = []
    for row in read_export(path):
        number = _first(row, "Weld Number", "Weld #", "Weld No")
        if not number:
            continue

        drawing = _first(row, "Drawing Number", "Drawing #")
        revision = _first(row, "Drawing Revision", "Drawing Rev")
        if not drawing:
            # The test pack export fuses the two into one column.
            drawing, revision = split_trailing_revision(
                _first(row, "Drawing # - Revision", "Drawing - Revision"))

        wps = _first(row, "WPS", "WPS #", "WPs#")
        wps_revision = _first(row, "WPS Revision", "WPs Revision")
        if not wps:
            wps, wps_revision = split_trailing_revision(
                _first(row, "WPs# - Revision", "WPS # - Revision", "WPS - Revision"))

        test_pack, _pack_revision = split_trailing_revision(
            _first(row, "Test Pack # - Rev", "Test Pack #", "Test Pack"))

        exams: dict[str, Examination] = {}
        for method in NDE_METHODS:
            verdict, report, revision_no, date = parse_result(
                _first(row, f"{method} Result & Report-Rev & Date"))
            retest_verdict, _r, _rr, _rd = parse_result(
                _first(row, f"{method} Retest Result & Report-Rev & Date"))
            exam = Examination(
                method=method,
                requested=_requested(row, f"{method} Test Requested"),
                verdict=verdict, report=report, revision=revision_no, date=date,
                retest_requested=_requested(row, f"{method} Retest Requested"),
                retest_verdict=retest_verdict,
            )
            # An export with no request column still says a method was wanted
            # by carrying a result for it.
            if exam.requested is None and exam.recorded:
                exam.requested = True
            exams[method] = exam

        materials = (_first(row, "Material1", "Material 1", "Material1 - Type"),
                     _first(row, "Material2", "Material 2", "Material2 - Type"))
        heat_columns = (("Material 1 - Heat Number", "Material1 - Heat Number"),
                        ("Material 2 - Heat Number", "Material2 - Heat Number"))
        heats = tuple(_first(row, *names) for names in heat_columns)
        # A welds export names the material and leaves the heat implicit in it
        # - but only where it has no heat column at all.  Where the column is
        # there and empty the heat is genuinely missing, and falling back would
        # read the product form out of `Material2` as a heat number called
        # PIPE: a joint that reports a heat it does not have, and a WT-07 that
        # can never fire on the export that needs it most.
        heats = tuple(
            heat or ("" if _has_column(row, *names) else heat_of(material))
            for heat, material, names in zip(heats, materials, heat_columns))

        welds.append(WeldRow(
            weld_number=number,
            tag=parse_tag(number),
            test_pack=test_pack,
            pack_reference=_first(row, "Test Pack Reference", "Weld Test Ref"),
            drawing=drawing,
            revision=revision,
            line=_first(row, "Line Number", "Line #", "Tag Number"),
            line_class=_first(row, "Line Class"),
            category=_first(row, "Category"),
            joint_type=_first(row, "Joint Type"),
            size=_first(row, "Weld Size (in)", "Weld in/mm"),
            wps=wps,
            wps_revision=wps_revision,
            welders={
                "root": split_ids(_first(row, "Welder ID Root", "Weld Id Root")),
                "fill": split_ids(_first(row, "Welder ID Fill", "Weld Id Fill")),
                "cap": split_ids(_first(row, "Welder ID Cap", "Weld Id Cap")),
            },
            date_planned=parse_date(_first(row, "Date Planned")),
            date_welded=parse_date(_first(row, "Date Welded")),
            materials=materials,
            heats=heats,
            exams=exams,
            result=_first(row, "Test Result", "Inspection Result"),
            penalty=_first(row, "Penalty"),
            row=row,
        ))
    return welds


def parse_material_register(path: str | Path) -> dict[str, HeatRow]:
    """Read a project materials export, keyed by heat as written."""
    out: dict[str, HeatRow] = {}
    for row in read_export(path):
        heat = _first(row, "Heat Number", "Heat #", "Heat")
        if not heat:
            continue
        out[heat] = HeatRow(
            heat=heat,
            name=_first(row, "Material Name", "Material"),
            product_form=_first(row, "Product Form"),
            fitting_type=_first(row, "Pipe Fitting Type"),
            supplier=_first(row, "Supplier", "Manufacturer"),
            spec_no=_first(row, "Spec No.", "Spec No", "Specification"),
            grade=_first(row, "Alloy Type or Grade", "Grade"),
            p_no=_first(row, "P-No.", "P-No", "P Number"),
            mtr_file=_first(row, "File Name", "MTR", "Document"),
            status=_first(row, "Status"),
            row=row,
        )
    return out


# ---------------------------------------------------------------------------
# The as-built stamps
# ---------------------------------------------------------------------------

_ANNOTATION_HEADER = re.compile(
    r"Annotation\s*-\s*Drawing:\s*(?P<drawing>[^;]*);\s*Rev:\s*(?P<rev>[^;]*);")

#: A welder stencil as it is stamped beside a weld tag.
_STENCIL = re.compile(r"^[A-Z]{2,4}\d{0,2}$")
#: A stamped date, including the malformed ones - ``8/222/2026`` is in the
#: sample, and is matched here so it can be rejected by the date parser rather
#: than read as part of some other field.
_STAMP_DATE = re.compile(r"^\d{1,3}/\d{1,3}/\d{2,4}$")


def parse_annotation_pdf(path: str | Path) -> list[Stamp]:
    """Scrape weld stamps off a Bluebeam annotation export, a page per drawing.

    Each page carries exactly one ``Annotation - Drawing:`` header naming the
    isometric, and below it that sheet's annotations in reading order: the
    weld tag, the welder who laid it, the date.  Parsing page by page is what
    makes that reliable - the header precedes the stamps it describes on its
    own page, so any reading that splits one continuous text stream on the
    header attributes every page's stamps to the *next* drawing.

    Heats are stamped on these sheets too, in a block of their own below the
    welds.  They are deliberately not paired to a weld here: pairing a callout
    to a joint needs the leader line rather than the reading order, and a
    wrong pairing would attach material to the wrong weld invisibly.
    """
    import pymupdf

    stamps: list[Stamp] = []
    with pymupdf.open(str(path)) as doc:
        for page_no, page in enumerate(doc, start=1):
            text = page.get_text()
            header = _ANNOTATION_HEADER.search(text)
            if not header:
                continue
            drawing = clean(header.group("drawing"))
            revision = clean(header.group("rev"))

            lines = [ln.strip() for ln in text[header.end():].splitlines() if ln.strip()]
            seen: set[str] = set()
            for i, line in enumerate(lines):
                tag = parse_tag(line)
                if tag is None or line.upper() in seen:
                    continue
                seen.add(line.upper())
                welder = date = ""
                for follower in lines[i + 1:i + 3]:
                    if not welder and _STENCIL.match(follower.upper()) \
                            and parse_tag(follower) is None:
                        welder = follower.upper()
                    elif not date and _STAMP_DATE.match(follower):
                        date = parse_date(follower)
                stamps.append(Stamp(drawing=drawing, revision=revision, tag=tag,
                                    raw_tag=line.upper(), welder=welder,
                                    date=date, page_no=page_no))
    return stamps


def parse_annotation_csv(path: str | Path) -> list[Stamp]:
    """The same stamps, where the download exported them as a table.

    The bubble text and the welder initials beneath it are one cell with the
    line break left in, which is why :func:`read_export` rejoins short rows.
    """
    stamps: list[Stamp] = []
    for row in read_export(path):
        kind = _first(row, "Type").lower()
        if kind and kind != "weld":
            continue                    # a Note bubble: a heat, not a joint
        bubble = _first(row, "Text inside bubble")
        text, _, under = bubble.partition("\n")
        tag = parse_tag(text) or parse_tag(_first(row, "Weld Number"))
        if tag is None:
            continue
        drawing = _first(row, "Drawing Number", "Drawing #")
        revision = _first(row, "Drawing Revision", "Drawing Rev")
        if not drawing:
            drawing, revision = split_trailing_revision(
                _first(row, "Drawing # - Revision"))
        welder = under.strip().upper()
        stamps.append(Stamp(
            drawing=drawing, revision=revision, tag=tag,
            raw_tag=(clean(text) or str(tag)).upper(),
            welder=welder if _STENCIL.match(welder) else "",
            date=parse_date(_first(row, "Date Welded", "Date Planned")),
            page_no=int(_first(row, "Sheet Number") or 0) or None,
        ))
    return stamps


# ---------------------------------------------------------------------------
# Matching the register against the drawings
# ---------------------------------------------------------------------------


def index_stamps(stamps: list[Stamp]) -> dict[WeldTag, list[Stamp]]:
    """``{tag: stamps}``, for matching a register against the drawings."""
    out: dict[WeldTag, list[Stamp]] = {}
    for stamp in stamps:
        out.setdefault(stamp.tag, []).append(stamp)
    return out


def match_stamps(weld: WeldRow, by_tag: dict[WeldTag, list[Stamp]]) -> list[Stamp]:
    """The stamps for one weld, an exact reading preferred over a looser one."""
    if weld.tag is None:
        return []
    for variant in weld.tag.variants:
        if variant in by_tag:
            return by_tag[variant]
    # The register may be the plain side and the stamp the prefixed one.
    for tag, found in sorted(by_tag.items()):
        if weld.tag in tag.variants:
            return found
    return []
