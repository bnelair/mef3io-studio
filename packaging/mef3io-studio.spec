# PyInstaller spec for mef3io-studio (onedir; a .app bundle on macOS).
#
#   pyinstaller --noconfirm --clean packaging/mef3io-studio.spec
#
# Output: dist/mef3io-studio/ (Windows, Linux) or dist/mef3io-studio.app (macOS).
# Every build is smoke-tested with --self-test / --self-test-gui in
# .github/workflows/build.yml, and checked for files that must not ship.

import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).resolve().parent
sys.path.insert(0, str(ROOT / "packaging"))
from collect_licenses import collect  # noqa: E402

VERSION = (ROOT / "VERSION").read_text().strip()
NAME = "mef3io-studio"
LINUX = sys.platform.startswith("linux")

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
# QtSvg renders the icon set.
hiddenimports += ["PySide6.QtSvg"]

excludes = [
    "tkinter",
    # Build tools: nothing at run time imports them (they arrived through
    # PyInstaller hooks and would bring setuptools' vendored packages along).
    "setuptools",
    "pkg_resources",
    "_distutils_hack",
    # No TLS/network code path exists; keep the bundle free of it.
    "ssl",
    "_ssl",
    # GPL-licensed (GNU readline) and terminal-only: never in a GUI bundle.
    "readline",
    "curses",
    "_curses",
    "_curses_panel",
    # The app makes no network connections; QtNetwork must never ship.
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

# --------------------------------------------------------------------------
# Remove what must not ship. PyInstaller's PySide6 hook collects every Qt
# platform plugin; several of them are servers or kiosk back ends:
#   vnc            -> serves the window over the network, no authentication
#   eglfs/linuxfb/minimal(egl)/vkkhrdisplay -> full-screen/kiosk back ends
#   generic/qtuiotouch -> listens for TUIO touch events on a UDP port
# plus QtNetwork itself, the GTK3 platform theme (it drags in the whole GTK
# stack) and GNU readline (GPLv3). Only the display plugins the app is meant
# to use stay: xcb, wayland, offscreen (Linux); windows, offscreen (Windows);
# cocoa, offscreen (macOS).
# --------------------------------------------------------------------------
KEEP_PLATFORMS = {"xcb", "wayland", "wayland-egl", "offscreen", "windows", "cocoa"}
DROP_PATTERNS = [
    r"(^|/)egldeviceintegrations/",
    r"(^|/)generic/",
    r"(^|/)platformthemes/[^/]*gtk3",
    r"(^|/)wayland-graphics-integration-client/.*(vulkan|dmabuf|drm-egl)",
    r"Qt6?Network",
    r"QtNetwork",
    r"(^|/)libQt6EglFS",
    r"(^|/)libQt6EglFsKms",
    r"(^|/)libQt6WlShellIntegration",
    r"(^|/)wayland-shell-integration/lib(wl-shell|ivi-shell|fullscreen-shell)",
    r"(^|/)(lib)?readline",
    r"(^|/)libtinfo",
    r"(^|/)libncurses",
    r"(^|/)_?curses",
    # Windows: Mesa's software OpenGL (~20 MB). The app draws with Qt's raster
    # engine and never needs OpenGL, let alone a software fallback for it.
    r"(^|/)opengl32sw\.dll$",
]
if LINUX:
    # glibc's companions: every target system has a libstdc++/libgcc at least
    # as new as the build machine's (we build on the oldest supported one),
    # and a bundled copy older than the host's breaks GPU drivers.
    DROP_PATTERNS += [r"^libstdc\+\+\.so", r"^libgcc_s\.so"]
_drop = re.compile("|".join(DROP_PATTERNS))


def _platform_plugin(dest):
    m = re.search(r"(?:^|/)platforms/(?:lib)?q([\w-]+?)(?:\.dll|\.so|\.dylib)$", dest)
    return m.group(1) if m else None


def keep(entry):
    dest = entry[0].replace("\\", "/")
    plugin = _platform_plugin(dest)
    if plugin is not None and plugin not in KEEP_PLATFORMS:
        return False
    return not _drop.search(dest)


a.binaries = [e for e in a.binaries if keep(e)]
a.datas = [e for e in a.datas if keep(e)]

if LINUX:
    # Drop system libraries that only the removed plugins needed: keep a
    # top-level library only if something still in the bundle links to it.
    from PyInstaller.depend import bindepend

    def _needed(path):
        try:
            return {Path(n).name for n, _ in bindepend.get_imports(path)}
        except Exception:
            return set()

    top = {e[0]: e for e in a.binaries if "/" not in e[0]}
    needed = set()
    queue = [e[1] for e in a.binaries if "/" in e[0] or e[0].startswith("libpython")]
    seen = set()
    while queue:
        src = queue.pop()
        if src in seen:
            continue
        seen.add(src)
        for name in _needed(src):
            if name in top and name not in needed:
                needed.add(name)
                queue.append(top[name][1])
    a.binaries = [e for e in a.binaries if "/" in e[0] or e[0] in needed or e[0].startswith("libpython")]

# Licence texts for everything that ships (THIRD_PARTY_LICENSES/).
_licences = collect(Path(workpath) / "THIRD_PARTY_LICENSES", a.binaries)
a.datas += [
    (f"THIRD_PARTY_LICENSES/{p.relative_to(_licences).as_posix()}", str(p), "DATA")
    for p in _licences.rglob("*")
    if p.is_file()
]

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
    console=LINUX,
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
            # PySide6 6.12 and numpy 2.5 wheels are built for macOS 14+.
            "LSMinimumSystemVersion": "14.0",
        },
    )
