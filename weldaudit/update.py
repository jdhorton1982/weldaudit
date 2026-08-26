"""Updating WeldAudit from a shared folder, instead of carrying a USB stick.

The arrangement is deliberately dull. One person puts a build in a folder and
shares that folder; every other copy of the program reads it and catches up.
There is no server, no account, no credential in the binary, and nothing is
downloaded from the internet -- the shared folder is an ordinary path on disk
that OneDrive (or a network share, or another USB stick) has already synced.

That matters more than convenience here. The program carries the approved
materials list inside it, so a public download would publish a customer's
document; a shared folder reaches exactly the people it was shared with.

**Nothing is ever taken on trust.** A half-synced OneDrive file is the normal
case, not the rare one -- it is a placeholder until the bytes arrive, and it
will happily open and read short. So a release is used only if its checksum
matches what the release file says it should be. A truncated update that
started would be far worse than no update.

The awkward part is Windows: a program cannot replace the folder it is
running from. So applying an update is handed to a short PowerShell script
that outlives us -- it waits for this process to exit, swaps the folders, and
starts the new one. See ``handoff``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path

#: The file a release folder is recognised by.
MARKER = "version.json"

#: What a release folder is called wherever it is looked for.
FOLDER = "WeldAudit Release"


#: Written into every release archive, and read back by the copy installed
#: from it. See ``current_version``.
STAMP = "weldaudit-version.txt"


def current_version() -> str:
    """Which release this copy is, preferring what the release said it was.

    ``__version__`` is compiled in, so it is whatever the source said on the
    day the build was made — and a release can be labelled something else.
    Found the hard way: a build compiled at 0.2.0 was published as 0.3.0, and
    the copy that installed it still called itself 0.2.0, so it was offered
    the same update on every start, forever.

    So a release stamps its version into the archive, and the installed copy
    reads that back. The compiled-in number is the fallback for a build
    nobody published.
    """
    from . import __version__

    if getattr(sys, "frozen", False):
        stamp = Path(sys.executable).parent / STAMP
        try:
            said = stamp.read_text(encoding="utf-8").strip()
            if said:
                return said
        except OSError:
            pass
    return __version__


def as_numbers(version: str) -> tuple:
    """"1.10.2" -> (1, 10, 2), so 1.10 sorts above 1.9 rather than below it.

    Anything unparseable sorts lowest, which means a malformed version can
    never present itself as an upgrade.
    """
    parts = re.findall(r"\d+", version or "")
    return tuple(int(p) for p in parts) if parts else (-1,)


def is_newer(offered: str, than: str) -> bool:
    return as_numbers(offered) > as_numbers(than)


@dataclass
class Release:
    version: str
    notes: str
    archive: Path | None       #: the zip holding the build, once it is local
    sha256: str
    size: int
    folder: Path | None
    #: Where the archive can be fetched from, for a release offered over the
    #: web rather than found on disk. Empty for a shared-folder release.
    url: str = ""

    @property
    def from_the_web(self) -> bool:
        return bool(self.url)

    @property
    def ready(self) -> bool:
        """Whether this release can be installed now.

        For a shared folder that means the bytes have actually arrived:
        OneDrive shows a file at its full size long before they have, so the
        size is a cheap first pass and the checksum settles it.

        A release offered over the web is ready by a different argument. There
        is nothing half-arrived to guard against because nothing has been
        fetched yet - :func:`fetch` downloads it and checks the same checksum
        before :func:`stage` will unpack anything.
        """
        if self.from_the_web:
            return True
        try:
            return bool(self.archive) and self.archive.is_file() \
                and self.archive.stat().st_size == self.size
        except OSError:
            return False

    @property
    def where(self) -> str:
        """Where this release came from, for a message meant for a person."""
        return self.url if self.from_the_web else str(self.folder or "")


def places(extra: str | Path | None = None) -> list[Path]:
    """Where a shared release folder might be, nearest and most likely first.

    The same shape as the readings-cache search, and for the same reason: the
    instructions say to copy the program to your Desktop, which leaves the
    shared folder wherever it was.
    """
    out: list[Path] = []
    if extra:
        out.append(Path(extra))

    here = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()
    home = Path.home()
    out += [here / FOLDER, here.parent / FOLDER]
    # OneDrive syncs the shared folder into the user's own tree, under
    # whatever the account is called.
    for one in sorted(home.glob("OneDrive*")):
        out += [one / FOLDER, one]
        # And one level down, because nobody keeps a shared folder loose at
        # the top of their OneDrive: it gets filed with whatever it belongs
        # with. The publisher's own copy sits in `OneDrive\Applications`,
        # which meant the machine that cuts the releases was the one machine
        # that could never take one — and the failure is silent, because a
        # folder that is not found is indistinguishable from a folder with
        # nothing new in it. No bar, no error, nothing in the log.
        #
        # One level, directories only. Measured on a OneDrive with seventeen
        # folders in it: about a millisecond, either way round. `scandir` is
        # used over `iterdir` because it answers from the listing rather than
        # stat-ing each entry, but the difference is half a millisecond and
        # not the reason to prefer it.
        #
        # The first call in a process can take a couple of seconds, and that
        # is the OneDrive tree being touched cold rather than anything here —
        # it costs the same however the folders are listed. Worth knowing
        # before someone tries to optimise this loop for it.
        try:
            with os.scandir(one) as listing:
                out += [Path(entry.path) / FOLDER for entry in listing
                        if not entry.name.startswith(".")
                        and entry.is_dir(follow_symlinks=False)]
        except OSError:                   # a root that is not there yet
            continue
    out += [home / FOLDER, home / "Desktop" / FOLDER, home / "Downloads" / FOLDER]

    import string

    for letter in string.ascii_uppercase[3:]:          # D: onwards
        root = Path(f"{letter}:/")
        try:
            if root.exists():
                out.append(root / FOLDER)
        except OSError:                   # mapped but not connected
            continue

    seen, unique = set(), []
    for place in out:
        key = str(place).lower()
        if key not in seen:
            seen.add(key)
            unique.append(place)
    return unique


def read_release(folder: str | Path) -> Release | None:
    """The release described by ``folder/version.json``, or None."""
    folder = Path(folder)
    marker = folder / MARKER
    try:
        said = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    name = said.get("file") or ""
    if not said.get("version") or not name:
        return None
    return Release(
        version=str(said["version"]),
        notes=str(said.get("notes") or ""),
        archive=folder / name,
        sha256=str(said.get("sha256") or ""),
        size=int(said.get("bytes") or 0),
        folder=folder,
    )


def find_release(extra: str | Path | None = None, url: str | None = None) -> Release | None:
    """The newest release on offer anywhere we know to look.

    Every folder, and then the URL if one is given - this reports the newest
    of them, and asking the network is its job when it is handed a URL.

    It is :func:`available` that decides whether to ask at all, and it asks
    the folders on their own first. The startup check goes through that one,
    so the copy that can see the shared folder still answers without a socket.
    """
    best: Release | None = None
    for place in places(extra):
        try:
            if not place.is_dir():
                continue
        except OSError:
            continue
        offered = read_release(place)
        if offered and (best is None or is_newer(offered.version, best.version)):
            best = offered

    where = update_url(url)
    if not where:
        return best
    try:
        offered = read_release_url(where)
    except NotOffered:
        raise
    except OSError:
        return best          # a host that is down is not an error worth raising
    if offered and (best is None or is_newer(offered.version, best.version)):
        best = offered
    return best


def available(extra: str | Path | None = None,
              than: str | None = None,
              url: str | None = None) -> Release | None:
    """A release worth installing: newer than us, and fully arrived.

    The folders are asked first and on their own. Only if they have nothing
    worth having does this reach for the network, so the copy that can see the
    shared folder - which is nearly every copy - answers the startup check
    without a socket, exactly as it did before there was a web fallback.
    """
    running = than or current_version()

    offered = find_release(extra)
    if offered and is_newer(offered.version, running) and offered.ready:
        return offered

    where = update_url(url)
    if not where:
        return None
    try:
        from_web = read_release_url(where)
    except OSError:
        return None
    if from_web is None or not is_newer(from_web.version, running):
        return None
    return from_web if from_web.ready else None


def digest(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


class NotWhatItSaid(Exception):
    """The archive did not match its checksum, so it was not used."""


# ---------------------------------------------------------------------------
# The same release, offered over the web
# ---------------------------------------------------------------------------
#
# The shared folder stays the way this is meant to work, and the reasons in
# this module's docstring have not changed: no server, no credential in the
# binary, and a folder that reaches exactly the people it was shared with.
#
# What the folder cannot do is reach somebody it was never shared with, or a
# machine where OneDrive is not signed in. For those, a release can also be
# published at a URL, and this is the fallback that finds it. Deliberately a
# fallback: an offline install keeps working, and a copy that can see the
# folder never touches the network.
#
# Two things are non-negotiable if it is used at all.
#
# `version.json` must come over HTTPS. It carries the checksum that vouches
# for the archive, so it is the one file that must not be rewritable in
# flight - fetch it over plain HTTP and an attacker supplies both the build
# and the hash that approves it.
#
# And whatever the URL serves is only as private as the host makes it. The
# program carries the approved materials list inside it, so a build published
# where anyone can fetch it publishes a customer's document. That is a
# decision about hosting rather than about this code, and this code will not
# make it silently: the feature is off until a URL is configured.

#: The environment variable naming the release URL. Unset means the web
#: fallback does not run at all, which is the default.
UPDATE_URL_VAR = "WELDAUDIT_UPDATE_URL"

#: How long to wait on the network before giving up and staying on this
#: version. An update that has not arrived is an ordinary Tuesday; a program
#: that will not start because a host is slow is a support call.
TIMEOUT = 10

#: Refuse an archive that claims to be wildly larger than the release said.
#: `version.json` states the size, so anything past it is either a mistake or
#: someone filling a disk.
_SIZE_SLACK = 1 << 20


class NotOffered(Exception):
    """The URL did not describe a release that could be used."""


def update_url(explicit: str | None = None) -> str:
    """The configured release URL, or ``''`` when there is none."""
    import os

    return (explicit or os.environ.get(UPDATE_URL_VAR) or "").strip().rstrip("/")


def _must_be_https(url: str) -> None:
    from urllib.parse import urlparse

    scheme = urlparse(url).scheme.lower()
    if scheme != "https":
        raise NotOffered(
            f"the release URL is {scheme or 'not a URL'} rather than https. "
            f"version.json carries the checksum that vouches for the build, so "
            f"it cannot be fetched over a connection somebody else can rewrite.")


def _open(url: str, timeout: int = TIMEOUT):
    import urllib.request

    request = urllib.request.Request(
        url, headers={"User-Agent": f"WeldAudit/{current_version()}"})
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 - https enforced


def read_release_url(base: str, timeout: int = TIMEOUT) -> Release | None:
    """The release described by ``<base>/version.json``, or None.

    Returns None for the ordinary "nothing published there" cases and raises
    :class:`NotOffered` only for a URL that should not be used at all, so a
    caller can stay quiet about a host being down and speak up about a host
    being http.
    """
    base = update_url(base)
    if not base:
        return None
    _must_be_https(base)

    try:
        with _open(f"{base}/{MARKER}", timeout) as response:
            said = json.loads(response.read(1 << 20).decode("utf-8"))
    except (OSError, ValueError):
        return None

    name = str(said.get("file") or "")
    if not said.get("version") or not name:
        return None
    # The archive is named by version.json, and version.json is fetched from
    # the host being asked - but a name is still not a path to obey. A "file"
    # of "../../etc/thing" would otherwise walk off the release URL.
    if "/" in name or "\\" in name or name.startswith("."):
        raise NotOffered(f"the release names its archive {name!r}, "
                         f"which is not a file in the release folder.")

    return Release(
        version=str(said["version"]),
        notes=str(said.get("notes") or ""),
        archive=None,
        sha256=str(said.get("sha256") or ""),
        size=int(said.get("bytes") or 0),
        folder=None,
        url=f"{base}/{name}",
    )


def fetch(release: Release, into: str | Path,
          progress=None, timeout: int = TIMEOUT) -> Path:
    """Download a web release's archive, and return where it landed.

    Checked before it is returned, on exactly the terms a shared-folder
    release is checked: the size it said, then the checksum it said. A
    download that stops halfway is the web's version of a half-synced file,
    and it is refused the same way rather than unpacked.

    ``progress`` is called with ``(bytes so far, total)`` so a window can show
    something during a 138 MB fetch.
    """
    if not release.from_the_web:
        raise NotOffered("this release is not one that is fetched")
    if not release.sha256:
        raise NotOffered("the release states no checksum, so a download of it "
                         "could not be checked before being installed.")
    _must_be_https(release.url)

    into = Path(into)
    into.mkdir(parents=True, exist_ok=True)
    target = into / Path(release.url).name

    cap = (release.size or 0) + _SIZE_SLACK
    got = 0
    with _open(release.url, timeout) as response, open(target, "wb") as out:
        while True:
            block = response.read(1 << 20)
            if not block:
                break
            got += len(block)
            if release.size and got > cap:
                out.close()
                target.unlink(missing_ok=True)
                raise NotWhatItSaid(
                    f"the download is larger than the {release.size:,} bytes "
                    f"the release said it would be, so it was stopped.")
            out.write(block)
            if progress:
                progress(got, release.size)

    if release.size and got != release.size:
        target.unlink(missing_ok=True)
        raise NotWhatItSaid(
            f"the download stopped at {got:,} of {release.size:,} bytes. "
            f"Nothing was installed; try again when the connection is better.")

    found = digest(target)
    if found != release.sha256.lower():
        target.unlink(missing_ok=True)
        raise NotWhatItSaid(
            f"what was downloaded is not the file the release describes "
            f"(expected {release.sha256[:12]}..., got {found[:12]}...). "
            f"Nothing was installed.")
    return target


def stage(release: Release, into: str | Path, progress=None) -> Path:
    """Unpack a verified release, and return the folder holding the new build.

    Verified first, always. A OneDrive file that is still syncing reads short
    without raising, and half a program that starts is worse than none.

    A release offered over the web is downloaded first, so that every caller
    applies both kinds the same way and neither can skip the checksum on its
    way in. It lands in a holding folder of its own rather than in ``into``,
    which is emptied before anything is unpacked into it - downloading there
    would delete the archive between fetching it and reading it.
    """
    import shutil
    import tempfile

    into = Path(into)
    holding: Path | None = None
    if release.from_the_web and release.archive is None:
        holding = Path(tempfile.mkdtemp(prefix="weldaudit-update-"))
        release = replace(release, archive=fetch(release, holding, progress=progress))

    try:
        if release.archive is None:
            raise NotWhatItSaid("this release has no archive to install.")

        if release.sha256:
            got = digest(release.archive)
            if got != release.sha256.lower():
                raise NotWhatItSaid(
                    f"{release.archive.name} is not the file the release describes "
                    f"(expected {release.sha256[:12]}..., got {got[:12]}...). "
                    f"If it is still syncing, try again once it has finished.")

        if into.exists():
            shutil.rmtree(into, ignore_errors=True)
        into.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(release.archive) as z:
            # A zip is an untrusted container even from a folder we trust: a
            # member named ..\..\something would otherwise be written outside.
            for member in z.namelist():
                target = (into / member).resolve()
                if not str(target).startswith(str(into.resolve())):
                    raise NotWhatItSaid(f"{release.archive.name} contains {member!r}, "
                                        f"which would write outside the folder")
            z.extractall(into)
        return into
    finally:
        if holding is not None:
            shutil.rmtree(holding, ignore_errors=True)


def publish(build: str | Path, into: str | Path, version: str,
            notes: str = "") -> Path:
    """Package a build into a release folder for everyone else to pick up.

    The other half of this file, and the half a person runs: zip the build,
    write down what it is and what it hashes to. Done here rather than by
    hand because a version.json that disagrees with its archive is a broken
    update on somebody else's machine.
    """
    build, into = Path(build), Path(into)
    into.mkdir(parents=True, exist_ok=True)
    archive = into / f"WeldAudit-{version}.zip"

    files = sorted(p for p in build.rglob("*") if p.is_file()
                   and p.name != STAMP) if build.is_dir() else [build]
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in files:
            z.write(p, p.relative_to(build) if build.is_dir() else p.name)
        # The release's own identity, travelling with it. Without this a
        # build compiled at one version and published as another installs
        # fine and then offers itself the same update forever.
        z.writestr(STAMP, version)

    (into / MARKER).write_text(json.dumps({
        "version": version,
        "notes": notes,
        "file": archive.name,
        "sha256": digest(archive),
        "bytes": archive.stat().st_size,
    }, indent=2), encoding="utf-8")

    # Older archives left in place would be dead weight in everyone's sync.
    for old in into.glob("WeldAudit-*.zip"):
        if old != archive:
            old.unlink(missing_ok=True)
    return archive


def install_dir() -> Path | None:
    """The folder this program runs from, if it is a build that can be swapped.

    None from source or from the one-file exe: a one-file build unpacks itself
    into a temporary directory, so there is no install folder to replace and
    updating it means replacing a single file somebody put wherever they liked.
    """
    if not getattr(sys, "frozen", False):
        return None
    here = Path(sys.executable).parent
    return here if (here / "_internal").is_dir() else None


def apply(release: Release) -> str:
    """Unpack the update and hand the swap to a process that outlives us.

    Returns the message to show before the window closes. Raises rather than
    half-applying: everything that can fail -- the checksum, the unpacking,
    the disk -- fails before anything on the live install is touched.
    """
    import os
    import subprocess

    install = install_dir()
    if install is None:
        raise NotWhatItSaid(
            "This copy cannot update itself. It is either the single-file "
            "build or running from source; the folder build is the one that "
            "updates in place.")

    staged = install.with_name(install.name + ".new")
    stage(release, staged)                 # verifies before it writes anything

    # -Command rather than -File, deliberately: a script *file* is subject to
    # PowerShell's execution policy, which on a managed laptop is set by group
    # policy and cannot be overridden from the command line. An inline command
    # runs whatever the policy says.
    #
    # The three streams are pinned to nul because this is a windowed build:
    # its own stdio handles are not real, and a child that inherits them can
    # fail to start at all. That is what went wrong the first time — the
    # script was correct and simply never ran.
    command = ["powershell", "-NoProfile", "-NonInteractive",
               "-WindowStyle", "Hidden", "-Command",
               handoff_script(staged, install, os.getpid())]
    quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                 stderr=subprocess.DEVNULL, close_fds=True,
                 # Never the folder about to be renamed: a process's current
                 # directory holds it open.
                 cwd=str(install.parent))
    # CREATE_NO_WINDOW, emphatically **not** DETACHED_PROCESS.
    #
    # Detached was the obvious choice and it silently did nothing: Popen
    # returned a pid, and PowerShell exited 0 without running a line of the
    # script -- it wants a console, and detached it has none. The update
    # simply never happened and there was nothing to read. Measured across
    # the flags: no flags, CREATE_NO_WINDOW and NO_WINDOW|BREAKAWAY all run;
    # anything with DETACHED_PROCESS does not.
    #
    # CREATE_BREAKAWAY_FROM_JOB is belt and braces: the packaged program runs
    # under the PyInstaller bootloader, and if that is ever placed in a job
    # object with kill-on-close, a child inside it would die with us halfway
    # through the swap. CreateProcess refuses the flag outright where a job
    # forbids breakaway, hence the fallback.
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
    try:
        subprocess.Popen(command, creationflags=no_window | breakaway, **quiet)
    except OSError:
        subprocess.Popen(command, creationflags=no_window, **quiet)
    return (f"Updating to {release.version}. WeldAudit will close and reopen "
            f"in a few seconds.")


def handoff_script(staged: Path, install: Path, pid: int) -> str:
    """PowerShell that swaps the folders once this process has gone.

    Windows will not let a program replace the directory it is running from,
    so the swap has to outlive us. PowerShell is used rather than a second
    executable because it is on every Windows machine, needs no rights, and
    adds nothing to the download -- and because a helper .exe would be one
    more unsigned binary for antivirus to object to.

    The old install is renamed rather than deleted first: if anything fails
    between the two renames, there is still a program on disk to put back.
    """
    log = install.parent / "weldaudit-update.log"
    return (
        f"$L='{log}';"
        f"function note($m){{try{{Add-Content -Path $L -Value "
        f"((Get-Date -Format 'HH:mm:ss')+' '+$m)}}catch{{}}}};"
        f"note 'handoff started, waiting for pid {pid}';"
        f"try{{Wait-Process -Id {pid} -Timeout 120}}catch{{}};"
        f"note 'the program has exited';"
        f"$i='{install}';$s='{staged}';$old=$i+'.old';"
        f"if(Test-Path $old){{Remove-Item $old -Recurse -Force -EA SilentlyContinue}};"
        # Windows releases a folder a moment after the last handle in it
        # closes, and an antivirus scanner can hold one open for longer than
        # that. Renaming is retried rather than attempted once, because the
        # cost of giving up here is an update that silently never happens.
        f"$moved=$false;"
        f"foreach($try in 1..30){{"
        f"try{{if(Test-Path $i){{Rename-Item $i $old -EA Stop}};$moved=$true;break}}"
        f"catch{{Start-Sleep -Milliseconds 500}}}};"
        # Never leave the machine with no program: if the old install could
        # not be moved aside, the new one is left staged and the old one is
        # started again, so the worst case is an update that did not happen.
        f"if(-not $moved){{"
        f"note 'could not move the old install aside; leaving it in place';"
        f"Start-Process -FilePath (Join-Path $i 'WeldAudit.exe') -WorkingDirectory $i;"
        f"exit 1}};"
        f"Rename-Item $s $i;"
        f"note 'swapped in the new build';"
        f"Remove-Item $old -Recurse -Force -EA SilentlyContinue;"
        f"note 'starting the new program';"
        f"Start-Process -FilePath (Join-Path $i 'WeldAudit.exe') -WorkingDirectory $i"
    )
