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


def test_the_report_is_written_where_the_user_said(window, monkeypatch):
    bridge, chosen = window
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
    del sys.modules["webview"].SAVE_DIALOG
    sys.modules["webview"].FileDialog = type("FileDialog", (), {"SAVE": 30})
    _serve(monkeypatch, b"x", 'attachment; filename="r.csv"')
    assert bridge.save_report("http://127.0.0.1:8765/api/export") == chosen["path"]
