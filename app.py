"""WeldAudit.exe — what runs when an auditor double-clicks the program.

With no arguments it opens in a window of its own: a Windows application with
a title bar and a taskbar button, not a browser tab. The interface is still a
web page, served on localhost and shown in the Edge WebView2 runtime that
ships with Windows 10 and 11 — but that is a build detail, not something the
person using it should have to know or manage.

With arguments it is the command line, so the one file is both the app and the
tooling:

    WeldAudit.exe                              the desktop UI
    WeldAudit.exe audit "D:\\Jobs\\Kestrel 8"       the same audit, scripted
    WeldAudit.exe vision "Kestrel 8" --kind mtr

Nothing here is installed. The program is one file, the database and the
Python it needs live under the user's own profile, and there is no service, no
registry entry and no administrator prompt — which matters, because the
machines this runs on are managed and their users are not administrators.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
import webbrowser

HOST = "127.0.0.1"
PORT = 8765


def _already_running(host: str, port: int) -> bool:
    """Whether something is serving here already.

    Double-clicking a program twice is normal behaviour, and the second one
    failing with a stack trace about a socket is not a useful answer. If the
    port is taken, the friendly assumption is that it is taken by us.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        return probe.connect_ex((host, port)) == 0


def _wait_until_serving(server, wait_for: float = 30.0) -> bool:
    """Block until *our* server has started.

    Asking uvicorn rather than probing the port, because the port cannot tell
    our new server from a previous one still shutting down. Relaunching
    quickly after a close then showed a window against a socket that was about
    to disappear, and the app came up blank.
    """
    deadline = time.monotonic() + wait_for
    while time.monotonic() < deadline:
        if getattr(server, "started", False):
            return True
        if getattr(server, "should_exit", False):
            return False        # it gave up, usually because the port is taken
        time.sleep(0.2)
    return False


def _open_when_ready(url: str, wait_for: float = 15.0) -> None:
    """Open the browser once the server answers, not before.

    Opening it immediately shows a connection error for the second or two the
    server takes to bind, and the first thing an auditor sees should not be a
    failure page.
    """
    deadline = time.monotonic() + wait_for
    while time.monotonic() < deadline:
        if _already_running(HOST, PORT):
            webbrowser.open(url)
            return
        time.sleep(0.25)


def _log_path():
    import tempfile
    from pathlib import Path

    return Path(tempfile.gettempdir()) / "weldaudit-start.log"


def _say(message: str) -> None:
    """Print, and always write it down.

    A windowed program has no console, so when it fails to start on somebody
    else's machine there is otherwise nothing whatever to go on. This file is
    the only account of what happened between double-click and window.
    """
    try:
        print(message, flush=True)
    except Exception:                     # noqa: BLE001 - no stream to write to
        pass
    try:
        with _log_path().open("a", encoding="utf-8") as log:
            log.write(f"{time.strftime('%H:%M:%S')} {message}\n")
    except Exception:                     # noqa: BLE001 - nowhere to write
        pass


def _speak_utf8() -> None:
    """Print in UTF-8 whatever code page the console was started in.

    A frozen build inherits the Windows console's legacy code page, usually
    cp1252, and every em-dash and accented mill name in the output becomes a
    replacement character. The findings quote company names off certificates —
    Soluções, OÑATI — so this is not only cosmetic.
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass            # a redirected or unusual stream; leave it alone


def _borrow_the_console() -> None:
    """Print into the terminal that launched us, if there was one.

    The packaged build is a windowed program, so double-clicking it does not
    flash up a console. Windows gives such a program no stdout at all, which
    would silently break every command-line use — `WeldAudit.exe audit ...`
    would run an audit and print nothing. Attaching to the parent console
    gives those back their output without giving the window a console.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        import ctypes

        if ctypes.windll.kernel32.AttachConsole(-1):
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace")
    except Exception:                     # noqa: BLE001 - no console to attach
        pass

    # Still nothing, because it was double-clicked rather than run from a
    # terminal. Something has to be there: uvicorn configures logging with
    # StreamHandlers on these two, and a handler over None raises before any
    # of this file's own error handling can see it. The symptom is a program
    # that starts, writes one line, opens no window and exits — which is how
    # this was found. The log file is a better destination than os.devnull
    # anyway, since it is the only record such a machine will have.
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            try:
                setattr(sys, name, _log_path().open("a", encoding="utf-8",
                                                    errors="replace"))
            except Exception:             # noqa: BLE001 - read-only temp
                import os
                setattr(sys, name, open(os.devnull, "w"))


def _complain(message: str) -> None:
    """Say something the user can actually see, with no console to print to."""
    _say(message)
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "WeldAudit", 0x10)
    except Exception:                     # noqa: BLE001 - not Windows
        pass


