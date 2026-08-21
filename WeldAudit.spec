# PyInstaller build of WeldAudit into a single WeldAudit.exe.
#
#     Build.bat            (or: pyinstaller WeldAudit.spec --noconfirm)
#
# One file rather than an installer, deliberately. These run on managed
# the operator laptops where the user is not an administrator: an installer would
# need rights they do not have, while a single exe copied to the desktop needs
# none. Nothing is written outside the user's own profile.
#
# The cost of one file is that the whole 134 MB payload is unpacked into %TEMP%
# on every launch — twenty to a hundred seconds on a cold start, and the
# behaviour that antivirus heuristics read as a packer. WeldAudit-folder.spec
# builds the same program as a folder for machines where that matters.

import sys

sys.path.insert(0, SPECPATH)  # noqa: F821 - PyInstaller defines it

import build_config  # noqa: E402

block_cipher = None

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    datas=build_config.datas(),
    hiddenimports=build_config.HIDDEN,
    hookspath=[],
    runtime_hooks=[],
    excludes=build_config.EXCLUDES,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)  # noqa: F821

exe = EXE(  # noqa: F821
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
    icon=build_config.ICON,
)
