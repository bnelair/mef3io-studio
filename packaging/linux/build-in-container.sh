#!/usr/bin/env bash
# Build the Linux onedir app inside AlmaLinux 9 (glibc 2.34), so the result
# runs on RHEL/Rocky/Alma 9+, Ubuntu 22.04+, Debian 12+, Fedora 35+:
# a bundle only runs where glibc is at least as new as the build machine's.
# 2.34 is also the floor PySide6 6.12 itself requires (manylinux_2_34 wheels),
# so an older build machine would not help.
# Run as root inside an almalinux:9 container with the repo at $PWD.
# Produces dist/mef3io-studio-<version>-linux-x86_64.tar.gz (permissions kept).
set -euo pipefail
# Container images install packages without documentation ("nodocs"), which
# also drops many licence files; collect_licenses.py must copy those into the
# bundle, so turn it off and reinstall what the base image already had.
sed -i '/^tsflags=nodocs/d' /etc/dnf/dnf.conf
dnf -y -q install epel-release dnf-plugins-core && dnf config-manager --set-enabled crb
dnf -y -q install python3.12 python3.12-devel binutils tar gzip findutils \
  libxkbcommon libxkbcommon-x11 fontconfig freetype dbus-libs libX11 libX11-xcb \
  xcb-util xcb-util-wm xcb-util-image xcb-util-keysyms xcb-util-renderutil xcb-util-cursor \
  libglvnd-egl libglvnd-glx mesa-libGL libwayland-client libwayland-cursor
rpm -qf --qf '%{NAME}\n' /usr/lib64/*.so* 2>/dev/null | grep -v "not owned" | sort -u \
  | xargs dnf -y -q reinstall >/dev/null
python3.12 -m venv /tmp/venv
/tmp/venv/bin/python -m pip install -q --upgrade pip==25.2
/tmp/venv/bin/pip install -q --require-hashes -r requirements/build.lock
/tmp/venv/bin/pip install -q --no-deps --no-build-isolation .
/tmp/venv/bin/pyinstaller --noconfirm --clean packaging/mef3io-studio.spec
VERSION="$(tr -d '[:space:]' < VERSION)"
tar -C dist -czf "dist/mef3io-studio-${VERSION}-linux-x86_64.tar.gz" mef3io-studio
echo "built dist/mef3io-studio-${VERSION}-linux-x86_64.tar.gz"
