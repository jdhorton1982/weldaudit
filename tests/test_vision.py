"""The vision pass, exercised without touching the API.

The network call is the only part of this pipeline that costs money, so it is
the only part stubbed out. Everything either side of it — page rendering,
cost estimation, refusal handling, the cache key, writing results back, and
the rules that only fire once a page has been read — runs for real here.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit import vision  # noqa: E402
from weldaudit.db import Database  # noqa: E402
from weldaudit.extract.vision_pass import Target, run  # noqa: E402
from weldaudit.index import is_junk  # noqa: E402
from weldaudit.rules import nde_coverage, ndetech  # noqa: E402

SHEET = Path(
    r"C:\Users\you\OneDrive\the operator\the operator Projects\Bluewater\Bluewater 14\20 LP"
    r"\11 NDE\Reader Sheets\GFB\20IN LP 09.09.25 GFB-037-040.pdf"
)


# -- AppleDouble junk -------------------------------------------------------

def test_appledouble_forks_are_not_documents():
    # macOS writes ._Foo.pdf beside every real file copied to a non-Mac
    # filesystem. Indexed, they look like a second copy of the sheet and
    # produce phantom "two sheets claim this shot" findings.
    assert is_junk("._20IN LP 08.27.25 CTI-001.pdf")
    assert is_junk("Thumbs.db")
    assert not is_junk("20IN LP 08.27.25 CTI-001.pdf")


# -- rendering --------------------------------------------------------------

@pytest.mark.skipif(not SHEET.exists(), reason="corpus not present")
def test_render_page_produces_a_bounded_jpeg():
    data, media_type = vision.render_page(SHEET, 0, max_edge=1200)
    assert media_type == "image/jpeg"
    assert data[:3] == b"\xff\xd8\xff"          # JPEG SOI marker
    assert 5_000 < len(data) < 2_000_000


# -- cost model -------------------------------------------------------------

def test_image_tokens_respect_the_high_resolution_cap():
    assert vision.image_tokens(1000) < vision.image_tokens(2000)
    assert vision.image_tokens(4000) <= 4784


def test_a_prompt_change_invalidates_the_cache():
    # The key had been page, kind and resolution only, so tuning a prompt
    # after a run silently replayed answers to the previous question — and
    # tuning after the first run is exactly what is expected to happen.
    from weldaudit.vision import PROMPTS, VisionReader, _extraction_version

    reader = VisionReader(None)
    before = reader._cache_key("fp1", "reader_sheet")
    original = PROMPTS["reader_sheet"]
    try:
        PROMPTS["reader_sheet"] = original + "\n\nAlso read the truck number."
        _extraction_version.cache_clear()
        assert reader._cache_key("fp1", "reader_sheet") != before
    finally:
        PROMPTS["reader_sheet"] = original
        _extraction_version.cache_clear()
    assert reader._cache_key("fp1", "reader_sheet") == before


def test_a_different_kind_is_a_different_key():
    from weldaudit.vision import VisionReader

    reader = VisionReader(None)
    assert reader._cache_key("fp1", "mtr") != reader._cache_key("fp1", "reader_sheet")


def test_cached_pages_are_free():
    est = vision.Estimate(documents=10, pages=40, cached_pages=40)
    assert est.pages_to_read == 0 and est.cost_usd == 0.0


def test_cost_scales_with_pages_and_model():
    opus = vision.Estimate(documents=1, pages=100, model="claude-opus-5")
    haiku = vision.Estimate(documents=1, pages=100, model="claude-haiku-4-5")
    assert opus.cost_usd > haiku.cost_usd > 0


def test_unknown_model_is_rejected_up_front():
    with pytest.raises(ValueError):
        vision.VisionReader(db=None, model="gpt-4")


# -- response handling ------------------------------------------------------

class _Blk:
    def __init__(self, text):
        self.type, self.text = "text", text


class _Msg:
    def __init__(self, content, stop_reason="end_turn", stop_details=None):
        self.content, self.stop_reason, self.stop_details = content, stop_reason, stop_details


def test_refusal_is_reported_not_indexed():
    # A refusal is a 200 with empty content; reading content[0] would raise on
    # exactly the responses worth surfacing.
    class D:
        category = "cyber"
    out = vision._payload_from(_Msg([], "refusal", D()))
    assert out["_error"] == "refused" and out["_category"] == "cyber"


def test_unparsable_output_is_captured_rather_than_crashing():
    out = vision._payload_from(_Msg([_Blk("not json at all")]))
    assert out["_error"] == "unparsable"


def test_valid_json_is_returned():
    assert vision._payload_from(_Msg([_Blk('{"heat": "12345"}')]))["heat"] == "12345"


def test_no_credentials_here():
    # This machine has none; the pass must say so rather than half-run.
    assert vision.credentials_available() is False


# -- write-back and the rules it unlocks ------------------------------------

class StubReader:
    """Stands in for the model: returns canned pages, records what was asked."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def cached(self, fingerprint, page_no, kind):
        return None

    def read_page(self, path, page_no, kind, fingerprint):
        self.calls.append((fingerprint, page_no))
        return self.pages[page_no]


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "t.db")
    pid = database.upsert_project("T", str(tmp_path))
    with database.tx() as c:
        c.execute(
            """INSERT INTO document(id, project_id, path, filename, ext, fingerprint,
                                    segment, kind)
               VALUES(1, ?, 'x.pdf', 'x.pdf', '.pdf', 'fp1', 'SEG A', 'nde_reader_sheet')""",
            (pid,),
        )
        # A weld citing GFB-037, welded by ARB/AMG.
        c.execute(
            """INSERT INTO weld(project_id, segment, line, weld_no, nde_id,
                                welder_root, welder_cap, source)
               VALUES(?, 'SEG A', '20 LP', '7', 'GFB-037', 'ARB/AMG', 'ARB/AMG',
                      'daily_weld_report')""",
            (pid,),
        )
        c.execute(
            """INSERT INTO nde_shot(project_id, document_id, fingerprint, copies,
                                    segments, segment, nde_id, prefix, number,
                                    suffix, evidence)
               VALUES(?, 1, 'fp1', 1, 'SEG A', 'SEG A', 'GFB-037', 'GFB', 37, '',
                      'filename')""",
            (pid,),
        )
        c.execute(
            """INSERT INTO nde_tech(project_id, segment, company, name, rig_letter,
                                    certs, acuity, cert_date, arrived)
               VALUES(?, 'SEG A', 'IIA', 'JOHN DAVID "JD" WILLIAMS', 'G', 'Y', 'Y',
                      '2022-11-03', '2025-08-09')""",
            (pid,),
        )
    return database, pid


