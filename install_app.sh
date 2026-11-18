#!/bin/bash
# Install (or update) the app built by build_mac_app.sh at one fixed place, /Applications, and start it.
#
#   ./install_app.sh
#
# Quits the running copy first (it stops cleanly: a backup in progress carries on next time). Keeping one path and,
# with ./make_signing_identity.sh, one signing identity means macOS keeps the app's permissions across updates.
set -euo pipefail
cd "$(dirname "$0")"
SRC="dist/Colour Project Manager.app"
DEST="/Applications/Colour Project Manager.app"
INFO="$HOME/Library/Application Support/resolve-cloud-backup/service.json"

[ -d "$SRC" ] || { echo "Build it first: ./build_mac_app.sh" >&2; exit 1; }

pid=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['pid'])" "$INFO" 2>/dev/null || true)
if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
  echo "Quitting the running copy (pid $pid)"
  osascript -e 'tell application id "com.resolvecloudbackup.app" to quit' || true
  for _ in $(seq 1 60); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
  if kill -0 "$pid" 2>/dev/null; then
    echo "It didn't quit within a minute; quit it from its menu bar icon and run this again." >&2
    exit 1
  fi
fi

rm -rf "$DEST"
ditto "$SRC" "$DEST"
open "$DEST"
echo "Installed and started: $DEST"
