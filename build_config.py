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
    "weldaudit.rules.welders", "weldaudit.rules.wps",
]
