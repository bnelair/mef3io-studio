#!/usr/bin/env bash
# Wrap the onedir build (dist/mef3io-studio) into
# dist/mef3io-studio-<version>-linux-x86_64.AppImage.
# appimagetool and the AppImage runtime are pinned and verified by SHA-256,
# and the runtime is passed explicitly so appimagetool never downloads one.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
TOOLS="${TOOLS_DIR:-$ROOT/build/tools}"
APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage"
APPIMAGETOOL_SHA256="ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"
RUNTIME_URL="https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64"
RUNTIME_SHA256="2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d"

fetch() {  # url sha256 dest
  if [ ! -f "$3" ] || ! echo "$2  $3" | sha256sum -c --status; then
    curl -fsSL -o "$3" "$1"
  fi
  echo "$2  $3" | sha256sum -c -
}
mkdir -p "$TOOLS"
fetch "$APPIMAGETOOL_URL" "$APPIMAGETOOL_SHA256" "$TOOLS/appimagetool"
fetch "$RUNTIME_URL" "$RUNTIME_SHA256" "$TOOLS/runtime-x86_64"
chmod +x "$TOOLS/appimagetool"

test -x "$ROOT/dist/mef3io-studio/mef3io-studio"
APPDIR="$ROOT/build/AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/lib" "$APPDIR/usr/share/applications" \
         "$APPDIR/usr/share/icons/hicolor/256x256/apps"
cp -a "$ROOT/dist/mef3io-studio" "$APPDIR/usr/lib/"
cp "$ROOT/packaging/linux/AppRun" "$APPDIR/AppRun"
cp "$ROOT/packaging/linux/mef3io-studio.desktop" "$APPDIR/"
cp "$ROOT/packaging/linux/mef3io-studio.desktop" "$APPDIR/usr/share/applications/"
"${PYTHON:-python}" "$ROOT/scripts/render_icons.py" --png "$APPDIR/mef3io-studio.png"
cp "$APPDIR/mef3io-studio.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/"
# --appimage-extract-and-run: CI runners have no FUSE.
ARCH=x86_64 "$TOOLS/appimagetool" --appimage-extract-and-run --no-appstream \
    --runtime-file "$TOOLS/runtime-x86_64" \
    "$APPDIR" "$ROOT/dist/mef3io-studio-${VERSION}-linux-x86_64.AppImage"
echo "built dist/mef3io-studio-${VERSION}-linux-x86_64.AppImage"
