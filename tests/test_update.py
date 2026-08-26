"""Updating from a shared folder.

The point of the design is that there is no server and no credential: one
person drops a build in a folder they have shared, and every other copy reads
it off disk. That keeps the approved list the program carries inside the
circle of people it was shared with, rather than on a public download.

Two things these tests exist to hold down.

**A half-arrived file must never be installed.** OneDrive presents a shared
file at its full size long before the bytes are local, and a truncated zip
reads short without raising. Half a program that starts would be far worse
than no update, so nothing is used unless its checksum matches.

**A malformed version can never look like an upgrade.** The comparison is
numeric, and anything unparseable sorts lowest.
"""

import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit import update  # noqa: E402
from weldaudit.update import (  # noqa: E402
    NotWhatItSaid, available, digest, is_newer, publish, read_release, stage,
)


@pytest.fixture(autouse=True)
def only_look_where_the_test_says(monkeypatch):
    """Keep the search off this machine's own folders.

    ``places`` deliberately sweeps every OneDrive and every removable drive,
    which is right in the program and wrong in a test: the moment a real
    release folder existed on the developer's machine, six tests asserting
    "nothing on offer" found 0.3.0 and failed. The same trap the readings
    cache fell into when a real .wacache appeared on a USB stick. A test must
    describe the program, not the desk it was written at.
    """
    monkeypatch.setattr(update, "places",
                        lambda extra=None: [Path(extra)] if extra else [])


@pytest.fixture
def build(tmp_path):
    """Something shaped like a folder build."""
    made = tmp_path / "dist" / "WeldAudit"
    (made / "_internal").mkdir(parents=True)
    (made / "WeldAudit.exe").write_bytes(b"MZ the program")
    (made / "_internal" / "python.dll").write_bytes(b"a library")
    return made


@pytest.fixture
def shared(tmp_path):
    return tmp_path / "WeldAudit Release"


# -- comparing versions ------------------------------------------------------

def test_a_higher_version_is_newer():
    assert is_newer("0.2.0", "0.1.0")


def test_ten_is_above_nine_not_below_it():
    """String comparison would put 1.10 below 1.9 and updates would stop."""
    assert is_newer("1.10.0", "1.9.0")


def test_the_same_version_is_not_newer():
    assert not is_newer("1.2.3", "1.2.3")


def test_an_older_version_is_not_newer():
    assert not is_newer("1.2.3", "1.3.0")


@pytest.mark.parametrize("junk", ["", "next", "latest", None])
def test_an_unparseable_version_never_looks_like_an_upgrade(junk):
    assert not is_newer(junk, "0.1.0")


# -- publishing --------------------------------------------------------------

def test_publishing_writes_an_archive_and_a_marker(build, shared):
    publish(build, shared, "0.2.0", notes="the PDF report")
    said = json.loads((shared / "version.json").read_text(encoding="utf-8"))
    assert said["version"] == "0.2.0"
    assert said["notes"] == "the PDF report"
    assert (shared / said["file"]).is_file()


def test_the_marker_records_a_checksum_that_matches(build, shared):
    """A version.json that disagrees with its archive is a broken update on
    somebody else's machine, so the two are written together."""
    publish(build, shared, "0.2.0")
    said = json.loads((shared / "version.json").read_text(encoding="utf-8"))
    archive = shared / said["file"]
    assert said["sha256"] == digest(archive)
    assert said["bytes"] == archive.stat().st_size


def test_publishing_again_clears_the_previous_archive(build, shared):
    """Old builds left behind are dead weight in everyone's sync."""
    publish(build, shared, "0.2.0")
    publish(build, shared, "0.3.0")
    assert sorted(p.name for p in shared.glob("*.zip")) == ["WeldAudit-0.3.0.zip"]


def test_the_archive_holds_the_build_without_a_wrapper_folder(build, shared):
    publish(build, shared, "0.2.0")
    with zipfile.ZipFile(shared / "WeldAudit-0.2.0.zip") as z:
        assert "WeldAudit.exe" in z.namelist()


# -- the release knows its own version ---------------------------------------
#
# Found by running the thing: a build compiled at 0.2.0 was published as
# 0.3.0. It installed correctly, and the installed copy still called itself
# 0.2.0 -- so it was offered the same update on every start, forever. The
# version now travels inside the archive.


def test_the_archive_carries_its_own_version(build, shared):
    publish(build, shared, "0.3.0")
    with zipfile.ZipFile(shared / "WeldAudit-0.3.0.zip") as z:
        assert z.read(update.STAMP).decode().strip() == "0.3.0"


