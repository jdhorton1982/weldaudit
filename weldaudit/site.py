"""Tokens that belong to one operator's paperwork, not to the program.

The program is generic; the documents it reads are not. An operator stamps its
own tag on purchase orders, and a job writes its own code in front of every
line number - and both have to be recognised, or a tag reads as a heat number
and a job code reads as a weld.

Hard-coding them was wrong twice over. It put a customer's name in a public
repository, and it assumed every package came from the same customer: the next
operator's tag is not ACME and the next job's code is not KST, so a program
that knows only one of each is a program that misreads everybody else.

They live in ``weldaudit/data/site.json``, beside the approved materials list
and the agreements and for the same two reasons: that folder is gitignored, so
nothing of a customer's reaches the history, and it is bundled into the build,
so a released copy carries whatever was there when it was made.

A build with no file, or with a half-written one, is a working build. Nothing
here is required - the generic prefixes below are enough to read a package
from an operator nobody has configured, and the cost of a missing entry is a
finding too many rather than a document silently misread.
"""

from __future__ import annotations

import functools
import json
import sys
from pathlib import Path

#: Prefixes that mark an internal reference rather than a heat number, on any
#: job anywhere. ``PO-4471`` is a purchase order and ``REV 2`` is a revision
#: whoever issued them.
GENERIC_TAGS: tuple[str, ...] = ("TAG", "PO", "REV")


def _folder() -> Path:
    unpacked = getattr(sys, "_MEIPASS", None)
    root = Path(unpacked) / "weldaudit" if unpacked else Path(__file__).parent
    return root / "data"


@functools.lru_cache(maxsize=1)
def _loaded() -> dict:
    """``site.json``, or an empty configuration.

    Read once. A malformed file is treated as absent rather than raised on:
    the alternative is a program that will not start because somebody left a
    trailing comma in a list of prefixes.
    """
    try:
        said = json.loads((_folder() / "site.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return said if isinstance(said, dict) else {}


def _words(key: str) -> tuple[str, ...]:
    got = _loaded().get(key) or ()
    if isinstance(got, str):
        got = [got]
    return tuple(str(w).strip().upper() for w in got if str(w).strip())


def tag_prefixes() -> tuple[str, ...]:
    """Prefixes that mark an internal tag: never a heat number.

    The generic ones always apply; the operator's own are added to them.
    """
    return tuple(dict.fromkeys(GENERIC_TAGS + _words("tag_prefixes")))


def job_codes() -> tuple[str, ...]:
    """Tokens that prefix a line number and are not weld identifiers.

    Nothing generic here: a job code is whatever this job calls itself, and
    guessing would cost real welds.
    """
    return _words("job_codes")


def operator() -> str:
    """The operator's name, for the titles on its own forms. '' if unset."""
    return str(_loaded().get("operator") or "").strip()


def titled(form: str) -> str:
    """``'DAILY FIELD COATING INSPECTION REPORT'`` with the operator in front.

    Used in the prompts that tell a vision model what page it is looking at.
    Naming the operator helps where the form carries its logo, and an unset
    operator simply asks for the form by its own title.
    """
    name = operator()
    return f"{name} {form}" if name else form


def forget() -> None:
    """Drop the cached file. For tests, and for a reload after editing it."""
    _loaded.cache_clear()
