"""SQLite store for the audit.

One database per audit run location.  Everything the app knows lives here so a
run is reproducible and an auditor can be shown exactly which file and page a
finding came from.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS project (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    root        TEXT NOT NULL,
    scanned_at  TEXT
);

-- Every file found on disk, classified but not yet read.
CREATE TABLE IF NOT EXISTS document (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    filename    TEXT NOT NULL,
    ext         TEXT,
    size_bytes  INTEGER,
    modified_at TEXT,
    sha1        TEXT,
    -- Content fingerprint. Contractors file the same reader sheet into every
    -- book that shares a spread, so the same document appears many times on
    -- disk. Everything downstream counts distinct fingerprints, not paths.
    fingerprint TEXT,
    segment     TEXT,
    section_no  INTEGER,
    section     TEXT,
    kind        TEXT,
    has_text    INTEGER,          -- 1 text layer, 0 scanned, NULL not probed
    page_count  INTEGER,
    UNIQUE(project_id, path)
);
CREATE INDEX IF NOT EXISTS ix_doc_seg  ON document(project_id, segment);
CREATE INDEX IF NOT EXISTS ix_doc_kind ON document(project_id, kind);
CREATE INDEX IF NOT EXISTS ix_doc_fp   ON document(project_id, fingerprint);

-- A weld as recorded on the weld map / daily weld report - the "should exist" side.
CREATE TABLE IF NOT EXISTS weld (
    id            INTEGER PRIMARY KEY,
    project_id    INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    document_id   INTEGER REFERENCES document(id),
    segment       TEXT,
    line          TEXT,
    weld_no       TEXT,            -- as written on the source
    weld_size     TEXT,
    weld_type     TEXT,
    process       TEXT,
    -- No weld report in this corpus records it yet, but it is the one
    -- essential variable that cannot be inferred from size or process, so the
    -- column exists for the day a report layout carries it. See WLD-12.
    position      TEXT,
    wps           TEXT,
    welder_root   TEXT,
    welder_hp     TEXT,
    welder_fill   TEXT,
    welder_cap    TEXT,
    date_welded   TEXT,
    heat_us       TEXT,
    heat_ds       TEXT,
    nde_id        TEXT,            -- normalised NdeId this weld claims
    nde_report    TEXT,
    nde_date      TEXT,
    nde_technique TEXT,
    nde_status    TEXT,
    defect        TEXT,
    repair_nde_id TEXT,
    repair_status TEXT,
    note          TEXT,            -- raw NOTES text, kept so unresolved
                                   -- references can be reported rather than guessed
    page_no       INTEGER,         -- the sheet of a multi-page isometric
    source        TEXT             -- 'weld_log_csv' | 'daily_weld_report' | ...
);
CREATE INDEX IF NOT EXISTS ix_weld_seg ON weld(project_id, segment);
CREATE INDEX IF NOT EXISTS ix_weld_nde ON weld(project_id, nde_id);

-- An NDE shot as evidenced by a filed reader sheet - the "does exist" side.
CREATE TABLE IF NOT EXISTS nde_shot (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    document_id INTEGER REFERENCES document(id),
    fingerprint TEXT,              -- of the sheet, so filing copies collapse
    copies      INTEGER DEFAULT 1, -- how many places on disk this sheet is filed
    segments    TEXT,              -- every segment folder the sheet is filed under
    segment     TEXT,
    nde_id      TEXT NOT NULL,     -- normalised, e.g. GFB-037
    prefix      TEXT,
    number      INTEGER,
    suffix      TEXT,
    sheet_date  TEXT,
    technique   TEXT,
    result      TEXT,              -- ACC / REJ / NULL when not yet read
    welder      TEXT,
    pipe_size   TEXT,
    wall_thk    TEXT,
    technician  TEXT,
    page_no     INTEGER,
    evidence    TEXT,              -- 'filename' | 'text' | 'vision'
    confidence  REAL
);
CREATE INDEX IF NOT EXISTS ix_shot_id  ON nde_shot(project_id, nde_id);
CREATE INDEX IF NOT EXISTS ix_shot_seg ON nde_shot(project_id, segment);

-- What a reader sheet says about itself, as opposed to about a weld.
-- One row per report, and a bundle holds several: `FAB COMBINED.pdf` is
-- fifteen pages carrying at least two, which state 18 welds and 42.
CREATE TABLE IF NOT EXISTS reader_sheet (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    document_id INTEGER REFERENCES document(id),
    fingerprint TEXT,              -- of the sheet, so filing copies collapse
    filename    TEXT,
    segment     TEXT,
    page_no     INTEGER,           -- 1-based page within the PDF
    weld_count  INTEGER,           -- the sheet's own count of welds examined
    ticket      TEXT,              -- Ticket No / REF#, the join between copies
    sheet_date  TEXT,              -- the report's own Date field, ISO
    stated_page INTEGER,           -- the 'Page N of M' the sheet prints...
    stated_pages INTEGER,          -- ...and its M
    evidence    TEXT               -- 'text' | 'vision'
);
CREATE INDEX IF NOT EXISTS ix_rsheet ON reader_sheet(project_id, fingerprint);

-- Where two close-ups of the same box on a page read differently.
--
-- Tiling reads a page again as four overlapping quarters (see vision.py). Most
-- of the time they agree, or a majority settles it. When they cannot be
-- reconciled the merge writes null rather than picking one, and that null is
-- indistinguishable from "the box was empty" by the time a rule sees it. This
-- table is what keeps the difference: it says a value was there and could not
-- be read the same way twice, which is a page for a human rather than a
-- silent gap.
CREATE TABLE IF NOT EXISTS vision_conflict (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    document_id INTEGER REFERENCES document(id),
    fingerprint TEXT,
    filename    TEXT,
    segment     TEXT,
    kind        TEXT,               -- the vision kind, e.g. 'mtr'
    page_no     INTEGER,            -- 1-based page within the PDF
    field       TEXT,               -- the schema field that disagreed
    readings    TEXT,               -- JSON list of what the close-ups said
    chosen      TEXT,               -- what the merge settled on, NULL if nothing
    decisive    INTEGER             -- 1 if this field drives a cross-check
);
CREATE INDEX IF NOT EXISTS ix_vconflict ON vision_conflict(project_id, decisive);

-- What a person read off the page when no reader could.
--
-- Some values are not recoverable by machine at any resolution. Tex-Tubo's
-- letterhead is a logotype — overlapping letters, a circle fused to the last
-- E — and a vision model reads it as TECKCUBO, TEKSUMEO or Tekube depending
-- on the day, while OCR returns nothing. Seven certificates became seven
-- critical "not on the approved list" findings against approved material.
--
-- VIS-02 tells the auditor to read the letterhead and enter the company. This
-- is where it goes. Keyed on the document fingerprint rather than its id, so
-- one correction covers every filing copy of the same certificate, and NOT
-- cleared by re-indexing: it is the only thing in this database that a person
-- typed, and losing it to a re-run would be unforgivable.
CREATE TABLE IF NOT EXISTS correction (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,     -- of the document the value was read from
    field       TEXT NOT NULL,     -- 'manufacturer' today; the column it sets
    value       TEXT,              -- what the person read. Empty means "blank"
    note        TEXT,
    made_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(project_id, fingerprint, field)
);
CREATE INDEX IF NOT EXISTS ix_correction ON correction(project_id, field);

-- A letterhead this job's readers cannot spell, and the mill it belongs to.
--
-- Distinct from the alias file, which maps one real company name to another
-- (ORTEGA Forja -> Ortega Advanced Forged Solutions) and says so in its header:
-- never map a misread onto a real name there, because that hides a bad scan
-- behind an approval everywhere, on every job, for ever.
--
-- This is narrower and safer. It is scoped to one project, each row is a
-- string somebody looked at a page to confirm, and it only ever overwrites a
-- value no human has set. Tex-Tubo prints its name as a logotype and this
-- corpus spells it nine ways -- TEXTUBOO, TEKTUBE, TEXQUBEO, tex-tubo.com --
-- across twenty-four certificates. Correcting those one document at a time
-- is twenty-four acts of judgement about one letterhead.
CREATE TABLE IF NOT EXISTS vendor_reading (
    id           INTEGER PRIMARY KEY,
    project_id   INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    as_read_key  TEXT NOT NULL,    -- normalised, so punctuation is not a new row
    as_read      TEXT NOT NULL,    -- what was actually on the material row
    manufacturer TEXT NOT NULL,    -- the mill it is
    note         TEXT,
    made_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(project_id, as_read_key)
);
CREATE INDEX IF NOT EXISTS ix_vendorread ON vendor_reading(project_id);

-- NDE technicians from the rig log, for qualification checks.
CREATE TABLE IF NOT EXISTS nde_tech (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    segment     TEXT,
    company     TEXT,
    name        TEXT,
    rig_letter  TEXT,
    certs       TEXT,
    acuity      TEXT,
    cert_date   TEXT,
    arrived     TEXT
);

-- A welder certification document on file, identified by stencil.
CREATE TABLE IF NOT EXISTS welder_cert (
    id           INTEGER PRIMARY KEY,
    project_id   INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    document_id  INTEGER REFERENCES document(id),
    segment      TEXT,
    stencil      TEXT,
    name         TEXT,
    process      TEXT,
    material     TEXT,
    cert_date    TEXT,
    expiry       TEXT,
    requal       INTEGER DEFAULT 0,
    wps          TEXT,             -- a ticket is scoped to one procedure
    code         TEXT,             -- API 1104 / ASME Sec IX
    result       TEXT,             -- PASS / FAIL as marked on the record
    -- As tested.
    test_position TEXT,
    progression  TEXT,
    test_od      TEXT,
    test_wall    TEXT,
    -- The Qualification Ranges block: what the certifying CWI wrote that this
    -- test qualifies. Authoritative, so it is read rather than derived.
    qual_process   TEXT,
    qual_position  TEXT,
    qual_diameter  TEXT,
    qual_thickness TEXT,
    f_number       TEXT,
    -- Who witnessed it, and whether their own ticket was current that day.
    qualifier_name   TEXT,
    qualifier_cwi    TEXT,
    qualifier_expiry TEXT,
    evidence     TEXT DEFAULT 'filename'
);
CREATE INDEX IF NOT EXISTS ix_wcert ON welder_cert(project_id, stencil);

-- One row per welder per weld: who actually laid metal, and when.
CREATE TABLE IF NOT EXISTS welder_pass (
    id           INTEGER PRIMARY KEY,
    project_id   INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    weld_id      INTEGER REFERENCES weld(id),
    document_id  INTEGER REFERENCES document(id),
    segment      TEXT,
    line         TEXT,
    weld_no      TEXT,
    stencil      TEXT,
    date_welded  TEXT
);
CREATE INDEX IF NOT EXISTS ix_wpass ON welder_pass(project_id, stencil);

-- A heat shown as installed on a line, from a heat-map isometric.
-- Deliberately separate from weld.heat_us / heat_ds: a heat map says "this
-- heat is in this line", not "this heat is on this end of that weld". Pairing
-- a boxed callout to a joint needs spatial reasoning about leader lines, and a
-- wrong pairing would attach material to the wrong weld invisibly.
CREATE TABLE IF NOT EXISTS installed_heat (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    document_id INTEGER REFERENCES document(id),
    segment     TEXT,
    line        TEXT,
    drawing_no  TEXT,
    heat        TEXT,
    heat_key    TEXT,
    note        TEXT,
    source      TEXT
);
CREATE INDEX IF NOT EXISTS ix_inst_heat ON installed_heat(project_id, heat_key);

-- One hydrostatic pressure test.  Requirements and result live on the same
-- row: a package states the required pressure on its own sheet and again on
-- the record, and the auditor's question is always the two together.
CREATE TABLE IF NOT EXISTS hydrotest (
    id             INTEGER PRIMARY KEY,
    project_id     INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    document_id    INTEGER REFERENCES document(id),
    fingerprint    TEXT,
    segment        TEXT,
    service        TEXT,             -- as written, e.g. 'LP SEG.D'
    line_no        TEXT,
    code           TEXT,
    req_min_press  REAL,
    req_max_press  REAL,
    req_hours      REAL,
    started_at     TEXT,             -- ISO date when parseable, else as written
    completed_at   TEXT,
    started_raw    TEXT,
    completed_raw  TEXT,
    stated_hours   REAL,
    medium         TEXT,
    result         TEXT,             -- ACCEPTABLE / UNACCEPTABLE / NULL if unmarked
    deadweight_sn  TEXT,
    press_rec_sn   TEXT,
    temp_rec_sn    TEXT,
    contractor_rep TEXT,
    inspector      TEXT,
    page_no        INTEGER,
    source         TEXT
);
CREATE INDEX IF NOT EXISTS ix_hydro ON hydrotest(project_id, segment);

-- The timed gauge readings that evidence the hold.
CREATE TABLE IF NOT EXISTS hydrotest_reading (
    id          INTEGER PRIMARY KEY,
    hydrotest_id INTEGER NOT NULL REFERENCES hydrotest(id) ON DELETE CASCADE,
    seq         INTEGER,
    reading_time TEXT,
    pressure    REAL,
    ambient     TEXT
);
CREATE INDEX IF NOT EXISTS ix_hydro_reading ON hydrotest_reading(hydrotest_id);

-- Calibration certificates for the gauges and recorders, keyed by serial so a
-- record can be asked whether the instrument it names was in date on the day.
CREATE TABLE IF NOT EXISTS instrument_cal (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    document_id INTEGER REFERENCES document(id),
    kind        TEXT,             -- holiday_detector | dft_gauge | dpm | profile_gauge
    serial      TEXT,
    serial_key  TEXT,             -- upper-cased, punctuation stripped
    calibrated  TEXT,
    description TEXT,             -- the filename's own wording, for messages
    page_no     INTEGER,
    evidence    TEXT,             -- 'filename' | 'vision'
    source      TEXT
);
CREATE INDEX IF NOT EXISTS ix_inst_cal ON instrument_cal(project_id, serial_key);

-- One pipe joint as recorded on the as-built: where it sits on the line,
-- what heat it is, and the NDE report on the weld at its end. The only
-- document in the corpus that puts a weld against a survey station.
CREATE TABLE IF NOT EXISTS asbuilt_joint (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    document_id INTEGER REFERENCES document(id),
    fingerprint TEXT,
    segment     TEXT,
    sheet       TEXT,
    band        INTEGER,
    seq         INTEGER,
    station     TEXT,               -- as written, e.g. '130+00'
    station_ft  REAL,               -- feet along the line, for range checks
    length      REAL,
    heat        TEXT,
    heat_key    TEXT,
    joint_no    TEXT,
    size        TEXT,
    description TEXT,
    xray        TEXT,               -- as written
    nde_id      TEXT,               -- normalised, for joining
    pipe_size   TEXT,
    grade       TEXT,
    wall        TEXT,
    service     TEXT,
    source      TEXT
);
CREATE INDEX IF NOT EXISTS ix_asbuilt ON asbuilt_joint(project_id, segment);
CREATE INDEX IF NOT EXISTS ix_asbuilt_nde ON asbuilt_joint(project_id, nde_id);

-- One RELEASE FOR BACKFILL: the hold point before the ditch closes over a
-- measured length of line. The form asserts that all weld and heat map data
-- is captured and all NDE cleared, which is checkable against both.
CREATE TABLE IF NOT EXISTS backfill_release (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    document_id INTEGER REFERENCES document(id),
    fingerprint TEXT,
    segment     TEXT,
    page_no     INTEGER,
    line_size   TEXT,
    wall        TEXT,
    material    TEXT,
    yield_grade TEXT,
    service     TEXT,
    from_station TEXT,
    to_station  TEXT,
    inspector_signed INTEGER,
    inspector_date   TEXT,
    contractor_signed INTEGER,
    contractor_date  TEXT,
    survey_signed    INTEGER,
    survey_date      TEXT,
    released_on TEXT,              -- the earliest signature date: when the
                                   -- ditch could first have been closed
    source      TEXT
);
CREATE INDEX IF NOT EXISTS ix_backfill ON backfill_release(project_id, segment);

-- The contractor's roster of welders on the job: who, which stencil, when
-- they qualified, when they must requalify, and when they were on site.
CREATE TABLE IF NOT EXISTS welder_roster (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    document_id INTEGER REFERENCES document(id),
    fingerprint TEXT,
    segment     TEXT,
    row_no      INTEGER,
    name        TEXT,
    stencil     TEXT,
    material    TEXT,             -- 'CS' or 'SS', the scope of the ticket
    cert_date   TEXT,
    requal_date TEXT,
    next_requal TEXT,             -- the roster's own stated expiry
    arrived     TEXT,
    left_job    TEXT,
    reason      TEXT,
    source      TEXT
);
CREATE INDEX IF NOT EXISTS ix_roster ON welder_roster(project_id, stencil);

-- A welding procedure the job's own standard approves, with the essential
-- variables worth checking a weld against.
CREATE TABLE IF NOT EXISTS procedure (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    document_id INTEGER REFERENCES document(id),
    wps         TEXT,              -- as written, e.g. 'XTO-X60-6010/8010'
    wps_key     TEXT,              -- punctuation-free, revision removed
    revision    TEXT,
    pqr         TEXT,              -- the supporting qualification record
    code        TEXT,              -- 'API 1104'
    process     TEXT,
    min_diameter REAL,
    min_wall    REAL,
    two_welder_over REAL,          -- OD at or above which two welders are required
    page_no     INTEGER,
    source      TEXT
);
CREATE INDEX IF NOT EXISTS ix_procedure ON procedure(project_id, wps_key);

-- One bolted flange joint, as recorded on a flange (torque) log.
CREATE TABLE IF NOT EXISTS flange (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    document_id INTEGER REFERENCES document(id),
    fingerprint TEXT,
    segment     TEXT,
    sheet       TEXT,              -- the workbook tab; a log can hold two
    row_no      INTEGER,
    flange_no   TEXT,
    nps         REAL,
    pressure_class REAL,
    gasket      TEXT,
    bolts       REAL,
    lubricant   TEXT,
    round1      REAL,
    round2      REAL,
    round3      REAL,              -- the 100% round: the final torque
    round4      REAL,
    pattern     TEXT,
    wrench      TEXT,
    wrench_key  TEXT,              -- upper-cased, punctuation stripped
    cert_checked TEXT,             -- the log's own "verify calibration" box
    inspector   TEXT,
    bolted_on   TEXT,
    drawing_no  TEXT,
    notes       TEXT,
    line_size   TEXT,
    service     TEXT,
    job_start   TEXT,
    source      TEXT
);
CREATE INDEX IF NOT EXISTS ix_flange ON flange(project_id, segment);

-- How many flanges a flange map drawing balloons, so the log can be asked
-- whether it torqued all of them.
CREATE TABLE IF NOT EXISTS flange_map (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    document_id INTEGER REFERENCES document(id),
    segment     TEXT,
    drawings    TEXT,              -- drawing numbers named on the sheet
    balloons    INTEGER,           -- highest contiguous balloon number from 1
    source      TEXT
);
CREATE INDEX IF NOT EXISTS ix_flange_map ON flange_map(project_id, segment);

-- One day's blasting and coating on a segment.
CREATE TABLE IF NOT EXISTS coating_report (
    id            INTEGER PRIMARY KEY,
    project_id    INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    document_id   INTEGER REFERENCES document(id),
    fingerprint   TEXT,
    segment       TEXT,
    page_no       INTEGER,
    report_date   TEXT,
    line_size     TEXT,
    line_nps      REAL,             -- parsed, for the flocking size rule
    material      TEXT,
    service       TEXT,
    contractor    TEXT,
    inspector     TEXT,
    start_station TEXT,
    end_station   TEXT,
    blast_media   TEXT,
    cleanliness   TEXT,
    profile_reqd  REAL,
    welds_coated  REAL,
    jeep_from     TEXT,
    jeep_to       TEXT,
    comments      TEXT,
    source        TEXT
);
CREATE INDEX IF NOT EXISTS ix_coating ON coating_report(project_id, segment);

-- The ambient readings that decide whether coating was allowed to start.
CREATE TABLE IF NOT EXISTS coating_environment (
    id          INTEGER PRIMARY KEY,
    report_id   INTEGER NOT NULL REFERENCES coating_report(id) ON DELETE CASCADE,
    seq         INTEGER,
    reading_time TEXT,
    air_temp    REAL,
    humidity    REAL,
    steel_temp  REAL,
    dew_point   REAL
);
CREATE INDEX IF NOT EXISTS ix_coating_env ON coating_environment(report_id);

-- Surface profile readings off the Testex tapes.
CREATE TABLE IF NOT EXISTS coating_profile (
    id          INTEGER PRIMARY KEY,
    report_id   INTEGER NOT NULL REFERENCES coating_report(id) ON DELETE CASCADE,
    seq         INTEGER,
    mils        REAL
);
CREATE INDEX IF NOT EXISTS ix_coating_profile ON coating_profile(report_id);

-- One row of the coating table: what was applied, and how thick.
CREATE TABLE IF NOT EXISTS coating_coat (
    id           INTEGER PRIMARY KEY,
    report_id    INTEGER NOT NULL REFERENCES coating_report(id) ON DELETE CASCADE,
    seq          INTEGER,
    nde_weld_no  TEXT,             -- as written, e.g. 'GXR 048'
    nde_id       TEXT,             -- normalised, e.g. 'GXR-048', for joining
    manufacturer TEXT,
    product      TEXT,
    color        TEXT,
    batch_a      TEXT,
    batch_b      TEXT,
    method       TEXT,
    wft          REAL,
    dft          REAL,
    layer        TEXT              -- primer | base | intermediate | top
);
CREATE INDEX IF NOT EXISTS ix_coating_coat ON coating_coat(report_id);

-- Which instruments a report says produced its numbers.
CREATE TABLE IF NOT EXISTS coating_instrument (
    id          INTEGER PRIMARY KEY,
    report_id   INTEGER NOT NULL REFERENCES coating_report(id) ON DELETE CASCADE,
    kind        TEXT,
    serial      TEXT,
    serial_key  TEXT
);
CREATE INDEX IF NOT EXISTS ix_coating_inst ON coating_instrument(report_id);

-- The approved materials list, as loaded for this project.
CREATE TABLE IF NOT EXISTS aml_source (
    project_id  INTEGER PRIMARY KEY REFERENCES project(id) ON DELETE CASCADE,
    path        TEXT,              -- the file the list was read from
    kind        TEXT,              -- 'pdf' | 'workbook'
    revision    TEXT,              -- as printed, e.g. 'Sept 30, 2026'
    valid_thru  TEXT,              -- ISO date when it could be parsed
    entries     INTEGER
);

CREATE TABLE IF NOT EXISTS aml_entry (
    id           INTEGER PRIMARY KEY,
    project_id   INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    category     TEXT,             -- the AML sheet, e.g. '1.0 Pipe'
    manufacturer TEXT,
    location     TEXT,
    limits_raw   TEXT,
    min_nps      REAL,
    max_nps      REAL,
    conditions   TEXT,             -- limit text that is not a size rule
    norm_name    TEXT
);
CREATE INDEX IF NOT EXISTS ix_aml_name ON aml_entry(project_id, norm_name);

-- A material as evidenced by a certificate on file, or by a pipe/heat export.
CREATE TABLE IF NOT EXISTS material (
    id           INTEGER PRIMARY KEY,
    project_id   INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    document_id  INTEGER REFERENCES document(id),
    segment      TEXT,
    heat         TEXT,             -- as written on the source
    heat_key     TEXT,             -- punctuation-free, for joining
    manufacturer TEXT,             -- best available: mill if known, else issuer
    -- A certificate's letterhead is often a distributor or a machine shop
    -- rather than the works that made the material. Both are kept so a finding
    -- can say which one it checked.
    issuing_company TEXT,
    mill_name    TEXT,
    grade        TEXT,
    nps          REAL,
    wall         TEXT,
    spec         TEXT,
    schedule     TEXT,
    description  TEXT,
    categories   TEXT,             -- candidate AML sheets, semicolon separated
    line         TEXT,
    source       TEXT,             -- 'mtr_file' | 'pipes_csv'
    confidence   TEXT,              -- 'text' | 'vision' | absent for exports
    evidence     TEXT               -- how a parsed manufacturer was decided
);
CREATE INDEX IF NOT EXISTS ix_mat_heat ON material(project_id, heat_key);
CREATE INDEX IF NOT EXISTS ix_mat_src  ON material(project_id, source);

-- Cached OCR / vision output, keyed by file hash so it never runs twice.
CREATE TABLE IF NOT EXISTS ocr_cache (
    sha1        TEXT NOT NULL,
    page_no     INTEGER NOT NULL,
    model       TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (sha1, page_no, model)
);

-- Audit exceptions.
CREATE TABLE IF NOT EXISTS finding (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    run_id      TEXT,
    rule        TEXT NOT NULL,
    severity    TEXT NOT NULL,     -- critical | major | minor | info
    segment     TEXT,
    subject     TEXT,              -- the weld / shot / heat the finding is about
    message     TEXT NOT NULL,
    detail      TEXT,              -- JSON blob of supporting values
    document_id INTEGER REFERENCES document(id),
    page_no     INTEGER,
    status      TEXT DEFAULT 'open',   -- open | accepted | dismissed
    note        TEXT,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_find_run ON finding(project_id, run_id);
CREATE INDEX IF NOT EXISTS ix_find_sev ON finding(project_id, severity);

CREATE TABLE IF NOT EXISTS run (
    id          TEXT PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    started_at  TEXT,
    finished_at TEXT,
    summary     TEXT
);
"""


