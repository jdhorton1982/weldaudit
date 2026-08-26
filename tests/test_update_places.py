"""Where a shared release folder is looked for.

The bug this exists to prevent: the folder is found only where the search
looks, and a folder that is not found is indistinguishable from a folder with
nothing new in it. No bar, no error, nothing in the log — the copy simply
never updates and nobody can tell why.

That is exactly what happened to the machine the releases are cut on. Its copy
of the shared folder sits in `OneDrive\\Applications`, one level below where
the search reached, so the publisher was the one person who could never take
their own update.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit import update  # noqa: E402


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """A home directory with a OneDrive in it, and nothing else real."""
    home = tmp_path / "home"
    (home / "OneDrive").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(update.sys, "executable", str(tmp_path / "nowhere" / "x.exe"))
    return home


def test_a_folder_at_the_top_of_onedrive_is_found(fake_home):
    where = fake_home / "OneDrive" / update.FOLDER
    where.mkdir()
    assert where in update.places()


def test_a_folder_filed_one_level_down_is_found(fake_home):
    # The regression. Nobody keeps a shared folder loose at the top of their
    # OneDrive; it gets filed with whatever it belongs with.
    where = fake_home / "OneDrive" / "Applications" / update.FOLDER
    where.mkdir(parents=True)
    assert where in update.places(), "a folder one level down is not searched"


@pytest.mark.parametrize("parent", ["Applications", "Documents", "Shared",
                                    "Work Stuff", "01 Tools"])
def test_it_does_not_matter_what_the_parent_is_called(fake_home, parent):
    where = fake_home / "OneDrive" / parent / update.FOLDER
    where.mkdir(parents=True)
    assert where in update.places()


def test_two_levels_down_is_not_searched(fake_home):
    # Deliberately bounded. One level covers how people actually file things;
    # walking a whole OneDrive on every start does not.
    deep = fake_home / "OneDrive" / "a" / "b" / update.FOLDER
    deep.mkdir(parents=True)
    assert deep not in update.places()


def test_a_second_onedrive_root_is_searched_too(fake_home):
    # A personal OneDrive beside a work one is the ordinary case.
    other = fake_home / "OneDrive - Some Company"
    (other / "Applications" / update.FOLDER).mkdir(parents=True)
    assert (other / "Applications" / update.FOLDER) in update.places()


def test_hidden_folders_are_skipped(fake_home):
    hidden = fake_home / "OneDrive" / ".git" / update.FOLDER
    hidden.mkdir(parents=True)
    assert hidden not in update.places()


def test_a_onedrive_that_cannot_be_listed_is_not_fatal(fake_home, monkeypatch):
    # A root that is signed out, or a drive that is mapped but not connected.
    def refuse(_path):
        raise OSError("not there")
    monkeypatch.setattr(update.os, "scandir", refuse)
    assert update.places()          # still returns the other candidates


def test_the_release_is_actually_read_from_one_level_down(fake_home):
    import json

    folder = fake_home / "OneDrive" / "Applications" / update.FOLDER
    folder.mkdir(parents=True)
    (folder / "WeldAudit-9.9.9.zip").write_bytes(b"x" * 10)
    (folder / update.MARKER).write_text(json.dumps({
        "version": "9.9.9", "notes": "", "file": "WeldAudit-9.9.9.zip",
        "sha256": "", "bytes": 10}))

    found = update.find_release()
    assert found is not None, "the release one level down was not found"
    assert found.version == "9.9.9"
    assert found.folder == folder


def test_no_duplicates_when_a_folder_matches_two_ways(fake_home):
    # `OneDrive` itself is a candidate, and so is every child of it. A folder
    # reachable both ways must not be searched twice.
    (fake_home / "OneDrive" / update.FOLDER).mkdir()
    found = update.places()
    lowered = [str(p).lower() for p in found]
    assert len(lowered) == len(set(lowered))
