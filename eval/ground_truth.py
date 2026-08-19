"""Where the hand-read pages come from, when there are any.

The benchmark measures how much a model gets right *on these forms* — this
handwriting, these tick boxes, this way of writing a heat number. That only
means anything against pages transcribed by hand, field by field, and those
pages are a customer's documents: their certificates, their weld maps, their
job and contractor names. They are not ours to publish, so they live outside
the repository and are ignored by git.

Put a ``ground_truth.py`` in ``private/`` defining ``PAGES`` and ``CRITICAL``
and the benchmark runs against it. Without one the harness reports an empty
set rather than failing, so a checkout with no corpus still installs, imports
and passes its tests.

The shape ``private/ground_truth.py`` must define::

    #: Fields whose value decides a finding, per document kind.
    CRITICAL = {"mtr": ("heat", "issuing_company"), ...}

    #: One entry per transcribed page.
    PAGES = [
        {"document": "<filename as indexed>", "project": "<job>",
         "page": 0, "kind": "mtr",
         "expect": {"heat": "...", "issuing_company": "...", ...}},
    ]
"""

from __future__ import annotations

import sys
from pathlib import Path

_PRIVATE = Path(__file__).resolve().parents[1] / "private"

CRITICAL: dict[str, tuple[str, ...]] = {}
PAGES: list[dict] = []

if (_PRIVATE / "ground_truth.py").is_file():
    sys.path.insert(0, str(_PRIVATE.parent))
    try:
        from private.ground_truth import CRITICAL, PAGES  # type: ignore  # noqa: F401,F811
    except ImportError:                    # a corpus that does not import
        pass


def have_a_corpus() -> bool:
    """Whether there is anything to measure against on this machine."""
    return bool(PAGES)
