"""Local HTTP API behind the desktop UI.

Binds to localhost only.  Audits run on a background thread so the browser can
poll progress rather than holding a request open for a minute.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from .db import Database
from .index import completeness
from .pipeline import run
from .report import write_csv, write_excel
from .rules import registry
from .rules.coating import report_summary
from .rules.flanges import flange_summary
from .rules.hydrotest import test_summary
from .rules.asbuilt import asbuilt_summary
from .rules.backfill import release_summary
from .rules.roster import roster_summary
from .rules.wps import procedure_summary
from .rules.nde_coverage import coverage_summary

def _web_dir() -> Path:
    """Where index.html lives, running from source or from the packaged exe.

    PyInstaller unpacks a one-file build into a temporary directory and points
    ``sys._MEIPASS`` at it. The page is the whole user interface, so getting
    this wrong means the exe starts, serves, and shows nothing.
    """
    bundled = getattr(sys, "_MEIPASS", None)
    return Path(bundled) / "weldaudit" / "web" if bundled else Path(__file__).parent / "web"


WEB = _web_dir()


class CorrectionRequest(BaseModel):
    project_id: int
    document_id: int
    field: str = "manufacturer"
    value: str | None = None
    note: str = ""


class AuditRequest(BaseModel):
    name: str | None = None
    root: str


class StatusUpdate(BaseModel):
    status: str
    note: str | None = None


class RenameRequest(BaseModel):
    name: str


class OcrRequest(BaseModel):
    project_id: int


class ImportRequest(BaseModel):
    path: str


class _Job:
    """State of the currently running audit, polled by the UI."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.stage = ""
        self.message = ""
        self.error: str | None = None
        self.project_id: int | None = None

    def set(self, **kw: Any) -> None:
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "running": self.running, "stage": self.stage,
                "message": self.message, "error": self.error,
                "project_id": self.project_id,
            }



#: Where a transfer file might sit. Beside the exe is the obvious place and
#: the one that fails most often: the instructions say to copy the program to
#: your Desktop, which leaves the readings behind on the stick it came from.
#: So the stick is looked at too, and the places a file lands when somebody
#: copies one off it.
def _cache_places():
    import string
    import sys

    here = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()
    home = Path.home()
    places = [here, Path.cwd(), home / "Desktop", home / "Downloads", home / ".weldaudit"]
    # Any drive with a WeldAudit folder on it — where the stick keeps its copy.
    for letter in string.ascii_uppercase[3:]:          # D: onwards; C: is covered
        root = Path(f"{letter}:/")
        try:
            if root.exists():
                places += [root / "WeldAudit", root]
        except OSError:                   # a drive that is mapped but not there
            continue
    seen, out = set(), []
    for place in places:
        key = str(place).lower()
        if key not in seen:
            seen.add(key)
            out.append(place)
    return out


def _a_cache_to_offer() -> dict:
    """The largest transfer file lying around, and how much is in it."""
    best, most = None, 0
    for place in _cache_places():
        try:
            candidates = sorted(place.glob("*.wacache"))
        except OSError:
            continue
        for candidate in candidates:
            try:
                probe = sqlite3.connect(f"file:{candidate}?mode=ro", uri=True)
                n = probe.execute("SELECT COUNT(*) FROM ocr_cache").fetchone()[0]
                probe.close()
            except Exception:             # noqa: BLE001 - not a cache; ignore
                continue
            if n > most:
                best, most = str(candidate), n
    return {"offer": best, "offered": most}


