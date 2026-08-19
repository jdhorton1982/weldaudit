"""Names a certificate uses for a company the AML lists under another.

Fuzzy matching handles spelling, punctuation and legal forms. It cannot handle
a company that has been renamed, acquired, or trades under something unlike
its legal name — those are facts about the world, not about strings, and no
similarity score will recover them.

The case that prompted this: certificates read ``ORTEGA FORJA, S.COOP.`` and the
AML lists ``Ortega Advanced Forged Solutions``. Same firm. They share one word,
so the match scores 51 and fourteen certificates were reported as critical
non-conformances against approved material — the largest single block of false
findings on Bluewater 14.

So this is a file rather than a table in the code. It is the auditor's
knowledge of who is who, it will grow every time a mill rebrands, and needing
a developer for that would be absurd. It lives beside the database and is
created with its own instructions in it the first time anything looks for it.
"""

from __future__ import annotations

import csv
from pathlib import Path

from .aml import normalise_manufacturer

#: Written into a new file so the format explains itself to whoever opens it.
_HEADER = [
    "# WeldAudit — manufacturer aliases",
    "#",
    "# One line per company the certificates and the AML call different things.",
    "# Left: the name as it appears on the certificate. Right: the name as it",
    "# appears on the approved materials list. Case and punctuation do not",
    "# matter on either side.",
    "#",
    "# This is for companies that are genuinely the same firm — a rebrand, an",
    "# acquisition, a trading name. Do NOT use it to map a misread name onto a",
    "# real one: that hides a bad scan behind an approval, which is the one",
    "# mistake this tool exists to prevent.",
    "#",
    "# Lines starting with # are ignored. Edit in Notepad or Excel.",
    "",
    "certificate name,AML name",
]

#: Seeded from two verified by hand against the certificates and the AML.
_SEED = [
    ("ORTEGA FORJA, S.COOP.", "Ortega Advanced Forged Solutions"),
    ("Aceros del Norte, S.A.", "Norvale Tamsa"),
]


def default_path() -> Path:
    from .pipeline import default_db_path

    return Path(default_db_path()).parent / "manufacturer-aliases.csv"


def write_default(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        handle.write("\n".join(_HEADER) + "\n")
        writer = csv.writer(handle)
        for written, aml_name in _SEED:
            writer.writerow([written, aml_name])
    return path


def load(path: Path | None = None) -> dict[str, str]:
    """``{normalised certificate name: AML name}``, creating the file if absent.

    Keyed on the normalised form so one line covers every way the same name is
    punctuated — "ORTEGA FORJA, S.COOP." and "Ortega Forja S Coop" are one entry.
    """
    path = Path(path) if path else default_path()
    if not path.exists():
        try:
            write_default(path)
        except OSError:
            return {}

    out: dict[str, str] = {}
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.reader(handle):
                if not row or row[0].lstrip().startswith("#") or len(row) < 2:
                    continue
                written, aml_name = row[0].strip(), row[1].strip()
                if not written or not aml_name:
                    continue
                key = normalise_manufacturer(written)
                if key and key != normalise_manufacturer("certificate name"):
                    out[key] = aml_name
    except OSError:
        return {}
    return out
