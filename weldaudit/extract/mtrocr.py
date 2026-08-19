"""Reading scanned certificates with OCR, for nothing, on this machine.

The third way of reading a page, after its text layer and a paid vision model.
It exists because of what the benchmarks actually said, which was not what was
expected:

- A 7B local vision model made 20 silent critical errors against Haiku's 3,
  and tiling did not help it. Its failures were structural — it shifted every
  weld-map callout by one, and swapped as-tested with qualification-range on a
  welder cert. More pixels cannot fix a misunderstanding of the form.
- OCR on the same five certificate layouts found all five heat numbers
  verbatim and all five manufacturers at 100%, in 6-14 seconds a page. It read
  ``Heat Number:071B33`` correctly on the page where Haiku returned 29JD33.

Which is the same conclusion from both directions: recognising printed
characters is solved, and understanding a form is not. So this does no
understanding. It hands the words to the same selection logic ``mtrtext``
uses on text layers — prominent lines near the top, no customer or supplier
labels, and a name the AML already knows — and refuses everything else.

The result is written to ``ocr_cache`` in the shape a vision model would have
returned, under the model name ``local:ocr``. Everything downstream then works
untouched: the replay after a re-index, the supplier demotion, the VIS-01/02
review flags, and ``ocr_any``'s preference for a paid reading over a free one
where both exist.
"""

from __future__ import annotations

import re
from functools import lru_cache

from ..aml import Aml
from ..db import Database
from ..vision import render_page
from .mtrtext import MIN_CHARS, letterhead_from, prominent_lines

#: What the cached payloads are labelled with. The ``local:`` prefix is load
#: bearing: ``Database.ocr_any`` prefers a hosted reading over a local one, so
#: a free pass can never quietly overwrite a certificate somebody paid to read.
OCR_MODEL = "local:ocr"

#: Rendered edge for OCR. Higher than a vision model needs — nothing here is
#: billed per pixel and nothing downsamples the image, so the only cost of
#: detail is a second or two of CPU.
OCR_MAX_EDGE = 2600

#: A page whose text layer already says this much needs no OCR; mtrtext has it.
ALREADY_READABLE = MIN_CHARS

#: "Heat" labels a number on these forms, and also labels heat *treatment*,
#: heat analysis and the heat-affected zone. Requiring a digit in the value is
#: what separates them: a real page produced heat='Treatment' before this.
_HEAT_LABEL = re.compile(
    r"\b(?:heat|colada|coulee|schmelze|charge)\s*"
    # number / No. / N.o / Nr / a bare N., which is how the Spanish and
    # Italian certificates abbreviate it: "COLADA N. 24913".
    r"(?:number|nr|n\.?[o°]|n)?\.?\s*[:.]?\s*"
    r"(?![a-z]+\b)"                      # not a word: Treatment, Analysis
    r"([A-Z0-9][A-Z0-9./-]{2,18})\b",
    re.IGNORECASE)

#: A heat number carries at least one digit. Everything in this corpus does —
#: 24913, 071B33, 34L682W, C48207361 — and nothing that does not is one.
_HAS_A_DIGIT = re.compile(r"\d")

#: Words that follow "Heat" often enough to be worth naming outright.
_NOT_A_HEAT = re.compile(
    r"^(treat(ment)?|analysis|affected|number|no|lot|code|input|exchanger)$",
    re.IGNORECASE)


def heat_in(text: str) -> str | None:
    """The heat number a line states, if it states one."""
    found = _HEAT_LABEL.search(text)
    if not found:
        return None
    value = found.group(1)
    if _NOT_A_HEAT.match(value) or not _HAS_A_DIGIT.search(value):
        return None
    return value


#: rapidocr loads its detector, recogniser and classifier with
#: ``importlib.import_module("ch_ppocr_v3_det")`` — a bare top-level name,
#: which works only because the library puts its own directory on sys.path.
#: A packaged build has no such directory: the modules are in the archive
#: under their real dotted names, so the bare import finds an empty namespace
#: and the failure reads "module has no attribute TextDetector".
_BARE_NAMES = ("ch_ppocr_v3_det", "ch_ppocr_v3_rec", "ch_ppocr_v2_cls")


def _alias_the_submodules() -> None:
    """Make the bare names resolve, frozen or not."""
    import importlib
    import sys

    for name in _BARE_NAMES:
        if name in sys.modules:
            continue
        try:
            sys.modules[name] = importlib.import_module(
                f"rapidocr_onnxruntime.{name}")
        except ImportError:
            pass          # let rapidocr raise its own error about it


@lru_cache(maxsize=1)
def _engine():
    """The OCR engine, loaded once. Returns None if it was not bundled."""
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return None
    _alias_the_submodules()
    try:
        return RapidOCR()
    except Exception:                     # noqa: BLE001 - a broken bundle
        return None


def available() -> tuple[bool, str]:
    return ((True, "rapidocr") if _engine() is not None
            else (False, "OCR is not available in this build"))


def read_page_lines(path, page_no: int = 0):
    """``(lines, image height)`` where each line is ``(text, top, height)``.

    The height comes back too because OCR reports pixels of the rendered
    image, not points, and "near the top of the page" is a fraction of it.
    Deriving it from the boxes instead — the extent of the text that was
    found — silently shrinks the page to fit whatever was read, so a
    certificate whose only text is a letterhead has no top band at all.
    """
    engine = _engine()
    if engine is None:
        return [], 1.0
    image, _ = render_page(path, page_no, OCR_MAX_EDGE)
    result, _ = engine(image)
    lines: list[tuple[str, float, float]] = []
    for row in result or []:
        box, text = row[0], str(row[1])
        ys = [point[1] for point in box]
        lines.append((text, min(ys), max(ys) - min(ys)))
    return lines, _image_height(image)


