"""Reading scanned documents with a vision model.

About 57% of the PDFs in this corpus are image-only, and they are concentrated
where the audit value is: MTRs and NDE reader sheets.  Everything the other
extractors do is deterministic and free; this module is the one place that
spends money, so it is built around three rules.

**Targeted, not exhaustive.**  Nothing here sweeps the corpus.  Callers pass
the specific documents a finding says are worth reading - the heats with no
machine-readable manufacturer, the reader sheets behind an unresolved reject -
so a useful pass costs cents rather than reading eight thousand pages.

**Cached by content.**  Results are keyed on the page's document fingerprint,
so the eleven filed copies of one reader sheet cost one read, re-runs cost
nothing, and a rule change never re-reads a page.

**Never silently guessing.**  Every extracted field can come back null, and the
model is told to leave it null rather than infer.  A confident wrong heat
number is far worse than a blank one, because a blank is visible.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from .db import Database

#: Input / output price per million tokens, for the dry-run estimate.
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

DEFAULT_MODEL = "claude-opus-5"

#: Models that accept ``output_config.effort``. Sending it to one that does not
#: is a 400, not a warning, so the pass fails on every page rather than reading
#: them slightly differently. Listed rather than inferred from the name because
#: getting it wrong is silent until a run costs someone an afternoon.
EFFORT_CAPABLE = frozenset({"claude-opus-5", "claude-sonnet-5"})

#: A model served by a local Ollama daemon, named ``local:qwen2.5vl:7b``.
#: These cost nothing, send nothing off the machine, and are the only way the
#: pass runs at all on an install with no Anthropic credentials — which is
#: every install so far. Quality is the open question, which is what
#: ``eval/`` exists to answer.
LOCAL_PREFIX = "local:"

#: Where the daemon listens. Ollama's own environment variable, so a user who
#: has already moved it does not have to tell us twice.
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")

#: How long to wait for one page. Generous, because the first request of a run
#: also loads six gigabytes of weights.
LOCAL_TIMEOUT_S = 900

#: Local models take images at their native resolution and turn them into
#: vision tokens, so a 2000px page costs roughly four times a 1000px one and
#: on a laptop GPU that is the difference between reading a page and timing
#: out. The hosted models are billed per image at a capped token count, so
#: they have no such incentive — hence a separate default.
LOCAL_MAX_EDGE = 1100


def is_local(model: str) -> bool:
    return model.startswith(LOCAL_PREFIX)


def local_model_name(model: str) -> str:
    return model[len(LOCAL_PREFIX):]

#: Longest image edge sent to the model.  These are scans of forms - the
#: numbers that matter are often hand-written into small boxes, so resolution
#: buys accuracy.  Claude's high-resolution tier accepts up to 2576.
DEFAULT_MAX_EDGE = 2000

#: Roughly how many image tokens a rendered page costs.  Anthropic's guidance
#: is width*height/750; the high-resolution tier caps out near 4784.
_TOKENS_PER_PIXEL = 1 / 750
_MAX_IMAGE_TOKENS = 4784

#: Cost model for the estimate: a cached instruction prefix plus a short reply.
_PROMPT_TOKENS = 700
_OUTPUT_TOKENS = 500


class VisionUnavailable(RuntimeError):
    """No Anthropic credentials are configured on this machine."""


# ---------------------------------------------------------------------------
# Extraction schemas
# ---------------------------------------------------------------------------

#: Hand-written rather than generated from a model class: structured outputs
#: require `additionalProperties: false` and an explicit `required` list on
#: every object, and writing them out keeps the contract visible next to the
#: prompt that has to satisfy it.

READER_SHEET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ticket_no", "sheet_date", "technician", "inspector",
                 "procedure", "job_number", "weld_count", "page_number",
                 "page_total", "rows", "page_is_reader_sheet"],
    "properties": {
        "page_is_reader_sheet": {
            "type": "boolean",
            "description": "False if this page is not an NDE examination report.",
        },
        "weld_count": {
            "type": ["integer", "null"],
            "description": "The form's own stated count of welds examined, "
                           "where it prints one - the Precision Group form has "
                           "a 'Weld Count' box at the foot. Null if absent.",
        },
        "page_number": {
            "type": ["integer", "null"],
            "description": "From the report's own 'Page __ of __' at the top "
                           "right: the first box. The labels are printed on "
                           "every sheet and usually left blank - return null "
                           "when they are, rather than counting for the crew.",
        },
        "page_total": {
            "type": ["integer", "null"],
            "description": "The second box of 'Page __ of __'. Null if blank.",
        },
        "ticket_no": {"type": ["string", "null"]},
        "sheet_date": {
            "type": ["string", "null"],
            "description": "Examination date exactly as printed, e.g. '09/09/25'.",
        },
        "technician": {
            "type": ["string", "null"],
            "description": "Technician or radiographer name printed on the sheet.",
        },
        "inspector": {"type": ["string", "null"]},
        "procedure": {
            "type": ["string", "null"],
            "description": "Procedure/revision, e.g. 'IIA-FS-RT-002 REV 1'.",
        },
        "job_number": {"type": ["string", "null"]},
        "rows": {
            "type": "array",
            "description": "One entry per weld row that has any data.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["weld_id", "area", "result", "pipe_diameter",
                             "wall_thickness", "welder_stencil", "indications",
                             "remarks"],
                "properties": {
                    "weld_id": {
                        "type": ["string", "null"],
                        "description": "Weld number exactly as written, e.g. 'GFB-37'. "
                                       "Where one weld spans several sub-rows, repeat "
                                       "it on every one of them.",
                    },
                    "area": {
                        "type": ["string", "null"],
                        "description": "The Area cell where the form assesses a weld "
                                       "in sections, e.g. '0-A', 'A-B', 'B-0'. Null "
                                       "on forms with one row per weld.",
                    },
                    "result": {
                        "enum": ["ACC", "REJ", None],
                        "description": "ACC if the accept box is marked or the result "
                                       "cell reads Accept, REJ if the reject box is "
                                       "marked or the cell reads Rejected, null if "
                                       "neither is clear.",
                    },
                    "pipe_diameter": {"type": ["string", "null"]},
                    "wall_thickness": {"type": ["string", "null"]},
                    "welder_stencil": {
                        "type": ["string", "null"],
                        "description": "Welder stencil column, e.g. 'ANR/AOI'.",
                    },
                    "indications": {"type": ["string", "null"]},
                    "remarks": {"type": ["string", "null"]},
                },
            },
        },
    },
}

MTR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["page_is_certificate", "heat", "issuing_company", "customer",
                 "mill_name", "mill_source", "mill_heat", "mill_location",
                 "country_of_melt",
                 "country_of_manufacture", "specification", "grade", "size",
                 "wall_thickness", "description"],
    "properties": {
        "page_is_certificate": {
            "type": "boolean",
            "description": "False if this page is not a material test certificate.",
        },
        "heat": {
            "type": ["string", "null"],
            "description": "Heat / cast number exactly as printed. Printed in "
                           "several tables on the same page; report it only if "
                           "they agree, null if they do not.",
        },
        "issuing_company": {
            "type": ["string", "null"],
            "description": "Company on the letterhead, top-left beside the logo "
                           "in small type - may be a distributor or a machine "
                           "shop rather than the mill. Never the client, buyer "
                           "or steel supplier.",
        },
        "customer": {
            "type": ["string", "null"],
            "description": "Whoever bought the material - the CLIENT, CUSTOMER, "
                           "CLIENTE, BESTELLER, PURCHASER, SOLD TO or SHIP TO "
                           "field. Report it even though the audit does not "
                           "check it: naming it is how the buyer is kept out "
                           "of the manufacturer, which is the single most "
                           "common way these certificates are misread.",
        },
        "mill_name": {
            "type": ["string", "null"],
            "description": "The mill that actually produced the material, if the "
                           "certificate states one distinct from the letterhead. "
                           "A company name, never a place - a town or state "
                           "belongs in mill_location. Null if the document "
                           "does not say.",
        },
        "mill_heat": {
            "type": ["string", "null"],
            "description": "If the line naming mill_name also states a heat, "
                           "cast or certificate number OF ITS OWN, report it "
                           "exactly as printed. Null if that line states no "
                           "number, or states this certificate's own heat. "
                           "This is how a melt origin gives itself away: the "
                           "steel it names arrived under a different heat "
                           "than the item this certificate covers.",
        },
        "mill_source": {
            "enum": ["letterhead", "works_line", "supplier_line", None],
            "description": "Which part of the page mill_name was read from. "
                           "'letterhead' if it is simply the company whose "
                           "letterhead this is; 'works_line' if a Works, "
                           "Plant, Mill or Manufactured-By field names a "
                           "different producer of THIS item; 'supplier_line' "
                           "if it came from a Supplier, Steel Supplier, Steel "
                           "Rolling, Starting Material or Mill Test Report "
                           "line, which name the source of the raw material. "
                           "Null when mill_name is null.",
        },
        "mill_location": {
            "type": ["string", "null"],
            "description": "Where the material was made, e.g. 'Mingo Junction, "
                           "OH'. A place, not a company.",
        },
        "country_of_melt": {
            "type": ["string", "null"],
            "description": "Country the steel was melted in, e.g. 'USA'. Often "
                           "a 'Steel Melted' or 'Country of Melt' cell.",
        },
        "country_of_manufacture": {
            "type": ["string", "null"],
            "description": "Country the product was made in, e.g. from a "
                           "'Pipe Manufactured in USA' note. May differ from "
                           "the country of melt.",
        },
        "specification": {
            "type": ["string", "null"],
            "description": "Material specification only, e.g. 'API 5L', "
                           "'ASTM A106' or 'SA312'. Not the edition, revision "
                           "or grade, even when printed in the same cell.",
        },
        "grade": {
            "type": ["string", "null"],
            "description": "Material grade only, e.g. 'X52M PSL2' or 'B'. "
                           "Split from the specification where one cell holds "
                           "both.",
        },
        "size": {
            "type": ["string", "null"],
            "description": "Nominal size as written, e.g. '20.00\" OD'. Take it "
                           "from a combined size cell without the wall or the "
                           "length class.",
        },
        "wall_thickness": {
            "type": ["string", "null"],
            "description": "Wall thickness as written, e.g. '0.375\"'. The WT "
                           "part of a combined size cell.",
        },
        "description": {
            "type": ["string", "null"],
            "description": "What the item is, e.g. 'HFW', 'ERW', 'SMLS', or "
                           "'WELD NECK FLANGE'. Often a 'Type of Pipe' cell.",
        },
    },
}

WELDER_CERT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["page_is_qualification_record", "welder_name", "stencil",
                 "test_date", "code", "wps", "result", "processes_tested",
                 "test_position", "progression", "test_od", "test_wall",
                 "qual_process", "qual_position", "qual_progression",
                 "qual_diameter", "qual_thickness", "f_number",
                 "qualifier_name", "qualifier_cert_number", "qualifier_cert_expiry"],
    "properties": {
        "page_is_qualification_record": {
            "type": "boolean",
            "description": "False if this page is not a welder qualification record.",
        },
        "welder_name": {"type": ["string", "null"]},
        "stencil": {
            "type": ["string", "null"],
            "description": "The welder's stencil, e.g. 'ABF'. Often hand-written.",
        },
        "test_date": {"type": ["string", "null"]},
        "code": {
            "type": ["string", "null"],
            "description": "Whichever code checkbox is ticked, e.g. 'API 1104 Multiple' "
                           "or 'ASME Sec. IX'. Null if none is ticked.",
        },
        "wps": {
            "type": ["string", "null"],
            "description": "Welding procedure, e.g. 'XTO-X60-6010/8010 Rev.1'.",
        },
        "result": {
            "enum": ["PASS", "FAIL", None],
            "description": "The overall result box at the foot of the record.",
        },
        "processes_tested": {
            "type": ["string", "null"],
            "description": "Process(es) ticked in the header, e.g. 'SMAW'.",
        },
        "test_position": {
            "type": ["string", "null"],
            "description": "Position the coupon was welded in, e.g. '5G'.",
        },
        "progression": {"type": ["string", "null"]},
        "test_od": {"type": ["string", "null"]},
        "test_wall": {"type": ["string", "null"]},
        "qual_process": {
            "type": ["string", "null"],
            "description": "Qualification Ranges -> Welding Process.",
        },
        "qual_position": {
            "type": ["string", "null"],
            "description": "Qualification Ranges -> Welding Position, e.g. 'ALL'.",
        },
        "qual_progression": {"type": ["string", "null"]},
        "qual_diameter": {
            "type": ["string", "null"],
            "description": "Qualification Ranges -> Pipe Diameter, exactly as "
                           "written, e.g. 'ALL' or '12.75 and above'.",
        },
        "qual_thickness": {"type": ["string", "null"]},
        "f_number": {
            "type": ["string", "null"],
            "description": "Qualification Ranges -> Filler Metal F-Number.",
        },
        "qualifier_name": {
            "type": ["string", "null"],
            "description": "Representative who witnessed and signed the test.",
        },
        "qualifier_cert_number": {
            "type": ["string", "null"],
            "description": "Their certification number, e.g. a CWI number.",
        },
        "qualifier_cert_expiry": {
            "type": ["string", "null"],
            "description": "Their certification expiry, e.g. 'EXP. 6/1/2026'. "
                           "Return just the date.",
        },
    },
}

_SHARED_RULES = """
You are reading a scanned document from a pipeline construction turnover
package for a QA/QC auditor. Transcribe only what is legibly printed or
written on the page.

