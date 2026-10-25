#!/bin/bash
# Build "Colour Project Manager.app", a standalone menu bar app. Run this ON A MAC (apps can't be built cross-platform).
#
#   ./build_mac_app.sh
#
# Needs Python 3.9+ (Xcode command line tools provide one: run `xcode-select --install`) and internet.
# Result: dist/Colour Project Manager.app and dist/Colour-Project-Manager.zip. Nothing is installed system-wide.
set -euo pipefail
cd "$(dirname "$0")"

if [ "$(uname)" != "Darwin" ]; then echo "This builds the macOS app and must run on a Mac." >&2; exit 1; fi

python3 -m venv .build-venv
# shellcheck disable=SC1091
source .build-venv/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet pyinstaller rumps certifi

# App icon: a green dot, drawn with the standard library (same drawing the menu bar uses).
rm -rf .icon.iconset && mkdir .icon.iconset
python - <<'PY'
import menubar
for base in (16, 32, 128, 256, 512):  # the names iconutil expects
    open(f".icon.iconset/icon_{base}x{base}.png", "wb").write(menubar.icon_png(menubar.COLOURS["ok"], base))
    open(f".icon.iconset/icon_{base}x{base}@2x.png", "wb").write(menubar.icon_png(menubar.COLOURS["ok"], base * 2))
PY
iconutil -c icns .icon.iconset -o .icon.icns || echo "(icon skipped)"

ICON=()
[ -f .icon.icns ] && ICON=(--icon .icon.icns)

pyinstaller --noconfirm --clean --windowed --name "Colour Project Manager" \
  --osx-bundle-identifier com.resolvecloudbackup.app \
  --add-data "dashboard.html:." \
  --hidden-import rumps --hidden-import AppKit --hidden-import Foundation --collect-data certifi \
  "${ICON[@]}" app.py

# Menu bar only: no Dock icon, no app-switcher entry.
APP="dist/Colour Project Manager.app"
python - "$APP/Contents/Info.plist" <<'PY'
import plistlib, sys
import resolve_cloud_backup as rcb
p = sys.argv[1]
d = plistlib.load(open(p, "rb"))
d.update({"LSUIElement": True, "CFBundleShortVersionString": rcb.VERSION, "CFBundleVersion": rcb.VERSION,
          "NSHumanReadableCopyright": "resolve-cloud-backup"})
plistlib.dump(d, open(p, "wb"))
PY

# Ad-hoc signature so macOS will run it on this Mac (it is not notarised).
codesign --force --deep --sign - "$APP"
ditto -c -k --keepParent "$APP" "dist/Colour-Project-Manager.zip"

echo
echo "Built: $APP"
echo "Drag it to /Applications and open it. If macOS says it can't verify the developer, right-click > Open once."
