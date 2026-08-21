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
import re
import sys
import zipfile
from dataclasses import dataclass
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
    archive: Path              #: the zip holding the build
    sha256: str
    size: int
    folder: Path

    @property
    def ready(self) -> bool:
        """Whether the archive has actually finished arriving.

        OneDrive shows a file at its full size long before the bytes are
        local, so the size is checked as a cheap first pass and the checksum
        settles it.
        """
        try:
            return self.archive.is_file() and self.archive.stat().st_size == self.size
        except OSError:
            return False


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


def find_release(extra: str | Path | None = None) -> Release | None:
    """The newest release on offer anywhere we know to look."""
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
    return best


def available(extra: str | Path | None = None,
              than: str | None = None) -> Release | None:
    """A release worth installing: newer than us, and fully arrived."""
    offered = find_release(extra)
    if offered is None:
        return None
    if not is_newer(offered.version, than or current_version()):
        return None
    return offered if offered.ready else None


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


def stage(release: Release, into: str | Path) -> Path:
    """Unpack a verified release, and return the folder holding the new build.

    Verified first, always. A OneDrive file that is still syncing reads short
    without raising, and half a program that starts is worse than none.
    """
    into = Path(into)
    if release.sha256:
        got = digest(release.archive)
        if got != release.sha256.lower():
            raise NotWhatItSaid(
                f"{release.archive.name} is not the file the release describes "
                f"(expected {release.sha256[:12]}..., got {got[:12]}...). "
                f"If it is still syncing, try again once it has finished.")

    if into.exists():
        import shutil

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
