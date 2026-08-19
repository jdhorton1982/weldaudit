"""AML matching, size-limit enforcement and MTR filename parsing.

The manufacturer names and heats in the fixtures are invented, and the shapes
they are in are not: every filename convention, limit string and awkward
letterhead here was taken from a real one and then renamed. Where a test needs
a genuine approved list it finds one on the machine and takes the names out of
it, so it holds for whichever list a site uses and carries none of it here.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit import mtrname  # noqa: E402
from weldaudit.aml import (  # noqa: E402
    Aml, AmlEntry, SizeLimit, normalise_manufacturer, parse_limit, parse_nps,
)

#: An approved list on this machine, if there is one. Found the way the
#: program finds it rather than written down, so these run for anyone who has
#: one and skip for anyone who does not -- and so no customer's path is baked
#: into a public test.
def _an_aml_on_this_machine():
    from weldaudit.extract.materials import find_aml_workbook

    # A workbook specifically. find_aml_workbook prefers a dated PDF, which
    # is right for an audit and wrong here — Aml.from_workbook needs a sheet.
    del find_aml_workbook
    for start in (Path.cwd(), *Path.cwd().parents, Path.home()):
        for pattern in ("AML*.xlsx", "AML*.xlsm", "*Approved Material*.xlsx"):
            found = sorted(start.glob(pattern))
            if found:
                return found[0]
        if start == Path.home():
            break
    return None


AML_BOOK = _an_aml_on_this_machine()


# -- nominal pipe size ------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ('16"', 16.0), ("16”", 16.0), ("NPS 1½", 1.5), ("1-1/2", 1.5),
    ("3/4", 0.75), ("4 IN", 4.0), ("20", 20.0), ("10.75", 10.75),
])
def test_parse_nps(text, expected):
    assert parse_nps(text) == expected


# -- specific limits --------------------------------------------------------

def test_up_to_limit():
    limit, cond = parse_limit("Up to NPS 20")
    assert limit.max_nps == 20 and limit.min_nps is None and cond == ""


def test_range_limit():
    limit, _ = parse_limit("NPS 8 to 42")
    assert (limit.min_nps, limit.max_nps) == (8, 42)


def test_and_larger_and_smaller():
    assert parse_limit("NPS 12 and larger")[0].min_nps == 12
    assert parse_limit("NPS 2 and smaller")[0].max_nps == 2


def test_size_marker_is_required():
    # "Up to 9% Cr, Up to NPS 6" must yield 6, not 9 - the percentage is not a size.
    limit, cond = parse_limit("Up to 9% Cr, Up to NPS 6")
    assert limit.max_nps == 6
    assert "9% Cr" in cond


def test_limit_embedded_in_a_brand_note():
    limit, _ = parse_limit("Brand: Grove (up to NPS 48)")
    assert limit.max_nps == 48


def test_non_size_condition_is_surfaced_not_guessed():
    limit, cond = parse_limit("Induction bends only")
    assert limit is None and cond == "Induction bends only"


def test_fractional_limit_renders_readably():
    limit, _ = parse_limit("Up to NPS 1½")
    assert limit.describe() == "up to NPS 1-1/2"


def test_size_limit_boundaries_are_inclusive():
    limit = SizeLimit(max_nps=20)
    assert limit.allows(20) and limit.allows(16) and not limit.allows(24)


# -- manufacturer normalisation --------------------------------------------

def test_legal_forms_are_dropped_but_industry_words_are_kept():
    # Stripping "steel"/"pipe" would collapse this to "american" and match
    # anything American-anything.
    assert normalise_manufacturer("American Steel Pipe") == "american steel pipe"
    assert normalise_manufacturer("Barrow Forge Europe S.p.A.") == "barrow forge europe"


def test_status_markers_and_parentheticals_are_dropped():
    assert normalise_manufacturer("*Ameriforge (AF Global) (F)") == "ameriforge"
    assert normalise_manufacturer("KMT Tagmet – HOLD (Russia Sanctioned)") == "kmt tagmet"


# -- matching against the real AML -----------------------------------------

pytestmark_book = pytest.mark.skipif(AML_BOOK is None, reason="no AML workbook on this machine")


@pytest.fixture(scope="module")
def aml() -> Aml:
    if AML_BOOK is None:
        pytest.skip("no AML workbook on this machine")
    return Aml.from_workbook(AML_BOOK)


def test_section_headings_are_not_loaded_as_manufacturers(aml):
    # "Carbon Steel - Seamless" is a heading; it has no location.
    assert not [e for e in aml.entries if e.manufacturer == "Carbon Steel - Seamless"]
    assert len(aml) > 1000


def test_every_name_on_the_list_matches_the_list(aml):
    """The floor beneath every other match: a name copied off the AML must
    come back approved. Taken from the list itself rather than written down
    here, so it holds for whichever AML a site uses -- and so a public test
    carries no customer's supplier names."""
    pipe = [e for e in aml.entries if e.category == "1.0 Pipe"][:25]
    assert pipe, "no pipe manufacturers on this list"
    for entry in pipe:
        got = aml.match(entry.manufacturer, ["1.0 Pipe"])
        assert got.status == "approved", entry.manufacturer