def create_app(db_path: str | Path) -> FastAPI:
    app = FastAPI(title="WeldAudit", docs_url=None, redoc_url=None)
    db = Database(db_path)
    job = _Job()

    # -- pages -------------------------------------------------------------

    @app.get("/logo.svg")
    def logo() -> FileResponse:
        """The mark, for the tab icon. Inline in the page as well, because the
        header needs the theme's colours applied to it and a linked image
        cannot see them."""
        return FileResponse(WEB / "logo.svg", media_type="image/svg+xml")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (WEB / "index.html").read_text(encoding="utf-8")

    # -- projects and audits ----------------------------------------------

    @app.get("/api/projects")
    def projects() -> list[dict]:
        out = []
        for p in db.projects():
            counts = db.one(
                """SELECT
                     SUM(severity='critical') critical, SUM(severity='major') major,
                     SUM(severity='minor') minor, SUM(severity='info') info,
                     COUNT(*) total
                   FROM finding WHERE project_id=?""",
                (p["id"],),
            )
            stored = db.stored_for(p["id"])
            out.append(
                {
                    "id": p["id"], "name": p["name"], "root": p["root"],
                    "scanned_at": p["scanned_at"],
                    "counts": {k: (counts[k] or 0) for k in
                               ("critical", "major", "minor", "info", "total")},
                    "documents": stored.get("document", 0),
                    # Values a person read off a page and typed in. These are
                    # the only thing here that re-running the audit cannot
                    # reproduce, so deleting a job has to say how many go.
                    "typed_by_hand": (stored.get("correction", 0)
                                      + stored.get("vendor_reading", 0)),
                    # A job whose folder has been moved or archived is the
                    # likeliest thing somebody means by "old".
                    "folder_here": Path(p["root"]).is_dir(),
                }
            )
        return out

    @app.post("/api/projects/{project_id}/name")
    def rename_project(project_id: int, body: RenameRequest) -> dict:
        """Rename an audit.

        Names stopped changing on their own when the folder became an audit's
        identity, so this is the only way to change one.
        """
        try:
            return {"name": db.rename_project(project_id, body.name)}
        except LookupError:
            raise HTTPException(404, "no such audit") from None
        except ValueError as bad:
            raise HTTPException(400, str(bad)) from None

    @app.delete("/api/projects/{project_id}")
    def forget_project(project_id: int) -> dict:
        """Remove a stored audit. The job folder itself is never touched.

        Audits accumulate — one per folder ever pointed at, including the same
        job indexed twice under two names — and every one of them sits in the
        dropdown and in the database forever. There was no way to remove one
        short of deleting the whole database, which would also throw away the
        page readings that were paid for.
        """
        row = db.one("SELECT name, root FROM project WHERE id=?", (project_id,))
        if row is None:
            raise HTTPException(404, "no such audit")
        removed = db.delete_project(project_id)
        return {
            "name": row["name"], "root": row["root"],
            "findings": removed.get("finding", 0),
            "documents": removed.get("document", 0),
            "typed_by_hand": (removed.get("correction", 0)
                              + removed.get("vendor_reading", 0)),
            # Said out loud because it is the thing worth knowing before
            # pressing this: the expensive part is keyed by file hash, not by
            # job, so auditing the same folder again re-reads nothing.
            "cached_pages_kept": db.cached_pages(),
        }

    @app.post("/api/audit")
    def start_audit(req: AuditRequest) -> dict:
        root = Path(req.root).expanduser()
        if not root.is_dir():
            raise HTTPException(400, f"Not a folder: {root}")
        if job.running:
            raise HTTPException(409, "An audit is already running")

        name = req.name or root.name

        def work() -> None:
            job.set(running=True, error=None, stage="starting", message="", project_id=None)
            try:
                result = run(
                    db, name, root,
                    progress=lambda stage, msg: job.set(stage=stage, message=msg),
                )
                job.set(project_id=result.project_id, stage="done",
                        message=f"{len(result.findings):,} findings")
            except Exception:
                job.set(error=traceback.format_exc(limit=3), stage="failed")
            finally:
                job.set(running=False)

        threading.Thread(target=work, daemon=True).start()
        return {"started": True, "name": name}

    @app.get("/api/readings")
    def readings() -> dict:
        """How many page readings this machine has, and any it could load.

        A reading is the expensive part and it does not travel with the exe:
        it lives in the user's own profile. So a colleague handed the program
        and pointed at the same folder reads nothing off the scans, and their
        report comes back missing every approved-manufacturer finding — three
        of them critical on one real line. Shorter, and blinder.

        The transfer file existed before this did; it was just only reachable
        from a command line, on machines where nobody opens one. So the page
        asks here on load and offers to load what it finds beside the program.
        """
        return {"cached": db.cached_pages(), **_a_cache_to_offer()}


    @app.post("/api/readings/import")
    def load_readings(body: ImportRequest) -> dict:
        try:
            return db.import_cache(body.path)
        except FileNotFoundError:
            raise HTTPException(404, f"No such file: {body.path}") from None
        except ValueError as bad:
            raise HTTPException(400, str(bad)) from None

    @app.post("/api/ocr")
    def start_ocr(req: OcrRequest) -> dict:
        """Read this job's scanned certificates on this machine, then re-audit.

        Kept off the audit path because it is slow — seconds a page — but it
        has to be reachable without a command line, and until now it was not.
        The consequence was measurable: the same job audited on a machine that
        had never run it came back with 2 manufacturers named out of 495
        material rows instead of 28 of 37, and therefore with none of the
        approved-manufacturer findings at all. Fewer findings, blinder audit.

        The re-audit at the end is not a convenience. A reading that has been
        taken but not folded in changes nothing anybody can see, and leaving
        the user to know that is how the gap opened in the first place.
        """
        from .extract import mtrocr
        from .rules.materials import _aml_from_db

        project = db.one("SELECT id, name, root FROM project WHERE id=?",
                         (req.project_id,))
        if project is None:
            raise HTTPException(404, "no such audit")
        if job.running:
            raise HTTPException(409, "Something is already running")

        ready, why = mtrocr.available()
        if not ready:
            raise HTTPException(400, why)
        if _aml_from_db(db, project["id"]) is None:
            raise HTTPException(
                400, "No approved materials list is loaded for this job, so a "
                     "scanned name could not be confirmed against anything. "
                     "Put the AML where the audit can find it and run again.")

        def work() -> None:
            job.set(running=True, error=None, stage="reading", message="",
                    project_id=project["id"])
            try:
                aml = _aml_from_db(db, project["id"])
                def progress(i: int, total: int, name: str) -> None:
                    job.set(stage="reading scans",
                            message=f"{i} of {total} — {name[:48]}")
                done = mtrocr.read_scans(db, project["id"], aml, None, progress)
                job.set(stage="re-auditing",
                        message=f"folding in {done['named']:,} names")
                result = run(db, project["name"], project["root"],
                             progress=lambda stage, msg: job.set(stage=stage,
                                                                 message=msg))
                job.set(project_id=result.project_id, stage="done",
                        message=(f"read {done['read']:,} scans, named "
                                 f"{done['named']:,}; {len(result.findings):,} findings"))
            except Exception:
                job.set(error=traceback.format_exc(limit=3), stage="failed")
            finally:
                job.set(running=False)

        threading.Thread(target=work, daemon=True).start()
        return {"started": True}

    @app.get("/api/status")
    def status() -> dict:
        return job.snapshot()

    # -- findings ----------------------------------------------------------

    @app.get("/api/findings")
    def findings(
        project_id: int,
        severity: str | None = None,
        rule: str | None = None,
        segment: str | None = None,
        status: str | None = None,
        q: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> dict:
        where = ["f.project_id = ?"]
        params: list[Any] = [project_id]
        for col, val in (("f.severity", severity), ("f.rule", rule),
                         ("f.segment", segment), ("f.status", status)):
            if val:
                where.append(f"{col} = ?")
                params.append(val)
        if q:
            where.append("(f.message LIKE ? OR f.subject LIKE ? OR f.segment LIKE ?)")
            params += [f"%{q}%"] * 3
        clause = " AND ".join(where)

        total = db.one(f"SELECT COUNT(*) n FROM finding f WHERE {clause}", tuple(params))["n"]
        rows = db.q(
            f"""SELECT f.*, d.path AS doc_path, d.filename
                FROM finding f LEFT JOIN document d ON d.id = f.document_id
                WHERE {clause}
                ORDER BY CASE f.severity WHEN 'critical' THEN 0 WHEN 'major' THEN 1
                                         WHEN 'minor' THEN 2 ELSE 3 END,
                         f.segment, f.rule, f.subject
                LIMIT ? OFFSET ?""",
            tuple(params) + (limit, offset),
        )
        return {"total": total, "rows": [dict(r) for r in rows]}

    @app.get("/api/facets")
    def facets(project_id: int) -> dict:
        def group(col: str) -> list[dict]:
            return [
                {"value": r[col], "n": r["n"]}
                for r in db.q(
                    f"SELECT {col}, COUNT(*) n FROM finding WHERE project_id=? "
                    f"GROUP BY {col} ORDER BY n DESC",
                    (project_id,),
                )
                if r[col]
            ]

        return {
            "severity": group("severity"),
            "rule": group("rule"),
            "segment": group("segment"),
            "status": group("status"),
            "rule_titles": {code: title for code, (title, _) in registry().items()},
        }

    @app.post("/api/findings/{finding_id}/status")
    def set_status(finding_id: int, body: StatusUpdate) -> dict:
        """Accept, dismiss, or put a finding back on the list.

        Reopening matters as much as closing. Accepting a finding is one click
        next to a dismiss button, the row leaves the default view the instant
        it is pressed, and the thing that just disappeared may have been the
        critical one. Without a way back, the safe response to a mis-click is
        to re-run the whole audit.
        """
        if body.status not in ("open", "accepted", "dismissed"):
            raise HTTPException(400, "bad status")
        row = db.one("SELECT status FROM finding WHERE id=?", (finding_id,))
        if row is None:
            raise HTTPException(404, "no such finding")
        with db.tx() as c:
            if body.status == "open":
                # Withdrawing a decision withdraws its reason too. A note
                # saying why something was accepted, left on a finding that is
                # open again, reads as a justification nobody stands behind.
                c.execute(
                    "UPDATE finding SET status='open', note=NULL WHERE id=?",
                    (finding_id,),
                )
            elif body.note is None:
                # Sending no note means "leave it as it was", not "erase it".
                # This used to write NULL unconditionally, so every undo — and
                # every second click on the same button — quietly threw away
                # the reason somebody had typed.
                c.execute(
                    "UPDATE finding SET status=? WHERE id=?",
                    (body.status, finding_id),
                )
            else:
                c.execute(
                    "UPDATE finding SET status=?, note=? WHERE id=?",
                    (body.status, body.note, finding_id),
                )
        # What it was before, so the caller can offer an accurate undo. The
        # browser's copy of the row can be minutes old, or belong to a second
        # window; the database knows and it does not.
        return {"ok": True, "was": row["status"] or "open"}

    # -- summaries ---------------------------------------------------------

    @app.get("/api/summary")
    def summary(project_id: int) -> dict:
        return {
            "coverage": coverage_summary(db, project_id),
            "pressure_tests": test_summary(db, project_id),
            "coating": report_summary(db, project_id),
            "flanges": flange_summary(db, project_id),
            "procedures": procedure_summary(db, project_id),
            "welders": roster_summary(db, project_id),
            "backfill": release_summary(db, project_id),
            "asbuilt": asbuilt_summary(db, project_id),
            "completeness": completeness(db, project_id),
        }

    # -- source documents --------------------------------------------------

    @app.get("/api/document/{document_id}")
    def document(document_id: int) -> FileResponse:
        row = db.one("SELECT path, filename FROM document WHERE id=?", (document_id,))
        if not row or not Path(row["path"]).exists():
            raise HTTPException(404, "Document not found on disk")
        return FileResponse(row["path"], filename=row["filename"])

    # -- what a person read off the page -----------------------------------

    @app.get("/api/corrections")
    def corrections_for(project_id: int) -> list[dict]:
        from .extract.corrections import listing

        return [dict(r) for r in listing(db, project_id)]

    @app.post("/api/correct")
    def correct(req: CorrectionRequest) -> dict:
        """Record the value an auditor read, and apply it immediately.

        Applied here rather than only on the next audit, because the point of
        this is that somebody has just looked at the page — making them re-run
        the whole job to see their own correction take effect would be a good
        way to stop them entering any.
        """
        from .extract.corrections import CORRECTABLE, apply_corrections, record

        if req.field not in CORRECTABLE:
            raise HTTPException(400, f"Cannot correct '{req.field}'. "
                                     f"Try: {', '.join(CORRECTABLE)}")
        doc = db.one("SELECT fingerprint, filename FROM document WHERE id=?",
                     (req.document_id,))
        if not doc or not doc["fingerprint"]:
            raise HTTPException(404, "No such document")

        record(db, req.project_id, doc["fingerprint"], req.field, req.value, req.note)
        applied = apply_corrections(db, req.project_id)
        return {"saved": True, "applied": applied, "filename": doc["filename"],
                "rerun_needed": True}

    # -- browsing for a job folder ----------------------------------------
    #
    # A browser cannot hand a web page a real folder path — it deliberately
    # hides them — so the picker has to run on this side. That is only
    # reasonable because the server is bound to localhost and is the same
    # person's own machine; it is listing their folders back to them.

    @app.get("/api/browse")
    def browse(path: str = "") -> dict:
        """Folders inside `path`, or the places worth starting from."""
        if not path:
            roots = [Path.home(), Path.home() / "OneDrive", Path.home() / "Desktop",
                     Path.home() / "Documents"]
            drives = [Path(f"{d}:\\") for d in "CDEFGHIJKLMNOPQRSTUVWXYZ"
                      if Path(f"{d}:\\").exists()]
            here = [p for p in [*roots, *drives] if p.is_dir()]
            return {"path": "", "parent": None, "up": None,
                    "folders": [{"name": str(p), "path": str(p)} for p in
                                dict.fromkeys(here)]}

        here = Path(path).expanduser()
        if not here.is_dir():
            raise HTTPException(400, f"Not a folder: {here}")
        try:
            folders = sorted(
                (e for e in here.iterdir() if e.is_dir() and not e.name.startswith(".")),
                key=lambda e: e.name.lower())
        except PermissionError:
            raise HTTPException(403, f"No permission to read {here}") from None
        parent = here.parent if here.parent != here else None
        return {
            "path": str(here),
            "up": str(parent) if parent else "",
            "folders": [{"name": e.name, "path": str(e)} for e in folders[:400]],
        }

    # -- reports -----------------------------------------------------------

    def _report_path(project, fmt: str, where: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in " -_" else "_"
                       for ch in project["name"])
        stem = f"{safe} - exceptions.{'csv' if fmt == 'csv' else 'xlsx'}"
        if where == "job":
            # Beside the job it describes, in a folder of its own so a report
            # is never mistaken for one of the contractor's own documents.
            return Path(project["root"]) / "WeldAudit Reports" / stem
        return Path.home() / ".weldaudit" / "reports" / stem

    @app.get("/api/export")
    def export(project_id: int, fmt: str = "xlsx", to: str = "download"):
        """Write the exceptions report, and either save it or send it back.

        ``to=job`` puts it in the audited folder, which is where an auditor
        wants it — with the package it is about, on the share the rest of the
        team can already reach. ``to=download`` hands it to the browser, for
        when the job folder is read-only or on someone else's drive.
        """
        project = db.one("SELECT name, root FROM project WHERE id=?", (project_id,))
        if not project:
            raise HTTPException(404, "No such project")
        if fmt not in ("xlsx", "csv"):
            raise HTTPException(400, f"Unknown format: {fmt}")

        write = write_csv if fmt == "csv" else write_excel
        out = _report_path(project, fmt, to)
        try:
            write(db, project_id, out)
        except (OSError, PermissionError) as why:
            if to != "job":
                raise HTTPException(500, f"Could not write {out}: {why}") from None
            # A read-only share or a synced folder mid-lock. Say so, and say
            # where it went instead, rather than failing with nothing.
            fallback = _report_path(project, fmt, "app")
            write(db, project_id, fallback)
            return JSONResponse({
                "path": str(fallback), "saved": True, "fell_back": True,
                "reason": f"{out.parent} could not be written to ({why.__class__.__name__})",
            })

        if to == "job":
            return JSONResponse({"path": str(out), "saved": True, "fell_back": False})
        return FileResponse(out, filename=out.name)

    @app.exception_handler(HTTPException)
    def http_error(_request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    return app