def main() -> int:
    _borrow_the_console()
    _speak_utf8()
    argv = sys.argv[1:]
    if argv:
        from weldaudit.cli import main as cli_main

        return cli_main(argv)

    url = f"http://{HOST}:{PORT}/"
    if _already_running(HOST, PORT):
        _say(f"WeldAudit is already open at {url}")
        webbrowser.open(url)
        return 0

    import uvicorn

    from weldaudit.api import create_app
    from weldaudit.pipeline import default_db_path

    _say(f"WeldAudit is starting at {url}")
    server = uvicorn.Server(uvicorn.Config(
        create_app(default_db_path()), host=HOST, port=PORT, log_level="warning"))

    def serve() -> None:
        # Whatever goes wrong in here goes wrong out of sight: it is a daemon
        # thread in a program with no console. Without this the window opens
        # against a server that never was, and there is nothing to read.
        try:
            server.run()
        except BaseException as why:      # noqa: BLE001 - report anything
            import traceback
            _say(f"server thread died: {why!r}")
            _say(traceback.format_exc(limit=6))
            server.should_exit = True

    threading.Thread(target=serve, daemon=True).start()

    if _open_window(url, server):
        # The window has been closed, which is how this program is quit.
        server.should_exit = True
        return 0

    # No native window available, so fall back to the browser and keep the
    # server in the foreground — the console is then the only way to stop it.
    _say("Opening in your browser instead. Close this window to stop.")
    threading.Thread(target=_open_when_ready, args=(url,), daemon=True).start()
    try:
        while not server.should_exit:
            time.sleep(0.5)
    except KeyboardInterrupt:
        server.should_exit = True
    return 0