def _target():
    return Target(1, "x.pdf", "x.pdf", "fp1", 1, "test", "SEG A")


def test_reader_sheet_results_are_written_back(db):
    database, pid = db
    reader = StubReader([{
        "page_is_reader_sheet": True,
        "technician": "JD WILLIAMS",
        "rows": [{"weld_id": "GFB-37", "result": "ACC", "pipe_diameter": "20",
                  "wall_thickness": ".375", "welder_stencil": "ARB/AMG",
                  "indications": None, "remarks": None}],
    }])
    result = run(database, pid, "reader_sheet", reader, [_target()])
    assert result.updated == 1

    shot = database.one(
        "SELECT * FROM nde_shot WHERE project_id=? AND nde_id='GFB-037'", (pid,))
    assert shot["result"] == "ACC"
    assert shot["evidence"] == "vision"
    assert shot["technician"] == "JD WILLIAMS"


def test_the_printed_date_is_kept(db):
    # A sheet nothing else can read is the one case where the vision result is
    # the *only* record of it, and the insert was hardcoding sheet_date to
    # NULL — so every date rule skipped exactly the sheets that were read.
    database, pid = db
    reader = StubReader([{
        "page_is_reader_sheet": True, "technician": "JD WILLIAMS",
        "sheet_date": "05/29/2025",
        "rows": [{"weld_id": "GFB-37", "result": "ACC", "pipe_diameter": None,
                  "wall_thickness": None, "welder_stencil": None,
                  "indications": None, "remarks": None}],
    }])
    run(database, pid, "reader_sheet", reader, [_target()])
    shot = database.one(
        "SELECT * FROM nde_shot WHERE project_id=? AND nde_id='GFB-037'", (pid,))
    assert shot["sheet_date"] == "2025-05-29"


def test_the_filename_date_is_the_fallback(db):
    # PLU names its sheets for the day, so even a blank scan has a date.
    database, pid = db
    reader = StubReader([{
        "page_is_reader_sheet": True, "technician": "JD WILLIAMS",
        "sheet_date": None,
        "rows": [{"weld_id": "GFB-37", "result": "ACC", "pipe_diameter": None,
                  "wall_thickness": None, "welder_stencil": None,
                  "indications": None, "remarks": None}],
    }])
    target = Target(1, "x.pdf", "DTD22 NDE 5.29.25 FG SEG.C RT RIG.A.pdf",
                    "fp1", 1, "test", "SEG A")
    run(database, pid, "reader_sheet", reader, [target])
    shot = database.one(
        "SELECT * FROM nde_shot WHERE project_id=? AND nde_id='GFB-037'", (pid,))
    assert shot["sheet_date"] == "2025-05-29"


