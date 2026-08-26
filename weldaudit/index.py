"""Walk a project folder and classify every file.

This stage is deliberately cheap: no PDF is opened, nothing is hashed unless
asked for.  It exists so the auditor gets an instant map of the project - which
segments exist, which of the 22 sections are populated - before any extraction
runs.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator

from .db import Database
from .taxonomy import REQUIRED_SECTIONS, SECTIONS, kind_for, section_for, segment_for

#: Folders that never contain audit evidence.
SKIP_DIRS = {
    ".claude", ".git", "__pycache__", "weldaudit", "trash",
    "personal", "personal projects", "node_modules", ".weldaudit",
}

#: Extensions worth recording.
KEEP_EXT = {".pdf", ".xlsx", ".xls", ".xlsm", ".csv", ".docx", ".doc", ".dwg", ".txt"}

#: Junk that Windows/OneDrive leaves behind, plus the note that travels with
#: a WeldTrace download and with the shared release folder. "READ ME FIRST"
#: describes how WeldAudit updates itself; indexed as project evidence it is a
#: stray text file in section 22 that nothing reads, and it is one of the
#: things that makes the release folder look like a job folder.
SKIP_FILES = {"thumbs.db", "desktop.ini", ".ds_store", "read me first.txt"}


def is_junk(name: str) -> bool:
    """Whether a filename is an artefact rather than a document.

    ``._Foo.pdf`` is an AppleDouble resource fork: macOS writes one alongside
    every real file copied to a non-Mac filesystem. They carry the real file's
    name and extension but none of its content, so left in they masquerade as a
    second copy of the document - inflating shot counts and producing
    "two different sheets claim this shot" findings that are pure noise.
    """
    lower = name.lower()
    return lower in SKIP_FILES or name.startswith("._")


@dataclass
class IndexStats:
    files_seen: int = 0
    files_indexed: int = 0
    segments: int = 0
    bytes_indexed: int = 0


def sha1_of(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


#: How much of a file to read for the fingerprint.
_FP_HEAD = 512 * 1024
_FP_TAIL = 64 * 1024


def fingerprint_of(path: str | Path, size: int) -> str:
    """Cheap content fingerprint: size plus a hash of the head and tail.

    A full hash of 9 GB on every scan is not worth it.  Two PDFs that share a
    size, their first 512 KB and their last 64 KB are the same document for our
    purposes - and this runs at disk speed rather than CPU speed.
    """
    h = hashlib.sha1()
    h.update(str(size).encode())
    try:
        with open(path, "rb") as fh:
            h.update(fh.read(_FP_HEAD))
            if size > _FP_HEAD + _FP_TAIL:
                fh.seek(-_FP_TAIL, os.SEEK_END)
                h.update(fh.read(_FP_TAIL))
    except OSError:
        return f"unreadable:{size}:{Path(path).name}"
    return h.hexdigest()


def walk(root: str | Path) -> Iterator[Path]:
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if is_junk(fn):
                continue
            if Path(fn).suffix.lower() not in KEEP_EXT:
                continue
            yield Path(dirpath) / fn


def chosen_files(paths: Iterable[str | Path]) -> list[Path]:
    """Keep the ones worth recording, from a list somebody picked by hand.

    The same two filters ``walk`` applies, so choosing every file in a folder
    and choosing the folder come to the same thing. Junk and unreadable
    extensions go, and the order the dialog returned is kept.
    """
    out: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if is_junk(p.name) or p.suffix.lower() not in KEEP_EXT:
            continue
        if p.is_file():
            out.append(p)
    return out


def common_parent(paths: list[Path]) -> Path:
    """The folder a hand-picked set of files belongs to.

    It becomes the project root, which is what segments are measured against,
    so it has to be a real folder rather than the longest shared string.
    """
    if not paths:
        return Path.cwd()
    if len(paths) == 1:
        return paths[0].parent
    shared = os.path.commonpath([str(p.parent) for p in paths])
    return Path(shared)


def index_project(
    db: Database,
    name: str,
    root: str | Path,
    *,
    hash_files: bool = False,
    only: list[str | Path] | None = None,
    progress: Callable[[int, str], None] | None = None,
) -> tuple[int, IndexStats]:
    """Index ``root`` as project ``name``.  Returns ``(project_id, stats)``.

    ``only`` indexes exactly those files instead of walking the folder, for
    when somebody picked them out of a file dialog. They keep their real
    paths, so a file chosen out of ``BOOK\\7 MTRS`` is still filed under
    section 7 — picking every file in a package and picking the package
    itself give the same answer.
    """
    root = str(Path(root).resolve())
    project_id = db.upsert_project(name, root)
    stats = IndexStats()

    # Re-indexing invalidates everything derived from the old document rows, so
    # clear those first. Doing it explicitly (rather than leaning on cascade
    # rules) keeps the order correct on databases created by older versions.
    with db.tx() as c:
        c.execute(
            "DELETE FROM hydrotest_reading WHERE hydrotest_id IN "
            "(SELECT id FROM hydrotest WHERE project_id=?)", (project_id,))
        for child in ("coating_environment", "coating_profile", "coating_coat",
                      "coating_instrument"):
            c.execute(
                f"DELETE FROM {child} WHERE report_id IN "
                "(SELECT id FROM coating_report WHERE project_id=?)", (project_id,))
        # NB: `correction` and `finding_note` are deliberately absent from
        # this list. Everything else here can be rebuilt by reading the
        # documents again; a value a person typed cannot.
        for table in ("finding", "material", "installed_heat", "welder_pass",
                      "welder_cert", "nde_shot", "reader_sheet", "weld",
                      "vision_conflict",
                      "nde_tech", "aml_entry",
                      "hydrotest", "instrument_cal", "coating_report",
                      "flange", "flange_map", "procedure", "welder_roster", "backfill_release",
                      "asbuilt_joint", "weldtrace_exam", "weldtrace_weld",
                      "weldtrace_heat", "weldtrace_stamp"):
            c.execute(f"DELETE FROM {table} WHERE project_id=?", (project_id,))
        c.execute("DELETE FROM document WHERE project_id=?", (project_id,))

    batch: list[tuple] = []
    segments: set[str] = set()

    for path in (chosen_files(only) if only is not None else walk(root)):
        stats.files_seen += 1
        try:
            st = path.stat()
        except OSError:
            continue  # OneDrive placeholder that will not materialise

        spath = str(path)
        section = section_for(spath)
        segment = segment_for(root, spath)
        segments.add(segment)

        batch.append(
            (
                project_id,
                spath,
                path.name,
                path.suffix.lower(),
                st.st_size,
                datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds"),
                sha1_of(path) if hash_files else None,
                fingerprint_of(path, st.st_size),
                segment,
                section.number if section else None,
                section.name if section else None,
                kind_for(spath),
            )
        )
        stats.files_indexed += 1
        stats.bytes_indexed += st.st_size

        if progress and stats.files_indexed % 250 == 0:
            progress(stats.files_indexed, spath)

        if len(batch) >= 500:
            _flush(db, batch)
            batch.clear()

    _flush(db, batch)
    stats.segments = len(segments)

    with db.tx() as c:
        c.execute(
            "UPDATE project SET scanned_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), project_id),
        )
    return project_id, stats


def _flush(db: Database, batch: list[tuple]) -> None:
    if not batch:
        return
    with db.tx() as c:
        c.executemany(
            """INSERT OR REPLACE INTO document
               (project_id, path, filename, ext, size_bytes, modified_at, sha1,
                fingerprint, segment, section_no, section, kind)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            batch,
        )


# ---------------------------------------------------------------------------
# Completeness view
# ---------------------------------------------------------------------------


def completeness(db: Database, project_id: int) -> list[dict]:
    """Per-segment presence of each of the 22 book sections."""
    rows = db.q(
        """SELECT segment, section_no, COUNT(*) AS n
           FROM document WHERE project_id=? GROUP BY segment, section_no""",
        (project_id,),
    )
    by_segment: dict[str, dict[int | None, int]] = {}
    for r in rows:
        by_segment.setdefault(r["segment"], {})[r["section_no"]] = r["n"]

    out: list[dict] = []
    for segment, counts in sorted(by_segment.items()):
        present = {n for n, c in counts.items() if n is not None and c > 0}
        missing = [s for s in REQUIRED_SECTIONS if s.number not in present]
        out.append(
            {
                "segment": segment,
                "total_files": sum(counts.values()),
                "sections": {s.number: counts.get(s.number, 0) for s in SECTIONS},
                "missing_required": [f"{s.number} {s.name}" for s in missing],
                "pct_complete": round(
                    100 * (len(REQUIRED_SECTIONS) - len(missing)) / len(REQUIRED_SECTIONS)
                ),
            }
        )
    return out
