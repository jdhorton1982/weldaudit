"""What a person read off the page, and the last word on it.

Every other reader in this package is a guess with a confidence attached: a
filename, a spreadsheet column, a text layer, a model, an OCR engine. This one
is a fact. It runs after all of them and overwrites whatever they concluded.

It exists because some values are not recoverable by machine at any
resolution. Tex-Tubo prints its name as a logotype — overlapping letters, a
circle fused to the final E — and a vision model reads it as TECKCUBO,
TEKSUMEO or Tekube depending on the page, while OCR returns nothing at all.
Seven certificates became seven critical "not on the approved list" findings
against material that is approved. No filter can fix that, because nothing is
wrong with the filters; the name is a picture of a word.

VIS-02 already tells the auditor to read the letterhead and enter the company.
Until now there was nowhere to enter it.
"""

from __future__ import annotations

from ..db import Database

#: Columns of `material` a correction may set. Deliberately short: this
#: overrides every automated reader, so each field added here is a field where
#: a typo silently becomes the truth.
CORRECTABLE = ("manufacturer", "heat", "issuing_company", "mill_name", "grade", "spec")


def record(db: Database, project_id: int, fingerprint: str, field: str,
           value: str | None, note: str = "") -> None:
    """Set — or with a null value, clear — what a person read from a document."""
    if field not in CORRECTABLE:
        raise ValueError(f"{field!r} is not correctable. Try: {', '.join(CORRECTABLE)}")
    with db.tx() as c:
        if value is None:
            c.execute(
                "DELETE FROM correction WHERE project_id=? AND fingerprint=? "
                "AND field=?", (project_id, fingerprint, field))
        else:
            c.execute(
                """INSERT INTO correction(project_id, fingerprint, field, value, note)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(project_id, fingerprint, field)
                   DO UPDATE SET value=excluded.value, note=excluded.note,
                                 made_at=CURRENT_TIMESTAMP""",
                (project_id, fingerprint, field, value.strip(), note))


def listing(db: Database, project_id: int) -> list:
    return db.q(
        """SELECT c.*, (SELECT filename FROM document d
                        WHERE d.project_id=c.project_id AND d.fingerprint=c.fingerprint
                        LIMIT 1) AS filename
           FROM correction c WHERE c.project_id=? ORDER BY c.made_at DESC""",
        (project_id,))


def apply_corrections(db: Database, project_id: int) -> int:
    """Write every recorded correction over what the readers produced.

    Marked ``confidence='human'``, which matters beyond bookkeeping: VIS-03
    only second-guesses names a model read, and the supplier demotion and the
    disputed-name deferral both look at vision readings. A corrected value is
    exempt from all of them, because the doubt they encode has been answered.
    """
    rows = db.q(
        """SELECT c.fingerprint, c.field, c.value
           FROM correction c WHERE c.project_id=?""", (project_id,))
    if not rows:
        return 0

    changed = 0
    for r in rows:
        if r["field"] not in CORRECTABLE:
            continue            # a field retired since the note was written
        # Every filing copy of the certificate, which is why this is keyed on
        # the fingerprint: the same page is filed into four segment books.
        with db.tx() as c:
            done = c.execute(
                f"""UPDATE material SET {r['field']}=?, confidence='human',
                        evidence='entered by hand'
                    WHERE project_id=? AND document_id IN (
                        SELECT id FROM document
                        WHERE project_id=? AND fingerprint=?)""",
                (r["value"], project_id, project_id, r["fingerprint"]),
            ).rowcount
        changed += max(done, 0)
    return changed