# -- one weld, several assessed areas ---------------------------------------
#
# The Precision Group radiography form shoots a weld in three overlapping
# exposures and assesses each one. FTI-039 on `FTI-036-039-039R 8-11-25 SEG
# A.pdf` is accepted on area 0-A, rejected on A-B with a 1.625" elongated slag
# inclusion, and accepted on B-0.

def _area(weld, area, result, **extra):
    row = {"weld_id": weld, "area": area, "result": result,
           "pipe_diameter": "6.625\"", "wall_thickness": ".280",
           "welder_stencil": "AEA", "indications": None, "remarks": None}
    row.update(extra)
    return row


def test_a_reject_on_one_area_rejects_the_weld(db):
    # The rows arrive top to bottom and each upserts onto the same shot, so
    # the last one written wins — and the last one here says accepted.
    database, pid = db
    reader = StubReader([{
        "page_is_reader_sheet": True, "technician": "JIMMY HANKS",
        "sheet_date": "Aug 11, 2025", "weld_count": 1,
        "rows": [_area("GFB-37", "0-A", "ACC"),
                 _area("GFB-37", "A-B", "REJ", indications="ESI",
                       remarks="1.625"),
                 _area("GFB-37", "B-0", "ACC")],
    }])
    run(database, pid, "reader_sheet", reader, [_target()])
    shot = database.one(
        "SELECT * FROM nde_shot WHERE project_id=? AND nde_id='GFB-037'", (pid,))
    assert shot["result"] == "REJ"


def test_the_three_areas_are_one_shot_not_three(db):
    database, pid = db
    reader = StubReader([{
        "page_is_reader_sheet": True, "technician": "JIMMY HANKS",
        "rows": [_area("GFB-37", a, "ACC") for a in ("0-A", "A-B", "B-0")],
    }])
    result = run(database, pid, "reader_sheet", reader, [_target()])
    assert result.updated == 1
    assert database.q(
        "SELECT id FROM nde_shot WHERE project_id=? AND nde_id='GFB-037'",
        (pid,)).__len__() == 1


def test_an_area_with_no_verdict_does_not_erase_one(db):
    # Continuation sub-rows often carry an area and nothing else.
    database, pid = db
    reader = StubReader([{
        "page_is_reader_sheet": True, "technician": "JIMMY HANKS",
        "rows": [_area("GFB-37", "0-A", "ACC"),
                 _area("GFB-37", "A-B", None)],
    }])
    run(database, pid, "reader_sheet", reader, [_target()])
    shot = database.one(
        "SELECT * FROM nde_shot WHERE project_id=? AND nde_id='GFB-037'", (pid,))
    assert shot["result"] == "ACC"


def test_the_sheets_own_weld_count_is_kept(db):
    database, pid = db
    reader = StubReader([{
        "page_is_reader_sheet": True, "technician": "JIMMY HANKS",
        "weld_count": 8,
        "rows": [_area("GFB-37", None, "ACC")],
    }])
    run(database, pid, "reader_sheet", reader, [_target()])
    row = database.one(
        "SELECT * FROM reader_sheet WHERE project_id=? AND fingerprint='fp1'", (pid,))
    assert row["weld_count"] == 8 and row["evidence"] == "vision"


def test_a_read_count_replaces_the_one_ocr_guessed(db):
    # These counts sit on scans. A model looking at the page is the better
    # witness than the OCR layer, so it must not end up with two rows.
    database, pid = db
    with database.tx() as c:
        c.execute(
            """INSERT INTO reader_sheet(project_id, document_id, fingerprint,
                                        filename, segment, page_no, weld_count,
                                        evidence)
               VALUES(?, 1, 'fp1', 'x.pdf', 'SEG A', 1, 3, 'text')""",
            (pid,),
        )
    reader = StubReader([{
        "page_is_reader_sheet": True, "technician": "JIMMY HANKS",
        "weld_count": 8, "rows": [_area("GFB-37", None, "ACC")],
    }])
    run(database, pid, "reader_sheet", reader, [_target()])
    rows = database.q(
        "SELECT * FROM reader_sheet WHERE project_id=? AND fingerprint='fp1'", (pid,))
    assert len(rows) == 1 and rows[0]["weld_count"] == 8


def test_no_count_on_the_page_records_nothing(db):
    database, pid = db
    reader = StubReader([{
        "page_is_reader_sheet": True, "technician": "JD WILLIAMS",
        "weld_count": None, "rows": [_area("GFB-37", None, "ACC")],
    }])
    run(database, pid, "reader_sheet", reader, [_target()])
    assert database.q("SELECT * FROM reader_sheet WHERE project_id=?", (pid,)) == []