Rules that matter more than completeness:
- If a field is absent, illegible, or ambiguous, return null. Never infer a
  value from context, and never complete a partially legible number.
- Transcribe identifiers exactly as written, including leading zeros,
  suffixes and punctuation. Do not normalise or reformat them.
- A blank field is a useful answer. A confidently wrong one is not: this
  feeds an audit where a wrong heat number or weld number hides a real defect.
""".strip()

PROMPTS: dict[str, str] = {
    "reader_sheet": _SHARED_RULES + """

This page should be a non-destructive examination (NDE) report - typically a
radiographic, visual, or penetrant examination sheet with a grid of weld rows.

For each weld row that contains data, read the weld number, the accept/reject
marking, pipe size, wall thickness and welder stencil.

**The result is recorded two different ways** and both mean the same thing.
Most of these forms use a narrow pair of checkbox columns headed ACC and REJ:
report ACC only when the accept box is clearly marked, REJ only when the
reject box is clearly marked, and null when neither marking is unambiguous.
Others print the verdict as a word in a Status or Results column - "Accept" or
"Rejected" - usually on a coloured fill, green and red. Read the word, not the
colour: the fill is decoration and a monochrome copy loses it.

**One weld may be assessed in several sections.** A radiograph goes round the
pipe in overlapping exposures, so a form may give one weld three sub-rows in
an Area column - 0-A, A-B, B-0 - each with its own result, and print the weld
number only against the first. Return one entry per sub-row, repeating the
weld number on each and filling in the area. Never merge them yourself and
never carry one area's result onto another: a weld is commonly accepted on two
sections and rejected on the third, and that third row is the whole point of
the document.

Where a row is rejected, the Dim. and Discontinuity cells beside it size and
name the flaw - `1.625` and `ESI`. Put the code in indications and the size in
remarks.

Ignore blank rows entirely. If the page is not an examination report, set
page_is_reader_sheet to false and leave the other fields null.""",

    "mtr": _SHARED_RULES + """

This page should be a material test report / mill certificate for pipe,
fittings, flanges or valves.

**Four or five different companies are named on a page like this, and only one
of them is the producer.** Work out which before filling anything in.

- issuing_company is whoever's letterhead the document carries. Identify it by
  what it looks like, not by where it sits: a company logo with the company's
  own name and *its own street address* beside it, set apart from the body of
  the form and carrying no field label. It may be top-left, top-right or
  centred across the width, and a page may carry two logos — a division's and
  its parent group's. Take the one whose address block is printed with it.
- mill_name is any *other* company the page names as having made this item or
  the material it was made from — a Works or Plant line, a Supplier line, a
  Steel Supplier. Not the letterhead company, not the customer, and never a
  place. Null if the page names no company but the letterhead and the buyer.
  Report it even when you think it is the wrong company to credit: which of
  the two counts is decided downstream from mill_source, and it can only
  decide that if you say what is there.

This distinction decides whether the material can be checked against an
approved manufacturer list, so do not collapse the two.

**The buyer is never the producer.** Fields headed `CLIENT NAME`, `CUSTOMER`,
`CLIENTE`, `BESTELLER`, `PURCHASER`, `BUYER`, `SOLD TO` or `SHIP TO` name
whoever ordered the material. They are often printed larger and more legibly
than the letterhead, and on many of these forms the customer occupies the
top-left corner while the producer's letterhead sits top-right. A name in a
labelled box is a field of the form; a letterhead has no label. Never put a
customer in issuing_company or mill_name.

**A `Supplier` line is where the raw material came from.** On a fitting or
flange certificate, `Supplier`, `Starting Material` or `Mill Test Report No.`
refer to the pipe or bar the item was forged from — that company made the
steel, not the fitting. Leave mill_name null for it. Recording it makes the
audit check the wrong company, and it will often be a real approved one, so
the mistake passes silently.

**Name the buyer as well, in `customer`.** Every certificate has one, and on
these forms it is usually set larger and more legibly than the letterhead —
`PURCHASER: DODSON GLOBAL` above a Rigid Industries logo, `CLIENTE: MRC Global
Inc.` beside a ORTEGA one. Writing it down is not busywork: it is what stops the
same name being reported as the manufacturer, which is the most common way
these pages are misread. If two fields would carry the same company, you have
the wrong one somewhere.

**Say where you got mill_name from.** Whenever you fill mill_name in, set
mill_source to the kind of line you read it from — `letterhead`, `works_line`
for a Works/Plant/Mill/Manufactured-By field naming a different producer of
this item, or `supplier_line` for a Supplier, Steel Supplier, Steel Rolling,
Starting Material or Mill Test Report line. Answer for where the text
physically sits on the page, not for who you believe made the item. That
judgement is made downstream and it needs the evidence, not the conclusion.
If mill_name is null, leave mill_source null too.

