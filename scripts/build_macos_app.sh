#!/usr/bin/env bash
# Build "Epic Compliance.app" and a distributable .dmg from this checkout.
#
# Usage: scripts/build_macos_app.sh
# Output: dist/Epic Compliance.app, dist/Epic Compliance-<version>.dmg
set -euo pipefail

cd "$(dirname "$0")/.."

BUILD_VENV=".venv-build"
VERSION="$(python3 -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")"

echo "==> Building Epic Compliance.app v$VERSION"

if [ ! -d "$BUILD_VENV" ]; then
  echo "==> Creating build virtualenv ($BUILD_VENV)"
  python3 -m venv "$BUILD_VENV"
fi
# shellcheck disable=SC1091
source "$BUILD_VENV/bin/activate"

echo "==> Installing project + packaging deps"
pip install -q -e ".[macapp]"

echo "==> Cleaning previous build output"
rm -rf build dist

echo "==> Running PyInstaller"
pyinstaller packaging/epic_compliance.spec --distpath dist --workpath build --noconfirm

APP="dist/Epic Compliance.app"
DMG="dist/Epic Compliance-${VERSION}.dmg"

echo "==> Packaging $DMG"
rm -f "$DMG"
hdiutil create -volname "Epic Compliance" -srcfolder "$APP" -ov -format UDZO "$DMG"

echo
echo "Done:"
echo "  $APP"
echo "  $DMG"
echo
echo "Distribute the .dmg. Since it isn't notarized/signed, first-run users"
echo "must right-click > Open (or allow it in System Settings > Privacy & Security)."
