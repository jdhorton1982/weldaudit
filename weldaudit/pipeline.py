"""Index -> extract -> reconcile, in one call."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import rules
from .db import Database
from .extract import (
    asbuilt, corrections, dwr, flanges, instruments, materials, mtrtext, ndelog,
    readings,
    readersheets, roster,
    vision_pass,
    weldlog_csv, weldmaps, welders, wps,
)
from .index import IndexStats, completeness, index_project
from .rules.nde_coverage import coverage_summary

Progress = Callable[[str, str], None]


@dataclass
class RunResult:
    project_id: int
    run_id: str
    index: IndexStats
    counts: dict[str, int] = field(default_factory=dict)
    findings: list[dict] = field(default_factory=list)

    @property
    def by_severity(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f["severity"]] = out.get(f["severity"], 0) + 1
        return out


def default_db_path() -> Path:
    return Path.home() / ".weldaudit" / "weldaudit.db"


def run(
    db: Database,
    name: str,
    root: str | Path,
    *,
    only_rules: list[str] | None = None,
    aml_workbook: str | Path | None = None,
    progress: Progress | None = None,
) -> RunResult:
    def say(stage: str, msg: str) -> None:
        if progress:
            progress(stage, msg)

    run_id = uuid.uuid4().hex[:12]
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")

    say("index", f"Scanning {root}")
    project_id, stats = index_project(db, name, root)
    say("index", f"{stats.files_indexed:,} files across {stats.segments} segments")

    counts: dict[str, int] = {}

    say("extract", "Reading reader-sheet filenames")
    counts["nde_shots"] = readersheets.extract(db, project_id)

    # Not every job names its sheets for the welds they cover. Kestrel 8 names
    # them for the day, so its shots exist only inside the sheet.
    say("extract", "Reading reader-sheet text layers")
    shots, sheets = readersheets.extract_text(db, project_id)
    counts["text_shots"], counts["text_sheets"] = shots, sheets

    say("extract", "Reading daily weld reports")
    files, welds = dwr.extract(db, project_id)
    counts["dwr_files"], counts["dwr_welds"] = files, welds

    say("extract", "Reading weld log exports")
    files, welds = weldlog_csv.extract(db, project_id)
    counts["csv_files"], counts["csv_welds"] = files, welds

    # Most isometrics were plotted from CAD rather than scanned, so their weld
    # and heat callouts are text. On Kestrel 8 this is the only weld register
    # there is - its daily reports leave the weld number blank.
    say("extract", "Reading weld and heat map callouts")
    maps, map_welds, map_heats = weldmaps.extract(db, project_id)
    counts["map_files"], counts["map_welds"] = maps, map_welds
    counts["map_heats"] = map_heats

    # The as-built is the largest weld register in the corpus and the only
    # one that puts a joint at a survey station.
    say("extract", "Reading as-built sheets")
    joints, books = asbuilt.extract(db, project_id)
    counts["asbuilt_joints"], counts["asbuilt_files"] = joints, books

    say("extract", "Reading NDE rig logs")
    files, techs = ndelog.extract(db, project_id)
    counts["riglog_files"], counts["nde_techs"] = files, techs

    # Instrument serials and calibration dates come off the certificate
    # filenames, so the coating rules have something to check against before
    # any page has been read.
    say("extract", "Reading instrument calibration certificates")
    counts["instruments"] = instruments.extract(db, project_id)

    say("extract", "Reading the welding procedure register")
    counts["procedures"] = wps.extract(db, project_id)

    say("extract", "Reading flange logs and maps")
    joints, maps, wrenches = flanges.extract(db, project_id)
    counts["flanges"], counts["flange_maps"] = joints, maps
    counts["instruments"] += wrenches

    # Vision results live in a cache keyed by document fingerprint, but the
    # tables they populate are cleared by re-indexing. Replaying weld reports
    # here - before the welder extractors - means welds recovered from a scan
    # are indistinguishable downstream from welds read out of a spreadsheet.
    replayed = vision_pass.replay(db, project_id, ("daily_weld_report", "weld_map"))
    if replayed:
        counts["vision_welds"] = sum(replayed.values())
        say("extract", f"Replayed {counts['vision_welds']:,} records read from scans")

    say("extract", "Reading welder stencils")
    passes, stencils = welders.extract_passes(db, project_id)
    counts["welder_passes"], counts["stencils"] = passes, stencils

    say("extract", "Reading the project welder log")
    counts["roster"] = roster.extract(db, project_id)

    say("extract", "Reading welder certifications")
    counts["welder_certs"] = welders.extract_certs(db, project_id)

    say("extract", "Reading material certificates")
    counts["certificates"] = materials.extract_certificates(db, project_id)

    say("extract", "Reading pipe / heat exports")
    files, heats = materials.extract_pipes(db, project_id)
    counts["pipe_files"], counts["pipe_heats"] = files, heats

    say("extract", "Loading approved materials list")
    aml, aml_path = materials.load_aml(db, project_id, root, aml_workbook)
    counts["aml_entries"] = len(aml) if aml else 0
    if aml_path:
        say("extract", f"AML: {aml_path.name} ({len(aml):,} entries)")
    else:
        say("extract", "No AML workbook found")

    # The remaining kinds annotate rows the extractors above have just created,
    # so they replay after everything else is in place.
    rest = vision_pass.replay(
        db, project_id,
        ("reader_sheet", "mtr", "welder_cert", "hydrotest", "coating",
         "backfill"))
    if rest:
        counts.update({f"vision_{k}": v for k, v in rest.items()})
        say("extract", "Replayed " + ", ".join(
            f"{v:,} {k.replace('_', ' ')} records" for k, v in rest.items()))

    # After the replay, deliberately. A name lifted from a text layer is the
    # characters the certificate was authored with; a name from the vision pass
    # is a model's reading of a picture of them. Where both exist the exact one
    # should stand, and it also spares that certificate the VIS-02 and VIS-03
    # hedging a scanned name has to carry.
    say("extract", "Reading certificate text layers")
    counts["mtr_text"] = mtrtext.extract_letterheads(db, project_id, aml)
    if counts["mtr_text"]:
        say("extract", f"{counts['mtr_text']:,} manufacturers read from text "
                       f"layers, at no cost")

    # Before the demotion, because both reason over what the readers recorded.
    disagreed = vision_pass.note_reader_disagreements(db, project_id)
    if disagreed:
        counts["reader_disagreements"] = disagreed
        say("extract", f"{disagreed:,} page(s) where two readers named "
                       f"different companies")

    # After both readers, because it reasons over everything they recorded.
    demoted = vision_pass.demote_known_suppliers(db, project_id)
    if demoted:
        counts["suppliers_demoted"] = demoted
        say("extract", f"{demoted:,} manufacturer(s) taken back from companies "
                       f"this project calls a steel supplier")

    # Last, over everything every reader concluded. A value a person read off
    # the page is not another opinion to weigh; it is the answer.
    fixed = corrections.apply_corrections(db, project_id)
    if fixed:
        counts["corrections"] = fixed
        say("extract", f"{fixed:,} value(s) set from corrections entered by hand")

    # After the document-level corrections, and the ordering is the point:
    # those mark a row `human`, and this skips anything already marked. A
    # value somebody typed against a specific page therefore outranks a rule
    # about a name, which is the right way round — the page was looked at.
    byname = readings.apply_readings(db, project_id)
    if byname:
        counts["vendor_readings"] = byname
        say("extract", f"{byname:,} certificate(s) matched a letterhead "
                       f"identified by hand")

    say("reconcile", "Running audit rules")
    db.clear_findings(project_id)
    found = rules.sort_findings(rules.run_all(db, project_id, run_id, only=only_rules))
    db.add_findings(found)
    # Findings are rebuilt from scratch every run; the comments people wrote on
    # them are not, and are put back here.
    db.reattach_comments(project_id)

    with db.tx() as c:
        c.execute(
            "INSERT OR REPLACE INTO run(id, project_id, started_at, finished_at, summary) "
            "VALUES(?,?,?,?,?)",
            (
                run_id, project_id, started,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                json.dumps(counts),
            ),
        )

    say("done", f"{len(found):,} findings")
    return RunResult(project_id=project_id, run_id=run_id, index=stats,
                     counts=counts, findings=found)


def summary(db: Database, project_id: int) -> dict:
    """Everything the UI needs for an overview screen."""
    return {
        "completeness": completeness(db, project_id),
        "coverage": coverage_summary(db, project_id),
        "rules": {code: title for code, (title, _fn) in rules.registry().items()},
    }
