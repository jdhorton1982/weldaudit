"""Local HTTP API behind the desktop UI.

Binds to localhost only.  Audits run on a background thread so the browser can
poll progress rather than holding a request open for a minute.
"""

from __future__ import annotations

import re
import sqlite3
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, PlainTextResponse,
)
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

#: What the audit window can draw in its own document panel, rather than
#: handing to whatever program Windows has registered. WebView2 renders a PDF
#: itself and paints images, which between them is most of what a finding
#: points at. A spreadsheet or a drawing is not on this list and still opens
#: the way it always did.
VIEWABLE: dict[str, str] = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


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


class Acceptance(BaseModel):
    """One person saying yes to one document.

    At module level rather than inside ``create_app``, and it has to be:
    this file uses ``from __future__ import annotations``, so FastAPI resolves
    ``req: Acceptance`` against the module's globals. Declared in a function
    it is invisible there, and the body is silently read as a query parameter
    instead - a 422 that says "field required" about a field that was sent.
    """

    document_key: str
    sha256: str
    name: str
    company: str = ""
    email: str = ""


class AuditRequest(BaseModel):
    name: str | None = None
    root: str
    #: Exactly these files, when they were picked out of a file dialog rather
    #: than a folder chosen. They keep their real paths, so files picked out
    #: of a package are filed under the same sections as the package.
    paths: list[str] | None = None


class StatusUpdate(BaseModel):
    status: str
    note: str | None = None


class RenameRequest(BaseModel):
    name: str


class OcrRequest(BaseModel):
    project_id: int


class ImportRequest(BaseModel):
    path: str


class CommentRequest(BaseModel):
    text: str


