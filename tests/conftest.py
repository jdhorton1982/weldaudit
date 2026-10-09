"""Shared setup for the whole suite.

The agreements are read from ``weldaudit/data/agreements``, which is gitignored
- it holds commercial terms and this repository is public. So whether that
folder exists depends on the machine: it is there on the author's, and absent
in a fresh clone.

Left alone, that decides whether the audit gate is armed, and therefore
whether every test that posts to ``/api/audit`` passes. The suite would go
green on a clone and red on the machine the release is cut from, which is the
worst way round: the failure appears only where it blocks shipping, and looks
like a bug in whatever was last touched.

So every test runs against a build carrying no agreements, and the tests that
are about agreements point the loader at documents of their own.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit import agreements, site  # noqa: E402


@pytest.fixture(autouse=True)
def no_agreements_unless_asked_for(tmp_path_factory, monkeypatch):
    """Point the agreement loader at an empty folder, for every test.

    A test that wants the gate armed overrides this by setting
    ``agreements.folder`` itself - see ``test_agreements.py``.
    """
    empty = tmp_path_factory.mktemp("no-agreements")
    monkeypatch.setattr(agreements, "folder", lambda: empty)


#: The operator this suite pretends to work for. Every example in the tests
#: uses it, so the fixture below and the test data say the same thing.
TEST_SITE = {
    "operator": "ACME",
    "tag_prefixes": ["ACME", "ACM", "ACNE"],
    "job_codes": ["KST", "KSTMP"],
}


@pytest.fixture(autouse=True)
def a_known_operator(monkeypatch):
    """Give every test the same site configuration, whatever the machine has.

    ``weldaudit/data/site.json`` holds one operator's own tags and job codes,
    and it is gitignored for the same reason the agreements are: it is theirs,
    and this repository is public. So whether it exists, and what is in it,
    depends on the machine.

    Left alone that decides whether a tag is read as a heat number, which
    decides whether half the filename tests pass - green on the author's
    machine and red in a fresh clone, or the reverse. The suite pins its own
    operator instead, and the tests that are about the loader point it
    somewhere of their own.
    """
    monkeypatch.setattr(site, "_loaded", lambda: dict(TEST_SITE))