A MILL/COUNTRY OF ORIGIN line is the commonest trap on a fitting or flange
certificate. It names where the STEEL was melted, and it usually carries that
steel's own heat number — different from the heat this certificate covers. The
company that forged the item is the one on the letterhead; the melt source is
upstream of it. Where such a line states its own heat, put that number in
mill_heat, and it will be treated as a supply line however it is labelled.

**A supplier of the raw material is not the producer of the product.** A pipe
mill's certificate often names the steelmaker whose coil it rolled — fields
like `STEEL SUPPLIER`, `STEEL ROLLING`, `Steel Melted` or `Slab Supplier`.
That company made the steel, not the pipe, and it is one step further up the
chain than the approved-manufacturer list is asking about. Leave mill_name
null in that case, and never put it in issuing_company either: the producer is
the one on the letterhead, and naming the steelmaker sends the check after the
wrong company.

**Read the heat number more than once.** It is normally printed in three or
four places — the released-quantity table near the top, and again in the Heat
No. column of the chemical composition and mechanical properties tables. Read
every one of them and report the number only if they agree. If they disagree,
or if any digit of one of them is unclear, return null. A heat number is
matched character for character against the as-built, so one wrong digit
reports a certified joint as uncertified and hides the uncertified one.

**The header cells combine several fields, and each must be split out.** These
certificates pack the description into a few wide cells at the top right:

- `PIPE SIZE : 20.00" OD x 0.375" WT x DRL` gives size `20.00" OD` and
  wall_thickness `0.375"`. DRL/SRL is a length class, not a wall.
- `SPECIFICATION & GRADE : API 5L 46th Edition X52M PSL2 (Nace MR0175 ...)`
  gives specification `API 5L` and grade `X52M PSL2`. The edition, revision
  and any parenthesised note belong to neither — drop them.
- `TYPE OF PIPE : HFW` — or ERW, SAW, seamless — is the description.

**The standard in the title is not the material specification.** These
certificates head themselves `AS PER EN 10204 / ISO 10474 TYPE 3.1 / 3.2`.
EN 10204 governs what kind of inspection document this is, and the 3.1/3.2
type says who witnessed it. Neither says anything about the steel. The
material specification is the one against the product — `API 5L`, `ASTM A106`
— and it is never EN 10204, ISO 10474, or a 3.1/3.2 type.

Report each part under its own field and never the whole cell under one of
them. Where the certificate really does give only a combined value and you
cannot tell where one field ends and the next begins, return null rather than
splitting it at a guess.

mill_location is the plant the certificate points at — a `STEEL ROLLING`,
`WORKS`, `PLANT` or `MILL` line, or the address under the letterhead when that
is where the material was made. Give the place, not the company name.

If the page is not a material certificate, set page_is_certificate to false
and leave the other fields null.""",

    "welder_cert": _SHARED_RULES + """

This page should be a Welder Performance Qualification Test Record.

The single most important part is the block headed "Qualification Ranges".
That block is what the certifying inspector determined the test qualifies the
welder to do, and it is separate from — and usually broader than — the
conditions the coupon was actually welded under. Read the two independently:

- test_position, processes_tested, test_od and test_wall describe the coupon
  as welded (e.g. position 5G on 12.75" x 0.25" pipe).
- qual_position, qual_process, qual_diameter and qual_thickness come from the
  Qualification Ranges block (e.g. position ALL, diameter ALL).

Never copy an as-tested value into a qualification-range field, or the reverse.
Confusing them would either narrow a welder's ticket or widen it, and both are
wrong in ways an auditor cannot see.

Transcribe range values exactly as written, including the word "ALL". The
stencil is usually hand-written and may be in a different colour; return it
only if you can read it confidently.

If the page is not a qualification record, set page_is_qualification_record to
false and leave the other fields null.""",

    "daily_weld_report": _SHARED_RULES + """

This page should be a Daily Weld Report: a header block, then a grid with one
row per weld made that day. It is normally filled in by hand.

Three things about these grids cause most errors.

**Ditto marks.** A cell holding a ditto mark — a double-quote, two apostrophes,
or a short repeated squiggle — means "the same as the row above". Expand it to
the value from the row above and set expanded_from_ditto to true for that row.
Do not return the ditto mark itself, and do not leave the cell null.

**A dash means nothing was entered.** A dash, en-dash or a struck-through cell
in a welder column means no welder for that pass. Return null for it. Do not
mistake a dash for a ditto mark.

**WELD TYPE and PROCESS are different, adjacent columns.** WELD TYPE holds a
joint or location type — BW, ML, TIE-IN, FAB, FILLET. PROCESS holds a welding
process — SMAW, GTAW, GMAW, FCAW. Read each from its own column and never put
a value from one into the other, even when a cell is ambiguous on its own. If
a column's cell is genuinely unreadable, return null for that column only.

The WELD # column is frequently left blank. That is normal: return null rather
than numbering the rows yourself. Transcribe the NOTES column verbatim — it may
hold an NDE report number, a spool or drawing number, or a bore reference, and
it is not your job to tell them apart.

Skip rows that are entirely blank. If the page is not a daily weld report, set
page_is_weld_report to false and leave the other fields null.""",

    "weld_map": _SHARED_RULES + """

This page should be a piping isometric — a line drawing of a pipe run with
annotations attached to it by leader lines. The same drawing is issued as a
"weld map" and as a "heat map", carrying different annotations, so a given
sheet may have weld callouts, heat callouts, both, or neither.

Read the annotations, not the geometry:

- **Weld callouts** are balloons or boxes on a leader line, holding a weld or
  NDE report id, the welder stencils, and sometimes a date. Return one entry
  per callout.

  **Identify each field by what it looks like, not by which line it is on.**
  The order differs between drawings: one job writes 'AFB-18' / 'AFM/ARV' /
  '06/01/25', another writes 'W-AQR/ARG' / 'X-Ray-GFB-48' — welders first,
  identifier second, no date. A weld or NDE id is letters, a dash and digits
  (`GFB-48`, `GTI-23`, `AFB-18`); welder stencils are two- or three-letter
  codes, usually a pair separated by a slash. Strip a leading `W-` from the
  stencils and a leading `X-Ray-` or `RT-` from the identifier; those label
  the field rather than belonging to its value. Putting them the wrong way
  round makes every weld on the sheet unmatchable.
- **Heat callouts** are usually boxes prefixed 'HT:'. Return the heat alone
  without the prefix. If the same heat is boxed twice on the drawing, return
  it twice: the repetition is real.
- **The title block** is bottom right. LINE NO. is the identifier the rest of
  the package refers to this line by.

Do not attempt to work out which weld a heat sits next to, or which spool a
callout points at. That needs spatial reasoning about leader lines, and a
wrong pairing would silently attach a heat to the wrong joint. Return the
callouts as a flat list and leave the association alone.

Small circled numbers on the pipe are field weld marks from the drawing itself,
not weld callouts — ignore them unless they carry a stencil and a date.

If the page is not a piping isometric, set page_is_isometric to false and leave
the other fields empty.""",

    "hydrotest": _SHARED_RULES + """

This page comes from a hydrostatic pressure test package. Such a package holds
several kinds of page, and your first job is to say which one this is:

- **test_record** — the completed test, on a form headed something like
  'PRESSURE TEST RECORD'. It names the section tested, when the test started
  and finished, the pressures held, the instrument serial numbers, and carries
  a table of timed gauge readings and two signatures.
- **test_requirements** — the sheet that states what the test *must* achieve:
  minimum and maximum test pressure, required duration, code, and the pipe it
  applies to. It also states elevations, length and volume; there is nowhere
  to put those and no rule uses them, so leave them.
- **chart** — a circular or strip recorder chart.
- **calibration_certificate** — an instrument calibration certificate. Return
  the instrument's serial number and the date it was calibrated.
- **other** — a plan, a cover sheet, boilerplate.

Fill in only the fields the page itself carries, and leave the rest null. On a
requirements sheet that means the required_* fields; on a test record it means
the actual ones. Where the record repeats the requirement, fill in both.

Read the pressures carefully:

- required_min_pressure and required_max_pressure are the limits the test was
  written to. On a test record they are usually labelled 'Min Test Press' and
  'Max Test Press' near the top, and are the *target* pressures, not readings.
- The readings table is the evidence the hold was maintained. Transcribe every
  populated row in order, top to bottom of the left block and then the right.
  Where a figure has been written over or corrected, return the correction; if
  you cannot tell which value is current, return null for that reading rather
  than choosing.

The result box is the point of the whole document. Return ACCEPTABLE or
UNACCEPTABLE only when one is unambiguously ticked, circled or struck. Leaving
both blank is common and is itself an audit finding, so report null rather than
inferring the outcome from the pressures — that inference is exactly what the
auditor needs to make for themselves.""",

    "coating": _SHARED_RULES + """

This page should be an XTO DAILY FIELD COATING INSPECTION REPORT — a one-page
form recording a day's blasting and coating on a pipeline segment. The form
exists in two revisions and the later one adds an NDE Weld # column and a row
of equipment serial numbers; read whichever this page has and leave the rest
null.

This form is mostly printed instruction with a little handwriting on top, and
the single most common error is returning the printed text as if it were an
entry. Three places where that happens:

