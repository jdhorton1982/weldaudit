# PyInstaller build of WeldAudit into a single WeldAudit.exe.
#
#     Build.bat            (or: pyinstaller WeldAudit.spec --noconfirm)
#
# One file rather than an installer, deliberately. These run on managed
# the operator laptops where the user is not an administrator: an installer would
# need rights they do not have, while a single exe copied to the desktop needs
# none. Nothing is written outside the user's own profile.

from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

# The OCR models and their character dictionary. Without these the exe builds,
# starts, and fails the moment anyone asks it to read a scan.
ocr_data = collect_data_files("rapidocr_onnxruntime")

hidden = [
    # uvicorn loads its protocol and lifespan implementations by name, so the
    # bundler cannot see them by following imports. Without these the exe
    # builds cleanly and then fails to serve a single request.
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
    # collected below; onnxruntime loads its providers by name at runtime.
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

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    # The interface is one HTML file; api.py finds it through sys._MEIPASS.
    datas=[("weldaudit/web", "weldaudit/web"), *ocr_data],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    # Nothing here draws, plots or opens a window of its own.
    # tkinter stays out: pywebview only needs it on Linux, and on Windows it
    # would add a toolkit nothing here draws with.
    excludes=["matplotlib", "numpy.testing", "pytest", "IPython", "notebook"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="WeldAudit",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    # No console. WeldAudit opens in a Windows window of its own, and a black
    # box flashing up behind it is what makes a tool look like a script rather
    # than a program. Command-line use still prints: app.py attaches to the
    # terminal that launched it, and startup failures raise a message box
    # rather than disappearing.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # The program's own mark rather than PyInstaller's default. Same drawing
    # the toolbar shows, so the exe on the desktop and the window it opens
    # are recognisably one thing.
    icon="weldaudit.ico",
)