def test_a_short_trade_name_finds_the_full_entry(aml):
    """A certificate says the trade name where the AML says the full one.
    The leading word has to reach the entry, whatever the names are."""
    long_ones = [e for e in aml.entries
                 if len(e.manufacturer.split()) > 1 and e.manufacturer[:1].isalpha()]
    assert long_ones, "no multi-word manufacturers on this list"
    entry = long_ones[0]
    result = aml.match(entry.manufacturer.split()[0])
    assert result.status in ("approved", "confirm")
    assert result.entries


@pytest.mark.parametrize("name", ["Kandal", "Halloran Murray", "Wexford Co"])
def test_manufacturers_absent_from_the_aml_are_not_listed(aml, name):
    assert aml.match(name, ["1.0 Pipe"]).status == "not_listed"


def test_hold_entries_do_not_silently_approve(aml):
    """A mill on hold must never come back approved on its name alone."""
    held = [e for e in aml.entries if "HOLD" in (e.manufacturer or "").upper()]
    if not held:
        pytest.skip("nothing on hold on this list")
    result = aml.match(held[0].manufacturer)
    assert result.status == "confirm"
    assert "HOLD" in result.reason


def test_size_limit_is_enforced(aml):
    """A mill approved only up to a size must fail above it. The entry is
    taken from the list rather than named here, so this holds for whichever
    AML a site uses."""
    capped = [e for e in aml.entries
              if e.size_limit and e.size_limit.max_nps and not e.size_limit.min_nps]
    if not capped:
        pytest.skip("no size-capped entries on this list")
    entry = capped[0]
    cap = entry.size_limit.max_nps

    allowing, forbidding = aml.check_size([entry], cap)
    assert allowing and not forbidding          # at the cap it is approved
    allowing, forbidding = aml.check_size([entry], cap + 4)
    assert forbidding and not allowing          # above it, it is not


def test_superseded_entries_cannot_be_the_only_approval():
    entries = [
        AmlEntry("3.0 Flanges", "Maass (deleted)", "Konnern, GERMANY", "", key="maass"),
    ]
    assert Aml(entries).match("Maass").status == "confirm"


# -- MTR filenames ----------------------------------------------------------

