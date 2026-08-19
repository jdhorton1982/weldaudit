"""Reading the approved welding procedure register out of the job's standard.

XTO's welding procedures standard, GPPB-0110, is not a policy document with a
list of procedure names in it — it *contains* the procedures, one API 1104
specification per two pages, each stating its number, its supporting PQR and
its essential variables.  Extracting them turns "which procedures may be used
on this job" from something this tool would otherwise have to assert into
something the corpus supplies.

Only the standard is read.  Contractor procedures submitted for one-time use
are permitted by GPPB-0110 but have to be approved and filed separately, and
nothing in the corpus is one, so there is no format to parse yet.
"""

from __future__ import annotations

import pymupdf

from ..db import Database
from ..wps import parse_register

SOURCE = "wps_standard"

#: Below this many procedure pages a document is not the register — a single
#: stray "WPS NO:" on a cover sheet or a reader sheet should not be read as
#: the job's approved list.
MIN_PROCEDURES = 2


def extract(db: Database, project_id: int) -> int:
    """Load the approved procedure register. Returns the procedures found."""
    documents = db.q(
        """SELECT id, path, filename, fingerprint FROM document
           WHERE project_id=? AND kind='wps' AND ext IN ('.pdf','.PDF')""",
        (project_id,),
    )

    records: list[tuple] = []
    seen: set[str] = set()
    for doc in documents:
        key = doc["fingerprint"] or f"doc:{doc['id']}"
        if key in seen:
            continue
        seen.add(key)
        try:
            with pymupdf.open(doc["path"]) as pdf:
                pages = [(i + 1, page.get_text()) for i, page in enumerate(pdf)]
        except Exception:                     # noqa: BLE001 - an unreadable
            continue                          # standard is not worth a crash
        procedures = parse_register(pages)
        if len(procedures) < MIN_PROCEDURES:
            continue
        for p in procedures:
            records.append((
                project_id, doc["id"], p.wps, p.base_key, p.revision, p.pqr,
                p.code, p.process, p.min_diameter, p.min_wall,
                p.two_welder_over, p.page_no, SOURCE,
            ))

    with db.tx() as c:
        c.execute("DELETE FROM procedure WHERE project_id=? AND source=?",
                  (project_id, SOURCE))
        if records:
            c.executemany(
                """INSERT INTO procedure
                   (project_id, document_id, wps, wps_key, revision, pqr, code,
                    process, min_diameter, min_wall, two_welder_over, page_no,
                    source)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                records,
            )
    return len(records)
