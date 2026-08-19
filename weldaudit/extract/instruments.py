"""Building the instrument inventory from calibration-certificate filenames.

Cheapest evidence first: a certificate filed as `Positector 6000 SN 1073795
4.24.25.pdf` gives the instrument, its serial and its calibration date without
opening the file.  Bluewater's convention omits the date, so those rows carry a
serial and no date until a vision pass fills one in — which is enough to
answer "is there a certificate for this gauge at all", the question that
actually catches things.
"""

from __future__ import annotations

from ..db import Database
from ..instruments import parse

SOURCE = "instrument_filename"


def extract(db: Database, project_id: int) -> int:
    """Record one instrument per calibration certificate. Returns the count."""
    rows = db.q(
        """SELECT id, filename, fingerprint FROM document
           WHERE project_id=? AND kind='instrument_cal'""",
        (project_id,),
    )

    # The same certificate is filed into several segment books, exactly as the
    # reader sheets are, so copies collapse on content before anything counts
    # them - otherwise one gauge looks like six.
    seen: set[str] = set()
    records = []
    for r in rows:
        identity = parse(r["filename"])
        if not identity.serial:
            continue
        key = r["fingerprint"] or f"doc:{r['id']}"
        if key in seen:
            continue
        seen.add(key)
        records.append((
            project_id, r["id"], identity.kind, identity.serial,
            identity.serial_key, identity.calibrated, identity.description,
            "filename", SOURCE,
        ))

    with db.tx() as c:
        c.execute("DELETE FROM instrument_cal WHERE project_id=? AND source=?",
                  (project_id, SOURCE))
        if records:
            c.executemany(
                """INSERT INTO instrument_cal
                   (project_id, document_id, kind, serial, serial_key, calibrated,
                    description, evidence, source)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                records,
            )
    return len(records)
