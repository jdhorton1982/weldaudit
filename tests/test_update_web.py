"""Updating from a URL instead of a shared folder.

The shared folder is still how this is meant to work. The web is the fallback
for a machine the folder was never shared with, and it is off unless a URL is
configured -- because the build carries a customer's approved materials list,
and where it is published is a decision about hosting rather than about code.

Everything here is about the ways a download can be wrong. A truncated fetch
is the web's version of a half-synced OneDrive file, and it gets refused on
the same terms: the size it said, then the checksum it said, before anything
is unpacked.
"""

import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit import update  # noqa: E402

BASE = "https://example.invalid/weldaudit"


def build_zip(tmp_path: Path, version: str = "9.9.9") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("WeldAudit.exe", b"not really an exe")
        z.writestr(update.STAMP, version)
    return buf.getvalue()


def serve(monkeypatch, *, meta: dict, archive: bytes, truncate: int = 0):
    """Answer the two URLs the updater asks for, and nothing else."""
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()
            return False

    def opener(url, timeout=10):
        target = url.url if hasattr(url, "url") else url
        if target.endswith(update.MARKER):
            return Response(json.dumps(meta).encode())
        if target.endswith(meta.get("file", "nope.zip")):
            body = archive[:truncate] if truncate else archive
            return Response(body)
        raise OSError(f"404 {target}")

    monkeypatch.setattr(update, "_open", opener)


def meta_for(archive: bytes, version: str = "9.9.9", **over) -> dict:
    said = {
        "version": version,
        "notes": "a newer one",
        "file": f"WeldAudit-{version}.zip",
        "sha256": hashlib.sha256(archive).hexdigest(),
        "bytes": len(archive),
    }
    said.update(over)
    return said


# -- it is off until it is configured ---------------------------------------

def test_no_url_means_the_web_is_never_consulted(monkeypatch):
    monkeypatch.delenv(update.UPDATE_URL_VAR, raising=False)
    assert update.update_url() == ""
    assert update.read_release_url("") is None


def test_the_url_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv(update.UPDATE_URL_VAR, BASE + "/")
    assert update.update_url() == BASE          # trailing slash trimmed


def test_an_explicit_url_beats_the_environment(monkeypatch):
    monkeypatch.setenv(update.UPDATE_URL_VAR, "https://elsewhere.invalid")
    assert update.update_url(BASE) == BASE


# -- https, because version.json carries the checksum -----------------------

def test_plain_http_is_refused(monkeypatch):
    with pytest.raises(update.NotOffered) as bad:
        update.read_release_url("http://example.invalid/weldaudit")
    assert "https" in str(bad.value)


def test_a_file_path_is_not_a_release_url():
    with pytest.raises(update.NotOffered):
        update.read_release_url("ftp://example.invalid/weldaudit")


# -- reading what is on offer ----------------------------------------------

def test_a_published_release_is_read(monkeypatch, tmp_path):
    archive = build_zip(tmp_path)
    serve(monkeypatch, meta=meta_for(archive), archive=archive)

    offered = update.read_release_url(BASE)
    assert offered.version == "9.9.9"
    assert offered.from_the_web
    assert offered.ready                      # nothing to half-arrive yet
    assert offered.url.endswith("WeldAudit-9.9.9.zip")
    assert offered.where == offered.url


def test_a_host_that_is_down_is_not_an_error(monkeypatch):
    def opener(url, timeout=10):
        raise OSError("connection refused")
    monkeypatch.setattr(update, "_open", opener)
    assert update.read_release_url(BASE) is None


def test_an_archive_name_that_is_a_path_is_refused(monkeypatch, tmp_path):
    # version.json is fetched from the host being asked, but a name is still
    # not a path to obey.
    archive = build_zip(tmp_path)
    serve(monkeypatch, meta=meta_for(archive, file="../../../etc/passwd"),
          archive=archive)
    with pytest.raises(update.NotOffered):
        update.read_release_url(BASE)


# -- fetching it ------------------------------------------------------------

def test_a_good_download_is_kept(monkeypatch, tmp_path):
    archive = build_zip(tmp_path)
    serve(monkeypatch, meta=meta_for(archive), archive=archive)
    offered = update.read_release_url(BASE)

    seen = []
    got = update.fetch(offered, tmp_path / "dl", progress=lambda n, t: seen.append(n))
    assert got.read_bytes() == archive
    assert seen and seen[-1] == len(archive)


