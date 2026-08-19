"""Excel exception reports - the deliverable an auditor hands to a contractor."""

from __future__ import annotations

import json
from pathlib import Path

import xlsxwriter

from .db import Database
from .index import completeness
from .rules import registry
from .rules.nde_coverage import coverage_summary

_SEV_COLOUR = {
    "critical": "#C00000",
    "major": "#C55A11",
    "minor": "#BF8F00",
    "info": "#808080",
}


def write_excel(db: Database, project_id: int, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    project = db.one("SELECT * FROM project WHERE id=?", (project_id,))
    name = project["name"] if project else "project"

    wb = xlsxwriter.Workbook(str(path), {"constant_memory": True})
    head = wb.add_format({"bold": True, "bg_color": "#1F3864", "font_color": "white",
                          "border": 1, "text_wrap": True, "valign": "top"})
    wrap = wb.add_format({"text_wrap": True, "valign": "top"})
    title = wb.add_format({"bold": True, "font_size": 14})
    sev_fmt = {k: wb.add_format({"font_color": v, "bold": True, "valign": "top"})
               for k, v in _SEV_COLOUR.items()}

    _findings_sheet(db, project_id, wb, head, wrap, sev_fmt)
    _coverage_sheet(db, project_id, wb, head, title)
    _materials_sheet(db, project_id, wb, head, wrap)
    _qualifications_sheet(db, project_id, wb, head, title)
    _pressure_tests_sheet(db, project_id, wb, head, wrap)
    _coating_sheet(db, project_id, wb, head, wrap)
    _flange_sheet(db, project_id, wb, head, wrap)
    _procedures_sheet(db, project_id, wb, head, wrap)
    _roster_sheet(db, project_id, wb, head, wrap)
    _backfill_sheet(db, project_id, wb, head, wrap)
    _asbuilt_sheet(db, project_id, wb, head, wrap)
    _completeness_sheet(db, project_id, wb, head)
    _rules_sheet(wb, head, wrap)

    wb.close()
    return path


def paths_for(db: Database, row) -> list[str]:
    """Every document a finding covers, as full paths, primary first.

    A finding about a manufacturer can span a dozen certificates while the row
    links to whichever sorted first. Somebody going back to correct the record
    needs all of them, and needs them as paths — a document id tells a person
    nothing, and a bare filename does not say which of the four segment books
    it is filed in.
    """
    out: list[str] = []
    if row["doc_path"]:
        out.append(row["doc_path"])
    try:
        detail = json.loads(row["detail"]) if row["detail"] else {}
    except (TypeError, ValueError):
        return out
    ids = [i.strip() for i in str(detail.get("document_ids", "")).split(",") if i.strip()]
    if not ids:
        return out
    placeholders = ",".join("?" * len(ids))
    for extra in db.q(
            f"SELECT path FROM document WHERE id IN ({placeholders})", tuple(ids)):
        if extra["path"] not in out:
            out.append(extra["path"])
    return out


def _findings_sheet(db, project_id, wb, head, wrap, sev_fmt) -> None:
    ws = wb.add_worksheet("Findings")
    cols = [("Severity", 10), ("Rule", 9), ("Segment", 30), ("Subject", 16),
            ("Finding", 78), ("Detail", 40), ("Source document", 46),
            ("Full path", 96), ("Status", 10), ("Comments", 46)]
    for i, (label, width) in enumerate(cols):
        ws.write(0, i, label, head)
        ws.set_column(i, i, width)
    ws.freeze_panes(1, 0)
    ws.autofilter(0, 0, 0, len(cols) - 1)

    rows = db.q(
        """SELECT f.*, d.path AS doc_path, d.filename
           FROM finding f LEFT JOIN document d ON d.id = f.document_id
           WHERE f.project_id=?
           ORDER BY CASE f.severity WHEN 'critical' THEN 0 WHEN 'major' THEN 1
                                    WHEN 'minor' THEN 2 ELSE 3 END,
                    f.segment, f.rule, f.subject""",
        (project_id,),
    )
    for r, row in enumerate(rows, start=1):
        ws.write(r, 0, row["severity"], sev_fmt.get(row["severity"], wrap))
        ws.write(r, 1, row["rule"], wrap)
        ws.write(r, 2, row["segment"] or "", wrap)
        ws.write(r, 3, row["subject"] or "", wrap)
        ws.write(r, 4, row["message"], wrap)
        ws.write(r, 5, _flatten(row["detail"]), wrap)
        paths = paths_for(db, row)
        if row["doc_path"]:
            label = row["filename"] or row["doc_path"]
            if len(paths) > 1:
                label += f"  (+{len(paths) - 1} more)"
            ws.write_url(r, 6, f"external:{row['doc_path']}", string=label)
        else:
            ws.write(r, 6, "", wrap)
        # The paths in full, one per line. The link above opens the first one;
        # this is what somebody copies when they go to the share to correct a
        # record, and what survives the workbook being emailed on.
        ws.write(r, 7, "\n".join(paths), wrap)
        ws.write(r, 8, row["status"] or "open", wrap)
        # What a person wrote about this finding. Last column because it is the
        # one an auditor adds to, and a column you type in belongs at the end of
        # the row rather than between two the program owns.
        ws.write(r, 9, (row["note"] if "note" in row.keys() else "") or "", wrap)


def _flatten(detail: str | None) -> str:
    if not detail:
        return ""
    try:
        data = json.loads(detail)
    except (TypeError, ValueError):
        return str(detail)
    if isinstance(data, dict):
        return "; ".join(f"{k}={v}" for k, v in data.items())
    return str(data)


def _coverage_sheet(db, project_id, wb, head, title) -> None:
    ws = wb.add_worksheet("NDE coverage")
    ws.write(0, 0, "NDE coverage by segment", title)
    ws.write(
        1, 0,
        "Where a segment has more than one weld register, the segment row is a "
        "deduplicated estimate and the registers are listed beneath it.",
    )
    cols = [("Segment", 40), ("Register", 26), ("Welds", 10),
            ("Welds citing NDE", 18), ("Distinct shots on file", 22),
            ("% welds with NDE", 18)]
    for i, (label, width) in enumerate(cols):
        ws.write(3, i, label, head)
        ws.set_column(i, i, width)

    indent = wb.add_format({"indent": 2, "font_color": "#666666"})
    row = 4
    for c in coverage_summary(db, project_id):
        ws.write(row, 0, c["segment"])
        ws.write(row, 1, "all registers (deduplicated)"
                 if c["multiple_registers"] else
                 (c["registers"][0]["register"] if c["registers"] else ""))
        ws.write(row, 2, c["welds"])
        ws.write(row, 3, c["welds_with_nde"])
        ws.write(row, 4, c["sheets_on_file"])
        ws.write(row, 5, c["pct_referenced"] / 100)
        row += 1
        if c["multiple_registers"]:
            for reg in c["registers"]:
                ws.write(row, 1, reg["register"], indent)
                ws.write(row, 2, reg["welds"], indent)
                ws.write(row, 3, reg["welds_with_nde"], indent)
                row += 1
    ws.freeze_panes(4, 0)


def _materials_sheet(db, project_id, wb, head, wrap) -> None:
    """Every heat, what evidence exists for it, and how it fares against the AML."""
    from .rules.materials import _aml_from_db, _certified_heats, _welded_heats

    ws = wb.add_worksheet("Materials")
    cols = [("Heat", 18), ("Welded on", 34), ("Weld ends", 10), ("Certificate", 44),
            ("Manufacturer", 26), ("AML status", 13), ("AML entry", 34),
            ("Size limit", 18), ("NPS", 7)]
    for i, (label, width) in enumerate(cols):
        ws.write(0, i, label, head)
        ws.set_column(i, i, width)
    ws.freeze_panes(1, 0)
    ws.autofilter(0, 0, 0, len(cols) - 1)

    aml = _aml_from_db(db, project_id)
    certified = _certified_heats(db, project_id)
    welded = _welded_heats(db, project_id)

    known_mfr = {
        r["heat_key"]: r
        for r in db.q(
            """SELECT heat_key, heat, manufacturer, nps, categories, line FROM material
               WHERE project_id=? AND IFNULL(manufacturer,'')<>''""",
            (project_id,),
        )
    }

    keys = sorted(set(welded) | set(certified) | set(known_mfr))
    for r, key in enumerate(keys, start=1):
        w = welded.get(key)
        certs = certified.get(key, [])
        mfr_row = known_mfr.get(key)
        heat = (w or (certs[0] if certs else None) or mfr_row)["heat"]

        ws.write(r, 0, heat)
        ws.write(r, 1, (w or {}).get("line") or (w or {}).get("segment") or "")
        ws.write(r, 2, len((w or {}).get("welds", [])) or "")
        ws.write(r, 3, certs[0]["filename"] if certs else "— none on file —")

        manufacturer = mfr_row["manufacturer"] if mfr_row else ""
        ws.write(r, 4, manufacturer)
        nps = (mfr_row["nps"] if mfr_row else None) or (certs[0]["nps"] if certs else None)
        ws.write(r, 8, nps if nps is not None else "")

        if not (aml and manufacturer):
            ws.write(r, 5, "not checked" if aml else "no AML")
            continue
        cats = [c for c in ((mfr_row["categories"] or "").split("; ")) if c] or None
        result = aml.match(manufacturer, cats)
        entry = result.entries[0] if result.entries else None
        status = result.status
        if status == "approved" and nps is not None:
            allowing, forbidding = aml.check_size(result.entries, nps)
            if forbidding and not allowing:
                status = "size violation"
                entry = forbidding[0]
        ws.write(r, 5, status.replace("_", " "))
        ws.write(r, 6, f"{entry.manufacturer} [{entry.location}]" if entry else "", wrap)
        ws.write(r, 7, entry.size_limit.describe() if entry and entry.size_limit else "")


def _qualifications_sheet(db, project_id, wb, head, title) -> None:
    """Who welded and who shot, with their certification status."""
    from .rules.ndetech import rig_letter_for
    from .welders import continuity_gaps, nearest_stencils

    ws = wb.add_worksheet("Qualifications")
    ws.write(0, 0, "Welders", title)
    cols = [("Stencil", 10), ("Name", 22), ("Passes", 8), ("First weld", 12),
            ("Last weld", 12), ("Certification", 40), ("Cert date", 12),
            ("Status", 30)]
    for i, (label, width) in enumerate(cols):
        ws.write(2, i, label, head)
        ws.set_column(i, i, width)

    certs: dict[str, list] = {}
    for r in db.q(
        """SELECT c.*, d.filename FROM welder_cert c
           LEFT JOIN document d ON d.id = c.document_id
           WHERE c.project_id=? AND c.stencil<>''""",
        (project_id,),
    ):
        certs.setdefault(r["stencil"], []).append(r)

    passes: dict[str, list] = {}
    for r in db.q("SELECT * FROM welder_pass WHERE project_id=?", (project_id,)):
        passes.setdefault(r["stencil"], []).append(r)

    row = 3
    for stencil in sorted(set(passes) | set(certs)):
        mine = passes.get(stencil, [])
        dates = sorted(p["date_welded"] for p in mine if p["date_welded"])
        cert = (certs.get(stencil) or [None])[0]

        if cert:
            gaps = continuity_gaps(dates)
            status = f"{len(gaps)} continuity gap(s)" if gaps else "certified"
        elif not mine:
            status = "certified, never welded here"
        else:
            near = nearest_stencils(stencil, set(certs))
            status = f"no cert — did they mean {' or '.join(near[:2])}?" if near \
                else "NO CERTIFICATION ON FILE"

        ws.write(row, 0, stencil)
        ws.write(row, 1, (cert["name"] if cert else "") or "")
        ws.write(row, 2, len(mine))
        ws.write(row, 3, dates[0] if dates else "")
        ws.write(row, 4, dates[-1] if dates else "")
        ws.write(row, 5, (cert["filename"] if cert else "") or "— none —")
        ws.write(row, 6, (cert["cert_date"] if cert else "") or "")
        ws.write(row, 7, status)
        row += 1

    row += 2
    ws.write(row, 0, "NDE technicians", title)
    row += 2
    for i, label in enumerate(["Rig", "Name", "Company", "Certs", "Acuity",
                               "Cert date", "Arrived", "Shots attributed"]):
        ws.write(row, i, label, head)
    row += 1

    shots_by_rig: dict[str, int] = {}
    for s in db.q(
        """SELECT s.prefix, d.filename FROM nde_shot s
           LEFT JOIN document d ON d.id = s.document_id WHERE s.project_id=?""",
        (project_id,),
    ):
        letter = rig_letter_for(s["prefix"], s["filename"])
        if letter:
            shots_by_rig[letter] = shots_by_rig.get(letter, 0) + 1

    seen: set[tuple] = set()
    for t in db.q(
        "SELECT * FROM nde_tech WHERE project_id=? ORDER BY rig_letter, arrived",
        (project_id,),
    ):
        letter = (t["rig_letter"] or "").strip().upper()[:1]
        key = (letter, (t["name"] or "").upper(), t["arrived"])
        if key in seen:
            continue
        seen.add(key)
        ws.write(row, 0, letter)
        ws.write(row, 1, t["name"] or "")
        ws.write(row, 2, t["company"] or "")
        ws.write(row, 3, t["certs"] or "")
        ws.write(row, 4, t["acuity"] or "")
        ws.write(row, 5, (t["cert_date"] or "")[:10])
        ws.write(row, 6, (t["arrived"] or "")[:10])
        ws.write(row, 7, shots_by_rig.get(letter, 0))
        row += 1


def _pressure_tests_sheet(db, project_id, wb, head, wrap) -> None:
    """One row per hydrostatic test: what was required, and what was held.

    The required and actual columns sit side by side because that comparison
    is the whole audit of a pressure test, and an auditor asked to confirm a
    line was tested wants to see both numbers without opening the package.
    """
    ws = wb.add_worksheet("Pressure tests")
    cols = [("Segment", 30), ("Service", 16), ("Code", 9), ("Started", 17),
            ("Completed", 17), ("Required psig", 12), ("Held (low)", 10),
            ("Held (high)", 11), ("Max allowed", 11), ("Required hrs", 11),
            ("Actual hrs", 10), ("Result", 13), ("Medium", 13),
            ("Readings", 9), ("Inspector", 20), ("Source document", 46)]
    for i, (label, width) in enumerate(cols):
        ws.write(0, i, label, head)
        ws.set_column(i, i, width)
    ws.freeze_panes(1, 0)

    from .rules.hydrotest import test_summary

    paths = {d["id"]: d["path"] for d in db.q(
        "SELECT id, path FROM document WHERE project_id=?", (project_id,))}

    for r, t in enumerate(test_summary(db, project_id), start=1):
        ws.write(r, 0, t["segment"], wrap)
        ws.write(r, 1, t["service"], wrap)
        ws.write(r, 2, t["code"])
        ws.write(r, 3, t["started"])
        ws.write(r, 4, t["completed"])
        for col, key in ((5, "required_min"), (6, "held_low"), (7, "held_high"),
                         (8, "required_max"), (9, "required_hours"),
                         (10, "actual_hours")):
            if t[key] is not None:
                ws.write(r, col, t[key])
        ws.write(r, 11, t["result"])
        ws.write(r, 12, t["medium"])
        ws.write(r, 13, t["readings"])
        ws.write(r, 14, t["inspector"], wrap)
        if path := paths.get(t["document_id"]):
            ws.write_url(r, 15, f"external:{path}", string=t["filename"],
                         cell_format=wrap)


def _coating_sheet(db, project_id, wb, head, wrap) -> None:
    """One row per daily coating report, ending in what it left blank.

    The blank column is last and widest deliberately: on these reports the
    recorded numbers nearly always pass, and the audit is mostly about what
    was never written down.
    """
    ws = wb.add_worksheet("Coating")
    cols = [("Segment", 26), ("Date", 11), ("Service", 14), ("Size", 7),
            ("Product", 26), ("Method", 14), ("Blast media", 13),
            ("Cleanliness", 12), ("Profile reqd", 11), ("Profile low", 11),
            ("DFT low", 9), ("RH high", 8), ("Dew pt margin", 12),
            ("Jeeped", 8), ("Welds named", 11), ("Inspector", 18),
            ("Not recorded", 46), ("Source document", 40)]
    for i, (label, width) in enumerate(cols):
        ws.write(0, i, label, head)
        ws.set_column(i, i, width)
    ws.freeze_panes(1, 0)
    ws.autofilter(0, 0, 0, len(cols) - 1)

    from .rules.coating import report_summary

    paths = {d["id"]: d["path"] for d in db.q(
        "SELECT id, path FROM document WHERE project_id=?", (project_id,))}

    for r, row in enumerate(report_summary(db, project_id), start=1):
        for col, key in ((0, "segment"), (1, "report_date"), (2, "service"),
                         (3, "line_size"), (4, "products"), (5, "method"),
                         (6, "blast_media"), (7, "cleanliness")):
            ws.write(r, col, row[key], wrap)
        for col, key in ((8, "profile_required"), (9, "profile_low"),
                         (10, "dft_low"), (11, "humidity_high"),
                         (12, "dew_point_margin")):
            if row[key] is not None:
                ws.write(r, col, row[key])
        ws.write(r, 13, "yes" if row["jeeped"] else "NO")
        ws.write(r, 14, row["welds_named"])
        ws.write(r, 15, row["inspector"], wrap)
        ws.write(r, 16, ", ".join(row["missing"]), wrap)
        if path := paths.get(row["document_id"]):
            ws.write_url(r, 17, f"external:{path}", string=row["filename"],
                         cell_format=wrap)


def _flange_sheet(db, project_id, wb, head, wrap) -> None:
    """One row per torque log, ending in the three columns left blank.

    A bolted joint has no NDE and no pressure record of its own, so the log's
    own sign-off columns are the whole of its evidence.
    """
    ws = wb.add_worksheet("Flange bolt-up")
    cols = [("Segment", 26), ("Log", 44), ("Sheet", 12), ("Service", 12),
            ("Joints", 8), ("Torqued", 8), ("Sizes", 20), ("Wrenches", 30),
            ("No wrench", 10), ("Cal not verified", 15), ("No sign-off", 11)]
    for i, (label, width) in enumerate(cols):
        ws.write(0, i, label, head)
        ws.set_column(i, i, width)
    ws.freeze_panes(1, 0)
    ws.autofilter(0, 0, 0, len(cols) - 1)

    from .rules.flanges import flange_summary

    for r, row in enumerate(flange_summary(db, project_id), start=1):
        for col, key in ((0, "segment"), (1, "log"), (2, "sheet"),
                         (3, "service"), (6, "sizes"), (7, "wrenches")):
            ws.write(r, col, row[key], wrap)
        for col, key in ((4, "joints"), (5, "torqued"), (8, "no_wrench"),
                         (9, "not_verified"), (10, "no_signoff")):
            ws.write(r, col, row[key])


def _procedures_sheet(db, project_id, wb, head, wrap) -> None:
    """Every procedure the job approves or refers to, and how it is used.

    "Filed" is the column that matters: a procedure with welds against it and
    no specification in the package is a line handed over with no recipe.
    """
    ws = wb.add_worksheet("Procedures")
    cols = [("WPS", 30), ("Rev", 6), ("Filed", 7), ("Code", 11),
            ("Supporting PQR", 42), ("Welds", 8), ("Certificates", 12),
            ("Min dia", 9), ("2 welders over", 13), ("Written as", 46)]
    for i, (label, width) in enumerate(cols):
        ws.write(0, i, label, head)
        ws.set_column(i, i, width)
    ws.freeze_panes(1, 0)
    ws.autofilter(0, 0, 0, len(cols) - 1)

    from .rules.wps import procedure_summary

    for r, row in enumerate(procedure_summary(db, project_id), start=1):
        ws.write(r, 0, row["wps"], wrap)
        ws.write(r, 1, row["revision"])
        ws.write(r, 2, "yes" if row["filed"] else "NO")
        ws.write(r, 3, row["code"])
        ws.write(r, 4, row["pqr"], wrap)
        ws.write(r, 5, row["welds"])
        ws.write(r, 6, row["certs"])
        for col, key in ((7, "min_diameter"), (8, "two_welder_over")):
            if row[key] is not None:
                ws.write(r, col, row[key])
        ws.write(r, 9, row["spellings"], wrap)


def _roster_sheet(db, project_id, wb, head, wrap) -> None:
    """The contractor's welder log against what each welder actually welded.

    The name column is the point: everywhere else in the audit a welder is
    three letters, and this is the only document that says who that is.
    """
    ws = wb.add_worksheet("Welders")
    cols = [("Stencil", 9), ("Name", 24), ("Cert", 7), ("Qualified", 11),
            ("Requalified", 12), ("Requal due", 11), ("Arrived", 11),
            ("Left", 11), ("Passes", 8), ("First weld", 11), ("Last weld", 11),
            ("Reason left", 30), ("On logs", 40)]
    for i, (label, width) in enumerate(cols):
        ws.write(0, i, label, head)
        ws.set_column(i, i, width)
    ws.freeze_panes(1, 0)
    ws.autofilter(0, 0, 0, len(cols) - 1)

    from .rules.roster import roster_summary

    for r, row in enumerate(roster_summary(db, project_id), start=1):
        ws.write(r, 0, row["stencil"])
        ws.write(r, 1, row["name"], wrap)
        ws.write(r, 2, "yes" if row["certificate"] else "NO")
        for col, key in ((3, "cert_date"), (4, "requal_date"), (5, "next_requal"),
                         (6, "arrived"), (7, "left_job")):
            ws.write(r, col, row[key])
        ws.write(r, 8, row["passes"])
        ws.write(r, 9, row["first_weld"])
        ws.write(r, 10, row["last_weld"])
        ws.write(r, 11, row["reason"], wrap)
        ws.write(r, 12, row["segments"], wrap)


def _backfill_sheet(db, project_id, wb, head, wrap) -> None:
    """Every release for backfill, with the length it covers and who signed.

    The three date columns sit together because the gap between them is the
    finding: one release here was counter-signed six weeks late.
    """
    ws = wb.add_worksheet("Backfill")
    cols = [("Segment", 26), ("Page", 6), ("Size", 7), ("Service", 12),
            ("From", 10), ("To", 10), ("Released", 11), ("Inspector", 11),
            ("Contractor", 11), ("Survey", 11), ("Signed by", 28),
            ("Source document", 34)]
    for i, (label, width) in enumerate(cols):
        ws.write(0, i, label, head)
        ws.set_column(i, i, width)
    ws.freeze_panes(1, 0)
    ws.autofilter(0, 0, 0, len(cols) - 1)

    from .rules.backfill import release_summary

    paths = {d["id"]: d["path"] for d in db.q(
        "SELECT id, path FROM document WHERE project_id=?", (project_id,))}

    for r, row in enumerate(release_summary(db, project_id), start=1):
        ws.write(r, 0, row["segment"], wrap)
        ws.write(r, 1, row["page_no"])
        for col, key in ((2, "line_size"), (3, "service"), (4, "from_station"),
                         (5, "to_station"), (6, "released_on"),
                         (7, "inspector_date"), (8, "contractor_date"),
                         (9, "survey_date"), (10, "signed_by")):
            ws.write(r, col, row[key], wrap)
        if path := paths.get(row["document_id"]):
            ws.write_url(r, 11, f"external:{path}", string=row["filename"],
                         cell_format=wrap)


def _asbuilt_sheet(db, project_id, wb, head, wrap) -> None:
    """One row per as-built sheet: the stretch of line it covers.

    The station span is the column nothing else in the audit can supply — it
    is what ties a joint to a released stretch of ditch.
    """
    ws = wb.add_worksheet("As-built")
    cols = [("Segment", 26), ("Document", 38), ("Sheet", 14), ("Service", 12),
            ("Size", 8), ("From", 10), ("To", 10), ("Length ft", 10),
            ("Joints", 8), ("With heat", 10), ("With NDE", 10)]
    for i, (label, width) in enumerate(cols):
        ws.write(0, i, label, head)
        ws.set_column(i, i, width)
    ws.freeze_panes(1, 0)
    ws.autofilter(0, 0, 0, len(cols) - 1)

    from .rules.asbuilt import asbuilt_summary

    for r, row in enumerate(asbuilt_summary(db, project_id), start=1):
        for col, key in ((0, "segment"), (1, "document"), (2, "sheet"),
                         (3, "service"), (4, "pipe_size"), (5, "from_station"),
                         (6, "to_station")):
            ws.write(r, col, row[key], wrap)
        for col, key in ((7, "length"), (8, "joints"), (9, "with_heat"),
                         (10, "with_nde")):
            ws.write(r, col, row[key])


def _completeness_sheet(db, project_id, wb, head) -> None:
    from .taxonomy import SECTIONS

    ws = wb.add_worksheet("Book completeness")
    ws.write(0, 0, "Segment", head)
    ws.set_column(0, 0, 40)
    ws.write(0, 1, "% complete", head)
    ws.write(0, 2, "Missing required sections", head)
    ws.set_column(2, 2, 60)
    for i, s in enumerate(SECTIONS):
        ws.write(0, 3 + i, f"{s.number} {s.name}", head)
        ws.set_column(3 + i, 3 + i, 6)
    ws.freeze_panes(1, 1)

    for r, c in enumerate(completeness(db, project_id), start=1):
        ws.write(r, 0, c["segment"])
        ws.write(r, 1, c["pct_complete"] / 100)
        ws.write(r, 2, ", ".join(c["missing_required"]))
        for i, s in enumerate(SECTIONS):
            ws.write(r, 3 + i, c["sections"].get(s.number, 0))


def _rules_sheet(wb, head, wrap) -> None:
    ws = wb.add_worksheet("Rules")
    ws.write(0, 0, "Code", head)
    ws.write(0, 1, "What it checks", head)
    ws.set_column(0, 0, 10)
    ws.set_column(1, 1, 70)
    for r, (code, (title, fn)) in enumerate(sorted(registry().items()), start=1):
        ws.write(r, 0, code, wrap)
        doc = (fn.__doc__ or title).strip().split("\n")[0]
        ws.write(r, 1, doc, wrap)


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def write_csv(db: Database, project_id: int, path: str | Path) -> Path:
    """The findings as one flat CSV.

    The Excel workbook is eleven sheets and is what an auditor reads. This is
    the one sheet that matters, in the format that opens anywhere and pastes
    into anything — a punch list to work through, or a column to sort in
    someone else's tracker.

    UTF-8 with a BOM, because Excel on Windows reads a plain UTF-8 CSV as
    cp1252 and turns the mill names the findings quote — Soluções, OÑATI —
    into mojibake. The BOM is what makes a double-click open it correctly.
    """
    import csv

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    rows = db.q(
        """SELECT f.*, d.path AS doc_path, d.filename
           FROM finding f LEFT JOIN document d ON d.id = f.document_id
           WHERE f.project_id=?
           ORDER BY CASE f.severity WHEN 'critical' THEN 0 WHEN 'major' THEN 1
                                    WHEN 'minor' THEN 2 ELSE 3 END,
                    f.segment, f.rule, f.subject""",
        (project_id,),
    )

    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        out = csv.writer(handle)
        out.writerow(["Severity", "Rule", "Segment", "Subject", "Finding",
                      "Detail", "Source document", "Full path", "Status",
                      "Comments"])
        for r in rows:
            keys = r.keys()
            out.writerow([
                r["severity"], r["rule"], r["segment"] or "", r["subject"] or "",
                r["message"] or "",
                (r["detail"] if "detail" in keys else "") or "",
                r["doc_path"] or r["filename"] or "",
                " | ".join(paths_for(db, r)),
                (r["status"] if "status" in keys else "") or "open",
                (r["note"] if "note" in keys else "") or "",
            ])
    return path