@pytest.mark.parametrize("filename,heat", [
    ("071B33 - 16IN 150 RFWN FLANGE STD A105N XTO-414.pdf", "071B33"),
    ("377314022-003 ~ 16IN 150 TRUNNION BV CS NACE XTO-304.pdf", "377314022-003"),
    ("4F214-FLG-WN-2IN-CL600-SCH160-A105N.pdf", "4F214"),
    ("FLANGE 3 INCH SCH 80- 4F318P.pdf", "4F318P"),
    ("45° 8 INCH 3R SEGM-5J17DK.pdf", "5J17DK"),
    ("12589 375 X 52.pdf", "12589"),
    ("PIPE 16 INCH .375- 2245438.pdf", "2245438"),
    # A letters-only heat is only safe to take from the "<heat> - <desc>" form.
    ("CCDS - 2IN 300 RF BLIND 1IN TAP SS XTO-219.pdf", "CCDS"),
    # Short trailing heats are real: "FLANGE 16 INCH STD EL8".
    ("FLANGE 16 INCH STD EL8.pdf", "EL8"),
])
def test_heat_is_read_from_the_filename(filename, heat):
    assert mtrname.parse(filename).heat == heat


def test_description_glued_on_by_a_dash_is_trimmed():
    # The dash sits between two digits, so tokenising alone leaves "...-4IN".
    assert mtrname.parse("A241114AB24-4IN-PIPE-SCH40-316L-SS.pdf").heat == "A241114AB24"
    assert mtrname.parse("FEG24-4IN-300-FLG-WN-SCH40-F316-SS.pdf").heat == "FEG24"


def test_filenames_with_no_heat_report_none():
    assert mtrname.parse("2IN_600_FLG_WN_SCH160_A105N_CS.pdf").heat == ""
    assert mtrname.parse("BUTTER FLY VALVE.pdf").heat == ""


def test_attributes_are_read_alongside_the_heat():
    ident = mtrname.parse("071B33 - 16IN 150 RFWN FLANGE STD A105N XTO-414.pdf")
    assert ident.nps == 16 and ident.schedule == "STD" and ident.spec == "A105N"
    assert "3.0 Flanges" in ident.categories


def test_split_schedule_and_size_are_stitched_together():
    ident = mtrname.parse("FLANGE 3 INCH SCH 80- 4F318P.pdf")
    assert ident.nps == 3 and ident.schedule == "SCH80"


def test_heat_normalisation_ignores_punctuation_and_case():
    assert mtrname.normalise_heat("377314022-003") == "377314022003"
    assert mtrname.normalise_heat("4f318p") == "4F318P"


# -- a ship-to depot is not a manufacturer ----------------------------------

def test_a_branch_code_is_not_a_company():
    """Distributors ship from numbered depots. The vision pass recorded
    'MRC GLOBAL #172' as the maker of flanges Halden made, and Halden is on
    the AML — so approved material was reported as unapproved, seven times."""
    from weldaudit.extract.vision_pass import _looks_like_a_company as ok

    for depot in ("MRC GLOBAL #172", "MRC GLOBAL #078", "MRC GLOBAL B172",
                  "MRC GLOBAL 8078"):
        assert not ok(depot), depot
    for maker in ("halden mfg. co., l.p.", "ORTEGA FORJA, S.COOP.", "Norvale Tamsa",
                  "ISK MFG CO., LTD.", "Barrow Forge", "Kandal Pipe USA, Inc"):
        assert ok(maker), maker


def test_a_ship_to_depot_never_becomes_the_manufacturer(tmp_path):
    from weldaudit.db import Database
    from weldaudit.extract.vision_pass import Target, _apply_mtr

    db = Database(tmp_path / "t.db")
    pid = db.upsert_project("T", str(tmp_path))
    with db.tx() as c:
        c.execute(
            """INSERT INTO document(id, project_id, path, filename, ext, kind)
               VALUES(1, ?, 'c.pdf', 'c.pdf', '.pdf', 'mtr')""", (pid,))
        c.execute(
            """INSERT INTO material(project_id, document_id, segment, heat,
                                    heat_key, source) VALUES(?, 1, 'S', '1C663',
                                    '1C663', 'mtr_file')""", (pid,))
    _apply_mtr(db, pid, Target(1, "c.pdf", "c.pdf", "fp", 1, "t", "S"),
               {"page_is_certificate": True, "issuing_company": "MRC GLOBAL #172",
                "mill_name": None}, 0)
    assert not (db.one("SELECT manufacturer FROM material")["manufacturer"] or "")


