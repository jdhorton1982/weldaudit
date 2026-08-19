"""Companies the certificates and the AML call different things.

Fuzzy matching crosses spelling; it cannot cross a rebrand. ORTEGA Forja is
listed as Ortega Advanced Forged Solutions — one shared word, 51% similar — and
fourteen certificates were reported as critical non-conformances against
material that is approved.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit import aliases  # noqa: E402
from weldaudit.aml import Aml, AmlEntry, normalise_manufacturer  # noqa: E402


def _aml(names, alias_map=None):
    return Aml([AmlEntry(category="Flanges", manufacturer=n, location="",
                         limits_raw="", conditions="", key=normalise_manufacturer(n))
                for n in names], aliases=alias_map)


def test_a_renamed_company_is_recognised():
    plain = _aml(["Ortega Advanced Forged Solutions"])
    assert plain.match("ORTEGA FORJA, S.COOP.").status == "not_listed"

    aliased = _aml(["Ortega Advanced Forged Solutions"],
                   {normalise_manufacturer("ORTEGA FORJA, S.COOP."):
                    "Ortega Advanced Forged Solutions"})
    result = aliased.match("ORTEGA FORJA, S.COOP.")
    assert result.status == "approved"
    assert result.entries[0].manufacturer == "Ortega Advanced Forged Solutions"


def test_one_line_covers_every_punctuation_of_the_same_name():
    """Keyed on the normalised form, so the auditor writes it once."""
    aliased = _aml(["Ortega Advanced Forged Solutions"],
                   {normalise_manufacturer("ORTEGA FORJA, S.COOP."):
                    "Ortega Advanced Forged Solutions"})
    for spelling in ("ORTEGA FORJA, S.COOP.", "ORTEGA FORJA S.COOP.",
                     "Ortega Forja S Coop", "ortega forja s.coop"):
        assert aliased.match(spelling).status == "approved", spelling


def test_an_alias_does_not_approve_anyone_else():
    aliased = _aml(["Ortega Advanced Forged Solutions"],
                   {normalise_manufacturer("ORTEGA FORJA, S.COOP."):
                    "Ortega Advanced Forged Solutions"})
    assert aliased.match("Kandal Pipe USA, Inc").status == "not_listed"


def test_the_strict_lookup_honours_aliases_too():
    """`nearest` is what picks a letterhead off a page; it must agree."""
    aliased = _aml(["Ortega Advanced Forged Solutions"],
                   {normalise_manufacturer("ORTEGA FORJA, S.COOP."):
                    "Ortega Advanced Forged Solutions"})
    score, entry = aliased.nearest("ORTEGA FORJA, S.COOP.")
    assert entry == "Ortega Advanced Forged Solutions" and score == 100


# -- the file the auditor edits ---------------------------------------------

def test_the_file_is_created_with_its_own_instructions(tmp_path):
    path = tmp_path / "manufacturer-aliases.csv"
    loaded = aliases.load(path)
    assert path.exists()
    text = path.read_text(encoding="utf-8-sig")
    assert "certificate name,AML name" in text
    assert "Do NOT use it to map a misread name" in text
    assert loaded[normalise_manufacturer("ORTEGA FORJA, S.COOP.")] == \
        "Ortega Advanced Forged Solutions"


def test_comments_and_blank_lines_are_ignored(tmp_path):
    path = tmp_path / "a.csv"
    path.write_text("# a note\n\ncertificate name,AML name\nFoo Ltd,Bar Forge\n",
                    encoding="utf-8")
    loaded = aliases.load(path)
    assert loaded == {normalise_manufacturer("Foo Ltd"): "Bar Forge"}


def test_a_half_filled_line_is_skipped_rather_than_breaking_the_audit(tmp_path):
    path = tmp_path / "a.csv"
    path.write_text("Foo Ltd,\n,Bar Forge\nBaz Inc,Baz Forge\n", encoding="utf-8")
    assert aliases.load(path) == {normalise_manufacturer("Baz Inc"): "Baz Forge"}


def test_an_unreadable_file_is_not_fatal(tmp_path):
    """A locked or malformed file must not stop the whole audit."""
    assert aliases.load(tmp_path / "nested" / "deep" / "a.csv") is not None
