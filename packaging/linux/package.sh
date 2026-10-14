#!/usr/bin/env bash
# Package the PyInstaller onedir build (dist/mef3io-studio) for Linux:
#   dist/mef3io-studio-<version>-linux-x86_64.tar.gz
#   dist/mef3io-studio-<version>-linux-x86_64.AppImage
# Needs APPIMAGETOOL pointing at an appimagetool binary for the AppImage.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
ARCH="$(uname -m)"
BASE="mef3io-studio-${VERSION}-linux-${ARCH}"
cd "$ROOT/dist"
test -x mef3io-studio/mef3io-studio

tar -czf "${BASE}.tar.gz" mef3io-studio
echo "built dist/${BASE}.tar.gz"

if [ -n "${APPIMAGETOOL:-}" ]; then
  APPDIR="$ROOT/build/AppDir"
  rm -rf "$APPDIR"
  mkdir -p "$APPDIR/usr/lib" "$APPDIR/usr/share/applications" \
           "$APPDIR/usr/share/icons/hicolor/256x256/apps"
  cp -a mef3io-studio "$APPDIR/usr/lib/"
  cp "$ROOT/packaging/linux/AppRun" "$APPDIR/AppRun"
  cp "$ROOT/packaging/linux/mef3io-studio.desktop" "$APPDIR/"
  cp "$ROOT/packaging/linux/mef3io-studio.desktop" "$APPDIR/usr/share/applications/"
  python "$ROOT/scripts/render_icons.py" --png "$APPDIR/mef3io-studio.png"
  cp "$APPDIR/mef3io-studio.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/"
  # --appimage-extract-and-run: CI runners have no FUSE.
  ARCH="$ARCH" "$APPIMAGETOOL" --appimage-extract-and-run --no-appstream \
      "$APPDIR" "${BASE}.AppImage"
  echo "built dist/${BASE}.AppImage"
fi