def test_a_rejected_sheet_with_no_closure_is_critical(db):
    database, pid = db
    reader = StubReader([{
        "page_is_reader_sheet": True, "technician": "JD WILLIAMS",
        "rows": [{"weld_id": "GFB-37", "result": "REJ", "pipe_diameter": None,
                  "wall_thickness": None, "welder_stencil": None,
                  "indications": "IP", "remarks": None}],
    }])
    run(database, pid, "reader_sheet", reader, [_target()])

    findings = nde_coverage.sheet_reject_unclosed(database, pid, "r1")
    assert len(findings) == 1
    assert findings[0]["severity"] == "critical"
    assert "REJECTED" in findings[0]["message"]


def test_a_reject_closed_by_a_repair_shot_is_not_reported(db):
    database, pid = db
    with database.tx() as c:
        c.execute(
            """INSERT INTO nde_shot(project_id, document_id, fingerprint, segments,
                                    segment, nde_id, prefix, number, suffix, evidence)
               VALUES(?, 1, 'fp2', 'SEG A', 'SEG A', 'GFB-037R', 'GFB', 37, 'R',
                      'filename')""",
            (pid,),
        )
    reader = StubReader([{
        "page_is_reader_sheet": True, "technician": "JD WILLIAMS",
        "rows": [{"weld_id": "GFB-37", "result": "REJ", "pipe_diameter": None,
                  "wall_thickness": None, "welder_stencil": None,
                  "indications": None, "remarks": None}],
    }])
    run(database, pid, "reader_sheet", reader, [_target()])
    assert nde_coverage.sheet_reject_unclosed(database, pid, "r1") == []


def test_welder_disagreement_between_sheet_and_report_is_flagged(db):
    database, pid = db
    reader = StubReader([{
        "page_is_reader_sheet": True, "technician": "JD WILLIAMS",
        "rows": [{"weld_id": "GFB-37", "result": "ACC", "pipe_diameter": None,
                  "wall_thickness": None, "welder_stencil": "AEA/ANR",
                  "indications": None, "remarks": None}],
    }])
    run(database, pid, "reader_sheet", reader, [_target()])

    findings = nde_coverage.sheet_welder_mismatch(database, pid, "r1")
    assert len(findings) == 1
    assert "AEA" in findings[0]["message"] and "ARB" in findings[0]["message"]


def test_matching_welders_produce_no_finding(db):
    database, pid = db
    reader = StubReader([{
        "page_is_reader_sheet": True, "technician": "JD WILLIAMS",
        "rows": [{"weld_id": "GFB-37", "result": "ACC", "pipe_diameter": None,
                  "wall_thickness": None, "welder_stencil": "ARB/AMG",
                  "indications": None, "remarks": None}],
    }])
    run(database, pid, "reader_sheet", reader, [_target()])
    assert nde_coverage.sheet_welder_mismatch(database, pid, "r1") == []


def test_technician_name_spelling_drift_is_tolerated(db):
    # The rig logs spell the same person several ways; only a genuinely
    # different person should be a finding.
    database, pid = db
    reader = StubReader([{
        "page_is_reader_sheet": True, "technician": "Jd Willams",  # one keystroke off
        "rows": [{"weld_id": "GFB-37", "result": "ACC", "pipe_diameter": None,
                  "wall_thickness": None, "welder_stencil": None,
                  "indications": None, "remarks": None}],
    }])
    run(database, pid, "reader_sheet", reader, [_target()])
    assert ndetech.sheet_technician_mismatch(database, pid, "r1") == []


def test_a_different_technician_than_the_rig_log_is_flagged(db):
    database, pid = db
    reader = StubReader([{
        "page_is_reader_sheet": True, "technician": "BREYDON BURKETT",
        "rows": [{"weld_id": "GFB-37", "result": "ACC", "pipe_diameter": None,
                  "wall_thickness": None, "welder_stencil": None,
                  "indications": None, "remarks": None}],
    }])
    run(database, pid, "reader_sheet", reader, [_target()])

    findings = ndetech.sheet_technician_mismatch(database, pid, "r1")
    assert len(findings) == 1
    assert "BREYDON BURKETT" in findings[0]["message"]


def test_a_page_that_is_not_a_reader_sheet_writes_nothing(db):
    database, pid = db
    reader = StubReader([{"page_is_reader_sheet": False, "rows": []}])
    assert run(database, pid, "reader_sheet", reader, [_target()]).updated == 0