class _Job:
    """State of the currently running audit, polled by the UI."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.stage = ""
        self.message = ""
        self.error: str | None = None
        self.project_id: int | None = None
        #: Where the run is spending its time, as `pipeline.run` reports it.
        #: Kept after the run finishes so the gauges still read afterwards -
        #: how long it took is the question people ask once it has.
        self.timing: dict = {}

    def set(self, **kw: Any) -> None:
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "running": self.running, "stage": self.stage,
                "message": self.message, "error": self.error,
                "project_id": self.project_id, "timing": self.timing,
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

    @app.middleware("http")
    async def never_cache(request, call_next):
        """Tell the window not to keep any of this.

        The interface is one page served from a fixed address, and an update
        replaces the file behind that address without the address changing.
        Nothing here said how long a response stayed good, so WebView2 applied
        its own guess - and a window opened after an update could show the
        previous interface out of cache while running the new program. That is
        exactly as confusing as it sounds: the exe is new, the file on disk is
        new, the server returns the new page, and the screen does not.

        The polled endpoints matter as much. A cached `/api/status` is a
        progress bar that never moves; a cached `/api/update` is an update
        that is never offered.

        Everything here is local, so caching buys nothing worth that.
        """
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        return response

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
        from . import agreements
        from .index import chosen_files, common_parent

        # Before anything is read. An audit opens a customer's whole turnover
        # package, and that is exactly the thing the agreements are about, so
        # the gate belongs here rather than at the door of the window: it
        # cannot be got round by leaving the program open from yesterday.
        if waiting := agreements.outstanding(db):
            raise HTTPException(409, {
                "reason": "agreement",
                "message": "There are terms to read before an audit can run: "
                           + ", ".join(d.title for d in waiting) + ".",
                "documents": [d.key for d in waiting],
            })

        picked = chosen_files(req.paths) if req.paths else []
        if req.paths and not picked:
            raise HTTPException(
                400, "None of those files can be audited. WeldAudit reads "
                     "PDF, Excel, CSV, Word, DWG and text.")
        # The folder they came out of, so segments and sections are measured
        # against something real. Choosing every file in a package and
        # choosing the package come to the same answer.
        root = common_parent(picked) if picked else Path(req.root).expanduser()
        if not picked and not root.is_dir():
            raise HTTPException(400, f"Not a folder: {root}")
        if job.running:
            raise HTTPException(409, "An audit is already running")

        name = req.name or root.name

        def work() -> None:
            job.set(running=True, error=None, stage="starting", message="",
                    project_id=None, timing={})
            try:
                result = run(
                    db, name, root,
                    only_files=[str(p) for p in picked] if picked else None,
                    progress=lambda stage, msg: job.set(stage=stage, message=msg),
                    on_timing=lambda t: job.set(timing=t),
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

    @app.post("/api/findings/{finding_id}/comment")
    def set_comment(finding_id: int, body: CommentRequest) -> dict:
        """Write what an auditor has to say about a finding.

        Stored against the rule, segment and subject rather than the finding's
        id, because every audit deletes its findings and builds them again —
        a comment kept on the row would last until the next run. The finding
        on screen is updated at the same time, so the table and the export
        show it without waiting for another audit.
        """
        row = db.one(
            """SELECT project_id, rule, IFNULL(segment,'') AS segment,
                      IFNULL(subject,'') AS subject
               FROM finding WHERE id=?""", (finding_id,))
        if row is None:
            raise HTTPException(404, "no such finding")
        stored = db.set_comment(row["project_id"], row["rule"],
                                row["segment"], row["subject"], body.text)
        return {"comment": stored}

    @app.get("/api/comments")
    def comments(project_id: int) -> list[dict]:
        """Every comment on this job, whether or not its finding still exists."""
        return [dict(r) for r in db.comments(project_id)]

    @app.post("/api/findings/{finding_id}/status")
    def set_status(finding_id: int, body: StatusUpdate) -> dict:
        """Accept, dismiss, or put a finding back on the list.

        Reopening matters as much as closing. Accepting a finding is one click
        next to a dismiss button, the row leaves the default view the instant
        it is pressed, and the thing that just disappeared may have been the
        critical one. Without a way back, the safe response to a mis-click is
        to re-run the whole audit.

        The decision is stored against the rule, segment and subject rather
        than the finding's id, because every audit deletes its findings and
        builds them again. Kept on the row, a morning's review was gone by the
        afternoon's run and every finding came back open with nothing to show
        it had already been read.
        """
        if body.status not in ("open", "accepted", "dismissed"):
            raise HTTPException(400, "bad status")
        row = db.one(
            """SELECT project_id, rule, IFNULL(segment,'') AS segment,
                      IFNULL(subject,'') AS subject, status
               FROM finding WHERE id=?""", (finding_id,))
        if row is None:
            raise HTTPException(404, "no such finding")

        # A note supplied alongside the decision is a comment like any other;
        # sending none means "leave what is there alone", not "erase it".
        # Reopening no longer clears it either: the column stopped being a
        # justification for an acceptance when it became the place an auditor
        # writes what they found.
        db.remember(row["project_id"], row["rule"], row["segment"],
                    row["subject"], status=body.status,
                    comment=body.note if body.note is not None else None)

        # What it was before, so the caller can offer an accurate undo. The
        # browser's copy of the row can be minutes old, or belong to a second
        # window; the database knows and it does not.
        return {"ok": True, "was": row["status"] or "open"}

    # -- summaries ---------------------------------------------------------

    @app.get("/api/update")
    def update_offer() -> dict:
        """Whether a newer build is sitting in the shared folder.

        A local file read, not a network call: the folder is one OneDrive (or
        a share, or a stick) has already synced. So this is cheap enough to
        ask on every startup and costs nothing when there is nothing to say.

        It stays a local read unless a release URL is configured *and* no
        folder is offering anything newer - see ``update.available``. Where
        that is set, this can make one short request on startup.
        """
        from .update import available, current_version, install_dir

        offered = available()
        return {
            "running": current_version(),
            "version": offered.version if offered else None,
            "notes": offered.notes if offered else "",
            "from": offered.where if offered else "",
            # A one-file build has no folder to swap, so the offer is worth
            # showing but the button is not.
            "can_apply": install_dir() is not None,
        }

    @app.post("/api/update")
    def update_apply() -> dict:
        from .update import NotWhatItSaid, apply, available

        offered = available()
        if offered is None:
            raise HTTPException(409, "There is no update to install.")
        try:
            message = apply(offered)
        except NotWhatItSaid as why:
            raise HTTPException(400, str(why)) from None

        # And now get out of the way. The swap waiting outside cannot rename a
        # folder this process is running from, so staying alive would mean the
        # update silently never happened — which is how it behaved the first
        # time it was tried. The delay is for this reply to reach the window.
        def bow_out() -> None:
            import os
            import time

            time.sleep(1.5)
            try:
                import webview

                for window in list(webview.windows):
                    window.destroy()
            except Exception:         # noqa: BLE001 - no window; exit anyway
                pass
            time.sleep(0.5)
            # Abrupt on purpose. Nothing here is mid-write that matters — the
            # database is WAL and recovers — and a clean shutdown through
            # uvicorn from inside one of its own request handlers is not
            # reliable enough to bet an update on.
            os._exit(0)

        threading.Thread(target=bow_out, daemon=True).start()
        return {"applied": True, "message": message}

    @app.get("/api/report-scope")
    def report_scope(project_id: int) -> dict:
        """What a download would and would not contain, before it is saved.

        The report carries only findings marked Issue. That is what was asked
        for, and it means a report taken before the list has been worked
        through comes out empty -- which is indistinguishable from a clean
        package. So the window asks first, and this is what it asks with.
        """
        from .report import reportable, unreviewed

        going = len(reportable(db, project_id))
        return {
            "going_out": going,
            "unreviewed": unreviewed(db, project_id),
            "no_issue": db.one(
                "SELECT COUNT(*) AS n FROM finding WHERE project_id=? "
                "AND status='dismissed'", (project_id,))["n"],
        }

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

    # -- the approved materials list ----------------------------------------

    @app.get("/api/aml")
    def aml_lookup(project_id: int, q: str = "", nps: str = "",
                   category: str = "") -> dict:
        """Search the list this project was audited against.

        Per project on purpose. The rows are copied in at audit time, so a job
        run in June is still searchable against the June list after a new one
        is issued — which is the only honest way to explain a June finding.
        """
        from .amlsearch import categories as aml_categories
        from .amlsearch import search as aml_search
        from .rules.materials import _aml_from_db

        source = db.one("SELECT * FROM aml_source WHERE project_id=?", (project_id,))
        aml = _aml_from_db(db, project_id)
        if aml is None:
            return {"loaded": False, "verdict": None, "rows": [],
                    "shown": 0, "total": 0, "categories": [], "source": None}

        out = aml_search(aml, q, nps, category)
        out["loaded"] = True
        out["categories"] = aml_categories(aml)
        out["entries"] = len(aml.entries)
        out["source"] = dict(source) if source else None
        if source and source["valid_thru"]:
            from datetime import date

            out["expired"] = source["valid_thru"] < date.today().isoformat()
        return out

    # -- what has to be agreed to ------------------------------------------

    @app.get("/api/agreements")
    def agreements_list() -> dict:
        """Every document this build carries, and whether it still needs a yes."""
        from . import agreements

        waiting = {d.key for d in agreements.outstanding(db)}
        return {
            "armed": agreements.gate_is_armed(),
            "outstanding": sorted(waiting),
            "documents": [
                {"key": d.key, "title": d.title, "body": d.body,
                 "sha256": d.sha256, "version": d.version, "words": d.words,
                 "accepted": d.key not in waiting}
                for d in agreements.documents()
            ],
        }

    @app.post("/api/agreements/accept")
    def agreements_accept(req: Acceptance) -> dict:
        """Record that a named person accepted one document, as it reads now.

        The hash comes back from the page and is checked rather than trusted:
        it is what proves the text recorded is the text that was on screen,
        and a stale page offering an old wording must not be able to record
        agreement to it.
        """
        from . import agreements

        found = next((d for d in agreements.documents()
                      if d.key == req.document_key), None)
        if found is None:
            raise HTTPException(404, f"No such document: {req.document_key}")
        if found.sha256 != req.sha256:
            raise HTTPException(
                409, "That document has changed since it was put on screen. "
                     "It has been reloaded; please read it again.")
        try:
            agreements.record(db, found, req.name, req.company, req.email)
        except ValueError as bad:
            raise HTTPException(400, str(bad)) from None

        waiting = [d.key for d in agreements.outstanding(db)]
        return {"recorded": found.key, "outstanding": waiting}

    @app.get("/api/agreements/record")
    def agreements_record() -> PlainTextResponse:
        """The acceptances on this machine, as text somebody can send on."""
        from . import agreements

        return PlainTextResponse(
            agreements.record_as_text(db),
            headers={"Content-Disposition":
                     'attachment; filename="weldaudit-agreement-record.txt"'})

    # -- source documents --------------------------------------------------

    @app.get("/api/document/{document_id}")
    def document(document_id: int, download: bool = False) -> FileResponse:
        """Serve a document, inline where the window can draw it itself.

        ``FileResponse(path, filename=...)`` sets ``Content-Disposition:
        attachment``, which tells the browser to save the file rather than
        show it. That is the reason opening a finding's document needed a
        browser window at all: a panel in the audit window pointed at this
        would have downloaded the certificate instead of displaying it.

        So the kinds the window can draw come back inline, and everything
        else - a spreadsheet, a drawing - keeps the old behaviour, because
        those still need whichever program is registered for them.
        """
        row = db.one("SELECT path, filename FROM document WHERE id=?", (document_id,))
        if not row or not Path(row["path"]).exists():
            raise HTTPException(404, "Document not found on disk")

        kind = VIEWABLE.get(Path(row["path"]).suffix.lower())
        if not kind or download:
            return FileResponse(row["path"], filename=row["filename"])

        # The name travels so that saving from the viewer produces something
        # called after the document rather than after its id. A quote or a
        # newline would end the header early, so they go.
        safe = re.sub(r'[^\w \-.()\[\]]', "_",
                      row["filename"] or f"document-{document_id}")
        return FileResponse(
            row["path"], media_type=kind,
            headers={"Content-Disposition": f'inline; filename="{safe}"'})

    @app.get("/api/document/{document_id}/where")
    def document_where(document_id: int) -> dict:
        """Where a document sits on disk, for "Open folder" in the viewer.

        The panel shows the page; this is what lets somebody get to the file
        itself - to send it on, or to look at what is filed beside it.
        """
        row = db.one("SELECT path, filename FROM document WHERE id=?", (document_id,))
        if not row:
            raise HTTPException(404, "No such document")
        return {"path": row["path"], "filename": row["filename"],
                "on_disk": Path(row["path"]).exists()}

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
        stem = f"{safe} - exceptions.{fmt if fmt in ('csv', 'pdf') else 'xlsx'}"
        if where == "job":
            # Beside the job it describes, in a folder of its own so a report
            # is never mistaken for one of the contractor's own documents.
            return Path(project["root"]) / "WeldAudit Reports" / stem
        if where == "downloads":
            # Written by the program rather than handed to the window to
            # download. A WebView2 window is not a browser: it has no
            # downloads folder and no download bar, and on a machine where
            # that goes wrong the auditor gets a Windows box offering to find
            # an app in the Store and no file they can point at. Writing it
            # here and naming the path depends on nothing but the disk.
            folder = Path.home() / "Downloads"
            if folder.is_dir():
                return folder / stem
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
        if fmt not in ("xlsx", "csv", "pdf"):
            raise HTTPException(400, f"Unknown format: {fmt}")

        if fmt == "pdf":
            from .reportpdf import write_pdf

            write = write_pdf
        else:
            write = write_csv if fmt == "csv" else write_excel
        out = _report_path(project, fmt, to)
        try:
            write(db, project_id, out)
        except (OSError, PermissionError) as why:
            if to == "download":
                raise HTTPException(500, f"Could not write {out}: {why}") from None
            # A read-only share or a synced folder mid-lock. Say so, and say
            # where it went instead, rather than failing with nothing.
            fallback = _report_path(project, fmt, "app")
            write(db, project_id, fallback)
            return JSONResponse({
                "path": str(fallback), "saved": True, "fell_back": True,
                "reason": f"{out.parent} could not be written to ({why.__class__.__name__})",
            })

        if to in ("job", "downloads"):
            return JSONResponse({"path": str(out), "saved": True, "fell_back": False})
        return FileResponse(out, filename=out.name)

    @app.exception_handler(HTTPException)
    def http_error(_request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    return app
