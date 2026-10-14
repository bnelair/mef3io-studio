#!/usr/bin/env bash
# Wrap dist/mef3io-studio.app into dist/mef3io-studio-<version>-macos-<arch>.dmg
# (drag-to-Applications layout).
# TODO(signing): codesign --deep --options runtime with a Developer ID
# certificate, then notarize (xcrun notarytool) and staple, before and after
# building the dmg. Until then the app is only ad-hoc signed (PyInstaller does
# that on arm64) and Gatekeeper will ask the user to confirm the first launch.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
ARCH="$(uname -m)"
DMG="$ROOT/dist/mef3io-studio-${VERSION}-macos-${ARCH}.dmg"
STAGE="$ROOT/build/dmg"
rm -rf "$STAGE" "$DMG"
mkdir -p "$STAGE"
cp -R "$ROOT/dist/mef3io-studio.app" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "mef3io-studio ${VERSION}" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
echo "built $DMG"
