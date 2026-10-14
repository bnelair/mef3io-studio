# PyInstaller spec for mef3io-studio (onedir; a .app bundle on macOS).
#
#   pyinstaller --noconfirm --clean packaging/mef3io-studio.spec
#
# Output: dist/mef3io-studio/ (Windows, Linux) or dist/mef3io-studio.app (macOS).
# Every build is smoke-tested with `--self-test` in .github/workflows/build.yml.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).resolve().parent
VERSION = (ROOT / "VERSION").read_text().strip()
NAME = "mef3io-studio"

# mef3io: its native extension (_mef3io*.so / .pyd) and its submodules, some of
# which are imported lazily inside functions. Its dist metadata lets
# mef3io.__version__ report the bundled version in About.
hiddenimports = collect_submodules("mef3io") + collect_submodules("mef3io_studio")
binaries = collect_dynamic_libs("mef3io")
datas = [
    (str(ROOT / "VERSION"), "."),
    *collect_data_files("mef3io_studio", includes=["resources/**/*"]),
    *copy_metadata("mef3io"),
]
# Qt: QtSvg renders the icon set; the PySide6 hooks bring the platform plugins
# (including "offscreen", which --self-test uses) and the style plugins.
hiddenimports += ["PySide6.QtSvg"]

# Keep the bundle lean: nothing here uses these, and QtNetwork in particular
# must never ship (the app makes no network connections).
excludes = [
    "tkinter",
    "PySide6.QtNetwork",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.Qt3DCore",
    "PySide6.QtMultimedia",
    "PySide6.QtPdf",
]

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

icon = None
if sys.platform == "win32" and (ROOT / "packaging" / "windows" / "mef3io-studio.ico").exists():
    icon = str(ROOT / "packaging" / "windows" / "mef3io-studio.ico")
elif sys.platform == "darwin" and (ROOT / "packaging" / "macos" / "mef3io-studio.icns").exists():
    icon = str(ROOT / "packaging" / "macos" / "mef3io-studio.icns")

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    # Windows/macOS: a GUI app (no console window). --self-test still prints
    # through the parent's redirected stdout and can write a report file.
    console=sys.platform.startswith("linux"),
    disable_windowed_traceback=False,
    argv_emulation=False,
    # TODO(signing): Windows Authenticode and macOS Developer ID signing +
    # notarization go here / in build.yml once certificates are decided (#3).
    codesign_identity=None,
    entitlements_file=None,
    upx=False,
    icon=icon,
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=NAME)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{NAME}.app",
        icon=icon,
        bundle_identifier="org.bnelair.mef3io-studio",
        version=VERSION,
        info_plist={
            "CFBundleName": NAME,
            "CFBundleDisplayName": NAME,
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,  # follow light/dark mode
            "LSMinimumSystemVersion": "12.0",
        },
    )