def test_mtr_keeps_the_mill_and_the_letterhead_apart(db):
    database, pid = db
    with database.tx() as c:
        c.execute(
            """INSERT INTO material(project_id, document_id, segment, heat, heat_key,
                                    source, categories)
               VALUES(?, 1, 'SEG A', '617294', '617294', 'mtr_file', '1.0 Pipe')""",
            (pid,),
        )
    reader = StubReader([{
        "page_is_certificate": True, "heat": "617294",
        "issuing_company": "J-K Enterprises, Inc.",   # a machine shop
        "mill_name": "Primus Pipe & Tube",            # the actual producer
        "mill_location": "Hope, AR, USA", "country_of_melt": "U.S.A.",
        "country_of_manufacture": "U.S.A.", "specification": "A312",
        "grade": None, "size": '16"', "wall_thickness": None, "description": None,
    }])
    run(database, pid, "mtr", reader, [_target()])

    row = database.one("SELECT * FROM material WHERE project_id=?", (pid,))
    # The AML check must run against the mill, not the letterhead.
    assert row["manufacturer"] == "Primus Pipe & Tube"
    assert row["issuing_company"] == "J-K Enterprises, Inc."
    assert row["nps"] == 16


def test_mtr_falls_back_to_the_letterhead_when_no_mill_is_named(db):
    database, pid = db
    with database.tx() as c:
        c.execute(
            """INSERT INTO material(project_id, document_id, segment, heat, heat_key,
                                    source) VALUES(?, 1, 'SEG A', 'H1', 'H1', 'mtr_file')""",
            (pid,),
        )
    reader = StubReader([{
        "page_is_certificate": True, "heat": "H1",
        "issuing_company": "Norvale", "mill_name": None, "mill_location": None,
        "country_of_melt": None, "country_of_manufacture": None,
        "specification": None, "grade": None, "size": None,
        "wall_thickness": None, "description": None,
    }])
    run(database, pid, "mtr", reader, [_target()])
    assert database.one("SELECT manufacturer FROM material WHERE project_id=?",
                        (pid,))["manufacturer"] == "Norvale"


def test_ocr_results_are_cached_by_fingerprint(db):
    database, _pid = db
    database.ocr_put("fp1:mtr:2000", 0, "claude-opus-5", {"heat": "X"})
    assert database.ocr_get("fp1:mtr:2000", 0, "claude-opus-5") == {"heat": "X"}
    # A different render size is a different question, so not a cache hit.
    assert database.ocr_get("fp1:mtr:1400", 0, "claude-opus-5") is None


# -- the request the API will actually accept ------------------------------
#
# These two shapes were both rejected with HTTP 400 on the first real hosted
# run, after every kind had been built and tested. Nothing caught them because
# the request itself is the one thing the stub replaces, so the schemas were
# only ever validated by me reading them.

def _every_schema_node():
    def walk(node, where):
        if isinstance(node, dict):
            yield where, node
            for key, child in node.items():
                yield from walk(child, f"{where}.{key}")
        elif isinstance(node, list):
            for i, child in enumerate(node):
                yield from walk(child, f"{where}[{i}]")

    for kind, schema in vision.SCHEMAS.items():
        yield from walk(schema, kind)


def test_no_field_declares_both_a_union_type_and_an_enum():
    """`{"type": ["string","null"], "enum": [...]}` is a 400.

    The enum alone already says what may appear, null included, so the type is
    redundant as well as fatal.
    """
    both = [where for where, node in _every_schema_node()
            if "enum" in node and isinstance(node.get("type"), list)]
    assert both == [], f"union type alongside an enum: {both}"


def test_effort_is_only_sent_to_models_that_accept_it():
    """Haiku 4.5 rejects `output_config.effort` outright."""
    assert "claude-haiku-4-5" not in vision.EFFORT_CAPABLE
    assert vision.DEFAULT_MODEL in vision.EFFORT_CAPABLE
    # Every priced model is answered one way or the other, so adding a model
    # to the price table without deciding this cannot pass silently.
    assert vision.EFFORT_CAPABLE <= set(vision.MODEL_PRICES)


def _unions(node) -> int:
    if isinstance(node, dict):
        n = 1 if isinstance(node.get("type"), list) or "anyOf" in node else 0
        return n + sum(_unions(v) for v in node.values())
    if isinstance(node, list):
        return sum(_unions(v) for v in node)
    return 0


def test_every_schema_fits_the_api_union_limit_once_relaxed():
    """Five of eight kinds were over the limit of 16 and failed on send."""
    for kind, schema in vision.SCHEMAS.items():
        sent = vision._null_free_strings(schema)
        assert _unions(sent) <= 16, f"{kind} still has {_unions(sent)} unions"


def test_no_field_is_made_optional_to_fit():
    """Optional properties compile worse than the unions they replace.

    A dozen optional properties under `additionalProperties: false` is a
    grammar over every subset in every order; the first attempt at this fix
    traded 'too many unions' for 'grammar compilation timed out'.
    """
    for kind, schema in vision.SCHEMAS.items():
        sent = vision._null_free_strings(schema)
        assert sent.get("required") == schema.get("required"), kind
        rows = sent.get("properties", {}).get("rows")
        if isinstance(rows, dict):        # the nested row objects too
            assert (rows["items"].get("required")
                    == schema["properties"]["rows"]["items"].get("required")), kind


