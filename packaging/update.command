#!/bin/bash
# Force-update Assembler to the latest version and reopen it.
# Use this if "Quit + reopen" isn't pulling the newest version.
# Double-click it. (First time: right-click → Open → Open, to get past Gatekeeper.)

REPO_ZIP="https://github.com/valeriihorodnychyi-code/assembler/archive/refs/heads/main.zip"
CODE="$HOME/.assembler/code"

# Force a UTF-8 locale so unzip/ditto never hit "Illegal byte sequence" on any
# non-ASCII filename that might be in the archive.
export LANG="en_US.UTF-8"
export LC_ALL="en_US.UTF-8"

echo "▸ Stopping any running Assembler…"
pkill -f "Assembler.app"      2>/dev/null
pkill -f ".assembler/code"    2>/dev/null
pkill -f "server.app"         2>/dev/null
# free the local port the app uses
if command -v lsof >/dev/null 2>&1; then
  lsof -ti tcp:8765 2>/dev/null | xargs kill -9 2>/dev/null
fi
sleep 1

echo "▸ Downloading the latest code…"
TMP="$(mktemp -d)"
if ! curl -fsSL "$REPO_ZIP" -o "$TMP/code.zip"; then
  echo "✗ Could not download the update (check your internet) — your current version is untouched."
  rm -rf "$TMP"; read -n 1 -s -r -p "Press any key to close."; exit 1
fi
# Prefer macOS-native `ditto` (handles unicode filenames cleanly); fall back to unzip.
mkdir -p "$TMP/x"
if command -v ditto >/dev/null 2>&1; then
  ditto -x -k "$TMP/code.zip" "$TMP/x" 2>/dev/null || unzip -q -o "$TMP/code.zip" -d "$TMP/x"
else
  unzip -q -o "$TMP/code.zip" -d "$TMP/x"
fi

# Find the real code root (the folder that contains server/app.py)
SRC="$(dirname "$(find "$TMP/x" -maxdepth 3 -path '*/server/app.py' | head -1)")"
if [ -z "$SRC" ] || [ ! -f "$SRC/server/app.py" ]; then
  echo "✗ The downloaded package looked wrong — your current version is untouched."
  rm -rf "$TMP"; read -n 1 -s -r -p "Press any key to close."; exit 1
fi

echo "▸ Installing…"
mkdir -p "$HOME/.assembler"
rm -rf "$CODE.prev"
[ -d "$CODE" ] && mv "$CODE" "$CODE.prev"     # keep a rollback copy
cp -R "$SRC" "$CODE"
rm -rf "$TMP"

VER="$(/usr/bin/python3 -c "import json;print(json.load(open('$CODE/version.json'))['version'])" 2>/dev/null || echo '?')"
echo "✓ Updated to v$VER"

echo "▸ Reopening Assembler…"
open -a Assembler 2>/dev/null || open "/Applications/Assembler.app" 2>/dev/null || \
  echo "  (Couldn't auto-open — launch Assembler manually.)"

echo ""
echo "Done. Assembler is now v$VER. You can close this window."
