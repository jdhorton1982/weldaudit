"""Welders: who welded what, and which certifications are on file.

Runs in two passes because each informs the other.  The weld reports establish
which stencils are real on this job, and that set is then used to read the
certification filenames - without it a surname like "Babb" is indistinguishable
from a welder code.
"""

from __future__ import annotations

from ..db import Database
from ..welders import parse_cert_filename, stencils_of


def known_nde_prefixes(db: Database, project_id: int) -> set[str]:
    return {
        r["prefix"]
        for r in db.q(
            "SELECT DISTINCT prefix FROM nde_shot WHERE project_id=? AND prefix<>''",
            (project_id,),
        )
    }


def extract_passes(db: Database, project_id: int) -> tuple[int, int]:
    """Explode each weld's welder columns into one row per welder.

    Returns ``(rows, distinct stencils)``.
    """
    prefixes = known_nde_prefixes(db, project_id) or None
    welds = db.q(
        """SELECT id, document_id, segment, line, weld_no, date_welded,
                  welder_root, welder_hp, welder_fill, welder_cap
           FROM weld WHERE project_id=?""",
        (project_id,),
    )

    with db.tx() as c:
        c.execute("DELETE FROM welder_pass WHERE project_id=?", (project_id,))

    rows: list[tuple] = []
    stencils: set[str] = set()
    for w in welds:
        field = stencils_of(
            w["welder_root"] or "", w["welder_hp"] or "",
            w["welder_fill"] or "", w["welder_cap"] or "",
            nde_prefixes=prefixes,
        )
        for stencil in field.stencils:
            stencils.add(stencil)
            rows.append(
                (
                    project_id, w["id"], w["document_id"], w["segment"],
                    w["line"], w["weld_no"], stencil, w["date_welded"],
                )
            )

    if rows:
        with db.tx() as c:
            c.executemany(
                """INSERT INTO welder_pass
                   (project_id, weld_id, document_id, segment, line, weld_no,
                    stencil, date_welded)
                   VALUES (?,?,?,?,?,?,?,?)""",
                rows,
            )
    return len(rows), len(stencils)


def extract_certs(db: Database, project_id: int) -> int:
    """Read every welder certification document found on disk.

    Must run after :func:`extract_passes`, which supplies the stencil
    vocabulary the filenames are read against.
    """
    known = {
        r["stencil"]
        for r in db.q(
            "SELECT DISTINCT stencil FROM welder_pass WHERE project_id=?", (project_id,)
        )
    }
    docs = db.q(
        """SELECT id, filename, segment, fingerprint FROM document
           WHERE project_id=? AND kind='welder_cert'""",
        (project_id,),
    )

    with db.tx() as c:
        c.execute("DELETE FROM welder_cert WHERE project_id=?", (project_id,))

    seen: set[str] = set()
    rows: list[tuple] = []
    for d in docs:
        # Certifications are filed into every book the welder worked in.
        fp = d["fingerprint"] or str(d["id"])
        if fp in seen:
            continue
        seen.add(fp)

        cert = parse_cert_filename(d["filename"], known or None)
        rows.append(
            (
                project_id, d["id"], d["segment"], cert.stencil, cert.name,
                cert.process, cert.material, cert.cert_date, cert.expiry,
                int(cert.requalification), cert.wps,
            )
        )

    if rows:
        with db.tx() as c:
            c.executemany(
                """INSERT INTO welder_cert
                   (project_id, document_id, segment, stencil, name, process,
                    material, cert_date, expiry, requal, wps)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
    return len(rows)