def test_a_truncated_download_is_refused_and_removed(monkeypatch, tmp_path):
    archive = build_zip(tmp_path)
    serve(monkeypatch, meta=meta_for(archive), archive=archive,
          truncate=len(archive) // 2)
    offered = update.read_release_url(BASE)

    with pytest.raises(update.NotWhatItSaid) as bad:
        update.fetch(offered, tmp_path / "dl")
    assert "stopped at" in str(bad.value)
    assert not list((tmp_path / "dl").glob("*.zip")), "the part-file was left behind"


def test_a_download_that_is_not_what_it_said_is_refused(monkeypatch, tmp_path):
    archive = build_zip(tmp_path)
    said = meta_for(archive)
    said["sha256"] = "0" * 64                 # the host serves something else
    serve(monkeypatch, meta=said, archive=archive)
    offered = update.read_release_url(BASE)

    with pytest.raises(update.NotWhatItSaid):
        update.fetch(offered, tmp_path / "dl")
    assert not list((tmp_path / "dl").glob("*.zip"))


def test_a_release_with_no_checksum_is_not_fetched(monkeypatch, tmp_path):
    archive = build_zip(tmp_path)
    serve(monkeypatch, meta=meta_for(archive, sha256=""), archive=archive)
    offered = update.read_release_url(BASE)

    with pytest.raises(update.NotOffered) as bad:
        update.fetch(offered, tmp_path / "dl")
    assert "checksum" in str(bad.value)


def test_a_download_far_larger_than_promised_is_stopped(monkeypatch, tmp_path):
    archive = build_zip(tmp_path)
    said = meta_for(archive)
    said["bytes"] = 10                        # the host serves far more
    serve(monkeypatch, meta=said, archive=archive + b"x" * (2 << 20))
    offered = update.read_release_url(BASE)

    with pytest.raises(update.NotWhatItSaid):
        update.fetch(offered, tmp_path / "dl")


# -- and installing it ------------------------------------------------------

def test_staging_a_web_release_downloads_it_first(monkeypatch, tmp_path):
    archive = build_zip(tmp_path)
    serve(monkeypatch, meta=meta_for(archive), archive=archive)
    offered = update.read_release_url(BASE)

    into = tmp_path / "staged"
    update.stage(offered, into)
    assert (into / "WeldAudit.exe").is_file()
    assert (into / update.STAMP).read_text().strip() == "9.9.9"


def test_a_folder_release_still_stages_without_touching_the_network(tmp_path, monkeypatch):
    def opener(url, timeout=10):
        raise AssertionError("the network was used for a folder release")
    monkeypatch.setattr(update, "_open", opener)

    folder = tmp_path / "rel"
    folder.mkdir()
    archive = build_zip(tmp_path)
    (folder / "WeldAudit-9.9.9.zip").write_bytes(archive)
    (folder / update.MARKER).write_text(json.dumps(meta_for(archive)))

    offered = update.read_release(folder)
    assert not offered.from_the_web
    update.stage(offered, tmp_path / "staged")
    assert (tmp_path / "staged" / "WeldAudit.exe").is_file()


# -- the folder wins ---------------------------------------------------------

def test_the_folder_is_preferred_when_it_has_the_same_release(monkeypatch, tmp_path):
    # Not a rule about precedence for its own sake: a copy that can see the
    # folder should never reach for the network at all.
    asked = []

    def opener(url, timeout=10):
        asked.append(url)
        raise OSError("should not have been asked")

    monkeypatch.setattr(update, "_open", opener)
    monkeypatch.setattr(update, "places", lambda extra=None: [tmp_path / "rel"])

    folder = tmp_path / "rel"
    folder.mkdir()
    archive = build_zip(tmp_path)
    (folder / "WeldAudit-9.9.9.zip").write_bytes(archive)
    (folder / update.MARKER).write_text(json.dumps(meta_for(archive)))

    found = update.find_release(url=BASE)
    assert found.version == "9.9.9"
    assert not found.from_the_web


def test_the_web_is_used_when_no_folder_has_anything(monkeypatch, tmp_path):
    archive = build_zip(tmp_path)
    serve(monkeypatch, meta=meta_for(archive), archive=archive)
    monkeypatch.setattr(update, "places", lambda extra=None: [tmp_path / "nothing"])

    found = update.find_release(url=BASE)
    assert found.from_the_web
    assert update.available(than="0.0.1", url=BASE) is not None
    assert update.available(than="9.9.9", url=BASE) is None


def test_the_startup_check_does_not_touch_the_network_when_the_folder_has_it(
        monkeypatch, tmp_path):
    """The claim in `available`, held to.

    Nearly every copy can see the shared folder. If asking "is there an
    update?" started making a request for those, a slow host would become a
    slow startup for people who were never going to use the web at all.
    """
    asked = []

    def opener(url, timeout=10):
        asked.append(url)
        raise OSError("should not have been asked")

    monkeypatch.setattr(update, "_open", opener)
    monkeypatch.setattr(update, "places", lambda extra=None: [tmp_path / "rel"])

    folder = tmp_path / "rel"
    folder.mkdir()
    archive = build_zip(tmp_path)
    (folder / "WeldAudit-9.9.9.zip").write_bytes(archive)
    (folder / update.MARKER).write_text(json.dumps(meta_for(archive)))

    offered = update.available(than="0.0.1", url=BASE)
    assert offered is not None and not offered.from_the_web
    assert asked == [], f"the network was contacted: {asked}"


def test_the_network_is_asked_when_the_folder_has_nothing_newer(monkeypatch, tmp_path):
    archive = build_zip(tmp_path)
    serve(monkeypatch, meta=meta_for(archive), archive=archive)
    monkeypatch.setattr(update, "places", lambda extra=None: [tmp_path / "empty"])

    offered = update.available(than="0.0.1", url=BASE)
    assert offered is not None and offered.from_the_web


def test_a_stale_folder_does_not_mask_a_newer_web_release(monkeypatch, tmp_path):
    # The folder offers 1.0.0, we are already on 2.0.0, the web has 9.9.9.
    archive = build_zip(tmp_path)
    serve(monkeypatch, meta=meta_for(archive), archive=archive)
    monkeypatch.setattr(update, "places", lambda extra=None: [tmp_path / "rel"])

    folder = tmp_path / "rel"
    folder.mkdir()
    old = build_zip(tmp_path, version="1.0.0")
    (folder / "WeldAudit-1.0.0.zip").write_bytes(old)
    (folder / update.MARKER).write_text(json.dumps(meta_for(old, version="1.0.0")))

    offered = update.available(than="2.0.0", url=BASE)
    assert offered is not None and offered.version == "9.9.9"


def test_the_api_names_where_a_web_release_came_from(monkeypatch, tmp_path):
    archive = build_zip(tmp_path)
    serve(monkeypatch, meta=meta_for(archive), archive=archive)
    offered = update.read_release_url(BASE)
    # `folder` is None for a web release, so anything rendering it directly
    # would show the string "None" to a person.
    assert offered.folder is None
    assert offered.where.startswith("https://")