def test_the_stamp_is_what_the_installed_copy_reports(build, shared, tmp_path,
                                                      monkeypatch):
    publish(build, shared, "0.3.0")
    into = stage(read_release(shared), tmp_path / "installed")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(into / "WeldAudit.exe"))
    assert update.current_version() == "0.3.0"


def test_installing_a_release_stops_it_being_offered_again(build, shared,
                                                           tmp_path, monkeypatch):
    """The loop this exists to prevent."""
    publish(build, shared, "0.3.0")
    into = stage(read_release(shared), tmp_path / "installed")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(into / "WeldAudit.exe"))
    assert available(shared) is None


def test_a_stale_stamp_from_the_previous_build_is_not_repackaged(build, shared):
    """Publishing from a folder that was itself installed from a release must
    stamp the new version, not carry the old one through."""
    (build / update.STAMP).write_text("0.1.0", encoding="utf-8")
    publish(build, shared, "0.5.0")
    with zipfile.ZipFile(shared / "WeldAudit-0.5.0.zip") as z:
        assert z.read(update.STAMP).decode().strip() == "0.5.0"
        assert z.namelist().count(update.STAMP) == 1


# -- what is on offer --------------------------------------------------------

def test_a_newer_release_is_offered(build, shared):
    publish(build, shared, "0.2.0")
    offered = available(shared, than="0.1.0")
    assert offered is not None
    assert offered.version == "0.2.0"


def test_our_own_version_is_not_offered(build, shared):
    publish(build, shared, "0.1.0")
    assert available(shared, than="0.1.0") is None


def test_an_older_release_is_not_offered(build, shared):
    publish(build, shared, "0.1.0")
    assert available(shared, than="0.9.0") is None


def test_a_folder_with_nothing_in_it_offers_nothing(tmp_path):
    empty = tmp_path / "WeldAudit Release"
    empty.mkdir()
    assert available(empty, than="0.1.0") is None


def test_a_folder_that_is_not_there_offers_nothing(tmp_path):
    assert available(tmp_path / "nowhere", than="0.1.0") is None


def test_a_corrupt_marker_offers_nothing(shared):
    shared.mkdir()
    (shared / "version.json").write_text("{not json", encoding="utf-8")
    assert read_release(shared) is None


def test_a_marker_naming_no_file_offers_nothing(shared):
    shared.mkdir()
    (shared / "version.json").write_text('{"version": "9.9.9"}', encoding="utf-8")
    assert read_release(shared) is None


# -- the half-synced file ----------------------------------------------------

def test_an_archive_that_has_not_finished_arriving_is_not_offered(build, shared):
    """The normal OneDrive case, not a rare one."""
    publish(build, shared, "0.2.0")
    archive = shared / "WeldAudit-0.2.0.zip"
    archive.write_bytes(archive.read_bytes()[:20])      # still syncing
    assert available(shared, than="0.1.0") is None


def test_a_missing_archive_is_not_offered(build, shared):
    publish(build, shared, "0.2.0")
    (shared / "WeldAudit-0.2.0.zip").unlink()
    assert available(shared, than="0.1.0") is None


def test_an_archive_that_does_not_match_its_checksum_is_refused(build, shared, tmp_path):
    """Right size, wrong bytes — so only the checksum can catch it."""
    publish(build, shared, "0.2.0")
    archive = shared / "WeldAudit-0.2.0.zip"
    was = archive.read_bytes()
    archive.write_bytes(b"X" * len(was))
    offered = read_release(shared)
    assert offered.ready, "the size still matches, which is the point"
    with pytest.raises(NotWhatItSaid):
        stage(offered, tmp_path / "staged")


def test_the_refusal_says_it_might_still_be_syncing(build, shared, tmp_path):
    publish(build, shared, "0.2.0")
    archive = shared / "WeldAudit-0.2.0.zip"
    archive.write_bytes(b"X" * archive.stat().st_size)
    with pytest.raises(NotWhatItSaid, match="syncing"):
        stage(read_release(shared), tmp_path / "staged")


# -- unpacking ---------------------------------------------------------------

def test_a_good_release_unpacks(build, shared, tmp_path):
    publish(build, shared, "0.2.0")
    into = stage(read_release(shared), tmp_path / "staged")
    assert (into / "WeldAudit.exe").read_bytes() == b"MZ the program"
    assert (into / "_internal" / "python.dll").is_file()


