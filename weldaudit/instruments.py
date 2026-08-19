"""Calibration certificates for coating and test instruments, read off the filename.

Every gauge that produces a number on a coating or pressure test report has a
calibration certificate filed beside it, and the filename carries the serial —
often the calibration date too.  That makes the instrument inventory free:

    Positector 6000 SN 1073795 4.24.25.pdf   -> DFT gauge 1073795, cal 2025-04-24
    HOLIDAY DETECTOR - 12594.pdf             -> holiday detector 12594, no date
    1090338 Coating Thickness Gauge.pdf      -> DFT gauge 1090338, no date

Two conventions, mirroring the MTR filenames: Kestrel 8 writes the instrument then
``SN`` then the serial then the date; Bluewater writes the serial first or after a
dash, and never the date.  Both are parsed here; the date, where a filename
does not give one, has to come off the page.

What the instrument *is* matters as much as its serial, because the coating
report names its equipment by role ("Environmental Conditions Equipment SN")
rather than by model, and the join is only reliable once both sides are
reduced to the same four kinds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: The four roles a coating report names, and the model names that fill them.
#: Ordered most specific first - "Positector DPM" is a DPM, not a DFT gauge,
#: even though "Positector" alone usually means the 6000 thickness gauge.
_KINDS: tuple[tuple[str, str], ...] = (
    ("dpm", r"\bdpm\b|dew ?point|environmental (gauge|conditions|meter)"),
    ("profile_gauge", r"\bspg\b|profile gauge|testex|press.?o.?film|micrometer|"
                      r"dial thickness ga(u)?ge"),
    ("holiday_detector", r"holiday detector|jeep ?meter|\bjeep\b|holiday"),
    ("torque_wrench", r"torque wrench|torque ?multiplier"),
    ("dft_gauge", r"coating thickness|thickness (gauge|gage|instrument)|"
                  r"posi ?tector|\bdft\b|mil ?gauge"),
)
_COMPILED = tuple((kind, re.compile(pat, re.IGNORECASE)) for kind, pat in _KINDS)

#: Human names, for messages.
LABELS = {
    "dpm": "environmental / dew point meter",
    "profile_gauge": "surface profile gauge",
    "holiday_detector": "holiday detector",
    "torque_wrench": "torque wrench",
    "dft_gauge": "coating thickness gauge",
}

#: Bluewater files most of its torque wrench certificates under the flange
#: section named for nothing but the serial - `0322600192.pdf`,
#: `0918602082 (1).pdf`. There is no instrument word to match on, so the shape
#: of the name is the only signal, and it is only trusted inside a folder that
#: is already known to be about flanges.
_BARE_SERIAL = re.compile(r"^\s*(\d{8,12})\s*(?:\(\d+\))?\s*$")

#: `... SN 1073795 4.24.25` and `... SN.894206` - an explicit marker, which is
#: the strongest signal a filename can give.
_SN = re.compile(r"\bS(?:/?N|ERIAL)\b[\s.:#-]*([A-Z0-9][A-Z0-9-]{2,17})", re.IGNORECASE)

#: `HOLIDAY DETECTOR - 12594`, `PULSE JEEP METER 20KV - PJM-7292`.
_AFTER_DASH = re.compile(r"[-–]\s*([A-Z0-9][A-Z0-9-]{2,17})\s*$", re.IGNORECASE)

#: `1090338 Coating Thickness Gauge`, `DKFP76 TESTEX MICROMETER`.
_LEADING = re.compile(r"^\s*([A-Z0-9]{4,18})\s+(?=\S)", re.IGNORECASE)

#: `Environmental Gauge  1061323` - serial last, no marker.
_TRAILING = re.compile(r"\s([A-Z0-9]{4,18})\s*$", re.IGNORECASE)

_DATE = re.compile(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})\b")

#: Words that look like a serial to the patterns above but are not.  Model
#: numbers are the trap: "Positector 6000" and "PULSE JEEP METER 20KV" both
#: put a plausible token where a serial would sit.
_NOT_A_SERIAL = re.compile(
    r"^(6000|20KV|100|200|300|400|PART|REV|SDS|PDS|NEW|OLD|COPY|SCAN|"
    r"\d{1,3}|20\d\d)$", re.IGNORECASE
)

#: Every serial in the corpus carries a digit, and requiring one is what stops
#: the leading-token pattern reading `Environmental Gauge 1061323` as serial
#: "ENVIRONMENTAL" - the instrument's own name sits exactly where a
#: serial-first filename puts the serial.
_HAS_DIGIT = re.compile(r"\d")


@dataclass
class InstrumentIdentity:
    kind: str = ""
    serial: str = ""
    calibrated: str = ""
    description: str = ""
    confidence: str = "none"      # 'high' (explicit SN) | 'medium' | 'none'

    @property
    def serial_key(self) -> str:
        return serial_key(self.serial)

    @property
    def label(self) -> str:
        return LABELS.get(self.kind, "instrument")


def serial_key(serial: str) -> str:
    """Serials are written with and without punctuation and case."""
    return re.sub(r"[^A-Z0-9]", "", str(serial or "").upper())


def kind_of(text: str) -> str:
    """Which of the four instrument roles a piece of text names."""
    for kind, pat in _COMPILED:
        if pat.search(text or ""):
            return kind
    return ""


def parse_date(text: str) -> str:
    """ISO date from `4.22.25`, tolerating the stray digit in `5.19.254`."""
    from datetime import date as _date

    m = _DATE.search(text or "")
    if not m:
        return ""
    mm, dd, yy = m.group(1), m.group(2), m.group(3)
    if len(yy) == 3:            # `5.19.254` - a slip of the pen on one file
        yy = yy[:2]
    year = int(yy) + 2000 if len(yy) == 2 else int(yy)
    try:
        return _date(year, int(mm), int(dd)).isoformat()
    except ValueError:
        return ""


def _plausible(token: str) -> bool:
    return bool(_HAS_DIGIT.search(token)) and not _NOT_A_SERIAL.match(token)


def parse(filename: str) -> InstrumentIdentity:
    """Instrument kind, serial and calibration date from a certificate filename."""
    stem = re.sub(r"\.(pdf|jpe?g|png|tiff?)$", "", filename or "", flags=re.IGNORECASE)
    stem = re.sub(r"\s+", " ", stem).strip()

    kind = kind_of(stem)
    if not kind:
        return InstrumentIdentity()

    calibrated = parse_date(stem)
    # Strip the date before hunting for the serial, or `4.22.25` competes with
    # it for the trailing position.
    without_date = _DATE.sub(" ", stem).strip(" -–")

    serial, confidence = "", "none"
    if m := _SN.search(without_date):
        serial, confidence = m.group(1), "high"
    else:
        for pattern in (_AFTER_DASH, _LEADING, _TRAILING):
            m = pattern.search(without_date)
            if m and _plausible(m.group(1)):
                serial, confidence = m.group(1), "medium"
                break

    if serial and not _plausible(serial):
        serial, confidence = "", "none"

    return InstrumentIdentity(kind=kind, serial=serial.upper(), calibrated=calibrated,
                              description=stem, confidence=confidence)


def parse_bare_serial(filename: str) -> InstrumentIdentity:
    """A torque wrench certificate named only for its serial number.

    Used by the flange extractor, which already knows the document is filed in
    the flange section; the same filename anywhere else means nothing.  A
    trailing ``(1)`` is Windows' duplicate marker, not part of the serial.
    """
    stem = re.sub(r"\.(pdf|jpe?g|png|tiff?)$", "", filename or "", flags=re.IGNORECASE)
    named = parse(stem)
    if named.serial:
        return named
    if m := _BARE_SERIAL.match(stem):
        return InstrumentIdentity(kind="torque_wrench", serial=m.group(1),
                                  description=stem.strip(), confidence="medium")
    return InstrumentIdentity()


def looks_like_certificate(filename: str) -> bool:
    """Whether a filename names an instrument calibration certificate.

    Used to classify, so it deliberately needs both an instrument name and
    something serial-shaped: a coating *report* for a segment can mention a
    gauge in passing, and misfiling one as a certificate would lose a report.
    """
    identity = parse(filename)
    return bool(identity.kind and identity.serial)


def nearest_serials(serial: str, known: set[str]) -> list[str]:
    """Serials within one edit of this one, for near-miss reporting.

    `DIAL THICKNESS GAGE - BTYG12.pdf` is on file and the report writes
    `BTYGL2`. Calling that instrument uncalibrated would be a false critical
    over a hand-written 1 read as an L, so the near miss is named instead.
    """
    from rapidfuzz.distance import DamerauLevenshtein

    key = serial_key(serial)
    if not key:
        return []
    return sorted(
        other for other in known
        if other != key and DamerauLevenshtein.distance(key, other) <= 1
    )
