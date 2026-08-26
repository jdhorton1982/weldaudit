"""The documents somebody agrees to before this program audits anything.

A beta build goes to people who are not the person who wrote it, and it goes
onto machines that hold a customer's turnover package. Two obligations run in
opposite directions at once: what the tester must not do with the program, and
what the program does with the tester's documents. Both are written down, both
are shown, and the answer is recorded.

**A recorded click is evidence, not a signature.** It lives in a database on
the tester's own machine, which the tester controls, so it proves far less
than a countersigned agreement obtained before the build was handed over. What
it does prove is the thing a signature does not reach: the second and third
engineer who got a copy from the person who signed. They open the program,
they are shown the terms, and their acceptance is recorded against their name
and their machine. "Nobody told me" stops being available.

Identity is the hash of the text, not a version number somebody remembers to
bump. Edit a document and it becomes a document nobody has accepted yet, so
everybody is asked again — which is the only behaviour that cannot silently
record agreement to wording that has since changed.

The documents live in ``weldaudit/data/agreements`` beside the approved
materials list, and for the same two reasons: that folder is gitignored, so
commercial terms do not reach a public repository, and it is bundled into the
build, so a released copy carries them. A build made without the folder has no
agreements to show and does not gate - see :func:`gate_is_armed`.
"""

from __future__ import annotations

import hashlib
import os
import platform
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

#: Shown, and asked about, in this order. A file that is not named here is
#: ignored rather than shown in whatever order the filesystem returns them:
#: the order these are read in is part of what was agreed to.
ORDER: tuple[tuple[str, str], ...] = (
    ("privacy", "privacy-and-security.txt"),
    ("nda", "nda.txt"),
    ("pilot", "pilot-agreement.txt"),
)


@dataclass(frozen=True)
class Document:
    """One agreement, as it is on disk right now."""

    key: str
    title: str
    body: str
    sha256: str

    @property
    def version(self) -> str:
        """A short handle for the exact wording, for a person to quote."""
        return self.sha256[:12]

    @property
    def words(self) -> int:
        return len(self.body.split())


def folder() -> Path:
    """Where the agreement texts live, from source or from the packaged exe."""
    unpacked = getattr(sys, "_MEIPASS", None)
    root = Path(unpacked) / "weldaudit" if unpacked else Path(__file__).parent
    return root / "data" / "agreements"


def documents() -> list[Document]:
    """Every agreement this build carries, in the order they are shown.

    A file that is missing is skipped rather than raising: a build without the
    data folder is a valid build, it simply has nothing to ask about.
    """
    out: list[Document] = []
    here = folder()
    for key, name in ORDER:
        path = here / name
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        text = raw.replace("\r\n", "\n").strip()
        if not text:
            continue
        title, _, body = text.partition("\n")
        out.append(Document(
            key=key,
            title=title.strip().lstrip("# ").strip(),
            body=body.strip(),
            # Over the whole file, title included: changing the title changes
            # what was agreed to as surely as changing a clause.
            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        ))
    return out


def gate_is_armed() -> bool:
    """Whether this build has anything to require agreement to.

    False for a build made without the data folder. That is deliberate rather
    than a hole: the same build carries no approved materials list either, and
    a program that refused to start because a folder the author chose not to
    ship is absent would be a worse failure than one that does not gate.
    """
    return bool(documents())


def who() -> dict[str, str]:
    """What the machine can say about itself, to sit alongside the name typed."""
    from . import __version__

    return {
        "machine": platform.node() or "",
        "account": os.environ.get("USERNAME") or os.environ.get("USER") or "",
        "app_version": __version__,
        "platform": f"{platform.system()} {platform.release()}".strip(),
    }


def accepted(db, document_key: str | None = None) -> list:
    """Every acceptance recorded on this machine, newest first."""
    if document_key:
        return db.q(
            "SELECT * FROM agreement_acceptance WHERE document_key=? "
            "ORDER BY accepted_at DESC", (document_key,))
    return db.q("SELECT * FROM agreement_acceptance ORDER BY accepted_at DESC")


def outstanding(db) -> list[Document]:
    """The documents nobody on this machine has accepted at this wording.

    Matched on the hash, so an edited document is outstanding again even
    though its name and its key have not changed.
    """
    if not (docs := documents()):
        return []
    agreed = {r["sha256"] for r in db.q(
        "SELECT DISTINCT sha256 FROM agreement_acceptance")}
    return [d for d in docs if d.sha256 not in agreed]


def record(db, document: Document, name: str, company: str,
           email: str = "") -> int:
    """Write down that a named person accepted this exact wording.

    The hash is stored rather than the text: the text is in the build, and a
    hash is what lets a record be checked against the wording later without
    keeping a copy of every version in the database.
    """
    if not (name or "").strip():
        raise ValueError("An acceptance has to carry the name of the person accepting.")

    facts = who()
    with db.tx() as c:
        cur = c.execute(
            """INSERT INTO agreement_acceptance
               (document_key, document_title, sha256, name, company, email,
                accepted_at, machine, account, app_version, platform)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (document.key, document.title, document.sha256,
             name.strip(), (company or "").strip(), (email or "").strip(),
             datetime.now(timezone.utc).isoformat(timespec="seconds"),
             facts["machine"], facts["account"], facts["app_version"],
             facts["platform"]))
    return cur.lastrowid


def acceptance_of(db, document: Document):
    """The record of this exact wording being accepted here, or None.

    Matched on the hash as well as the key, so what comes back is the
    acceptance of the text being shown rather than of an earlier revision
    that happened to carry the same name.
    """
    return db.one(
        "SELECT * FROM agreement_acceptance WHERE document_key=? AND sha256=? "
        "ORDER BY accepted_at DESC LIMIT 1",
        (document.key, document.sha256))


def record_as_text(db) -> str:
    """The acceptances on this machine, as something a person can read and send.

    The point of an exportable record is that it leaves the machine the tester
    controls. A row in a local database is only worth what the machine it sits
    on is worth.
    """
    rows = accepted(db)
    if not rows:
        return "No agreement has been accepted on this machine.\n"

    lines = ["WeldAudit - record of agreement", ""]
    for r in rows:
        lines += [
            f"  {r['document_title']}",
            f"    accepted by   {r['name']}"
            + (f" ({r['company']})" if r["company"] else "")
            + (f" <{r['email']}>" if r["email"] else ""),
            f"    when          {r['accepted_at']} UTC",
            f"    machine       {r['machine']} / {r['account']} / {r['platform']}",
            f"    WeldAudit     {r['app_version']}",
            f"    wording       sha256 {r['sha256']}",
            "",
        ]
    lines += [
        "The wording line identifies the exact text that was on screen. Compare",
        "it against the document of the same name in the build to confirm what",
        "was agreed to.",
        "",
    ]
    return "\n".join(lines)
