"""Master weld log CSV exports - the weld map for the digital projects.

These exports already carry the weld, both heat numbers, the welder, the WPS
and the NDE result on one row, which makes them the highest-quality evidence in
the corpus.  Column names vary a little between exports, so headers are matched
on a normalised form.

Two related exports are handled here:
  * ``Master_Weld_Log_Summary`` / ``* Weld Log Summary`` - one row per weld
  * ``Weld Map - QC`` / ``Heat Map Confirmation`` - lighter weld/material maps
"""

from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from ..db import Database
from ..ids import parse_one

#: Normalised header -> weld field.  First match wins.
_HEADERS: dict[str, tuple[str, ...]] = {
    "line":          ("line #", "line", "line number"),
    "weld_no":       ("weld number", "weld no", "weld #"),
    "final_weld_no": ("final weld number",),
    "weld_size":     ("pipe diameter", "weld size (in)", "weld size", "diameter"),
    "wall":          ("wall thickness",),
    "grade":         ("grade",),
    "weld_type":     ("joint type", "configuration"),
    "wps":           ("wps",),
    "welder":        ("welder id", "welder"),
    "welder_root":   ("welder id root",),
    "welder_fill":   ("welder id fill",),
    "welder_cap":    ("welder id cap",),
    "date_welded":   ("date welded", "w6 date"),
    "heat_us":       ("u/s heat #", "us heat", "upstream heat"),
    "heat_ds":       ("d/s heat #", "ds heat", "downstream heat"),
    "material1":     ("material1",),
    "material2":     ("material2",),
    "nde_report":    ("nde report",),
    "nde_date":      ("nde date", "date inspected"),
    "nde_technique": ("nde technique",),
    "nde_status":    ("nde001 status", "nde status"),
    "defect":        ("defect",),
    "vi_result":     ("vi result & report-rev & date", "vi result"),
    "repair_report": ("repair nde report",),
    "repair_status": ("repair nde001 status", "repair status"),
    "repair_date":   ("repair nde date",),
    "test_pack":     ("test pack",),
    "station":       ("station number",),
    "category":      ("category",),
}


def _norm(h: str) -> str:
    return re.sub(r"\s+", " ", (h or "").strip().lower().strip('"'))


def _map_headers(fieldnames: Iterable[str]) -> dict[str, str]:
    """``{weld field: actual csv column}``."""
    available = {_norm(f): f for f in fieldnames if f}
    out: dict[str, str] = {}
    for field, variants in _HEADERS.items():
        for v in variants:
            if v in available:
                out[field] = available[v]
                break
    return out


def _date(value: str) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%b-%d-%Y", "%d-%b-%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            continue
    return v


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().strip(";").strip()


def parse_csv(path: str) -> list[dict]:
    """Read one weld-log export into normalised weld dicts."""
    with open(path, "r", encoding="utf-8-sig", newline="", errors="replace") as fh:
        sample = fh.read(8192)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(fh, dialect=dialect)
        if not reader.fieldnames:
            return []
        colmap = _map_headers(reader.fieldnames)
        if "weld_no" not in colmap:
            return []

        out: list[dict] = []
        for raw in reader:
            rec = {field: _clean(raw.get(col)) for field, col in colmap.items()}
            if not rec.get("weld_no"):
                continue
            for f in ("date_welded", "nde_date", "repair_date"):
                if rec.get(f):
                    rec[f] = _date(rec[f])
            # The weld number on these exports *is* the NDE report id (GXR-7).
            nid = parse_one(rec.get("final_weld_no") or rec.get("weld_no", ""))
            rec["nde_id"] = str(nid) if nid else ""
            rid = parse_one(rec.get("repair_report", "")) if rec.get("repair_report") else None
            rec["repair_nde_id"] = str(rid) if rid else ""
            # Heats also arrive embedded in material strings on the QC exports.
            if not rec.get("heat_us") and rec.get("material1"):
                rec["heat_us"] = _heat_from_material(rec["material1"])
            if not rec.get("heat_ds") and rec.get("material2"):
                rec["heat_ds"] = _heat_from_material(rec["material2"])
            out.append(rec)
        return out


#: "321866-2-PIPES160FBEA106B" -> heat 321866
_MATERIAL_HEAT = re.compile(r"^([A-Z0-9]{4,10})-")


def _heat_from_material(material: str) -> str:
    m = _MATERIAL_HEAT.match(material.strip().upper())
    return m.group(1) if m else ""


def extract(db: Database, project_id: int) -> tuple[int, int]:
    """Load every weld-log / weld-map CSV.  Returns ``(files, welds)``."""
    docs = db.q(
        """SELECT id, path, segment, filename FROM document
           WHERE project_id=? AND ext='.csv'
             AND kind IN ('weld_log_csv', 'weld_map')""",
        (project_id,),
    )

    with db.tx() as c:
        c.execute("DELETE FROM weld WHERE project_id=? AND source='weld_log_csv'", (project_id,))

    rows: list[tuple] = []
    ok_files = 0
    for d in docs:
        try:
            welds = parse_csv(d["path"])
        except OSError:
            continue          # OneDrive placeholder not materialised locally
        if not welds:
            continue
        ok_files += 1
        for w in welds:
            # These exports usually sit loose in a project folder rather than
            # inside a segment book, so the line they name is the better label.
            segment = d["segment"]
            if segment in ("", "(unassigned)", None):
                # Fall back to the line the export names, then to the export
                # itself, so findings never read "(unassigned)".
                segment = w.get("line") or Path(d["filename"]).stem[:48]
            rows.append(
                (
                    project_id, d["id"], segment, w.get("line", ""),
                    w.get("weld_no", ""), w.get("weld_size", ""), w.get("weld_type", ""),
                    w.get("wps", ""),
                    w.get("welder_root") or w.get("welder", ""),
                    w.get("welder_fill") or w.get("welder", ""),
                    w.get("welder_cap") or w.get("welder", ""),
                    w.get("date_welded", ""), w.get("heat_us", ""), w.get("heat_ds", ""),
                    w.get("nde_id", ""), w.get("nde_report", ""), w.get("nde_date", ""),
                    w.get("nde_technique", ""),
                    w.get("nde_status") or w.get("vi_result", ""),
                    w.get("defect", ""), w.get("repair_nde_id", ""), w.get("repair_status", ""),
                    "weld_log_csv",
                )
            )

    if rows:
        with db.tx() as c:
            c.executemany(
                """INSERT INTO weld
                   (project_id, document_id, segment, line, weld_no, weld_size,
                    weld_type, wps, welder_root, welder_fill, welder_cap,
                    date_welded, heat_us, heat_ds, nde_id, nde_report, nde_date,
                    nde_technique, nde_status, defect, repair_nde_id,
                    repair_status, source)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
    return ok_files, len(rows)