- Every Testex tape carries the printed label "1.5 - 4.5 mils / 40 - 115 µm".
  That is the tape's measuring range. The reading is the number written by
  hand beside or above it, usually one figure to one decimal place.
- Above the blast media field is a printed line naming the media that may and
  may not be used. It is not an entry. Return only what was written into the
  Blast Media box, and null if that box is empty.
- Bands of small print run across the middle and bottom of the form about
  RD-6, wax tape and spot-check intervals. None of it is an entry.

A blank field on this form matters more than on most, because the blanks are
what the audit is looking for: an empty "Coating Jeeped From Stn", an empty
"Total Welds Coated", an empty DFT column. Return null and let the audit
report it. Do not fill a value in from another part of the page, and do not
treat a wavy line or a struck-through cell as a number — those mean the row
does not apply.

The coating table's Mils section has four columns — Primer, Base, Intermediate,
Top — and each product row spans two sub-lines, WFT above DFT. Report which
column a thickness was written under, and keep wet and dry thicknesses apart:
they differ by a factor of about two and confusing them turns a compliant
coat into a failure or the reverse.""",

    "backfill": _SHARED_RULES + """

This page should be an XTO RELEASE FOR BACKFILL — a short one-page form
releasing a measured length of pipeline for the ditch to be closed over it.
These are filed as bundles, one form per page, so read this page on its own.

Almost the whole page is printed. Only eight things are filled in: the line
size, wall, material, yield and service across the strip under the title; the
two survey stations; and the signatures with their dates at the bottom.

The stations are written as two boxes, a whole number and a plus offset —
`130` and `00` means station 130+00. Join them with a plus. If either box is
empty return null rather than assuming zero: a release with no stated extent
is a real finding, and inventing `0+00` would hide it.

For each signature line report whether a signature is actually there, and the
date written beside it. Three things about that:

- The dates on one form can differ from each other. Report each exactly as
  written and do not reconcile them; the difference is what is being audited.
- Some forms carry a typed or stamped inspector name block, often in blue,
  above the signature line. That is contact details, not a signature — a
  signature is handwriting on the line itself.
- Older forms have two signature lines and later ones add a third for the
  Survey Rep. Where there is no such line, return survey_signed false and
  survey_date null.

