# PyInstaller build of WeldAudit as a FOLDER.
#
#     pyinstaller WeldAudit-folder.spec --noconfirm     ->  dist/WeldAudit/
#
# The same program as WeldAudit.spec, laid out differently. Both read their
# contents from build_config.py so the two cannot drift into auditing to
# different standards.
#
# Why a second shape. The one-file exe carries its whole 134 MB payload inside
# itself and unpacks it into %TEMP% on every launch. Two consequences, both
# felt:
#
#   * a cold start takes twenty to a hundred seconds, every time, because the
#     unpacking is redone on each run;
#   * a large unsigned binary that extracts itself to a temporary folder,
#     spawns a child process and opens a listening socket reads to antivirus
#     heuristics exactly like a packer. Norton flagged it.
#
# A folder build extracts nothing: the DLLs and data sit on disk next to the
# exe and are loaded normally. It starts in a second or two and presents none
# of the packer behaviour. The cost is that it is a folder to copy rather than
# one file, which for a program already distributed as a folder on a USB stick
# with its readings cache and approved list is no cost at all.
#
# Neither build needs an installer or administrator rights.

import sys

sys.path.insert(0, SPECPATH)  # noqa: F821 - PyInstaller defines it

import build_config  # noqa: E402

block_cipher = None

a = Analysis(  # noqa: F821
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

# exclude_binaries=True is what makes this a folder build: the binaries and
# data are left for COLLECT to place beside the exe instead of being sealed
# inside it.
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="WeldAudit",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=build_config.ICON,
)

COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="WeldAudit",
)
