"""The commit guard, checked for the ways it can pass without checking.

A guard that refuses the wrong commit is a nuisance. A guard that silently
stops guarding is a hazard, because the thing it protects against is
irreversible: this repository is public, and a name that reaches a public
commit cannot be recalled from anyone who already has it.

Both failures below were real. The list is deliberately not read here - a test
that asserted on its contents would put them in the repository, which is the
thing the list exists to prevent.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import pytest  # noqa: E402

import pre_commit_check as guard  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def test_the_list_is_found_from_anywhere(tmp_path, monkeypatch):
    # The bug: the path was relative, so running the hook from a subdirectory
    # - or anywhere git happened to put the cwd - found no list, loaded no
    # patterns, and passed everything without saying so.
    from_root = len(guard.site_patterns())

    for where in (tmp_path, ROOT / "tools", ROOT / "weldaudit" / "rules"):
        monkeypatch.chdir(where)
        assert len(guard.site_patterns()) == from_root, f"differs from {where}"


def test_the_path_is_absolute_and_inside_the_repository():
    assert guard.FORBIDDEN_LIST.is_absolute()
    assert guard.FORBIDDEN_LIST.parent == ROOT / "private"


def test_a_missing_list_is_announced_rather_than_assumed(tmp_path, monkeypatch, capsys):
    # No list is legitimate - it is gitignored, so a fresh clone has none. The
    # check passing then means something entirely different from the check
    # passing with a list loaded, and the operator has to be able to tell.
    monkeypatch.setattr(guard, "FORBIDDEN_LIST", tmp_path / "absent.txt")
    assert guard.site_patterns() == []
    assert "not compared" in capsys.readouterr().err


def test_a_line_that_is_not_a_regex_still_matches_literally(tmp_path, monkeypatch):
    # Site lists are written by people, not by programmers, and a Windows path
    # is not a valid pattern - "\U" is an incomplete escape. It must not be
    # dropped on the floor for that.
    listing = tmp_path / "forbidden.txt"
    listing.write_text("C:\\Users\\someone\n", encoding="utf-8")
    monkeypatch.setattr(guard, "FORBIDDEN_LIST", listing)

    pats = guard.site_patterns()
    assert len(pats) == 1
    assert pats[0][0].search("a path C:\\Users\\someone here")


def test_a_line_that_is_valid_regex_is_read_as_one(tmp_path, monkeypatch):
    """The sharp edge in this file, written down so it is a decision.

    The fallback catches a line that will not compile. It cannot catch one
    that compiles into something other than what the writer meant: "Acme
    (Holdings)" is a valid pattern, so it blocks "Acme Holdings" and lets the
    bracketed form it was copied from straight through.

    That is the contract - the list takes patterns, and the entries in it rely
    on that - but anyone adding a name with brackets, a dot or a plus in it
    should escape them.
    """
    listing = tmp_path / "forbidden.txt"
    listing.write_text("Acme (Holdings)\n", encoding="utf-8")
    monkeypatch.setattr(guard, "FORBIDDEN_LIST", listing)

    pattern = guard.site_patterns()[0][0]
    assert pattern.search("sold by Acme Holdings ltd")
    assert not pattern.search("sold by Acme (Holdings) ltd")


def test_comments_and_blank_lines_are_not_patterns(tmp_path, monkeypatch):
    listing = tmp_path / "forbidden.txt"
    listing.write_text("# a comment\n\n   \nRealName\n", encoding="utf-8")
    monkeypatch.setattr(guard, "FORBIDDEN_LIST", listing)
    assert len(guard.site_patterns()) == 1


def test_the_site_list_actually_has_patterns_on_this_machine():
    # Not a property of the code - a check that this clone is configured. Skips
    # rather than fails where there is no list, because that is a valid clone.
    if not guard.FORBIDDEN_LIST.is_file():
        pytest.skip("no private/forbidden.txt in this clone")
    assert guard.site_patterns(), "the list is present but yielded no patterns"


# -- the commit message -----------------------------------------------------

def _msg(tmp_path, text: str) -> Path:
    p = tmp_path / "COMMIT_EDITMSG"
    p.write_text(text, encoding="utf-8")
    return p


def test_a_message_naming_a_customer_is_refused(tmp_path, monkeypatch, capsys):
    listing = tmp_path / "forbidden.txt"
    listing.write_text("Northwind Pipeline\n", encoding="utf-8")
    monkeypatch.setattr(guard, "FORBIDDEN_LIST", listing)

    assert guard.check_message(_msg(tmp_path, "Fix the Northwind Pipeline import\n")) == 1
    err = capsys.readouterr().err
    assert "the commit message" in err
    assert "reword it" in err


def test_a_message_carrying_a_secret_is_refused(tmp_path, monkeypatch):
    # Assembled rather than written out. A file testing a secret scanner, with
    # a literal secret in it, trips the secret scanner - which this one did,
    # on its own commit. The check reads file text, so splitting the token
    # keeps this file clean while leaving the test honest.
    fake = "sk-" + "ant-" + "abcdefghijklmnop"
    monkeypatch.setattr(guard, "FORBIDDEN_LIST", tmp_path / "absent.txt")
    text = f"Rotate the key\n\nWas {fake} in the config.\n"
    assert guard.check_message(_msg(tmp_path, text)) == 1


def test_an_ordinary_message_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "FORBIDDEN_LIST", tmp_path / "absent.txt")
    assert guard.check_message(_msg(tmp_path, "Read a WeldTrace download\n")) == 0


def test_gits_own_comments_are_not_scanned(tmp_path, monkeypatch):
    # The comment block lists staged paths and the branch. Git strips it, so a
    # name appearing there is never published - reporting it would be a false
    # alarm, and a guard that cries wolf gets bypassed.
    listing = tmp_path / "forbidden.txt"
    listing.write_text("Northwind\n", encoding="utf-8")
    monkeypatch.setattr(guard, "FORBIDDEN_LIST", listing)

    text = ("Tidy the parser\n"
            "#\n"
            "# On branch main\n"
            "# Changes to be committed:\n"
            "#\tmodified:   Northwind-notes.txt\n")
    assert guard.check_message(_msg(tmp_path, text)) == 0


def test_the_verbose_diff_below_the_scissors_is_not_scanned(tmp_path, monkeypatch):
    # `git commit -v` pastes the whole diff under a scissors line and strips it
    # again. The staged-file check is what covers that content.
    listing = tmp_path / "forbidden.txt"
    listing.write_text("Northwind\n", encoding="utf-8")
    monkeypatch.setattr(guard, "FORBIDDEN_LIST", listing)

    text = ("Tidy the parser\n\n"
            "# ------------------------ >8 ------------------------\n"
            "diff --git a/x b/x\n+Northwind everywhere\n")
    assert guard.check_message(_msg(tmp_path, text)) == 0


def test_the_body_is_still_scanned_above_the_scissors(tmp_path, monkeypatch):
    listing = tmp_path / "forbidden.txt"
    listing.write_text("Northwind\n", encoding="utf-8")
    monkeypatch.setattr(guard, "FORBIDDEN_LIST", listing)

    text = ("Tidy the Northwind parser\n\n"
            "# ------------------------ >8 ------------------------\n"
            "diff --git a/x b/x\n")
    assert guard.check_message(_msg(tmp_path, text)) == 1


def test_an_unreadable_message_is_reported_not_passed_over_in_silence(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(guard, "FORBIDDEN_LIST", tmp_path / "absent.txt")
    assert guard.check_message(tmp_path / "does-not-exist") == 0
    assert "not scanned" in capsys.readouterr().err


def test_an_argument_selects_the_message_check(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "FORBIDDEN_LIST", tmp_path / "absent.txt")
    assert guard.main([str(_msg(tmp_path, "Ordinary message\n"))]) == 0


def test_both_hooks_find_the_check_by_their_own_path():
    for hook in ("pre-commit", "commit-msg"):
        text = (ROOT / "tools" / "hooks" / hook).read_text(encoding="utf-8")
        assert 'dirname "$0"' in text, hook
        assert "pre_commit_check.py" in text, hook
    assert '"$1"' in (ROOT / "tools" / "hooks" / "commit-msg").read_text(encoding="utf-8")