class _Windows:
    """The few things the page needs Windows itself to do.

    A web page cannot open a folder dialog — browsers forbid it, and for good
    reason on the open web. Inside this window that restriction buys nothing
    and costs the user the dialog they know: their drives, their mapped
    network shares, the folders they opened recently. So the window offers
    one, and the page calls it as ``window.pywebview.api.pick_folder()``.

    Saving a report is the same shape of problem. "Download" works in a
    browser because the browser has a downloads folder and a bar to show it
    in; a WebView2 window has neither, so pointing it at an attachment URL
    did nothing at all — no file, no error, no dialog. The window therefore
    fetches the report itself and offers the Save As box instead.

    The server-side folder list, and a plain navigation to the export URL,
    stay as the fallbacks for when the interface is opened in a browser.
    """

    def save_report(self, url: str) -> str:
        """Fetch a report and save it where the user says. "" if cancelled.

        The name is taken from what the server called it rather than made up
        here, so the file is named the same whichever way it was saved.

        **The extension is put back if the user drops it.** The dialog hands
        back the name exactly as typed, and an auditor renaming the file to
        something useful -- "16 PW - PUNCHLIST" -- loses the ".xlsx" with it.
        The bytes are a perfectly good workbook; Windows just has nothing to
        open a file with no extension, so it offers to find an app in the
        Store. That looked from the far end like the download being broken,
        and the report had in fact saved correctly every time.
        """
        import urllib.parse
        import urllib.request
        from pathlib import Path

        import webview

        with urllib.request.urlopen(url, timeout=120) as answer:  # noqa: S310
            body = answer.read()
            said = answer.headers.get("content-disposition", "")
        suggested = ""
        if "filename*=utf-8''" in said:
            suggested = urllib.parse.unquote(said.split("filename*=utf-8''")[1].strip('"'))
        elif "filename=" in said:
            suggested = said.split("filename=")[1].strip('"')

        windows = webview.windows
        dialog = getattr(webview, "SAVE_DIALOG", None)
        if dialog is None:                    # newer pywebview renamed these
            dialog = webview.FileDialog.SAVE
        wanted = Path(suggested).suffix       # '.xlsx' or '.csv'
        kinds = (f"WeldAudit report (*{wanted})", "All files (*.*)") if wanted else ()
        try:
            chosen = windows[0].create_file_dialog(
                dialog, save_filename=suggested, file_types=kinds)
        except (TypeError, ValueError):
            # Some backends reject file_types on a save dialog. The filter is
            # a convenience; the extension is put back below regardless.
            chosen = windows[0].create_file_dialog(dialog, save_filename=suggested)
        if not chosen:
            return ""
        where = Path(chosen[0] if isinstance(chosen, (list, tuple)) else str(chosen))
        if wanted and where.suffix.lower() != wanted.lower():
            # Appending, not replacing -- "16 PW - REV 1.2" has a suffix of
            # ".2", and replacing it would eat part of the name the auditor
            # chose. The one exception is a name already ending in one of the
            # *other* report formats, which is a mistake rather than part of
            # the name: a CSV called .xlsx makes Excel refuse to open it.
            if where.suffix.lower() in {".xlsx", ".csv", ".pdf"}:
                where = where.with_suffix(wanted)
            else:
                where = where.with_name(where.name + wanted)
        where.write_bytes(body)
        return str(where)

    def reveal(self, path: str) -> bool:
        """Show a saved file in Explorer, selected.

        Not "open the file" — opening it needs a program registered for the
        extension, and the report of this going wrong was a machine that had
        Excel but had lost the association, so Windows offered the Store
        instead of a spreadsheet. Explorer needs no association, and seeing
        the file sitting there is what tells an auditor the report exists.
        """
        from pathlib import Path

        target = Path(path)
        if not target.exists():
            return False
        try:
            # /select, takes the path as one argument and Explorer is fussy
            # about quoting, so it is passed as a single pre-joined string.
            subprocess.Popen(f'explorer /select,"{target}"')  # noqa: S603,S607
        except OSError as why:
            _say(f"could not reveal {target}: {why!r}")
            return False
        return True

    def pick_files(self, start: str = "") -> list[str]:
        """Files the user chose, or [] if they cancelled. Multi-select.

        The Windows folder browser shows no files at all, so somebody handed a
        package cannot see what is in it, and somebody holding one document
        has nothing to point at. This is the other door.

        The paths come back in full, which is the point: a certificate picked
        out of ``BOOK\\7 MTRS`` is still under section 7, so selecting every
        file in a package gives the same audit as selecting the package.
        """
        import webview

        dialog = getattr(webview, "OPEN_DIALOG", None)
        if dialog is None:                    # newer pywebview renamed these
            dialog = webview.FileDialog.OPEN
        kinds = ("Documents (*.pdf;*.xlsx;*.xls;*.xlsm;*.csv;*.docx;*.doc;*.dwg;*.txt)",
                 "All files (*.*)")
        try:
            chosen = webview.windows[0].create_file_dialog(
                dialog, directory=start or "", allow_multiple=True, file_types=kinds)
        except (TypeError, ValueError):
            # A backend that will not take the filter still has to open.
            chosen = webview.windows[0].create_file_dialog(
                dialog, directory=start or "", allow_multiple=True)
        except Exception as why:              # noqa: BLE001 - report, don't die
            _say(f"file dialog failed: {why!r}")
            raise
        if not chosen:
            return []
        return [str(c) for c in chosen] if isinstance(chosen, (list, tuple)) else [str(chosen)]

    def pick_folder(self, start: str = "") -> str:
        """The folder the user chose, or "" if they cancelled."""
        import webview

        try:
            windows = webview.windows
            chosen = windows[0].create_file_dialog(
                webview.FOLDER_DIALOG, directory=start or "")
        except Exception as why:              # noqa: BLE001 - report, don't die
            _say(f"folder dialog failed: {why!r}")
            raise
        if not chosen:
            return ""
        return chosen[0] if isinstance(chosen, (list, tuple)) else str(chosen)


def _open_window(url: str, server) -> bool:
    """Show WeldAudit in a Windows window. False if this machine cannot.

    The interface is a web page, but an auditor should not have to find it in
    a browser tab among thirty others, and closing a tab should not leave a
    server running behind it. This puts the same page in a real window with a
    title bar and a taskbar button, using the Edge WebView2 runtime that ships
    with Windows 10 and 11 — so it is a desktop application to use and a local
    web app only in how it is built.

    Blocks until the window is closed, which is the signal to shut down.
    """
    if not _wait_until_serving(server):
        _complain("WeldAudit could not start its local server.")
        return False
    try:
        import webview
    except ImportError:
        return False                    # built without it; the browser will do

    try:
        webview.create_window("WeldAudit", url, width=1280, height=860,
                              min_size=(900, 600), confirm_close=False,
                              js_api=_Windows())
        # gui="edgechromium" is the WebView2 backend. Named rather than left
        # to autodetect, because the fallback pywebview reaches for otherwise
        # is the old MSHTML control, which renders this page as a blank frame
        # and looks like the app is broken rather than the renderer being old.
        webview.start(gui="edgechromium")
        return True
    except Exception as why:              # noqa: BLE001 - a missing runtime
        _say(f"Could not open a window ({why.__class__.__name__}).")
        return False


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        # A windowed program that raises just vanishes. Anyone it vanishes on
        # gets a message box and a file to send back.
        import traceback

        _say(traceback.format_exc())
        _complain(f"WeldAudit could not start.\n\nDetails: {_log_path()}")
        raise SystemExit(1)
