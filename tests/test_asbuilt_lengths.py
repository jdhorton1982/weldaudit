"""A LENGTH cell belongs to the pipe it measures, not the joint beside it.

The as-built writes each length *between* two stations, because that is what
it measures. Rounding it to the nearest joint block — right for every other
row — lands it one joint out whenever the cell sits closer to the station on
its right, and which side it sits nearer differs between drawings of the same
job: 16 PW writes it nearer the later station in 403 of 420 cases, 8 IN OIL
nearer the earlier one in all 85.

The cost was not a missing value but a wrong one. Every joint was credited
with the pipe arriving at it instead of the pipe leaving it, and AB-07 then
reported nine consistent stretches of 16 PW as contradicting themselves.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from weldaudit.asbuilt import parse_sheet  # noqa: E402


def _sheet(station_cols, length_cols, stations, lengths):
    """A band: HT#/JT#/DESC. to set the pitch, then STA and LENGTH rows."""
    width = max(max(station_cols), max(length_cols)) + 6
    def row(label, cols, values):
        r = [None] * width
        r[3] = label
        for c, v in zip(cols, values):
            r[c] = v
        return r
    # A band starts at a STA: row and runs to the next one, so every other
    # label sits below it. And the block pitch is measured from labels the
    # sheet repeats once per joint — HT#: appears in each block's own column,
    # unlike STA: which is written once at the left of its row.
    def labelled(label, values):
        r = [None] * width
        for c, v in zip(station_cols, values):
            r[c + 2] = label
            r[c + 3] = v
        return r
    n = len(station_cols)
    return [
        row("STA:", station_cols, stations),
        row("LENGTH:", length_cols, lengths),
        labelled("HT#:", [f"H{i}" for i in range(n)]),
        labelled("JT#:", [f"J{i}" for i in range(n)]),
        labelled("DESC.", ["ML"] * n),
    ]


def test_a_length_written_nearer_the_later_station():
    """As-Built 16 PW: stations at 4, 10, 16; lengths at 9, 15."""
    rows = _sheet([4, 10, 16], [9, 15], ["1+00", "1+39", "1+78"], [39, 39])
    joints = parse_sheet("As-Built (020)", rows).joints
    by_station = {j.station: j.length for j in joints}
    assert by_station["1+00"] == 39      # the pipe leaving 1+00
    assert by_station["1+39"] == 39
    assert by_station["1+78"] is None    # nothing recorded past the last joint


def test_a_length_written_nearer_the_earlier_station():
    """As-Built 8 IN OIL: lengths at 5, 11 rather than 9, 15."""
    rows = _sheet([4, 10, 16], [5, 11], ["1+00", "1+39", "1+78"], [39, 39])
    by_station = {j.station: j.length for j in parse_sheet("s", rows).joints}
    assert by_station["1+00"] == 39
    assert by_station["1+39"] == 39
    assert by_station["1+78"] is None


def test_the_pipe_leaving_each_joint_matches_the_step_to_the_next():
    """The property that makes AB-07 meaningful, on a drawing that agrees
    with itself: 112+71, 113+10, 113+23, 113+62 with 39, 15, 39 between."""
    rows = _sheet([4, 10, 16, 22], [9, 15, 21],
                  ["112+71", "113+10", "113+23", "113+62"], [39, 15, 39])
    joints = sorted(parse_sheet("s", rows).joints, key=lambda j: j.station_ft)
    assert len(joints) == 4, "the band was not read at all"
    for a, b in zip(joints, joints[1:]):
        step = b.station_ft - a.station_ft
        assert abs(step - a.length) <= 2, (a.station, b.station, a.length, step)


def test_a_length_left_of_every_station_goes_to_the_first_joint():
    rows = _sheet([10, 16], [8], ["1+00", "1+39"], [39])
    assert {j.station: j.length for j in parse_sheet("s", rows).joints}["1+00"] == 39


def test_lengths_are_read_at_all():
    """The guard against fixing the placement by dropping the value."""
    rows = _sheet([4, 10, 16], [9, 15], ["1+00", "1+39", "1+78"], [39, 39])
    got = [j.length for j in parse_sheet("s", rows).joints if j.length]
    assert len(got) == 2
