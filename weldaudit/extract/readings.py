"""A letterhead this job cannot spell, and the mill it belongs to.

The document-level correction in ``corrections.py`` answers "what does this
page say". This answers "what does this *letterhead* say", which is a
different question and a much cheaper one when a vendor's name is a logotype.

Tex-Tubo prints its name as overlapping letters with a circle fused to the
final E. Across twenty-four certificates on one job the readers spelled it
TEXTUBOO, TEKTUBE, TEXQUBEO, TEKKUBEO, tex-tubo.com and six other ways, and
returned nothing at all six times. Correcting that page by page is
twenty-four separate acts of judgement about one company.

Two rules keep it honest:

* **Nothing is guessed.** ``propose`` finds strings that look like a recorded
  misreading, but proposing is not recording — a person accepts them. The same
  filename family that holds the Tex-Tubo certificates also holds one from
  Borusan Mannesmann, and a sweep by pattern would have relabelled it.
* **It never overwrites a person.** A value somebody typed against a specific
  document outranks a rule about a name.
"""

from __future__ import annotations

from ..aml import normalise_manufacturer
from ..db import Database

#: How alike an unresolved name must be to one already confirmed before it is
#: worth putting in front of somebody. A suggestion threshold, not a decision:
#: the Tex-Tubo spellings sit around 70 to each other, and two genuinely
#: different companies on this corpus reached 78, so nothing here is safe to
#: apply without a person looking.
SUGGEST = 62


def record(db: Database, project_id: int, as_read: str, manufacturer: str,
           note: str = "") -> None:
    with db.tx() as c:
        c.execute(
            """INSERT INTO vendor_reading(project_id, as_read_key, as_read,
                                          manufacturer, note)
               VALUES(?,?,?,?,?)
               ON CONFLICT(project_id, as_read_key)
               DO UPDATE SET manufacturer=excluded.manufacturer,
                             as_read=excluded.as_read, note=excluded.note,
                             made_at=CURRENT_TIMESTAMP""",
            (project_id, normalise_manufacturer(as_read), as_read.strip(),
             manufacturer.strip(), note))


def forget(db: Database, project_id: int, as_read: str) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM vendor_reading WHERE project_id=? AND as_read_key=?",
                  (project_id, normalise_manufacturer(as_read)))


def listing(db: Database, project_id: int) -> list:
    return db.q("SELECT * FROM vendor_reading WHERE project_id=? ORDER BY manufacturer,"
                " as_read", (project_id,))


def unresolved_names(db: Database, project_id: int) -> list:
    """Manufacturer strings on this job that no person has confirmed."""
    return db.q(
        """SELECT manufacturer AS name, COUNT(*) AS n FROM material
           WHERE project_id=? AND IFNULL(manufacturer,'')<>''
             AND IFNULL(confidence,'') <> 'human'
           GROUP BY manufacturer ORDER BY manufacturer""",
        (project_id,))


def propose(db: Database, project_id: int, like: str,
            threshold: int = SUGGEST) -> list[tuple[str, int, int]]:
    """``(name, certificates, similarity)`` for names resembling ``like``.

    Suggestions only. Nothing here is recorded until somebody says so.
    """
    from rapidfuzz import fuzz

    seed = normalise_manufacturer(like)
    known = {r["as_read_key"] for r in listing(db, project_id)}
    out = []
    for r in unresolved_names(db, project_id):
        key = normalise_manufacturer(r["name"])
        if not key or key in known:
            continue
        score = int(fuzz.ratio(seed, key))
        if score >= threshold:
            out.append((r["name"], r["n"], score))
    return sorted(out, key=lambda x: -x[2])


def apply_readings(db: Database, project_id: int) -> int:
    """Rewrite every unresolved name a person has identified.

    Matched on the normalised form, so one row covers `TEX-TUBO`, `Tex Tubo`
    and `tex tubo.`. Never touches a row already set by hand against its own
    document: that is a statement about a specific page and outranks a rule
    about a name.
    """
    rules = {r["as_read_key"]: r["manufacturer"] for r in listing(db, project_id)}
    if not rules:
        return 0

    changed = 0
    for r in unresolved_names(db, project_id):
        target = rules.get(normalise_manufacturer(r["name"]))
        if not target or target == r["name"]:
            continue
        with db.tx() as c:
            done = c.execute(
                """UPDATE material SET manufacturer=?, confidence='human',
                       evidence=?
                   WHERE project_id=? AND manufacturer=?
                     AND IFNULL(confidence,'') <> 'human'""",
                (target, f"letterhead read by hand as '{r['name']}'",
                 project_id, r["name"]),
            ).rowcount
        changed += max(done, 0)
    return changed
