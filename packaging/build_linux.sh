#!/usr/bin/env bash
# Build a self-contained Linux desktop app (AppImage + .deb) with its own Python runtime.
#
#   packaging/build_linux.sh            -> desktop/dist/AI CAD-<version>.AppImage and ai-cad-desktop_<version>_amd64.deb
#
# The bundle contains: Electron + UI, a relocatable CPython 3.12 (python-build-standalone) with every
# CAD dependency installed, and the backend code. Nothing from this repository's .venv is used at runtime.
# Runtime system requirements: glibc 2.28+, libcairo2 (drawings/PDF), OpenGL for the 3D view.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
BUILD="$ROOT/build"
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20241016/cpython-3.12.7+20241016-x86_64-unknown-linux-gnu-install_only.tar.gz"
PY_SHA256="${PY_SHA256:-}"   # optional: pin the archive hash

mkdir -p "$BUILD/cache"
echo "== UI"
(cd web && npm ci --silent && npm run build)

echo "== Python runtime"
ARCHIVE="$BUILD/cache/python.tar.gz"
[ -f "$ARCHIVE" ] || curl -fL --retry 3 -o "$ARCHIVE" "$PY_URL"
if [ -n "$PY_SHA256" ]; then echo "$PY_SHA256  $ARCHIVE" | sha256sum -c -; fi
rm -rf "$BUILD/python" && mkdir -p "$BUILD/python"
tar -xzf "$ARCHIVE" -C "$BUILD/python" --strip-components=1
"$BUILD/python/bin/python3" -m pip install --quiet --no-warn-script-location --upgrade pip
"$BUILD/python/bin/python3" -m pip install --quiet --no-warn-script-location -r requirements.txt
# trim: tests, caches and headers are not needed at runtime
find "$BUILD/python" -name "__pycache__" -type d -prune -exec rm -rf {} +
rm -rf "$BUILD/python/include" "$BUILD/python/share/man"
find "$BUILD/python/lib/python3.12/site-packages" -type d \( -name tests -o -name test \) -prune -exec rm -rf {} + || true

echo "== Application code"
rm -rf "$BUILD/app" && mkdir -p "$BUILD/app/web"
cp -r cadcore server "$BUILD/app/"
cp -r web/dist "$BUILD/app/web/dist"
cp -r examples "$BUILD/app/examples"
find "$BUILD/app" -name "__pycache__" -type d -prune -exec rm -rf {} +
"$BUILD/python/bin/python3" -c "import sys; sys.path.insert(0, '$BUILD/app'); import server.app, cadcore.fea, cadcore.drawing; print('backend imports OK')"

echo "== Electron package (AppImage)"
(cd desktop && npm install --silent && unset ELECTRON_RUN_AS_NODE && npx electron-builder --linux AppImage --publish never)

# .deb with dpkg-deb + gzip: electron-builder's deb path runs 7za/xz over ~2 GB and needed >6 GB RAM,
# which triggered the kernel OOM killer on a 14 GB machine. This path needs only a few MB.
echo "== .deb (low-memory)"
VERSION=$(node -p "require('./desktop/package.json').version")
PKG="$BUILD/deb/ai-cad_${VERSION}_amd64"
rm -rf "$BUILD/deb" && mkdir -p "$PKG/DEBIAN" "$PKG/opt" "$PKG/usr/share/applications" "$PKG/usr/share/icons/hicolor/512x512/apps" "$PKG/usr/bin"
cp -a "desktop/dist/linux-unpacked" "$PKG/opt/ai-cad"
cp desktop/icon.png "$PKG/usr/share/icons/hicolor/512x512/apps/ai-cad.png"
ln -s /opt/ai-cad/ai-cad "$PKG/usr/bin/ai-cad"
cat > "$PKG/usr/share/applications/ai-cad.desktop" <<DESKTOP
[Desktop Entry]
Name=AI CAD
Comment=Parametric CAD with an AI design assistant
Exec=/opt/ai-cad/ai-cad %U
Icon=ai-cad
Type=Application
Categories=Graphics;Engineering;
DESKTOP
cat > "$PKG/DEBIAN/control" <<CONTROL
Package: ai-cad
Version: $VERSION
Architecture: amd64
Maintainer: AI CAD <noreply@example.com>
Depends: libcairo2, libgtk-3-0, libnss3, libxss1, libasound2t64 | libasound2
Description: Parametric CAD with an AI design assistant
CONTROL
nice -n 10 dpkg-deb --root-owner-group -Zgzip -z6 --build "$PKG" desktop/dist/ >/dev/null
ls -lh desktop/dist/*.AppImage desktop/dist/*.deb