#: A column declaration inside a CREATE TABLE block: four spaces, a name, a
#: type.  Constraint lines (PRIMARY KEY, FOREIGN KEY, UNIQUE) start with a
#: keyword and are excluded by the negative lookahead.
_COLUMN_LINE = re.compile(
    r"^\s{4}(?!PRIMARY|FOREIGN|UNIQUE|CHECK|CONSTRAINT)"
    r"([a-z_][a-z0-9_]*)\s+(.+)$",
    re.IGNORECASE | re.MULTILINE,
)
_TABLE_BLOCK = re.compile(
    r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\n\);", re.DOTALL | re.IGNORECASE
)
_TRAILING_COMMENT = re.compile(r"\s*--.*$", re.MULTILINE)


def declared_columns() -> dict[str, dict[str, str]]:
    """``{table: {column: definition}}`` as the schema above declares them."""
    out: dict[str, dict[str, str]] = {}
    for table, body in _TABLE_BLOCK.findall(SCHEMA):
        # The schema documents most columns with a trailing `-- comment`,
        # which would otherwise travel into the ALTER TABLE statement.
        body = _TRAILING_COMMENT.sub("", body)
        out[table] = {m.group(1): m.group(2).strip().rstrip(",").strip()
                      for m in _COLUMN_LINE.finditer(body)}
    return out


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._add_new_columns()
        self.conn.commit()

    def _add_new_columns(self) -> list[str]:
        """Bring an older database up to the current schema.

        ``CREATE TABLE IF NOT EXISTS`` silently leaves an existing table at
        whatever shape it already had, so a release that adds a column to a
        table someone already has would fail on the first insert.  This tool
        is installed by auditors who each keep their own database and update
        on their own schedule, so the schema has to migrate itself rather than
        assume a matching version.

        Only additive changes are handled, which is all the schema has ever
        needed; a column that is NOT NULL without a default cannot be added to
        a populated table and is left for a real migration to deal with.
        """
        added: list[str] = []
        for table, columns in declared_columns().items():
            have = {r["name"] for r in
                    self.conn.execute(f"PRAGMA table_info({table})")}
            if not have:
                continue                    # the CREATE above just made it
            for name, definition in columns.items():
                if name in have:
                    continue
                if re.search(r"NOT NULL(?!.*DEFAULT)", definition, re.IGNORECASE):
                    continue
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                added.append(f"{table}.{name}")
        return added

    # -- basics -------------------------------------------------------------

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def q(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def close(self) -> None:
        self.conn.close()

    # -- projects -----------------------------------------------------------

    @staticmethod
    def path_key(root: str | Path) -> str:
        """One folder's identity, however the path was spelled.

        The same folder arrives here in several spellings: typed into the box
        with a trailing slash, returned by the Windows picker, read back out
        of the database, or built with forward slashes by a script. Comparing
        the strings would call those four different folders.
        """
        text = str(Path(root).resolve())
        # normcase lowercases and squares up the slashes on Windows, and does
        # nothing at all on a case-sensitive filesystem, which is correct in
        # both places.
        return os.path.normcase(text).rstrip("/" + os.sep)

    def project_at(self, root: str | Path) -> sqlite3.Row | None:
        """The audit already stored for this folder, if there is one."""
        key = self.path_key(root)
        for p in self.q("SELECT * FROM project"):
            if self.path_key(p["root"]) == key:
                return p
        return None

    def free_name(self, name: str, root: str | Path) -> str:
        """``name``, or the nearest thing to it that is not already taken.

        Turnover folders are not named distinctively — several jobs have a
        ``BOOK`` in them, and the picker offers the folder's own name — so two
        different folders wanting the same name is ordinary, not a mistake to
        refuse. The parent folder is what tells them apart on screen, so it is
        what gets added.
        """
        taken = {r["name"] for r in self.q("SELECT name FROM project")}
        if name not in taken:
            return name
        parent = Path(root).resolve().parent.name
        if parent and f"{name} ({parent})" not in taken:
            return f"{name} ({parent})"
        n = 2
        while f"{name} ({n})" in taken:
            n += 1
        return f"{name} ({n})"

    def upsert_project(self, name: str, root: str | Path) -> int:
        """The audit for a folder, started if this is the first time.

        A folder is what identifies an audit. Keying on the name instead gave
        two wrong answers, both of which happened in a real database:

        - Auditing one folder under a second name made a second audit of it.
          The same 893 documents were indexed twice and sat side by side in
          the dropdown looking like two different jobs, with no way to tell
          from the list that they were not.
        - Two different folders sharing a name — ``BOOK``, ``3 As-Built`` —
          destroyed one another. The ``ON CONFLICT(name)`` clause repointed
          the first audit's row at the second folder and returned its id, and
          the caller's very next act is to clear that project's rows and index
          the new folder into it. So auditing a second ``BOOK`` silently threw
          the first one away: same name, same id, entirely different job, and
          nothing anywhere said it had happened.
        """
        resolved = str(Path(root).resolve())
        existing = self.project_at(resolved)
        if existing is not None:
            # The stored name stays. Re-auditing a folder is not a request to
            # rename the job, and the name arriving here is usually just the
            # folder's own — whatever the picker filled in — which would undo
            # a name somebody chose deliberately.
            with self.tx() as c:
                c.execute("UPDATE project SET root=? WHERE id=?",
                          (resolved, existing["id"]))
            return int(existing["id"])

        with self.tx() as c:
            cur = c.execute("INSERT INTO project(name, root) VALUES(?, ?)",
                            (self.free_name(name, resolved), resolved))
            return int(cur.lastrowid)

    def projects(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM project ORDER BY name")

    #: Long enough for "Kestrel 8 Lateral to Terminal", short enough that a
    #: pasted paragraph does not become a row in the dropdown.
    NAME_LIMIT = 120

    def rename_project(self, project_id: int, name: str) -> str:
        """Give an audit a different name. Returns the name as stored.

        Names became permanent when the folder took over as an audit's
        identity: re-auditing no longer renames anything, which is what stops
        the picker's folder-derived name overwriting one somebody chose. This
        is the way to change it on purpose.

        A clash is refused rather than worked around. ``free_name`` invents a
        suffix because the caller there did not choose the name and cannot be
        asked; here somebody typed it, and silently storing something else is
        the worse answer.
        """
        wanted = " ".join(name.split())          # collapse stray whitespace
        if not wanted:
            raise ValueError("A name is needed.")
        if len(wanted) > self.NAME_LIMIT:
            raise ValueError(f"Names are limited to {self.NAME_LIMIT} characters.")
        if self.one("SELECT id FROM project WHERE id=?", (project_id,)) is None:
            raise LookupError("no such audit")
        # Case-insensitively, because two audits called "Kestrel 8" and "kestrel 8"
        # are distinct to SQLite and identical to whoever reads the dropdown.
        clash = self.one(
            "SELECT name FROM project WHERE id<>? AND LOWER(name)=LOWER(?)",
            (project_id, wanted),
        )
        if clash is not None:
            raise ValueError(f"'{clash['name']}' already uses that name.")
        with self.tx() as c:
            c.execute("UPDATE project SET name=? WHERE id=?", (wanted, project_id))
        return wanted

    # -- removing a stored audit ---------------------------------------------
    #
    # The only operation here with no way back, so it is written out plainly
    # rather than left to the cascade rules to perform silently.
    #
    # What survives is as deliberate as what goes. ``ocr_cache`` is keyed by
    # file hash and holds readings that were paid for a page at a time; it has
    # no project_id, is therefore never found below, and re-auditing the same
    # folder afterwards costs nothing. Everything else an audit stored can be
    # rebuilt by reading the documents again — except the corrections and
    # vendor readings somebody typed, which cannot, and which the caller is
    # expected to count and warn about before asking for this.

    #: Tables that hang off a parent row instead of the project directly, and
    #: so cannot be found by looking for a project_id. Deepest first.
    _OWNED_VIA_PARENT = (
        ("hydrotest_reading", "hydrotest_id", "hydrotest"),
        ("coating_environment", "report_id", "coating_report"),
        ("coating_profile", "report_id", "coating_report"),
        ("coating_coat", "report_id", "coating_report"),
        ("coating_instrument", "report_id", "coating_report"),
    )

    def project_tables(self) -> list[str]:
        """Every table holding rows per project — discovered, not listed.

        A hand-written list of tables is wrong the day somebody adds one, and
        the symptom is invisible: rows belonging to a job that no longer
        exists, counted by some later query as though they did. Asking the
        schema cannot go stale.
        """
        out = []
        for row in self.q("SELECT name FROM sqlite_master WHERE type='table'"):
            table = row["name"]
            if table == "project":
                continue
            cols = {c["name"] for c in self.q(f"PRAGMA table_info({table})")}
            if "project_id" in cols:
                out.append(table)
        return sorted(out)

    def stored_for(self, project_id: int) -> dict[str, int]:
        """Row counts per table for one audit, for saying what will be lost."""
        counts: dict[str, int] = {}
        for table in self.project_tables():
            n = self.one(f"SELECT COUNT(*) c FROM {table} WHERE project_id=?",
                         (project_id,))["c"]
            if n:
                counts[table] = n
        return counts

    def delete_order(self, tables: list[str]) -> list[str]:
        """The tables in an order a delete can actually run in.

        Children before the rows they point at, worked out from the foreign
        keys. Alphabetical order is not merely untidy, it fails outright:
        `document` sorts ahead of `finding` and `material`, both of which
        reference it, so the first delete hits a foreign key error and the
        audit is left half-removed.
        """
        points_at = {
            t: {r["table"] for r in self.q(f"PRAGMA foreign_key_list({t})")
                if r["table"] in tables and r["table"] != t}
            for t in tables
        }
        # A table may go only once everything pointing at it has gone.
        waiting = {t: {a for a in tables if t in points_at[a]} for t in tables}
        order: list[str] = []
        done: set[str] = set()
        while len(order) < len(tables):
            ready = sorted(t for t in tables
                           if t not in done and not (waiting[t] - done))
            if not ready:
                # A reference cycle. Nothing in this schema has one; if one
                # ever appears, an arbitrary order beats not deleting at all.
                ready = sorted(set(tables) - done)
            order.extend(ready)
            done.update(ready)
        return order

    def delete_project(self, project_id: int) -> dict[str, int]:
        """Forget an audit entirely. Returns what was removed, by table.

        Nothing on disk is touched: this deletes what the program recorded
        about a folder, not the folder.
        """
        removed = self.stored_for(project_id)
        with self.tx() as c:
            for child, key, parent in self._OWNED_VIA_PARENT:
                c.execute(f"DELETE FROM {child} WHERE {key} IN "
                          f"(SELECT id FROM {parent} WHERE project_id=?)",
                          (project_id,))
            for table in self.delete_order(self.project_tables()):
                c.execute(f"DELETE FROM {table} WHERE project_id=?", (project_id,))
            c.execute("DELETE FROM project WHERE id=?", (project_id,))
        return removed

    def cached_pages(self) -> int:
        """Pages already read and kept, which no deletion here touches."""
        return self.one("SELECT COUNT(*) c FROM ocr_cache")["c"]

    # -- moving the readings between machines --------------------------------
    #
    # The cache is keyed by the hash of the page, not by the job, so a reading
    # is true of that document wherever it sits. It is also the only expensive
    # thing this program owns: 847 of the pages on one machine were read by a
    # paid model, a page at a time.
    #
    # That matters because the cache lives in the user's own profile, not in
    # the program. A colleague given the exe and pointed at the same folder
    # starts with none of it — and an audit does not run OCR, so their
    # scanned certificates are simply never read. Their report then comes back
    # *shorter*, missing every manufacturer check, which reads like a cleaner
    # package rather than a blinder audit. Handing over the cache is the
    # difference between those two.

    def export_cache(self, path: str | Path) -> int:
        """Write the page readings to a file another machine can load."""
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            out.unlink()
        other = sqlite3.connect(out)
        try:
            other.execute(
                """CREATE TABLE ocr_cache (
                       sha1 TEXT NOT NULL, page_no INTEGER NOT NULL,
                       model TEXT NOT NULL, payload TEXT NOT NULL,
                       created_at TEXT,
                       PRIMARY KEY (sha1, page_no, model))""")
            rows = [tuple(r) for r in self.q(
                "SELECT sha1, page_no, model, payload, created_at FROM ocr_cache")]
            other.executemany("INSERT INTO ocr_cache VALUES (?,?,?,?,?)", rows)
            other.commit()
        finally:
            other.close()
        return len(rows)

    def import_cache(self, path: str | Path) -> dict[str, int]:
        """Merge a cache file in. Nothing already here is overwritten.

        ``INSERT OR IGNORE`` on the key, so a reading this machine already has
        wins. That is the safe direction: the key carries the model, so a paid
        reading and a free one of the same page coexist, and ``ocr_any``
        already prefers the paid one.
        """
        source = Path(path)
        if not source.is_file():
            raise FileNotFoundError(str(source))
        other = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            other.row_factory = sqlite3.Row
            rows = [(r["sha1"], r["page_no"], r["model"], r["payload"],
                     r["created_at"])
                    for r in other.execute(
                        "SELECT sha1, page_no, model, payload, created_at "
                        "FROM ocr_cache")]
        except sqlite3.DatabaseError as bad:
            raise ValueError(f"{source.name} is not a WeldAudit cache: {bad}") from None
        finally:
            other.close()

        before = self.cached_pages()
        with self.tx() as c:
            c.executemany(
                "INSERT OR IGNORE INTO ocr_cache"
                "(sha1, page_no, model, payload, created_at) VALUES (?,?,?,?,?)",
                rows)
        added = self.cached_pages() - before
        return {"in_the_file": len(rows), "added": added,
                "already_here": len(rows) - added}

    # -- findings -----------------------------------------------------------

    def clear_findings(self, project_id: int) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM finding WHERE project_id=?", (project_id,))

    def add_findings(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        with self.tx() as c:
            c.executemany(
                """INSERT INTO finding
                   (project_id, run_id, rule, severity, segment, subject,
                    message, detail, document_id, page_no)
                   VALUES (:project_id, :run_id, :rule, :severity, :segment,
                           :subject, :message, :detail, :document_id, :page_no)""",
                [
                    {
                        "document_id": None,
                        "page_no": None,
                        "segment": None,
                        "subject": None,
                        **r,
                        "detail": json.dumps(r.get("detail")) if isinstance(r.get("detail"), (dict, list)) else r.get("detail"),
                    }
                    for r in rows
                ],
            )

    # -- ocr cache ----------------------------------------------------------

    def ocr_get(self, sha1: str, page_no: int, model: str) -> dict | None:
        row = self.one(
            "SELECT payload FROM ocr_cache WHERE sha1=? AND page_no=? AND model=?",
            (sha1, page_no, model),
        )
        return json.loads(row["payload"]) if row else None

    def ocr_any(self, fingerprint: str, kind: str, page_no: int) -> dict | None:
        """A cached reading of this page, best available first.

        Replaying cached results after a re-index must not depend on the model
        or image size the original pass happened to use - otherwise a run with
        ``--model claude-haiku-4-5`` would be silently discarded by the next
        audit, and the pages would have to be paid for again.

        A hosted reading wins over a local one regardless of age. Ordering by
        recency alone meant a free pass run after a paid one downgraded the
        audit without saying so: on Bluewater 14 a 7B model's reading of a
        mill certificate replaced Haiku's and put the preparer's signature
        forward as the issuing company. Newest-wins is right within a tier,
        because that is a re-read of the same page; across tiers it silently
        throws away the better evidence someone paid for.

        The ``local:`` prefix is ``vision.LOCAL_PREFIX``, spelled out here
        because importing it would close a cycle. A test ties the two together.
        """
        row = self.one(
            "SELECT payload FROM ocr_cache WHERE sha1 LIKE ? AND page_no=? "
            # rowid breaks the tie: created_at has one-second resolution, and
            # two readings of one page a few hundred milliseconds apart would
            # otherwise come back in whichever order SQLite felt like.
            "ORDER BY (model LIKE 'local:%') ASC, created_at DESC, rowid DESC "
            "LIMIT 1",
            (f"{fingerprint}:{kind}:%", page_no),
        )
        return json.loads(row["payload"]) if row else None

    def ocr_put(self, sha1: str, page_no: int, model: str, payload: dict) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO ocr_cache(sha1, page_no, model, payload) VALUES(?,?,?,?)",
                (sha1, page_no, model, json.dumps(payload)),
            )
