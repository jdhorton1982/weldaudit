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

from weldaudit import agreements  # noqa: E402


@pytest.fixture(autouse=True)
def no_agreements_unless_asked_for(tmp_path_factory, monkeypatch):
    """Point the agreement loader at an empty folder, for every test.

    A test that wants the gate armed overrides this by setting
    ``agreements.folder`` itself - see ``test_agreements.py``.
    """
    empty = tmp_path_factory.mktemp("no-agreements")
    monkeypatch.setattr(agreements, "folder", lambda: empty)