If the page is not a release for backfill, set page_is_release to false and
leave everything else null.""",
}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


DAILY_WELD_REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["page_is_weld_report", "report_date", "afe", "unit", "job_name",
                 "contractor", "inspector", "drawing_no", "system", "line_size",
                 "wall_thickness", "material", "service", "rows"],
    "properties": {
        "page_is_weld_report": {
            "type": "boolean",
            "description": "False if this page is not a daily weld report.",
        },
        "report_date": {
            "type": ["string", "null"],
            "description": "The 'Today's Date' field, as printed, e.g. '5/12/25'.",
        },
        "afe": {"type": ["string", "null"]},
        "unit": {"type": ["string", "null"]},
        "job_name": {"type": ["string", "null"]},
        "contractor": {"type": ["string", "null"]},
        "inspector": {
            "type": ["string", "null"],
            "description": "Job Lead Inspector.",
        },
        "drawing_no": {"type": ["string", "null"]},
        "system": {"type": ["string", "null"]},
        "line_size": {"type": ["string", "null"]},
        "wall_thickness": {"type": ["string", "null"], "description": "The WT field."},
        "material": {"type": ["string", "null"]},
        "service": {"type": ["string", "null"]},
        "rows": {
            "type": "array",
            "description": "One entry per grid row that has any entry at all. "
                           "Skip rows that are entirely blank.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["weld_no", "size", "weld_type", "process",
                             "welder_root", "welder_hot_pass", "welder_fill",
                             "welder_cap", "notes", "expanded_from_ditto"],
                "properties": {
                    "weld_no": {
                        "type": ["string", "null"],
                        "description": "The WELD # cell. Often left blank — return "
                                       "null rather than inventing a number.",
                    },
                    "size": {"type": ["string", "null"]},
                    "weld_type": {
                        "type": ["string", "null"],
                        "description": "The WELD TYPE column, e.g. 'BW', 'ML', "
                                       "'TIE-IN', 'FAB'. Not the process.",
                    },
                    "process": {
                        "type": ["string", "null"],
                        "description": "The PROCESS column, e.g. 'SMAW', 'GTAW'. "
                                       "This is the column to the right of WELD TYPE.",
                    },
                    "welder_root": {"type": ["string", "null"]},
                    "welder_hot_pass": {
                        "type": ["string", "null"],
                        "description": "The HP column. Many of these forms have no "
                                       "HP column at all — return null then.",
                    },
                    "welder_fill": {"type": ["string", "null"]},
                    "welder_cap": {"type": ["string", "null"]},
                    "notes": {"type": ["string", "null"]},
                    "expanded_from_ditto": {
                        "type": "boolean",
                        "description": "True if any cell on this row was a ditto "
                                       "mark you expanded from the row above.",
                    },
                },
            },
        },
    },
}

WELD_MAP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["page_is_isometric", "line_no", "drawing_no", "sheet", "revision",
                 "service", "afe", "weld_callouts", "heat_callouts",
                 "bill_of_material"],
    "properties": {
        "page_is_isometric": {
            "type": "boolean",
            "description": "False if this page is not a piping isometric drawing.",
        },
        "line_no": {
            "type": ["string", "null"],
            "description": "LINE NO. from the title block, e.g. 'DTD22MP-LP-16-1A'.",
        },
        "drawing_no": {"type": ["string", "null"]},
        "sheet": {"type": ["string", "null"]},
        "revision": {"type": ["string", "null"]},
        "service": {"type": ["string", "null"]},
        "afe": {"type": ["string", "null"]},
        "weld_callouts": {
            "type": "array",
            "description": "Balloon callouts naming a weld. Typically three lines: "
                           "a weld or NDE report id, welder stencils, and a date.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["weld_id", "welders", "date", "signed_off"],
                "properties": {
                    "weld_id": {
                        "type": ["string", "null"],
                        "description": "The weld or NDE identifier in the callout "
                                       "— letters, a dash and digits, e.g. "
                                       "'AFB-18', 'AFB-16C', 'GFB-48'. Not "
                                       "necessarily the first line. Drop any "
                                       "'X-Ray-' or 'RT-' label in front of it.",
                    },
                    "welders": {
                        "type": ["string", "null"],
                        "description": "Welder stencils in the callout, e.g. "
                                       "'AFM/ARV'. Two or three letters each, "
                                       "usually a pair. Drop a leading 'W-'.",
                    },
                    "date": {"type": ["string", "null"]},
                    "signed_off": {
                        "type": "boolean",
                        "description": "True if a tick or check mark is drawn on the "
                                       "callout. Report the mark; do not interpret it.",
                    },
                },
            },
        },
        "heat_callouts": {
            "type": "array",
            "description": "Callouts naming a material heat, usually boxed and "
                           "prefixed 'HT:'. One entry per box, even if repeated.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["heat", "note"],
                "properties": {
                    "heat": {
                        "type": ["string", "null"],
                        "description": "The heat number alone, without the 'HT:' "
                                       "prefix, e.g. 'NN0446' or '453M66'.",
                    },
                    "note": {"type": ["string", "null"]},
                },
            },
        },
        "bill_of_material": {
            "type": "array",
            "description": "Rows of the BILL OF MATERIAL table, if present.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["mark", "size", "quantity", "description"],
                "properties": {
                    "mark": {"type": ["string", "null"]},
                    "size": {"type": ["string", "null"]},
                    "quantity": {"type": ["string", "null"]},
                    "description": {"type": ["string", "null"]},
                },
            },
        },
    },
}

HYDROTEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["page_type", "service", "line_no", "code", "pipe_size", "wall",
                 "grade", "required_min_pressure", "required_max_pressure",
                 "required_duration_hours", "started_at", "completed_at",
                 "stated_duration_hours", "test_medium", "result",
                 "deadweight_sn", "pressure_recorder_sn", "temp_recorder_sn",
                 "contractor_representative", "inspector",
                 "instrument_sn", "calibration_date", "readings"],
    "properties": {
        "page_type": {
            "type": "string",
            "enum": ["test_record", "test_requirements", "chart",
                     "calibration_certificate", "other"],
            "description": "What this page is. 'test_record' is the completed "
                           "pressure test record with the readings table; "
                           "'test_requirements' is the sheet stating minimum and "
                           "maximum test pressure and duration.",
        },
        "service": {
            "type": ["string", "null"],
            "description": "Service or segment, e.g. 'LP SEG.D'.",
        },
        "line_no": {"type": ["string", "null"]},
        "code": {"type": ["string", "null"], "description": "e.g. 'B31.8'."},
        "pipe_size": {"type": ["string", "null"]},
        "wall": {"type": ["string", "null"]},
        "grade": {"type": ["string", "null"]},
        "required_min_pressure": {
            "type": ["number", "null"],
            "description": "Minimum test pressure the test must reach, in psig.",
        },
        "required_max_pressure": {"type": ["number", "null"]},
        "required_duration_hours": {"type": ["number", "null"]},
        "started_at": {
            "type": ["string", "null"],
            "description": "Date and time started, exactly as written, "
                           "e.g. '8/18/25 7:00am'.",
        },
        "completed_at": {"type": ["string", "null"]},
        "stated_duration_hours": {
            "type": ["number", "null"],
            "description": "Duration written on the form, e.g. 8.",
        },
        "test_medium": {"type": ["string", "null"]},
        "result": {
            "enum": ["ACCEPTABLE", "UNACCEPTABLE", None],
            "description": "Which result box is ticked. Null if neither is marked "
                           "— an unmarked pair is a real and common condition, "
                           "so do not guess from the readings.",
        },
        "deadweight_sn": {"type": ["string", "null"]},
        "pressure_recorder_sn": {"type": ["string", "null"]},
        "temp_recorder_sn": {"type": ["string", "null"]},
        "contractor_representative": {"type": ["string", "null"]},
        "inspector": {"type": ["string", "null"]},
        "instrument_sn": {
            "type": ["string", "null"],
            "description": "On a calibration certificate, the serial number of "
                           "the instrument it certifies.",
        },
        "calibration_date": {
            "type": ["string", "null"],
            "description": "On a calibration certificate, the date calibrated, "
                           "as written.",
        },
        "readings": {
            "type": "array",
            "description": "Every row of the readings table, in order, reading "
                           "the left column block before the right.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["time", "pressure_psig", "ambient_temp"],
                "properties": {
                    "time": {
                        "type": ["string", "null"],
                        "description": "As written, e.g. '7:00AM' or '1:25 PM'.",
                    },
                    "pressure_psig": {"type": ["number", "null"]},
                    "ambient_temp": {"type": ["string", "null"]},
                },
            },
        },
    },
}

COATING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["page_is_coating_report", "report_date", "line_size", "material",
                 "service", "job_name", "contractor", "inspector",
                 "starting_station", "ending_station", "blast_media",
                 "cleanliness_standard", "profile_required", "profile_readings",
                 "environmental", "coats", "total_welds_coated",
                 "jeeped_from_station", "jeeped_to_station", "instruments",
                 "comments"],
    "properties": {
        "page_is_coating_report": {
            "type": "boolean",
            "description": "True only for a DAILY FIELD COATING INSPECTION "
                           "REPORT. Product and safety data sheets, calibration "
                           "certificates and specifications are not.",
        },
        "report_date": {"type": ["string", "null"]},
        "line_size": {"type": ["string", "null"], "description": "e.g. '20\"'."},
        "material": {"type": ["string", "null"], "description": "CS, SS, HDPE or FIBGL."},
        "service": {"type": ["string", "null"]},
        "job_name": {"type": ["string", "null"]},
        "contractor": {"type": ["string", "null"]},
        "inspector": {
            "type": ["string", "null"],
            "description": "The printed name against the inspector signature, "
                           "not the signature itself.",
        },
        "starting_station": {
            "type": ["string", "null"],
            "description": "Written as stationing, e.g. '0+00'. Null if blank.",
        },
        "ending_station": {"type": ["string", "null"]},
        "blast_media": {
            "type": ["string", "null"],
            "description": "As written, e.g. 'Garnet'. Do not copy the printed "
                           "instruction above the field, which names permitted "
                           "and prohibited media; report only what was filled in.",
        },
        "cleanliness_standard": {
            "type": ["string", "null"],
            "description": "e.g. 'NACE#2' or 'SSPC-SP-6'.",
        },
        "profile_required": {
            "type": ["number", "null"],
            "description": "The 'Profile Reqd' figure in mils.",
        },
        "profile_readings": {
            "type": "array",
            "description": "The number written beside each Testex tape, in "
                           "order. '1.5 - 4.5 mils' is printed on every tape "
                           "label and is not a reading. Skip profiles struck "
                           "through or left blank.",
            "items": {"type": "number"},
        },
        "environmental": {
            "type": "array",
            "description": "Every populated row of the ambient conditions "
                           "table, left block then right.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["time", "air_temp_f", "relative_humidity",
                             "steel_temp_f", "dew_point_f"],
                "properties": {
                    "time": {"type": ["string", "null"]},
                    "air_temp_f": {"type": ["number", "null"]},
                    "relative_humidity": {
                        "type": ["number", "null"],
                        "description": "Percent, as a number: '17.4' not '17.4%'.",
                    },
                    "steel_temp_f": {"type": ["number", "null"]},
                    "dew_point_f": {"type": ["number", "null"]},
                },
            },
        },
        "coats": {
            "type": "array",
            "description": "Every populated row of the coating table. A row "
                           "carries WFT and DFT on two sub-lines; a wavy line "
                           "or 'NA' through a cell means not applicable, which "
                           "is null, not zero.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["nde_weld_no", "manufacturer", "product", "color",
                             "batch_a", "batch_b", "application_method",
                             "wft_mils", "dft_mils", "dft_layer"],
                "properties": {
                    "nde_weld_no": {
                        "type": ["string", "null"],
                        "description": "The NDE Weld # column, on revisions "
                                       "that have one. Null on older forms.",
                    },
                    "manufacturer": {"type": ["string", "null"]},
                    "product": {"type": ["string", "null"]},
                    "color": {"type": ["string", "null"]},
                    "batch_a": {"type": ["string", "null"]},
                    "batch_b": {"type": ["string", "null"]},
                    "application_method": {
                        "type": ["string", "null"],
                        "description": "e.g. 'Flocking', 'Gun 2100', 'Brush'.",
                    },
                    "wft_mils": {"type": ["number", "null"]},
                    "dft_mils": {"type": ["number", "null"]},
                    "dft_layer": {
                        "enum": ["primer", "base", "intermediate", "top", None],
                        "description": "Which of the four Mils columns the "
                                       "thickness was written under.",
                    },
                },
            },
        },
        "total_welds_coated": {"type": ["number", "null"]},
        "jeeped_from_station": {
            "type": ["string", "null"],
            "description": "'Coating Jeeped From Stn'. Null if left blank — "
                           "a blank here is a real and reportable condition.",
        },
        "jeeped_to_station": {"type": ["string", "null"]},
        "instruments": {
            "type": "array",
            "description": "The equipment serial numbers printed near the "
                           "bottom: DC jeep meter, coating thickness, "
                           "environmental conditions, dial thickness gauge, "
                           "holiday detector. Older forms have none.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["role", "serial"],
                "properties": {
                    "role": {
                        "type": "string",
                        "enum": ["holiday_detector", "dft_gauge", "dpm",
                                 "profile_gauge"],
                        "description": "A DC jeep meter and a holiday detector "
                                       "are both holiday_detector; 'coating "
                                       "thickness' is dft_gauge; 'environmental "
                                       "conditions' is dpm; a dial thickness "
                                       "gauge reads the Testex tape, so it is "
                                       "profile_gauge.",
                    },
                    "serial": {"type": "string"},
                },
            },
        },
        "comments": {"type": ["string", "null"]},
    },
}

BACKFILL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["page_is_release", "line_size", "wall", "material", "yield_grade",
                 "service", "from_station", "to_station", "inspector_signed",
                 "inspector_date", "contractor_signed", "contractor_date",
                 "survey_signed", "survey_date"],
    "properties": {
        "page_is_release": {
            "type": "boolean",
            "description": "True only for a RELEASE FOR BACKFILL form. These "
                           "documents bundle one form per page, so most pages "
                           "are a release and a few are covers or blanks.",
        },
        "line_size": {"type": ["string", "null"], "description": "e.g. '16\"'."},
        "wall": {"type": ["string", "null"]},
        "material": {"type": ["string", "null"],
                     "description": "CS, SS, HDPE, FIBGL or as written."},
        "yield_grade": {"type": ["string", "null"], "description": "e.g. 'X52'."},
        "service": {"type": ["string", "null"], "description": "e.g. 'LP', 'FUEL GAS'."},
        "from_station": {
            "type": ["string", "null"],
            "description": "The survey station the release starts at, joining "
                           "the two boxes with a plus: '130+00'. Null if blank.",
        },
        "to_station": {"type": ["string", "null"]},
        "inspector_signed": {
            "type": "boolean",
            "description": "Whether the inspector signature line carries a "
                           "signature. A typed or stamped name block above the "
                           "line is not a signature on its own.",
        },
        "inspector_date": {
            "type": ["string", "null"],
            "description": "The date beside that signature, as written.",
        },
        "contractor_signed": {"type": "boolean"},
        "contractor_date": {"type": ["string", "null"]},
        "survey_signed": {
            "type": "boolean",
            "description": "Later revisions of the form add a Survey Rep line. "
                           "False when the form has no such line at all.",
        },
        "survey_date": {"type": ["string", "null"]},
    },
}

SCHEMAS: dict[str, dict[str, Any]] = {
    "reader_sheet": READER_SHEET_SCHEMA,
    "mtr": MTR_SCHEMA,
    "welder_cert": WELDER_CERT_SCHEMA,
    "daily_weld_report": DAILY_WELD_REPORT_SCHEMA,
    "weld_map": WELD_MAP_SCHEMA,
    "hydrotest": HYDROTEST_SCHEMA,
    "coating": COATING_SCHEMA,
    "backfill": BACKFILL_SCHEMA,
}


@lru_cache(maxsize=None)
def _extraction_version(kind: str) -> str:
    """Short digest of the prompt and schema a kind is currently extracted with.

    Eight hex characters is plenty: this only has to differ between successive
    edits of one prompt, not resist collision.
    """
    material = json.dumps([PROMPTS.get(kind, ""), SCHEMAS.get(kind, {})],
                          sort_keys=True)
    return hashlib.sha1(material.encode()).hexdigest()[:8]


def render_page(path: str | Path, page_no: int, max_edge: int = DEFAULT_MAX_EDGE,
                clip: tuple[float, float, float, float] | None = None,
                ) -> tuple[bytes, str]:
    """Render one PDF page, or one region of it, to a JPEG.

    ``clip`` is ``(x0, y0, x1, y1)`` as fractions of the page. The zoom is
    computed from the region rather than the page, so a quarter of a page comes
    back at twice the detail rather than a quarter of the size — which is the
    entire point of asking for a region.

    JPEG rather than PNG: these are photographic scans of paper, where JPEG is
    several times smaller at a quality indistinguishable for reading text, and
    upload size is the main latency cost per page.
    """
    import pymupdf

    with pymupdf.open(str(path)) as doc:
        page = doc[page_no]
        box = page.rect
        if clip is not None:
            x0, y0, x1, y1 = clip
            box = pymupdf.Rect(
                box.x0 + x0 * box.width, box.y0 + y0 * box.height,
                box.x0 + x1 * box.width, box.y0 + y1 * box.height,
            )
        longest = max(box.width, box.height) or 1
        zoom = max_edge / longest
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=box)
        return pixmap.tobytes("jpeg", jpg_quality=88), "image/jpeg"


def page_count(path: str | Path) -> int:
    import pymupdf

    try:
        with pymupdf.open(str(path)) as doc:
            return doc.page_count
    except Exception:
        return 0


def image_tokens(max_edge: int = DEFAULT_MAX_EDGE, aspect: float = 0.77) -> int:
    """Approximate image tokens for a rendered page of the usual paper shape."""
    width = int(max_edge * aspect)
    return min(_MAX_IMAGE_TOKENS, int(width * max_edge * _TOKENS_PER_PIXEL))


# ---------------------------------------------------------------------------
# Fitting the schemas through the API's union limit
# ---------------------------------------------------------------------------
#
# Every optional field above is written the honest way — ``["string", "null"]``
# and required — so a page the model cannot read comes back with the key
# present and null rather than absent or guessed. The hosted API caps a schema
# at sixteen union-typed parameters, and five of the eight kinds are over it.
#
# Two levers were tried at the boundary. Making the fields optional removes the
# unions, but an object with a dozen optional properties and no additional ones
# allowed is a grammar that must accept any subset in any order, and the
# compiler either times out or rejects it as too complex — worse than the
# problem it solved, and on more kinds.
#
# So the fields stay required and lose only the null: a string the model cannot
# read comes back empty rather than null. That is one value in a fixed shape,
# so the grammar stays trivial, and the handful of nullable *numbers* are few
# enough to stay unions. Empty means unread nowhere but here, though, so it is
# turned back into null before the payload leaves this module. The schemas stay
# expressive, the cache keys stay stable, and no caller learns this limit
# exists.

#: Appended to the system prompt on the hosted path only, because it describes
#: a workaround for that path's schema limits. The local backend takes the
#: nullable schemas as written.
_EMPTY_MEANS_UNREAD = """

