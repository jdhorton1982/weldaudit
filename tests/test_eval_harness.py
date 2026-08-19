"""The scorer that decides whether a model is good enough to trust.

If this is wrong, every comparison it reports is wrong, so it is worth pinning
before anyone downloads six gigabytes of model on the strength of its output.
"""

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from eval.ground_truth import CRITICAL, PAGES  # noqa: E402
from eval.score import score_page  # noqa: E402

# The scorer is ours; the pages it scores are a customer's documents and live
# outside the repository. Without them there is nothing to score, so the whole
# file stands down rather than failing a fresh checkout.
pytestmark = pytest.mark.skipif(not PAGES, reason='no page corpus in private/')

CERT = next((p for p in PAGES if p["kind"] == "welder_cert"), None)


def counts(entry, payload):
    got = score_page(entry, payload)
    return {k: v for k, v in got.items() if k != "misses"}


def test_every_document_kind_is_covered():
    # Eight kinds of scanned document, eight kinds in the set — otherwise a
    # model could be adopted on evidence that never touched the form it will
    # do the most damage on.
    assert {p["kind"] for p in PAGES} == set(CRITICAL)


def test_a_perfect_reading_scores_perfectly():
    got = counts(CERT, copy.deepcopy(CERT["expected"]))
    assert got["wrong"] == 0 and got["blank"] == 0
    assert got["crit_wrong"] == 0 and got["crit_blank"] == 0


def test_blank_is_counted_apart_from_wrong():
    # A model that declines to read can be flagged for a human. One that
    # invents a plausible value cannot, which is why these never merge.
    blank = {k: (v if isinstance(v, (list, bool)) else None)
             for k, v in CERT["expected"].items()}
    got = counts(CERT, blank)
    assert got["wrong"] == 0 and got["blank"] > 0
    assert got["crit_wrong"] == 0 and got["crit_blank"] == 5


def test_a_wrong_critical_field_is_reported_as_such():
    wrong = copy.deepcopy(CERT["expected"])
    wrong["result"] = "FAIL"
    wrong["qual_position"] = "5G"        # the as-tested value, not the range
    got = counts(CERT, wrong)
    assert got["crit_wrong"] == 2


def test_punctuation_is_not_a_mistake():
    loose = copy.deepcopy(CERT["expected"])
    loose["test_date"] = "11-14-2024"
    loose["stencil"] = " aea "
    assert counts(CERT, loose)["crit_wrong"] == 0


def test_a_missing_row_is_counted_not_skipped():
    # Reading nine of thirty rows must not look like a perfect score on nine.
    sheet = next(p for p in PAGES if p["document"].startswith("DTD22 NDE 5.29"))
    short = copy.deepcopy(sheet["expected"])
    short["rows"] = short["rows"][:9]
    got = counts(sheet, short)
    assert got["blank"] > 0


def test_an_invented_reject_is_the_worst_case():
    # The failure the whole exercise exists to catch.
    sheet = next(p for p in PAGES if p["document"].startswith("DTD22 NDE 6.30"))
    flipped = copy.deepcopy(sheet["expected"])
    flipped["rows"][0]["result"] = "REJ"
    assert counts(sheet, flipped)["crit_wrong"] == 1


@pytest.mark.parametrize("entry", PAGES, ids=lambda e: e["document"][:30])
def test_each_page_declares_why_it_is_in_the_set(entry):
    assert entry["why"] and entry["kind"] in CRITICAL
    assert isinstance(entry["page"], int) and entry["page"] >= 0