# -- one answer per company, not one per book -------------------------------

def test_whether_a_mill_is_approved_is_asked_once_for_the_job():
    """The same manufacturer in four segments is one question with one answer;
    it was raising four criticals that said the same thing."""
    from weldaudit.rules.materials import _collapse

    made = [{"segment": s, "subject": "Kandal Pipe USA, Inc", "message": "x.",
             "detail": '{"heat": "H%d"}' % i}
            for i, s in enumerate(("16 PW", "20 LP", "6 FG", "6 FG"))]
    once = _collapse(list(made), by="subject", per_segment=False)
    assert len(once) == 1
    assert "3 segments" in once[0]["segment"]
    assert "16 PW" in once[0]["message"]
    # Per-segment is still the default; most checks are about a book.
    assert len(_collapse(list(made), by="subject")) == 3


# -- a material grade is not a manufacturer ---------------------------------

def test_a_specification_is_not_a_company():
    """When the model finds no letterhead it sometimes takes a grade out of
    the body of the form. 'A351-CF8M' was recorded as the maker of four items
    and became four critical 'not approved' findings."""
    from weldaudit.extract.vision_pass import _looks_like_a_company as ok

    for spec in ("A351-CF8M", "A105N", "X52M PSL2", "SA-105M", "B.W SMLS",
                 "0426A02+0049",
                 "ASME SA350LF2CL1-2-23/SA105M-23, ASTM A105M-24"):
        assert not ok(spec), spec


def test_short_and_odd_company_names_survive_it():
    """Hy-Grade, C&C, L&T and S.C.O.T. are all approved manufacturers on this
    job. Losing a real approval is worse than keeping a spec code: the spec
    code raises a finding somebody reads, the lost approval raises nothing."""
    from weldaudit.extract.vision_pass import _looks_like_a_company as ok

    for real in ("Hy-Grade", "C&C", "L&T", "S.C.O.T.", "Brϋck (F)",
                 "NOV", "Tex Tubo", "USG SEAMLESS TUBULAR OPS",
                 "ISK MFG CO., LTD."):
        assert ok(real), real
    # A two-character name like "3M" is refused, but by the older minimum
    # length rule rather than this filter. Nothing on the AML is that short.
    from weldaudit.extract.vision_pass import _looks_like_a_spec
    assert not _looks_like_a_spec("3M")


def test_the_spec_filter_clears_every_aml_entry_on_this_job():
    """It only earns its place if it rejects none of the approved list."""
    import json
    from pathlib import Path

    from weldaudit.db import Database
    from weldaudit.extract.vision_pass import _looks_like_a_spec
    from weldaudit.pipeline import default_db_path

    if not Path(default_db_path()).exists():
        pytest.skip("no local audit database")
    db = Database(default_db_path())
    names = [r["manufacturer"] for r in
             db.q("SELECT DISTINCT manufacturer FROM aml_entry")]
    if not names:
        pytest.skip("no AML loaded")
    rejected = [n for n in names if _looks_like_a_spec(n)]
    assert rejected == [], rejected


# -- where a company is, not who it is --------------------------------------

def test_an_address_is_not_a_manufacturer():
    """The address sits directly under the letterhead, and the model
    sometimes takes it instead: '3245 S. Harte Avenue' was recorded as the
    maker of a fitting. A depot is the same mistake one line further on."""
    from weldaudit.extract.vision_pass import _looks_like_a_company as ok

    for place in ("3245 S. Harte Avenue",
                  "RK Distribution Center",
                  "4901 Oates Road  Houston TX 77013",
                  "1411, S.FM 565, Baytown, Texas, 77523, USA",
                  "26, Noksansandan 262-ro, Gangseo-Gu, Busan, Korea"):
        assert not ok(place), place