One note on format. The rules above tell you to return null where a value is
absent, illegible or ambiguous. Some of the fields below are typed as plain
strings and cannot hold null. For those, return an empty string "" — it means
exactly what null means here, and it is always better than a guess."""


def _null_free_strings(node):
    """Drop ``null`` from string types, keeping the field required.

    Empty string takes over from null. Nullable numbers keep their union:
    there are at most eight on any kind, well inside the limit of sixteen, and
    "" is not a number.
    """
    if isinstance(node, list):
        return [_null_free_strings(v) for v in node]
    if not isinstance(node, dict):
        return node

    out = {k: _null_free_strings(v) for k, v in node.items()}
    if out.get("type") == ["string", "null"]:
        out["type"] = "string"
    if isinstance(out.get("enum"), list) and None in out["enum"]:
        # "" joins the accepted values so the model can still decline to pick.
        out["enum"] = [v for v in out["enum"] if v is not None] + [""]
    return out


def _restore_nulls(payload, schema):
    """Turn the empty strings back into the nulls the rest of the code expects.

    Guided by the original schema, so a field that was never nullable keeps
    whatever it was given.
    """
    if isinstance(schema, dict) and schema.get("type") == "array":
        items = schema.get("items", {})
        return ([_restore_nulls(v, items) for v in payload]
                if isinstance(payload, list) else payload)

    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict) or not isinstance(payload, dict):
        return payload

    filled = dict(payload)
    for name, spec in props.items():
        if name not in filled:
            # Required in the schema sent, so this is the model falling short
            # rather than declining; null says so.
            filled[name] = None
        elif filled[name] == "" and _was_nullable(spec):
            filled[name] = None
        else:
            filled[name] = _restore_nulls(filled[name], spec)
    return filled


def _was_nullable(spec) -> bool:
    if not isinstance(spec, dict):
        return False
    return (isinstance(spec.get("type"), list) and "null" in spec["type"]) or (
        isinstance(spec.get("enum"), list) and None in spec["enum"]
    )


# ---------------------------------------------------------------------------
# Tiling: a second look at the parts of the page that are too small to read
# ---------------------------------------------------------------------------
#
# The hosted API scales an image to at most ~1568px on its *long* edge, so a
# letter-size page arrives at about 2x zoom whatever ``--max-edge`` says, and
# 5pt type on a dense certificate lands a few pixels tall. The model does not
# report that as illegible; it reads a plausible neighbour instead. On the
# Kandal MTR the heat number 24913 came back 12987 twice, and the letterhead
# came back as three different invented companies across three runs.
#
# Cropping does not help unless it crops *both* dimensions: halving the width
# of a landscape page just makes the height the long edge, and the cap bites
# the same. Quartering it doubles the zoom, and that was enough — measured on
# that page, a 2x2 tile read both fields correctly where the whole page read
# neither, and 3x2 and 3x3 were no better. So 2x2 is the grid: the cheapest
# one that works.
#
# Tiles answer only the scalar fields. The row arrays already read well from
# the whole page (214 of 250 fields on a thirty-row RT sheet), a fragment
# cannot tell which rows it is missing, and merging row lists across four
# overlapping views is where this would go wrong.

TILE_GRID = (2, 2)

#: Kinds read as close-ups by default. Both hinge on identifiers that are
#: matched character for character against another document — a heat number
#: against the as-built, a weld number against the register — so one wrong
#: digit reports a certified joint as uncertified and hides the uncertified
#: one. Both are also printed small: mill certificates and reader sheets are
#: dense landscape sheets where the identifiers are the smallest text on the
#: page.
#:
#: The rest are left whole deliberately. Measured over eleven hand-read pages,
#: tiling everything halved the critical errors but cost four times as much and
#: read *non*-critical fields slightly worse, because a quarter of a page loses
#: the context that tells a fragment what it is looking at — a hydrotest time
#: came back as "7:00AM" with the date stranded in another tile.
TILED_KINDS = frozenset({"mtr", "reader_sheet"})

#: ``auto`` tiles the kinds above, ``always`` tiles everything (which is what
#: the benchmark measures against), ``never`` turns it off.
TILE_MODES = ("auto", "always", "never")

#: Fields whose value drives a cross-check, per kind. When two close-ups of one
#: of these disagree irreconcilably the page is put in front of a human; when
#: any other field disagrees it is recorded and left alone.
#:
#: The distinction is about volume, not principle. Measured over the pages in
#: ``eval/``, close-ups disagree beyond reconciling about eight times per ten
#: pages — a pass over a thousand certificates would raise eight hundred
#: findings if every one counted, and a review list that long is not read.
#: These are the fields where the null actually costs something: a heat number
#: nothing can match to the as-built, an issuer nothing can check against the
#: approved manufacturer list, a ticket that no longer joins its copies.
#:
#: Keep this in step with the appliers in ``extract/vision_pass.py`` — a field
#: named here that nothing stores raises findings about a value no rule reads.
DECISIVE_FIELDS: dict[str, frozenset[str]] = {
    "mtr": frozenset({"heat", "issuing_company", "mill_name"}),
    "reader_sheet": frozenset({"ticket_no", "sheet_date", "technician"}),
    "welder_cert": frozenset({"welder_name", "stencil", "test_date", "result"}),
    "daily_weld_report": frozenset({"report_date"}),
    "weld_map": frozenset({"line_no", "drawing_no"}),
    "hydrotest": frozenset({"required_min_pressure", "result", "completed_at"}),
    "coating": frozenset({"report_date", "inspector"}),
    "backfill": frozenset({"inspector_date", "from_station", "to_station"}),
}


def is_decisive(kind: str, field_name: str) -> bool:
    return field_name in DECISIVE_FIELDS.get(kind, frozenset())


def tiles_for(mode: str, kind: str) -> bool:
    """Whether this kind is read as close-ups under this mode."""
    if mode not in TILE_MODES:
        raise ValueError(f"Unknown tile mode {mode!r}. Known: {', '.join(TILE_MODES)}")
    if mode == "never":
        return False
    return True if mode == "always" else kind in TILED_KINDS

#: Tiles overlap so that a value sitting on a seam is whole in at least one of
#: them. Without it a cut company name reads as two different half-names, the
#: merge sees a disagreement, and a field the whole page had right goes null.
TILE_OVERLAP = 0.10


def page_tiles(grid: tuple[int, int] = TILE_GRID, overlap: float = TILE_OVERLAP,
               ) -> tuple[tuple[float, float, float, float], ...]:
    """Fractional clip rects covering the page, overlapping at the seams."""
    cols, rows = grid
    w, h = 1.0 / cols, 1.0 / rows
    out = []
    for row in range(rows):
        for col in range(cols):
            out.append((
                max(0.0, col * w - overlap), max(0.0, row * h - overlap),
                min(1.0, (col + 1) * w + overlap), min(1.0, (row + 1) * h + overlap),
            ))
    return tuple(out)


def _fragment_schema(schema: dict) -> dict:
    """The scalar fields of a schema — what one region of a page can answer.

    Nullable scalars only. A fragment needs to be able to say "not in this
    piece of the page", which is exactly what nullable means here, and a
    boolean like ``page_is_certificate`` is a judgement about the whole page
    that no single quarter of it is entitled to make.
    """
    props = {name: spec for name, spec in schema.get("properties", {}).items()
             if _was_nullable(spec)}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(props),
        "properties": props,
    }


_TILE_PROMPT = """

