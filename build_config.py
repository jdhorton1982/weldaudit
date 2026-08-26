"""What both PyInstaller builds need, in one place.

WeldAudit ships two ways and they must contain exactly the same program:

    WeldAudit.spec          one file        dist/WeldAudit.exe
    WeldAudit-folder.spec   one folder      dist/WeldAudit/

The lists below are the reason this module exists rather than a second copy of
the spec. ``hidden`` ends with every rule module, because each one registers
itself on import and the bundler cannot see that by following imports — one
missing is one check that silently never runs, and a report that is short
rather than wrong. Two specs drifting apart would mean the folder build and
the one-file build quietly auditing to different standards.
"""

import pathlib

from PyInstaller.utils.hooks import collect_data_files

ICON = "weldaudit.ico"


def version_info():
    """The Windows version resource, built from the program's own number.

    Without this the exe has no version at all in its properties: the Details
    tab is blank, and so is anything that reads a file version to decide what
    it is looking at - an inventory of what is installed on a managed laptop,
    an antivirus reputation lookup, a helpdesk asking which copy someone is
    running. The installer has carried a version since it was added, so the
    two artefacts of the same build disagreed about whether they had one.

    Composed here rather than kept in the version file PyInstaller normally
    reads, for the same reason `Build.bat` asks the program for its version
    rather than being told: a number written down in a second place is a
    number that eventually disagrees with the first.
    """
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo,
        VarStruct, VSVersionInfo,
    )

    version = _version()
    # The binary fields want exactly four integers; the string fields show the
    # version as it is actually written.
    parts = tuple(int(p) for p in version.split(".")) + (0, 0, 0, 0)
    numbers = parts[:4]

    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=numbers, prodvers=numbers,
                          mask=0x3F, flags=0x0, OS=0x40004, fileType=0x1,
                          subtype=0x0, date=(0, 0)),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("CompanyName", "WeldAudit"),
                StringStruct("FileDescription",
                             "Turnover package auditing for pipeline construction"),
                StringStruct("FileVersion", version),
                StringStruct("InternalName", "WeldAudit"),
                StringStruct("OriginalFilename", "WeldAudit.exe"),
                StringStruct("ProductName", "WeldAudit"),
                StringStruct("ProductVersion", version),
            ])]),
            # 0x0409 US English, 1200 UTF-16. Named because the codepage here
            # and the "040904B0" above have to agree, and nothing checks it.
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ],
    )


def _version() -> str:
    """The program's version, asked of the program.

    Read out of the source rather than imported: a spec runs inside
    PyInstaller's own process, and importing the package there pulls in the
    whole dependency tree before the analysis that is meant to discover it.
    """
    import re

    text = pathlib.Path("weldaudit/__init__.py").read_text(encoding="utf-8")
    found = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not found:
        raise SystemExit("build_config: no __version__ in weldaudit/__init__.py")
    return found.group(1)


def datas():
    """Data files both builds carry."""
    # The OCR models and their character dictionary. Without these the build
    # succeeds, starts, and fails the moment anyone asks it to read a scan.
    ocr = collect_data_files("rapidocr_onnxruntime")

    # The approved materials list, carried inside the program so a machine
    # with no copy beside its jobs still runs the manufacturer-approval
    # checks — the ones that catch an unapproved mill, and the ones that were
    # silently skipped on a colleague's machine. The folder is gitignored
    # because it holds a customer document and this repository is public, so a
    # build made without it is perfectly valid: it just finds the list on disk
    # the way it always did.
    aml_dir = pathlib.Path("weldaudit/data")
    aml = [("weldaudit/data", "weldaudit/data")] if any(aml_dir.glob("*")) else []
    if not aml:
        print("NOTE: no weldaudit/data — building without a built-in approved list.")

    # The interface is one HTML file; api.py finds it through sys._MEIPASS.
    return [("weldaudit/web", "weldaudit/web"), *aml, *ocr]


#: Nothing here draws, plots or opens a window of its own. tkinter stays out:
#: pywebview only needs it on Linux, and on Windows it would add a toolkit
#: nothing here draws with.
EXCLUDES = ["matplotlib", "numpy.testing", "pytest", "IPython", "notebook"]

HIDDEN = [
    # uvicorn loads its protocol and lifespan implementations by name, so the
    # bundler cannot see them by following imports. Without these the build
    # completes cleanly and then fails to serve a single request.
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # Imported inside functions, so static analysis misses them too.
    "anthropic",
    "truststore",
    # The native window. pywebview picks its backend at runtime by importing
    # by name, so the bundler cannot see the one that matters.
    "webview",
    "webview.platforms.edgechromium",
    "clr_loader",
    "pythonnet",
    # Free OCR. The models are data files rather than imports, so they are
    # collected above; onnxruntime loads its providers by name at runtime.
    "rapidocr_onnxruntime",
    "rapidocr_onnxruntime.ch_ppocr_v3_det",
    "rapidocr_onnxruntime.ch_ppocr_v3_rec",
    "rapidocr_onnxruntime.ch_ppocr_v2_cls",
    "onnxruntime",
    "onnxruntime.capi._pybind_state",
    "pymupdf",
    "openpyxl",
    "xlsxwriter",
    "rapidfuzz",
    # Every rule module registers itself on import; one missing is one check
    # that silently never runs.
    "weldaudit.rules.asbuilt", "weldaudit.rules.backfill",
    "weldaudit.rules.coating", "weldaudit.rules.flanges",
    "weldaudit.rules.hydrotest", "weldaudit.rules.materials",
    "weldaudit.rules.nde_coverage", "weldaudit.rules.ndetech",
    "weldaudit.rules.registers", "weldaudit.rules.review",
    "weldaudit.rules.roster", "weldaudit.rules.scope",
    "weldaudit.rules.welders", "weldaudit.rules.weldtrace",
    "weldaudit.rules.wps",
]