def test_strings_lose_their_null_but_numbers_keep_it():
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": ["string", "null"]},
            "depth": {"type": ["number", "null"]},
            "verdict": {"enum": ["ACC", "REJ", None]},
        },
        "required": ["name", "depth", "verdict"],
    }
    sent = vision._null_free_strings(schema)
    assert sent["properties"]["name"]["type"] == "string"
    assert sent["properties"]["depth"]["type"] == ["number", "null"]
    assert sent["properties"]["verdict"]["enum"] == ["ACC", "REJ", ""]
    assert sent["required"] == ["name", "depth", "verdict"]
    # The original is untouched, because it is also the cache key.
    assert schema["properties"]["name"]["type"] == ["string", "null"]


def test_empty_strings_come_back_as_nulls():
    """Downstream reads null for 'unreadable'; '' would read as a value."""
    schema = {
        "type": "object",
        "properties": {
            "heat": {"type": "string"},
            "mill": {"type": ["string", "null"]},
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"weld_id": {"type": "string"},
                                   "result": {"enum": ["ACC", "REJ", None]}},
                },
            },
        },
    }
    got = vision._restore_nulls(
        {"heat": "H1", "mill": "", "rows": [{"weld_id": "W1", "result": ""}]}, schema)
    assert got == {"heat": "H1", "mill": None,
                   "rows": [{"weld_id": "W1", "result": None}]}


def test_a_field_that_was_never_nullable_keeps_its_empty_string():
    """Only the fields '' was substituted into are read back as null."""
    schema = {"type": "object", "properties": {"heat": {"type": "string"}}}
    assert vision._restore_nulls({"heat": ""}, schema) == {"heat": ""}


def test_a_missing_field_is_still_nulled():
    """Required in the schema sent, so absence is the model falling short."""
    schema = {"type": "object", "properties": {"heat": {"type": "string"}}}
    assert vision._restore_nulls({}, schema) == {"heat": None}


def test_relaxed_schemas_keep_the_nesting_the_pages_actually_have():
    """A row list flattened by the rewrite would lose every weld on the page."""
    sent = vision._null_free_strings(vision.SCHEMAS["reader_sheet"])
    rows = sent["properties"]["rows"]
    assert rows["type"] == "array"
    assert "weld_id" in rows["items"]["properties"]


# -- tiling ----------------------------------------------------------------
#
# The API scales any image to ~1568px on its long edge, so small print on a
# whole page is a few pixels tall and the model reads a plausible neighbour
# rather than declining. Measured on the Kandal MTR: heat 24913 read as 12987
# from the whole page, correctly from a quarter of it.

def test_tiles_cover_the_whole_page():
    tiles = vision.page_tiles()
    assert len(tiles) == vision.TILE_GRID[0] * vision.TILE_GRID[1]
    assert min(t[0] for t in tiles) == 0.0 and min(t[1] for t in tiles) == 0.0
    assert max(t[2] for t in tiles) == 1.0 and max(t[3] for t in tiles) == 1.0


def test_tiles_overlap_so_nothing_falls_down_a_seam():
    """A value cut by a seam reads as two half-values that disagree."""
    (ax0, ay0, ax1, ay1), (bx0, _, _, _) = vision.page_tiles()[:2]
    assert bx0 < ax1, "left and right tiles do not overlap"
    assert ax1 - bx0 >= vision.TILE_OVERLAP


def test_a_tile_is_enlarged_not_shrunk():
    """Rendering a quarter page must buy detail, not just crop it."""
    import pymupdf

    doc = pymupdf.open()
    doc.new_page(width=792, height=612)          # letter, landscape
    path = Path(__file__).parent / "_tile_probe.pdf"
    doc.save(str(path))
    try:
        whole, _ = vision.render_page(path, 0, 800)
        quarter, _ = vision.render_page(path, 0, 800, clip=(0, 0, 0.5, 0.5))
        import io

        from PIL import Image  # noqa: PLC0415
    except ImportError:
        pytest.skip("Pillow not installed")
    else:
        w = Image.open(io.BytesIO(whole)).size
        q = Image.open(io.BytesIO(quarter)).size
        # Same longest edge, half the page — so twice the pixels per inch.
        assert max(w) == max(q) == 800
        assert q[0] / q[1] == pytest.approx(w[0] / w[1], abs=0.01)
    finally:
        path.unlink(missing_ok=True)