You are being shown **one region of a larger page**, enlarged so that small
print is legible. Report only the fields you can actually see in this fragment.

Most of the fields below belong to other parts of the page. That is expected:
leave every one of them empty rather than guessing at what the rest of the
page probably says. You are being asked precisely because the full page was
too small to read reliably, so a value you cannot see here is worth nothing.

Where a value is clipped by the edge of the fragment, leave it empty too — a
half-read identifier is worse than none."""


#: Fields holding a company name rather than an identifier. Two close-ups that
#: read "Kandal Pipe USA, Inc" and "Kandal Pipes USA, Inc." have not disagreed
#: about anything the audit cares about — the AML lookup they feed is fuzzy and
#: would resolve both to the same entry. Cancelling on that difference throws
#: away a company name over a plural.
#:
#: Identifiers get no such latitude. '24913' and '12581' are similar strings
#: and different heats, and a heat is matched character for character.
NAME_FIELDS = frozenset({"issuing_company", "mill_name", "manufacturer"})

#: How alike two normalised names must be to count as one company. High enough
#: that a misread distinctive word ('Model Pipe' for 'Kandal Pipe') still reads
#: as a disagreement, which is what puts the page in front of a human.
SAME_COMPANY = 90


def _same_company(a, b) -> bool:
    from rapidfuzz import fuzz

    from .aml import normalise_manufacturer

    x, y = normalise_manufacturer(str(a)), normalise_manufacturer(str(b))
    if not x or not y:
        return x == y
    return fuzz.token_set_ratio(x, y) >= SAME_COMPANY


def _group_equivalent(field_name: str, values: list) -> list[list]:
    """Cluster readings that say the same thing, by this field's standards."""
    if field_name not in NAME_FIELDS:
        groups: dict[str, list] = {}
        for value in values:
            groups.setdefault(str(value).strip().casefold(), []).append(value)
        return list(groups.values())

    clustered: list[list] = []
    for value in values:
        for group in clustered:
            if _same_company(group[0], value):
                group.append(value)
                break
        else:
            clustered.append([value])
    return clustered


def _merge_tiles(page: dict, tiles: list[dict], schema: dict) -> dict:
    """Let the close-up readings overrule the whole-page ones.

    A tile that saw the field beats the whole page, because it saw it several
    times larger. Where tiles disagree the majority wins: the tiles overlap,
    so a value is usually legible in two of them, while a tile that has wandered
    into the wrong table is on its own. Cancelling on any disagreement was the
    first rule here and it was too brittle — one tile reading the heat number
    off a %-elongation column vetoed two tiles that had it right.

    On a genuine tie the whole-page reading breaks it, and only if it backs one
    of the candidates. Two irreconcilable readings of the same box is the
    definition of a page to put in front of a human, and null is how this
    codebase says so.
    """
    merged = dict(page)
    notes: dict[str, dict] = {}
    for name, spec in schema.get("properties", {}).items():
        if not _was_nullable(spec):
            continue
        seen = [t[name] for t in tiles
                if isinstance(t, dict) and t.get(name) not in (None, "")]
        if not seen:
            continue                       # no tile saw it; keep the page's

        ranked = sorted(_group_equivalent(name, seen), key=len, reverse=True)

        if len(ranked) == 1:
            merged[name] = ranked[0][0]
            continue
        if len(ranked[0]) > len(ranked[1]):
            chosen = ranked[0][0]
        else:
            tied = [g for g in ranked if len(g) == len(ranked[0])]
            backs = str(page.get(name) or "").strip().casefold()
            chosen = next((g[0] for g in tied
                           if str(g[0]).strip().casefold() == backs), None)
        merged[name] = chosen
        notes[name] = {"readings": seen, "chose": chosen}
    if notes:
        merged["_tiles_disagreed"] = notes
    return merged


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------


@dataclass
class Estimate:
    documents: int = 0
    pages: int = 0
    cached_pages: int = 0
    model: str = DEFAULT_MODEL
    max_edge: int = DEFAULT_MAX_EDGE
    tiles: bool = True

    @property
    def pages_to_read(self) -> int:
        return max(0, self.pages - self.cached_pages)

    @property
    def reads_per_page(self) -> int:
        """One for the whole page, plus one per close-up tile."""
        return 1 + (TILE_GRID[0] * TILE_GRID[1] if self.tiles else 0)

    @property
    def cost_usd(self) -> float:
        if is_local(self.model):
            return 0.0          # your own GPU, your own electricity
        in_price, out_price = MODEL_PRICES.get(self.model, MODEL_PRICES[DEFAULT_MODEL])
        n = self.pages_to_read
        if not n:
            return 0.0
        reads = n * self.reads_per_page
        # The instruction prefix is identical on every read, so it is cached
        # after the first one and bills at roughly a tenth of list price.
        prompt = _PROMPT_TOKENS + 0.1 * _PROMPT_TOKENS * (reads - 1)
        # A tile is rendered to the same longest edge as a whole page, so it
        # costs the same to send. It is the enlargement that is being bought,
        # not a smaller picture.
        images = image_tokens(self.max_edge) * reads
        # Tiles answer the scalar fields only, so they return a fraction of a
        # full page's output — the row arrays are asked for once.
        out = _OUTPUT_TOKENS * n * (1 + 0.25 * (self.reads_per_page - 1))
        return ((prompt + images) * in_price + out * out_price) / 1_000_000

    def describe(self) -> str:
        price = ("free, on this machine" if is_local(self.model)
                 else f"about ${self.cost_usd:,.2f}")
        close_ups = (f", each read {self.reads_per_page}x for the small print"
                     if self.tiles else "")
        return (
            f"{self.documents:,} documents, {self.pages:,} pages "
            f"({self.cached_pages:,} already read, {self.pages_to_read:,} to read) "
            f"on {self.model} at {self.max_edge}px{close_ups} - {price}"
        )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _trust_the_operating_system() -> bool:
    """Verify TLS against the Windows certificate store rather than certifi's.

    Corporate networks inspect TLS, which means the certificate the API
    presents is signed by the employer's own root rather than a public one.
    Windows trusts that root — every browser on the machine works — but Python
    ships its own CA bundle and does not, so the SDK fails with
    ``CERTIFICATE_VERIFY_FAILED`` and reports it as a connection error, which
    reads like the network is down rather than like a trust-store mismatch.

    This is the machine's own trust decision, not a relaxation of it: the
    alternative people usually reach for is disabling verification, which
    would send an API key over a connection nobody has checked.
    """
    try:
        import truststore
    except ImportError:
        return False        # public network, or an install without it
    truststore.inject_into_ssl()
    return True


def credentials_available() -> bool:
    """Whether anything on this machine can authenticate to the API.

    Checked before spending time rendering pages, and used to keep the rest of
    the tool working normally when the answer is no.
    """
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    # `ant auth login` stores a profile the SDK picks up with no env var set.
    config = os.environ.get("ANTHROPIC_CONFIG_DIR")
    candidates = [Path(config)] if config else []
    candidates += [Path.home() / ".config" / "anthropic"]
    if appdata := os.environ.get("APPDATA"):
        candidates.append(Path(appdata) / "Anthropic")
    return any((c / "credentials").is_dir() or (c / "configs").is_dir() for c in candidates)


