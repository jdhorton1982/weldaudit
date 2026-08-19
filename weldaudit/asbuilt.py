"""The as-built sheet's geometry, and stationing arithmetic.

An as-built is not a table with a row per joint — it is a drawing rendered
into a spreadsheet, one **column per joint**.  Each joint occupies a block of
six columns, and the facts about it are stacked vertically inside that block:

===============  ==========  ==========  ==========
row              joint 1     joint 2     joint 3
===============  ==========  ==========  ==========
``STA:``         0+02        0+42        0+82
``LENGTH:``      40.2        40.2        40.2
``HT#:``         1244878     1251573     1251573
``JT#:``         ...-1       ...-2       ...-3
``X-RAY #:``     CML-27      CML-26      CML-25
===============  ==========  ==========  ==========

Two things make reading it harder than that suggests.  A sheet holds several
**bands** of joints stacked down the page — `As-Built 8 IN OIL.xlsx` has ten —
so a band runs from one ``STA:`` row to the next.  And the values within a
block do not share a column: merged cells push the station two columns left of
the heat and the X-ray number two further still.  So a value is assigned to
the block it is *nearest* to rather than to a fixed offset, with the block
pitch measured from the ``HT#:`` labels the sheet itself lays down.

The stationing is the reason this matters beyond being a third weld register.
`130+00` is 13,000 feet along the line, and the release for backfill states
the length it covers in exactly those terms — so the as-built is the only
document in the corpus that can place a weld inside or outside a released
stretch of ditch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Row labels worth reading, mapped to field names. Matched on the stripped
#: cell text, which the template writes consistently across both jobs.
ROW_LABELS: dict[str, str] = {
    "STA:": "station",
    "LENGTH:": "length",
    "HT#:": "heat",
    "JT#:": "joint_no",
    "SIZE": "size",
    "DESC.": "description",
    "X-RAY #:": "xray",
}

#: Labels that mark the start of a band of joints.
_BAND_START = "STA:"

#: The labels laid out one per joint block, used to measure the block pitch.
_PITCH_LABELS = ("HT#:", "JT#:", "SIZE", "DESC.")

#: `0+02`, `130+00`, `1+22.5`. The part before the plus counts whole stations
#: of one hundred feet; the part after is feet along the current station.
_STATION = re.compile(r"^(\d{1,4})\s*\+\s*(\d{1,3}(?:\.\d+)?)$")

#: The blank template writes the station as underscores.
_BLANK = re.compile(r"^[_\s\-]*$")


def parse_station(text: str | None) -> float | None:
    """Feet along the line from a survey station, or None.

    ``130+00`` is 13,000 feet. Returns None for the blank ``____+____`` the
    unfilled template carries, which must not read as station zero.
    """
    raw = re.sub(r"\s+", "", str(text or ""))
    if not raw or _BLANK.match(raw):
        return None
    m = _STATION.match(raw)
    if not m:
        return None
    return int(m.group(1)) * 100 + float(m.group(2))


def format_station(feet: float | None) -> str:
    """``13000`` back to ``130+00``, for messages."""
    if feet is None:
        return ""
    whole, part = divmod(round(float(feet), 2), 100)
    return f"{int(whole)}+{part:05.2f}".rstrip("0").rstrip(".") if part % 1 \
        else f"{int(whole)}+{int(part):02d}"


def parse_length(text: str | None) -> float | None:
    """Feet from ``44.5'`` or ``40.2``."""
    raw = re.sub(r"[^\d.]", "", str(text or ""))
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


@dataclass
class Joint:
    sheet: str = ""
    band: int = 0
    seq: int = 0
    station: str = ""
    station_ft: float | None = None
    length: float | None = None
    heat: str = ""
    joint_no: str = ""
    size: str = ""
    description: str = ""
    xray: str = ""

    @property
    def real(self) -> bool:
        """Whether this block is a joint rather than an annotation.

        A block must carry a station or an X-ray number. The sheets open with
        a note reading "SEE ISO DRAWING" spread across two blocks, which
        otherwise arrives as two joints with a heat of ``SEE ISO`` and a joint
        number of ``DRAWING``.
        """
        return bool(self.station_ft is not None or self.xray)

    @property
    def key(self) -> tuple:
        """What makes two blocks the same physical joint.

        Station and X-ray only. The repeated copy at a band boundary is often
        the sparser one — station and X-ray but no heat or joint number — so
        including those would let it through as a second joint.
        """
        return (self.station_ft, self.xray.upper())

    @property
    def filled(self) -> int:
        return sum(1 for v in (self.station_ft, self.length, self.heat,
                               self.joint_no, self.size, self.xray) if v)


@dataclass
class Sheet:
    """One as-built sheet: its header block and the joints on it."""

    name: str = ""
    pipe_size: str = ""
    grade: str = ""
    wall: str = ""
    service: str = ""
    joints: list[Joint] = field(default_factory=list)


def _text(value) -> str:
    return re.sub(r"\s+", " ", str(value)).strip() if value is not None else ""