def test_a_fragment_is_only_asked_what_a_fragment_could_know():
    frag = vision._fragment_schema(vision.SCHEMAS["reader_sheet"])
    assert "rows" not in frag["properties"], "a quarter page cannot list the rows"
    assert "page_is_reader_sheet" not in frag["properties"], "not a fragment's call"
    assert "ticket_no" in frag["properties"]
    assert frag["required"] == sorted(frag["properties"])


def test_a_close_up_overrules_the_whole_page():
    schema = {"properties": {"heat": {"type": ["string", "null"]}}}
    merged = vision._merge_tiles({"heat": "12987"},
                                 [{"heat": None}, {"heat": "24913"}], schema)
    assert merged["heat"] == "24913"


def test_the_whole_page_stands_where_no_close_up_saw_the_field():
    schema = {"properties": {"heat": {"type": ["string", "null"]}}}
    merged = vision._merge_tiles({"heat": "24913"}, [{"heat": None}, {"heat": ""}],
                                 schema)
    assert merged["heat"] == "24913"


def test_the_majority_of_close_ups_wins():
    """One tile wandering into the wrong table must not veto two right ones.

    Real readings from the Kandal MTR: two tiles had the heat number, a third
    picked up 13.15 from a %-elongation column.
    """
    schema = {"properties": {"heat": {"type": ["string", "null"]}}}
    merged = vision._merge_tiles(
        {"heat": "12987"},
        [{"heat": "24913"}, {"heat": "13.15"}, {"heat": "24913"}], schema)
    assert merged["heat"] == "24913"
    assert merged["_tiles_disagreed"]["heat"]["chose"] == "24913"


def test_the_whole_page_breaks_a_tie_between_close_ups():
    schema = {"properties": {"loc": {"type": ["string", "null"]}}}
    merged = vision._merge_tiles({"loc": "Mingo Junction, OH"},
                                 [{"loc": "Mingo Junction, OH"}, {"loc": "USA"}],
                                 schema)
    assert merged["loc"] == "Mingo Junction, OH"


def test_a_tie_the_whole_page_cannot_settle_is_left_for_a_human():
    """Two irreconcilable readings of one box is not something to guess at."""
    schema = {"properties": {"heat": {"type": ["string", "null"]}}}
    merged = vision._merge_tiles({"heat": "12987"},
                                 [{"heat": "24913"}, {"heat": "13587"}], schema)
    assert merged["heat"] is None
    assert merged["_tiles_disagreed"]["heat"]["readings"] == ["24913", "13587"]


def test_the_whole_page_does_not_get_a_vote_when_a_close_up_is_unopposed():
    """The close-up exists because the whole page cannot read this print."""
    schema = {"properties": {"heat": {"type": ["string", "null"]}}}
    merged = vision._merge_tiles({"heat": "12987"}, [{"heat": "24913"}], schema)
    assert merged["heat"] == "24913"


def test_agreement_is_not_defeated_by_spacing():
    schema = {"properties": {"co": {"type": ["string", "null"]}}}
    merged = vision._merge_tiles({"co": None},
                                 [{"co": "Kandal Pipe USA, Inc"},
                                  {"co": "kandal pipe usa, inc "}], schema)
    assert merged["co"] == "Kandal Pipe USA, Inc"


def test_rows_are_never_touched_by_the_merge():
    """Row arrays come from the whole page; a fragment cannot see what it lacks."""
    schema = vision.SCHEMAS["reader_sheet"]
    page = {"rows": [{"weld_id": "W1"}], "ticket_no": "111"}
    merged = vision._merge_tiles(page, [{"ticket_no": "222"}], schema)
    assert merged["rows"] == [{"weld_id": "W1"}]


def test_the_cache_can_tell_a_tiled_reading_from_a_whole_page_one(db):
    database, _pid = db
    whole = vision.VisionReader(database, max_edge=2000, tiles="never")
    tiled = vision.VisionReader(database, max_edge=2000, tiles="always")
    assert whole._cache_key("fp", "mtr") != tiled._cache_key("fp", "mtr")


def test_close_ups_are_spent_only_where_the_small_print_decides_a_finding():
    """Tiling everything cost 4x and read non-critical fields slightly worse."""
    assert vision.tiles_for("auto", "mtr")
    assert vision.tiles_for("auto", "reader_sheet")
    assert not vision.tiles_for("auto", "coating")
    assert not vision.tiles_for("auto", "hydrotest")


def test_the_mode_can_override_the_per_kind_default_both_ways():
    assert vision.tiles_for("always", "coating")
    assert not vision.tiles_for("never", "mtr")


def test_every_tiled_kind_is_a_kind_that_exists():
    """A typo here would silently turn tiling off for the kind it names."""
    assert vision.TILED_KINDS <= set(vision.SCHEMAS)


def test_an_unknown_tile_mode_is_refused_up_front(db):
    database, _pid = db
    with pytest.raises(ValueError):
        vision.VisionReader(database, tiles="sometimes")