def test_a_leading_number_alone_does_not_condemn_a_company():
    """'84 Lumber Company' is a real firm. A house number only counts as an
    address when a street word or a postal run follows it."""
    from weldaudit.extract.vision_pass import _looks_like_an_address

    for real in ("84 Lumber Company", "3M", "Barrow Forge", "Penn Machine Works",
                 "Mills Iron Works", "Broadway Metals", "Norvale Tamsa",
                 "ORTEGA FORJA, S.COOP.", "USG SEAMLESS TUBULAR OPS"):
        assert not _looks_like_an_address(real), real


def test_the_address_filter_clears_every_aml_entry_on_this_job():
    from pathlib import Path

    from weldaudit.db import Database
    from weldaudit.extract.vision_pass import _looks_like_an_address
    from weldaudit.pipeline import default_db_path

    if not Path(default_db_path()).exists():
        pytest.skip("no local audit database")
    names = [r["manufacturer"] for r in
             Database(default_db_path()).q(
                 "SELECT DISTINCT manufacturer FROM aml_entry")]
    if not names:
        pytest.skip("no AML loaded")
    assert [n for n in names if _looks_like_an_address(n)] == []


# -- the buyer, reported so it can be excluded ------------------------------

def _apply(tmp_path, payload):
    from weldaudit.db import Database
    from weldaudit.extract.vision_pass import Target, _apply_mtr

    db = Database(tmp_path / "t.db")
    pid = db.upsert_project("T", str(tmp_path))
    with db.tx() as c:
        c.execute("""INSERT INTO document(id, project_id, path, filename, ext, kind)
                     VALUES(1, ?, 'c.pdf', 'c.pdf', '.pdf', 'mtr')""", (pid,))
        c.execute("""INSERT INTO material(project_id, document_id, segment, heat,
                                          heat_key, source)
                     VALUES(?, 1, 'S', 'H1', 'H1', 'mtr_file')""", (pid,))
    _apply_mtr(db, pid, Target(1, "c.pdf", "c.pdf", "fp", 1, "t", "S"),
               {"page_is_certificate": True, **payload}, 0)
    return db.one("SELECT manufacturer, mill_name FROM material")


def test_the_buyer_cannot_be_the_manufacturer(tmp_path):
    """'DODSON GLOBAL' was recorded as the maker of fittings Rigid Industries
    made, and Rigid Industries is on the AML. No filter can tell a customer
    from a maker by the name alone, so the model is asked who bought it."""
    row = _apply(tmp_path, {"issuing_company": "DODSON GLOBAL",
                            "customer": "DODSON GLOBAL", "mill_name": None})
    assert not (row["manufacturer"] or "")


def test_a_buyer_spelled_differently_is_still_the_buyer(tmp_path):
    row = _apply(tmp_path, {"issuing_company": "MRC Global (U.S.) Inc.",
                            "customer": "MRC GLOBAL INC", "mill_name": None})
    assert not (row["manufacturer"] or "")


def test_naming_the_buyer_does_not_disturb_a_real_letterhead(tmp_path):
    """The usual case: two different companies, both correctly read."""
    row = _apply(tmp_path, {"issuing_company": "RIGID INDUSTRIES CO., LTD.",
                            "customer": "DODSON GLOBAL", "mill_name": None})
    assert row["manufacturer"] == "RIGID INDUSTRIES CO., LTD."


def test_a_buyer_in_the_mill_field_is_dropped_too(tmp_path):
    row = _apply(tmp_path, {"issuing_company": "ORTEGA FORJA, S.COOP.",
                            "customer": "Allied Fitting L.P.",
                            "mill_name": "Allied Fitting LP",
                            "mill_source": "works_line"})
    assert row["manufacturer"] == "ORTEGA FORJA, S.COOP." and row["mill_name"] is None


def test_readings_made_before_the_field_existed_still_apply(tmp_path):
    """Cached payloads have no `customer` key; they must not start failing."""
    row = _apply(tmp_path, {"issuing_company": "Barrow Forge", "mill_name": None})
    assert row["manufacturer"] == "Barrow Forge"