def _header(rows: list[list]) -> dict[str, str]:
    """PIPE SIZE / GRADE / WT / SERVICE, read from the strip above the joints.

    Each label is followed by its value a cell or two to the right, with the
    gap depending on how the template was merged, so the next non-empty cell
    is taken rather than a fixed offset.
    """
    wanted = {"PIPE SIZE:": "pipe_size", "GRADE:": "grade", "WT:": "wall",
              "SERVICE:": "service"}
    out: dict[str, str] = {}
    for row in rows[:12]:
        cells = [(j, _text(v)) for j, v in enumerate(row) if _text(v)]
        for k, (_j, text) in enumerate(cells):
            field_name = wanted.get(text.upper())
            if field_name and field_name not in out and k + 1 < len(cells):
                out[field_name] = cells[k + 1][1]
    return out


def _bands(rows: list[list]) -> list[tuple[int, int]]:
    """``(start, end)`` row indices for each band of joints on a sheet."""
    starts = [i for i, row in enumerate(rows)
              if any(_text(v) == _BAND_START for v in row)]
    return [(s, starts[i + 1] if i + 1 < len(starts) else len(rows))
            for i, s in enumerate(starts)]


def _pitch(rows: list[list]) -> tuple[int, int] | None:
    """``(first block column, columns per block)`` from the label layout."""
    for row in rows:
        columns = [j for j, v in enumerate(row) if _text(v) in _PITCH_LABELS]
        if len(columns) >= 2:
            gaps = [b - a for a, b in zip(columns, columns[1:])]
            step = min(gaps)
            if step >= 2:
                return columns[0], step
    return None



def _place_lengths(blocks, station_at, lengths, base, step) -> None:
    """Attach each LENGTH to the joint the pipe runs *from*.

    A LENGTH cell is not written in a joint's own column. It is written
    between two stations, because it measures the pipe between them — so the
    joint it belongs to is the one on its left.

    Rounding to the nearest block, which is right for every other row, cannot
    place it: the cell sits *exactly* midway between two blocks, and the
    nearest one is then a tie. Python breaks that tie to even, so a band's
    lengths come out at block 0, 2, 2, 4, 4 — every second value landing on
    the joint after the pipe it measures, and colliding with the one already
    there, which ``setdefault`` then discards. A sheet with a length against
    every joint was read as having one against every other joint, half of
    them belonging to the neighbour.

    Which side the cell leans is not even consistent between drawings of one
    job: `As-Built 16 PW` writes it nearer the later station in 403 of 420
    cases and `As-Built 8 IN OIL` nearer the earlier one in all 85. Asking
    which station it is *past* reads both; measuring how close it is reads
    neither.

    The cost was not a missing value but a wrong one. Joints were credited
    with the pipe arriving at them instead of the pipe leaving, and AB-07 —
    which compares a joint's length against the survey step to the next joint
    — reported nine consistent stretches of 16 PW as contradicting themselves.
    """
    columns = sorted(station_at.items(), key=lambda kv: kv[1])
    for column, text in lengths:
        before = [index for index, at in columns if at <= column]
        if before:
            index = before[-1]
        elif columns:
            index = columns[0][0]        # left of the first station on the band
        else:
            index = round((column - base) / step)   # no stations read; fall back
        if index >= 0:
            blocks.setdefault(index, {}).setdefault("length", text)


def parse_sheet(name: str, rows: list[list]) -> Sheet:
    """Every joint on one as-built sheet."""
    sheet = Sheet(name=name, **_header(rows))
    pitch = _pitch(rows)
    if not pitch:
        return sheet
    base, step = pitch

    for band_no, (start, end) in enumerate(_bands(rows), start=1):
        blocks: dict[int, dict[str, str]] = {}
        station_at: dict[int, int] = {}          # block index -> its column
        lengths: list[tuple[int, str]] = []      # (column, text), placed after

        for row in rows[start:end]:
            label = next((ROW_LABELS[_text(v)] for v in row
                          if _text(v) in ROW_LABELS), None)
            if not label:
                continue
            for column, value in enumerate(row):
                text = _text(value)
                if not text or text in ROW_LABELS:
                    continue
                if label == "length":
                    lengths.append((column, text))
                    continue
                index = round((column - base) / step)
                if index >= 0:
                    blocks.setdefault(index, {}).setdefault(label, text)
                    if label == "station":
                        station_at.setdefault(index, column)

        _place_lengths(blocks, station_at, lengths, base, step)

        for index in sorted(blocks):
            values = blocks[index]
            joint = Joint(
                sheet=name, band=band_no, seq=index,
                station=values.get("station", ""),
                station_ft=parse_station(values.get("station")),
                length=parse_length(values.get("length")),
                heat=values.get("heat", ""),
                joint_no=values.get("joint_no", ""),
                size=values.get("size", ""),
                description=values.get("description", ""),
                xray=values.get("xray", ""),
            )
            if joint.real:
                sheet.joints.append(joint)

    # Bands overlap by one joint: the last column of a band repeats as the
    # first of the next, because the drawing carries the tie-in station on
    # both. Keep whichever copy the sheet filled in more completely.
    best: dict[tuple, Joint] = {}
    for joint in sheet.joints:
        seen = best.get(joint.key)
        if seen is None or joint.filled > seen.filled:
            best[joint.key] = joint
    sheet.joints = sorted(
        best.values(),
        key=lambda j: (j.station_ft if j.station_ft is not None else 1e12,
                       j.band, j.seq),
    )
    return sheet