def test_one_kind_being_tiled_does_not_change_another_kinds_cache_key(db):
    """Turning tiling on for mtr must not invalidate every cached coating page."""
    database, _pid = db
    auto = vision.VisionReader(database, max_edge=2000, tiles="auto")
    never = vision.VisionReader(database, max_edge=2000, tiles="never")
    assert auto._cache_key("fp", "coating") == never._cache_key("fp", "coating")
    assert auto._cache_key("fp", "mtr") != never._cache_key("fp", "mtr")


def test_the_estimate_charges_for_every_read():
    tiled = vision.Estimate(documents=1, pages=10, model="claude-haiku-4-5")
    whole = vision.Estimate(documents=1, pages=10, model="claude-haiku-4-5",
                            tiles=False)
    assert whole.reads_per_page == 1
    assert tiled.reads_per_page == 5
    # Not free, and not hidden: the CLI prints this before sending anything.
    assert tiled.cost_usd > whole.cost_usd * 3
    assert "5x" in tiled.describe() and "5x" not in whole.describe()


# -- which cached reading a re-audit replays --------------------------------

def test_a_paid_reading_beats_a_free_one_however_old(db):
    """Benchmarking the local model must not downgrade a real audit.

    Both readings live in one cache keyed by page, and replay takes one. When
    that was "most recent", scoring qwen after paying for Haiku replaced the
    mill certificate's issuer with the preparer's signature — for free, and
    without saying so.
    """
    database, _pid = db
    database.ocr_put("fpX:mtr:2000:2x2:v1", 0, "claude-haiku-4-5",
                     {"issuing_company": "Kandal Pipe USA, Inc"})
    database.ocr_put("fpX:mtr:1100:2x2:v1", 0, "local:qwen2.5vl:7b",
                     {"issuing_company": "Aravind Nair"})
    got = database.ocr_any("fpX", "mtr", 0)
    assert got["issuing_company"] == "Kandal Pipe USA, Inc"


def test_within_a_tier_the_newer_reading_still_wins(db):
    """Re-reading a page with a better prompt must not be ignored."""
    database, _pid = db
    database.ocr_put("fpX:mtr:2000:whole:v1", 0, "claude-haiku-4-5", {"heat": "12987"})
    database.ocr_put("fpX:mtr:2000:2x2:v2", 0, "claude-haiku-4-5", {"heat": "24913"})
    assert database.ocr_any("fpX", "mtr", 0)["heat"] == "24913"


def test_a_free_reading_is_used_when_it_is_the_only_one(db):
    database, _pid = db
    database.ocr_put("fpX:mtr:1100:2x2:v1", 0, "local:qwen2.5vl:7b", {"heat": "H1"})
    assert database.ocr_any("fpX", "mtr", 0)["heat"] == "H1"


def test_the_local_prefix_matches_the_one_the_query_hardcodes():
    """db.py spells 'local:' out to avoid an import cycle; keep them in step."""
    assert vision.LOCAL_PREFIX == "local:"


# -- names are compared as companies, identifiers as strings ----------------

def test_a_plural_does_not_cancel_a_company_name():
    """The AML lookup these feed is fuzzy; cancelling over an 's' loses a name."""
    schema = {"properties": {"issuing_company": {"type": ["string", "null"]}}}
    merged = vision._merge_tiles(
        {"issuing_company": None},
        [{"issuing_company": "Kandal Pipe USA, Inc"},
         {"issuing_company": "Kandal Pipes USA, Inc."}], schema)
    assert merged["issuing_company"] == "Kandal Pipe USA, Inc"
    assert "_tiles_disagreed" not in merged


def test_a_different_company_still_cancels():
    """'Model Pipe' is not a spelling of 'Kandal Pipe'; that page needs a human."""
    schema = {"properties": {"issuing_company": {"type": ["string", "null"]}}}
    merged = vision._merge_tiles(
        {"issuing_company": None},
        [{"issuing_company": "Kandal Pipe USA, Inc"},
         {"issuing_company": "Model Pipe USA, Inc"}], schema)
    assert merged["issuing_company"] is None


def test_identifiers_get_no_such_latitude():
    """Two heats one digit apart are two heats, not one read two ways."""
    schema = {"properties": {"heat": {"type": ["string", "null"]}}}
    merged = vision._merge_tiles(
        {"heat": None}, [{"heat": "24913"}, {"heat": "12581"}], schema)
    assert merged["heat"] is None


def test_every_name_field_exists_in_some_schema():
    everywhere = {n for s in vision.SCHEMAS.values() for n in s.get("properties", {})}
    assert vision.NAME_FIELDS & everywhere == vision.NAME_FIELDS - {"manufacturer"}