@dataclass
class VisionReader:
    """Reads scanned pages, caching every result on the page's fingerprint."""

    db: Database
    model: str = DEFAULT_MODEL
    max_edge: int = DEFAULT_MAX_EDGE
    effort: str = "low"
    #: Re-read a page as four overlapping close-ups and let those overrule the
    #: whole-page reading of the scalar fields. Five times the reads, and the
    #: reason the small print is right — so it is spent only where the small
    #: print decides a finding. One of ``TILE_MODES``.
    tiles: str = "auto"
    _client: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.tiles not in TILE_MODES:
            raise ValueError(
                f"Unknown tile mode {self.tiles!r}. Known: {', '.join(TILE_MODES)}"
            )
        if not is_local(self.model) and self.model not in MODEL_PRICES:
            raise ValueError(
                f"Unknown model {self.model!r}. Known: "
                f"{', '.join(sorted(MODEL_PRICES))}, or local:<ollama-model>"
            )

    # -- cache -------------------------------------------------------------

    def _cache_key(self, fingerprint: str, kind: str) -> str:
        """Identifies a page *and the question asked of it*.

        The prompt and schema belong in the key, because changing the
        extraction means the cached answer is no longer an answer to this
        question. That was the stated intent from the start and the code did
        not do it — the key was page, kind and resolution only, so tuning a
        prompt after a run would silently replay the old answers, which is
        precisely what the first real run is expected to lead to.
        """
        grid = (f"{TILE_GRID[0]}x{TILE_GRID[1]}"
                if tiles_for(self.tiles, kind) else "whole")
        return (f"{fingerprint}:{kind}:{self.max_edge}:{grid}:"
                f"{_extraction_version(kind)}")

    def cached(self, fingerprint: str, page_no: int, kind: str) -> dict | None:
        return self.db.ocr_get(self._cache_key(fingerprint, kind), page_no, self.model)

    # -- reading -----------------------------------------------------------

    @property
    def client(self):
        if self._client is None:
            if not credentials_available():
                raise VisionUnavailable(
                    "No Anthropic credentials found. Set ANTHROPIC_API_KEY in your "
                    "environment, or run 'ant auth login' to store a profile."
                )
            _trust_the_operating_system()
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def read_page(self, path: str | Path, page_no: int, kind: str,
                  fingerprint: str) -> dict:
        """Extract one page, using the cache when the page has been read before."""
        if kind not in PROMPTS:
            raise ValueError(f"No extraction prompt for document kind {kind!r}")

        key = self._cache_key(fingerprint, kind)
        if (hit := self.db.ocr_get(key, page_no, self.model)) is not None:
            return hit

        schema = SCHEMAS[kind]
        payload = self._ask(path, page_no, PROMPTS[kind], schema, clip=None)

        if tiles_for(self.tiles, kind) and not payload.get("_error"):
            payload = self._add_the_close_ups(path, page_no, kind, schema, payload)

        self.db.ocr_put(key, page_no, self.model, payload)
        return payload

    def _add_the_close_ups(self, path, page_no: int, kind: str,
                           schema: dict, page: dict) -> dict:
        """Re-read the scalar fields from four overlapping quarters."""
        fragment = _fragment_schema(schema)
        if not fragment["properties"]:
            return page                       # nothing a fragment could answer

        prompt = PROMPTS[kind] + _TILE_PROMPT
        readings = []
        for clip in page_tiles():
            got = self._ask(path, page_no, prompt, fragment, clip=clip)
            # One unreadable tile is not a reason to discard the other three;
            # it just means that quarter contributes nothing to the merge.
            if not got.get("_error"):
                readings.append(got)
        if not readings:
            return page

        merged = _merge_tiles(page, readings, schema)
        merged["_tiles"] = len(readings)
        return merged

    def _ask(self, path, page_no: int, prompt: str, schema: dict,
             clip: tuple[float, float, float, float] | None) -> dict:
        """One model call about one image, hosted or local."""
        image, media_type = render_page(path, page_no, self.max_edge, clip=clip)

        if is_local(self.model):
            return _read_locally(self.model, prompt, schema, image)

        message = self.client.messages.create(
            model=self.model,
            max_tokens=8000,
            system=[{
                "type": "text",
                "text": prompt + _EMPTY_MEANS_UNREAD,
                # Identical on every page of the run, so it caches after the
                # first read instead of being re-billed thousands of times.
                "cache_control": {"type": "ephemeral"},
            }],
            output_config={
                **({"effort": self.effort}
                   if self.model in EFFORT_CAPABLE else {}),
                "format": {"type": "json_schema",
                           "schema": _null_free_strings(schema)},
            },
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": base64.standard_b64encode(image).decode(),
                        },
                    },
                    {"type": "text", "text": "Transcribe this page."},
                ],
            }],
        )

        payload = _payload_from(message)
        if payload.get("_error"):
            return payload
        payload = _restore_nulls(payload, schema)
        payload["_meta"] = {
            "model": message.model,
            "stop_reason": message.stop_reason,
            "input_tokens": message.usage.input_tokens,
            "output_tokens": message.usage.output_tokens,
            "cache_read_input_tokens": getattr(message.usage, "cache_read_input_tokens", 0),
        }
        return payload


def local_available() -> tuple[bool, str]:
    """Whether an Ollama daemon is reachable, and what it is serving."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=3) as reply:
            served = json.load(reply).get("models") or []
    except (urllib.error.URLError, OSError, ValueError) as why:
        return False, f"no Ollama daemon at {OLLAMA_HOST} ({why})"
    names = sorted(m.get("name", "") for m in served)
    return bool(names), ", ".join(names) or "the daemon is running but has no models"


def _read_locally(model: str, prompt: str, schema: dict, image: bytes) -> dict:
    """Read one page on the local daemon, asking for the same schema.

    Ollama takes a JSON schema in ``format`` and constrains generation to it,
    which is the same contract the hosted models honour — so a local reading
    lands in the cache in exactly the shape the appliers already expect, and
    nothing downstream needs to know which produced it.
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        f"{OLLAMA_HOST}/api/chat",
        data=json.dumps({
            "model": local_model_name(model),
            "stream": False,
            "format": schema,
            # Transcription, not composition: the same page must read the same
            # way twice or a re-run silently changes the audit.
            "options": {"temperature": 0},
            "messages": [{
                "role": "system", "content": prompt,
            }, {
                "role": "user", "content": "Transcribe this page.",
                "images": [base64.standard_b64encode(image).decode()],
            }],
        }).encode(),
        headers={"Content-Type": "application/json"},
    )
    # Distinguished, because they call for different actions and lumping them
    # together cost a diagnostic round trip: a rejected schema is a bug, a
    # timeout means the image is too large for this machine, and a refused
    # connection means the daemon is not running.
    try:
        with urllib.request.urlopen(request, timeout=LOCAL_TIMEOUT_S) as reply:
            body = json.load(reply)
    except urllib.error.HTTPError as why:
        detail = why.read().decode("utf-8", "replace")[:400]
        return {"_error": f"rejected ({why.code})", "_detail": detail}
    except TimeoutError:
        return {"_error": f"timed out after {LOCAL_TIMEOUT_S}s",
                "_detail": "try a smaller --max-edge; this page is too big "
                           "for the model on this machine"}
    except urllib.error.URLError as why:
        reason = getattr(why, "reason", why)
        if isinstance(reason, TimeoutError):
            return {"_error": f"timed out after {LOCAL_TIMEOUT_S}s",
                    "_detail": "try a smaller --max-edge"}
        return {"_error": "cannot reach the daemon", "_detail": str(reason)}
    except OSError as why:
        return {"_error": "cannot reach the daemon", "_detail": str(why)}
    except ValueError as why:
        return {"_error": "unparsable", "_detail": str(why)}

    text = (body.get("message") or {}).get("content") or ""
    try:
        payload = json.loads(text)
    except ValueError:
        return {"_error": "unparsable", "_raw": text[:2000]}
    payload["_meta"] = {
        "model": model,
        "eval_ms": round((body.get("total_duration") or 0) / 1e6),
        "input_tokens": body.get("prompt_eval_count"),
        "output_tokens": body.get("eval_count"),
    }
    return payload


def _payload_from(message) -> dict:
    """Pull the structured object out of a response, refusals included.

    A refusal arrives as a normal 200 with an empty or partial content list, so
    reading ``content[0]`` unconditionally would raise on exactly the responses
    worth reporting.
    """
    if message.stop_reason == "refusal":
        details = getattr(message, "stop_details", None)
        return {
            "_error": "refused",
            "_category": getattr(details, "category", None) if details else None,
        }
    for block in message.content:
        if block.type == "text":
            try:
                return json.loads(block.text)
            except ValueError:
                return {"_error": "unparsable", "_raw": block.text[:2000]}
    return {"_error": "empty_response", "_stop_reason": message.stop_reason}


def estimate(db: Database, targets: Iterable[tuple[str, str, int]], *,
             model: str = DEFAULT_MODEL, max_edge: int = DEFAULT_MAX_EDGE,
             kind: str = "reader_sheet", tiles: str = "auto") -> Estimate:
    """Cost of reading ``targets`` — ``(path, fingerprint, page_count)`` triples."""
    est = Estimate(model=model, max_edge=max_edge, tiles=tiles_for(tiles, kind))
    # Built with the same tiling setting, so the cache lookups below ask the
    # same question the pass will ask. Otherwise a run with tiling turned on
    # would report pages as already read that were only read whole.
    reader = VisionReader(db, model=model, max_edge=max_edge, tiles=tiles)
    for _path, fingerprint, pages in targets:
        est.documents += 1
        for page_no in range(pages):
            est.pages += 1
            if reader.cached(fingerprint, page_no, kind) is not None:
                est.cached_pages += 1
    return est
