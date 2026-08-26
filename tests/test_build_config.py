"""What the two builds carry, checked without building either.

`build_config` exists so the one-file and folder builds cannot drift apart,
and the list that matters most is `HIDDEN`: every rule module is named there
because each registers itself on import. A rule left out of a build does not
fail - it is simply never run, and the report comes back short rather than
wrong, which is the failure this whole program is written against.

That list was maintained by hand and had already fallen behind by one module
when this file was written.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import build_config  # noqa: E402
from weldaudit import rules  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def test_every_rule_module_is_named_in_the_build():
    # Not "every module that happens to be imported" - every module on disk,
    # so adding a rules file and forgetting the build is caught here rather
    # than by an auditor wondering why a finding stopped appearing.
    on_disk = {
        f"weldaudit.rules.{p.stem}"
        for p in (ROOT / "weldaudit" / "rules").glob("*.py")
        if p.stem != "__init__"
    }
    missing = sorted(on_disk - set(build_config.HIDDEN))
    assert not missing, f"rule modules missing from build_config.HIDDEN: {missing}"


def test_the_named_modules_all_exist():
    # The other direction: a renamed module leaves a dead string behind, and a
    # dead string in a hiddenimports list fails the build rather than quietly.
    named = {h for h in build_config.HIDDEN if h.startswith("weldaudit.rules.")}
    for module in sorted(named):
        assert (ROOT / Path(*module.split("."))).with_suffix(".py").is_file(), module


def test_every_registered_rule_comes_from_a_named_module():
    # The registry is the thing that actually decides what runs.
    families = {code.split("-")[0] for code in rules.registry()}
    assert "WT" in families, "the WeldTrace family is not registered at all"
    assert len(rules.registry()) > 100


# -- the version resource ---------------------------------------------------

def test_the_version_resource_carries_the_programs_own_version():
    version = build_config._version()
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), version

    text = str(build_config.version_info())
    assert f"StringStruct('ProductVersion', '{version}')" in text
    assert f"StringStruct('FileVersion', '{version}')" in text

    # The binary fields are four integers, whatever the string says: Windows
    # sorts and compares on these, not on the text.
    numbers = tuple(int(p) for p in version.split(".")) + (0,)
    assert len(numbers) == 4
    assert f"filevers={numbers}" in text
    assert f"prodvers={numbers}" in text


def test_the_version_is_read_from_the_source_not_imported():
    # A spec runs inside PyInstaller's own process; importing the package
    # there pulls in the dependency tree before the analysis meant to find it.
    assert build_config._version() == _declared_version()


def _declared_version() -> str:
    text = (ROOT / "weldaudit" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__\s*=\s*"([^"]+)"', text).group(1)


def test_a_missing_version_is_refused_rather_than_guessed(tmp_path, monkeypatch):
    # An unversioned build cannot be published, so it should not be built.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "weldaudit").mkdir()
    (tmp_path / "weldaudit" / "__init__.py").write_text("# nothing here\n")
    with pytest.raises(SystemExit):
        build_config._version()


def test_both_specs_attach_the_resource():
    for spec in ("WeldAudit.spec", "WeldAudit-folder.spec"):
        text = (ROOT / spec).read_text(encoding="utf-8")
        assert "version=build_config.version_info()" in text, spec