def test_staging_twice_does_not_mix_two_builds(build, shared, tmp_path):
    """A leftover file from an abandoned update would be part of the program."""
    publish(build, shared, "0.2.0")
    into = tmp_path / "staged"
    stage(read_release(shared), into)
    (into / "left-over.dll").write_bytes(b"from a previous attempt")
    stage(read_release(shared), into)
    assert not (into / "left-over.dll").exists()


def test_an_archive_reaching_outside_the_folder_is_refused(shared, tmp_path):
    """A shared folder is trusted; a zip inside it is still a container that
    can name any path it likes."""
    shared.mkdir()
    archive = shared / "WeldAudit-9.9.9.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("../../escaped.txt", "somewhere else")
    (shared / "version.json").write_text(json.dumps({
        "version": "9.9.9", "file": archive.name,
        "sha256": digest(archive), "bytes": archive.stat().st_size,
    }), encoding="utf-8")

    with pytest.raises(NotWhatItSaid, match="outside"):
        stage(read_release(shared), tmp_path / "staged")


# -- finding the folder ------------------------------------------------------

def test_the_named_folder_is_looked_at_first(build, tmp_path, monkeypatch):
    named = tmp_path / "somewhere else"
    publish(build, named, "0.2.0")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path / "home"))
    assert available(named, than="0.1.0").version == "0.2.0"


def test_a_onedrive_folder_is_searched(build, tmp_path, monkeypatch):
    """Where a shared folder lands once somebody adds it to their OneDrive."""
    home = tmp_path / "home"
    (home / "OneDrive").mkdir(parents=True)
    publish(build, home / "OneDrive" / update.FOLDER, "0.2.0")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setattr(update, "places",
                        lambda extra=None: [home / "OneDrive" / update.FOLDER])
    assert available(than="0.1.0").version == "0.2.0"


def test_the_newest_wins_when_two_folders_offer_one(build, tmp_path, monkeypatch):
    a, b = tmp_path / "a" / update.FOLDER, tmp_path / "b" / update.FOLDER
    publish(build, a, "0.2.0")
    publish(build, b, "0.4.0")
    monkeypatch.setattr(update, "places", lambda extra=None: [a, b])
    assert available(than="0.1.0").version == "0.4.0"


# -- the handoff -------------------------------------------------------------

def test_the_handoff_waits_for_us_before_touching_anything():
    """It runs while we are exiting; swapping the folder first would pull the
    ground out from under a process still shutting down."""
    script = update.handoff_script(Path(r"C:\x\WeldAudit.new"),
                                   Path(r"C:\x\WeldAudit"), 4321)
    assert script.index("Wait-Process -Id 4321") < script.index("Rename-Item")


def test_the_handoff_keeps_the_old_install_until_the_new_one_is_in_place():
    """If it fails between the two renames there is still a program on disk."""
    script = update.handoff_script(Path(r"C:\x\WeldAudit.new"),
                                   Path(r"C:\x\WeldAudit"), 1)
    moved_aside = script.index("Rename-Item $i $old")
    moved_in = script.index("Rename-Item $s $i")
    removed = script.rindex("Remove-Item $old -Recurse -Force -EA SilentlyContinue")
    assert moved_aside < moved_in < removed


def test_the_handoff_retries_a_folder_that_is_still_held():
    """Windows frees a folder a moment after the last handle closes, and a
    scanner can hold one for longer. Giving up on the first attempt would be
    an update that silently never happened."""
    script = update.handoff_script(Path(r"C:\x\WeldAudit.new"),
                                   Path(r"C:\x\WeldAudit"), 1)
    assert "1..30" in script and "Start-Sleep" in script


def test_the_handoff_restarts_the_old_program_if_it_cannot_swap():
    """The worst case must be an update that did not happen, never a machine
    left with no program on it at all."""
    script = update.handoff_script(Path(r"C:\x\WeldAudit.new"),
                                   Path(r"C:\x\WeldAudit"), 1)
    giving_up = script.index("if(-not $moved)")
    assert "Start-Process" in script[giving_up:giving_up + 200]
    assert script.index("Rename-Item $s $i") > giving_up


def test_the_handoff_starts_the_new_program():
    script = update.handoff_script(Path(r"C:\x\WeldAudit.new"),
                                   Path(r"C:\x\WeldAudit"), 1)
    assert "Start-Process" in script and "WeldAudit.exe" in script
