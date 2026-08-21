"""Saving a report from the native window.

"Download" works in a browser because a browser has a downloads folder and a
bar to show it in. A WebView2 window has neither, so navigating it to an
attachment URL did nothing at all -- no file, no error, no dialog, and no way
for the user to tell the difference between that and a slow export.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import app as launcher  # noqa: E402


class _Answer:
    """What urlopen gives back, as much of it as save_report touches."""

    def __init__(self, body, disposition):
        self._body = body
        self.headers = {"content-disposition": disposition}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@pytest.fixture
def window(monkeypatch, tmp_path):
    """The bridge, with the dialog answering with a path we choose."""
    chosen = {"path": str(tmp_path / "report.xlsx"), "asked_with": None}

    class _FakeWindow:
        def create_file_dialog(self, _kind, save_filename=""):
            chosen["asked_with"] = save_filename
            return chosen["path"]

    fake = type(sys)("webview")
    fake.windows = [_FakeWindow()]
    fake.SAVE_DIALOG = 30
    monkeypatch.setitem(sys.modules, "webview", fake)
    return launcher._Windows(), chosen


def _serve(monkeypatch, body, disposition):
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: _Answer(body, disposition))


def test_the_report_is_written_where_the_user_said(window, monkeypatch, tmp_path):
    bridge, chosen = window
    chosen["path"] = str(tmp_path / "report.csv")   # match the format served
    _serve(monkeypatch, b"col,col\n1,2\n", 'attachment; filename="r.csv"')
    where = bridge.save_report("http://127.0.0.1:8765/api/export?fmt=csv")
    assert where == chosen["path"]
    assert Path(where).read_bytes() == b"col,col\n1,2\n"


def test_the_name_the_server_chose_is_offered(window, monkeypatch):
    """Named the same whichever way it was saved, rather than made up here."""
    bridge, chosen = window
    _serve(monkeypatch, b"x", 'attachment; filename="WeldAudit Kestrel 8.xlsx"')
    bridge.save_report("http://127.0.0.1:8765/api/export")
    assert chosen["asked_with"] == "WeldAudit Kestrel 8.xlsx"


def test_a_percent_encoded_name_is_readable_again(window, monkeypatch):
    """The server sends RFC 5987 for names with spaces and punctuation."""
    bridge, chosen = window
    _serve(monkeypatch, b"x",
           "attachment; filename*=utf-8''001_%2016%20PW%20-%20READY.xlsx")
    bridge.save_report("http://127.0.0.1:8765/api/export")
    assert chosen["asked_with"] == "001_ 16 PW - READY.xlsx"


def test_cancelling_writes_nothing(window, monkeypatch, tmp_path):
    bridge, chosen = window
    chosen["path"] = None

    class _Cancelled:
        def create_file_dialog(self, _kind, save_filename=""):
            return None

    sys.modules["webview"].windows = [_Cancelled()]
    _serve(monkeypatch, b"x", 'attachment; filename="r.csv"')
    assert bridge.save_report("http://127.0.0.1:8765/api/export") == ""
    assert list(tmp_path.iterdir()) == []


def test_a_dialog_returning_a_list_is_understood(window, monkeypatch, tmp_path):
    """pywebview hands back a tuple for some dialogs and a string for others."""
    bridge, chosen = window
    target = str(tmp_path / "from-a-list.csv")

    class _ListWindow:
        def create_file_dialog(self, _kind, save_filename=""):
            return (target,)

    sys.modules["webview"].windows = [_ListWindow()]
    _serve(monkeypatch, b"data", 'attachment; filename="r.csv"')
    assert bridge.save_report("http://127.0.0.1:8765/api/export") == target
    assert Path(target).read_bytes() == b"data"


def test_the_newer_pywebview_dialog_name(window, monkeypatch, tmp_path):
    """SAVE_DIALOG is deprecated in favour of FileDialog.SAVE; the window has
    to work on whichever of the two the bundled version offers."""
    bridge, chosen = window
    chosen["path"] = str(tmp_path / "report.csv")   # match the format served
    del sys.modules["webview"].SAVE_DIALOG
    sys.modules["webview"].FileDialog = type("FileDialog", (), {"SAVE": 30})
    _serve(monkeypatch, b"x", 'attachment; filename="r.csv"')
    assert bridge.save_report("http://127.0.0.1:8765/api/export") == chosen["path"]


# -- revealing a saved file --------------------------------------------------
#
# Reported from a colleague's machine: pressing Download produced a Windows box
# offering to find an app in the Microsoft Store for .xlsx, on a PC that had
# Excel. Opening a file needs an association; showing it in Explorer does not,
# and seeing it on disk is what tells an auditor the report exists at all.


def test_reveal_selects_the_file_in_explorer(monkeypatch, tmp_path):
    made = tmp_path / "report.xlsx"
    made.write_bytes(b"x")
    ran = {}
    monkeypatch.setattr(launcher.subprocess, "Popen",
                        lambda cmd, *a, **k: ran.setdefault("cmd", cmd))
    assert launcher._Windows().reveal(str(made)) is True
    assert "/select," in ran["cmd"]
    assert str(made) in ran["cmd"]


def test_reveal_says_no_for_a_file_that_is_not_there(tmp_path):
    assert launcher._Windows().reveal(str(tmp_path / "nope.xlsx")) is False


def test_reveal_survives_explorer_being_unavailable(monkeypatch, tmp_path):
    """A failure to show the file must not lose the toast naming its path."""
    made = tmp_path / "report.xlsx"
    made.write_bytes(b"x")

    def boom(*_a, **_k):
        raise OSError("no shell here")

    monkeypatch.setattr(launcher.subprocess, "Popen", boom)
    assert launcher._Windows().reveal(str(made)) is False


# -- the extension the user drops --------------------------------------------
#
# The real report, from a colleague's machine: "it wont download to a excel or
# csv, it opens a popup to ask to download a program online". The file he sent
# back was a perfectly valid workbook -- ZIP header, 43,114 bytes, opens in
# Excel the moment it is renamed -- called
#
#     001_ 16 PW - READY FOR QA - PUNCHLIST_2
#
# with no extension at all. He had renamed it in the Save As box to something
# he would recognise, and the dialog returns the name exactly as typed. Windows
# has nothing registered for a file with no extension, so it offered to find an
# app in the Microsoft Store. Excel was installed the whole time; there was no
# extension for it to match. Every report had saved correctly and none could be
# opened.


def _dialog_returning(monkeypatch, path, accepts_file_types=True):
    seen = {"file_types": None, "save_filename": None}

    class _FakeWindow:
        if accepts_file_types:
            def create_file_dialog(self, _kind, save_filename="", file_types=()):
                seen["save_filename"] = save_filename
                seen["file_types"] = file_types
                return path
        else:
            def create_file_dialog(self, _kind, save_filename=""):
                seen["save_filename"] = save_filename
                return path

    fake = type(sys)("webview")
    fake.windows = [_FakeWindow()]
    fake.SAVE_DIALOG = 30
    monkeypatch.setitem(sys.modules, "webview", fake)
    return launcher._Windows(), seen


XLSX = 'attachment; filename="Kestrel 8 - exceptions.xlsx"'
CSV = 'attachment; filename="Kestrel 8 - exceptions.csv"'


def test_a_name_typed_without_an_extension_gets_one(monkeypatch, tmp_path):
    """The bug, exactly as it happened."""
    typed = tmp_path / "001_ 16 PW - READY FOR QA - PUNCHLIST"
    bridge, _ = _dialog_returning(monkeypatch, str(typed))
    _serve(monkeypatch, b"PK\x03\x04workbook", XLSX)

    where = bridge.save_report("http://127.0.0.1:8765/api/export")
    assert where.endswith(".xlsx")
    assert Path(where).read_bytes() == b"PK\x03\x04workbook"
    assert not typed.exists(), "the extensionless name must not be written"


def test_a_name_that_already_has_it_is_left_alone(monkeypatch, tmp_path):
    typed = tmp_path / "punchlist.xlsx"
    bridge, _ = _dialog_returning(monkeypatch, str(typed))
    _serve(monkeypatch, b"body", XLSX)
    assert bridge.save_report("u") == str(typed)


def test_the_extension_is_matched_whatever_the_case(monkeypatch, tmp_path):
    """A name typed .XLSX must not become .XLSX.xlsx."""
    typed = tmp_path / "PUNCHLIST.XLSX"
    bridge, _ = _dialog_returning(monkeypatch, str(typed))
    _serve(monkeypatch, b"body", XLSX)
    assert bridge.save_report("u") == str(typed)


def test_csv_gets_its_own_extension(monkeypatch, tmp_path):
    typed = tmp_path / "punchlist"
    bridge, _ = _dialog_returning(monkeypatch, str(typed))
    _serve(monkeypatch, b"a,b\n", CSV)
    assert bridge.save_report("u").endswith(".csv")


def test_a_name_holding_a_dot_still_gets_the_right_extension(monkeypatch, tmp_path):
    """'16 PW - REV 1.2' has a suffix of '.2', which opens nothing."""
    typed = tmp_path / "16 PW - REV 1.2"
    bridge, _ = _dialog_returning(monkeypatch, str(typed))
    _serve(monkeypatch, b"body", XLSX)
    assert bridge.save_report("u") == str(typed) + ".xlsx"


def test_the_dialog_is_offered_a_filter(monkeypatch, tmp_path):
    """So the backend appends the extension itself where it can."""
    bridge, seen = _dialog_returning(monkeypatch, str(tmp_path / "x.xlsx"))
    _serve(monkeypatch, b"body", XLSX)
    bridge.save_report("u")
    assert any(".xlsx" in kind for kind in seen["file_types"])


def test_a_backend_that_rejects_the_filter_still_saves(monkeypatch, tmp_path):
    """The filter is a convenience; losing it must not lose the report."""
    typed = tmp_path / "punchlist"
    bridge, _ = _dialog_returning(monkeypatch, str(typed), accepts_file_types=False)
    _serve(monkeypatch, b"body", XLSX)
    assert bridge.save_report("u").endswith(".xlsx")


def test_cancelling_still_writes_nothing(monkeypatch, tmp_path):
    bridge, _ = _dialog_returning(monkeypatch, "")
    _serve(monkeypatch, b"body", XLSX)
    assert bridge.save_report("u") == ""
    assert list(tmp_path.iterdir()) == []


def test_the_other_report_format_is_corrected_not_appended(monkeypatch, tmp_path):
    """A CSV named .xlsx makes Excel refuse to open it, so that one suffix is
    replaced rather than appended to."""
    bridge, _ = _dialog_returning(monkeypatch, str(tmp_path / "punchlist.xlsx"))
    _serve(monkeypatch, b"a,b\n", CSV)
    assert bridge.save_report("u") == str(tmp_path / "punchlist.csv")
