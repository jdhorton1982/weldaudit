"""A heat read one character out is still the same heat.

The melt-line rule turns on whether the mill's stated heat differs from the
certificate's. That comparison is only meaningful if it compares heats rather
than scan quality: a real certificate reported its own heat as 867985 against
367985 on the page, one digit out, and an exact test would have called that a
supply line and thrown away a producer the certificate does name.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.mtrname import same_heat_differently_read as same  # noqa: E402


@pytest.mark.parametrize("a,b", [
    ("367985", "367985"),          # identical
    ("867985", "367985"),          # the real misread, first digit
    ("367985", "367986"),          # last digit
    ("367-985", "367985"),         # punctuation only
    ("d32216284", "D32216284"),    # case only
    ("B3502111", "B3502I11"),      # O for zero, the classic scan error
])
def test_one_character_out_is_one_heat(a, b):
    assert same(a, b)


@pytest.mark.parametrize("a,b,why", [
    ("CN1G", "8410BB", "the steel supplier's heat on the Ryeburn flexolet"),
    ("MQ4233-29", "621766", "an origin certificate number, not a heat"),
    ("Z351324C31-2", "F293179", "a melt line on the TK elbow"),
    ("367985", "246974", "two characters apart is a different heat"),
    ("367985", "3679851", "a different length is a different heat"),
    ("", "367985", "nothing to compare"),
    ("367985", "", "nothing to compare"),
])
def test_genuinely_different_heats_stay_different(a, b, why):
    assert not same(a, b), why


def test_short_numbers_are_not_risked():
    """One character is most of a four-character heat, and the rolling from
    one mill runs 1234, 1235, 1236 — joining those hides real gaps."""
    assert not same("1234", "1235")
    assert same("12345", "12245")          # five is the floor


def test_it_is_symmetric():
    assert same("867985", "367985") == same("367985", "867985")