def _image_height(image: bytes) -> float:
    """Pixel height of a rendered JPEG, without decoding all of it."""
    import pymupdf

    try:
        return float(pymupdf.Pixmap(image).height) or 1.0
    except Exception:                     # noqa: BLE001 - fall back to the cap
        return float(OCR_MAX_EDGE)


def payload_for(path, aml: Aml | None, page_no: int = 0) -> dict | None:
    """A certificate payload from a scan, or None if nothing was settled."""
    lines, height = read_page_lines(path, page_no)
    if not lines:
        return None

    heading = prominent_lines(lines, height)
    decided = letterhead_from(heading, aml)
    heat = next((found for text, _top, _h in lines
                 if (found := heat_in(text))), None)

    if not decided and not heat:
        return None

    company = decided[0] if decided else None
    printed = decided[1] if decided else None
    return {
        "page_is_certificate": True,
        "heat": heat,
        # The AML's spelling, not the page's. OCR reads a Halden letterhead as
        # "halden mfg.co., I.p." and that string becomes the manufacturer on
        # the material row and in every finding that quotes it. The fuzzy match
        # already decided which company it is; recording anything else just
        # passes the scanner's mistakes on to the reader. What was actually
        # printed is kept in `_ocr` so the match can be checked.
        "issuing_company": company or printed,
        # OCR reads no labels, so it cannot say who bought the material. Null
        # rather than absent: the appliers treat a missing key and a null the
        # same, but a payload that matches the schema is one fewer thing to
        # reason about when a reading turns out wrong.
        "customer": None,
        # OCR is given no way to name a second producer. It cannot tell a
        # Works line from a Supplier line, and the whole reason mill_name
        # outranks the letterhead is that somebody read a label to say so.
        "mill_name": None,
        "mill_source": None,
        # OCR reads no labels, so it can no more find the heat on a melt line
        # than it can tell that line from a works line.
        "mill_heat": None,
        "mill_location": None,
        "country_of_melt": None,
        "country_of_manufacture": None,
        "specification": None,
        "grade": None,
        "size": None,
        "wall_thickness": None,
        "description": None,
        "_ocr": {"lines": len(lines),
                 "letterhead": printed,
                 "matched": company},
    }


def scanned_targets(db: Database, project_id: int, limit: int | None = None,
                    aml: Aml | None = None):
    """Scans worth reading: no manufacturer yet, or one nothing recognises.

    The second set is the interesting one. A paid reading that produced a name
    the approved list has never heard of is either a real non-conformance or a
    misread letterhead, and those look identical from here. OCR is free and
    reads small print differently — it got TEXTUBO off the page that Haiku
    read as TECKCUBO, TECKQUBO, TEKSUMEO and four other spellings — so asking
    it costs nothing and either corroborates the finding or contradicts it.

    Nothing is overturned on OCR's word: a contradiction is recorded as a
    disagreement between readers, which withholds the critical finding and
    asks a person to look. See ``note_reader_disagreements``.
    """
    import pymupdf

    rows = db.q(
        """SELECT m.id, m.manufacturer, m.confidence,
                  d.id AS document_id, d.path, d.filename, d.fingerprint
           FROM material m JOIN document d ON d.id = m.document_id
           WHERE m.project_id=? AND m.source='mtr_file'
             AND (IFNULL(m.manufacturer,'') = '' OR m.confidence = 'vision')""",
        (project_id,),
    )

    def worth_reading(r) -> bool:
        name = (r["manufacturer"] or "").strip()
        if not name:
            return True                    # nothing knows who made this
        # A paid reading nothing recognises. Whether the AML has never heard
        # of the mill or the letterhead was garbled looks identical from here,
        # and OCR is free, so ask it.
        return aml is not None and aml.match(name).status == "not_listed"
    out, seen = [], set()
    for r in rows:
        fp = r["fingerprint"] or str(r["document_id"])
        if fp in seen or not worth_reading(r):
            continue
        seen.add(fp)
        try:
            with pymupdf.open(r["path"]) as doc:
                if not doc.page_count:
                    continue
                if (not (r["manufacturer"] or "").strip()
                        and len(doc[0].get_text("text").strip()) >= ALREADY_READABLE):
                    continue          # mtrtext reads this one for free already
        except Exception:             # noqa: BLE001 - unreadable file, skip
            continue
        out.append(r)
        if limit and len(out) >= limit:
            break
    return out


def read_scans(db: Database, project_id: int, aml: Aml | None,
               limit: int | None = None, progress=None) -> dict:
    """OCR the scanned certificates and cache what they say.

    Nothing is written to `material` here. The payloads go into the same cache
    the vision passes use, and the ordinary replay folds them in — so an OCR
    reading is applied, demoted and reviewed by exactly the code that handles
    a paid one.
    """
    targets = scanned_targets(db, project_id, limit, aml)
    done = {"documents": len(targets), "read": 0, "named": 0, "cached": 0}
    for i, r in enumerate(targets, 1):
        fingerprint = r["fingerprint"] or str(r["document_id"])
        key = f"{fingerprint}:mtr:{OCR_MAX_EDGE}:ocr"
        if progress:
            progress(i, len(targets), r["filename"])
        if db.ocr_get(key, 0, OCR_MODEL) is not None:
            done["cached"] += 1
            continue
        try:
            payload = payload_for(r["path"], aml)
        except Exception:             # noqa: BLE001 - one bad scan is not fatal
            continue
        done["read"] += 1
        if payload is None:
            continue
        done["named"] += bool(payload.get("issuing_company"))
        db.ocr_put(key, 0, OCR_MODEL, payload)
    return done
