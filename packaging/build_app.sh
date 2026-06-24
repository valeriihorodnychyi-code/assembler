#!/bin/bash
# Build Assembler.app on a Mac (Apple Silicon / M-series).
# Run from anywhere:  bash packaging/build_app.sh
# Output: dist/Assembler.app
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
cd "$REPO"

echo "==> Repo: $REPO"

# --- 1. clean build venv with deps + pyinstaller -----------------------------
BUILD_VENV="$HERE/build/venv"
echo "==> Creating build venv"
rm -rf "$HERE/build"
mkdir -p "$HERE/build"
python3 -m venv "$BUILD_VENV"
source "$BUILD_VENV/bin/activate"
pip install --upgrade pip >/dev/null
pip install -r "$REPO/requirements.txt"
pip install pyinstaller

# --- 2. stage a clean code snapshot (what gets reloaded at runtime) ----------
STAGED="$HERE/build/code"
echo "==> Staging code snapshot -> $STAGED"
mkdir -p "$STAGED"
rsync -a \
  --exclude '.git' --exclude 'venv' --exclude 'packaging' \
  --exclude 'library' --exclude 'Heroes' --exclude 'design' \
  --exclude '__pycache__' --exclude '*.pyc' \
  --exclude 'config.json' --exclude 'license.json' \
  --exclude 'web/index_backup_*.html' \
  engine server web tools styles fonts version.json requirements.txt README.md \
  "$STAGED/"

# --- 3. ensure arm64 ffmpeg + ffprobe in packaging/vendor/bin ----------------
VENDOR_BIN="$HERE/vendor/bin"
mkdir -p "$VENDOR_BIN"
if [[ ! -x "$VENDOR_BIN/ffmpeg" || ! -x "$VENDOR_BIN/ffprobe" ]]; then
  echo "==> ffmpeg/ffprobe not found in vendor/bin."
  echo "    Trying to download native arm64 macOS builds…"
  # Native Apple-Silicon (arm64) static builds — no Rosetta needed on M-series.
  : "${FFMPEG_URL:=https://ffmpeg.martin-riedl.de/redirect/latest/macos/arm64/release/ffmpeg.zip}"
  : "${FFPROBE_URL:=https://ffmpeg.martin-riedl.de/redirect/latest/macos/arm64/release/ffprobe.zip}"
  TMP="$(mktemp -d)"
  if curl -fsSL "$FFMPEG_URL" -o "$TMP/ffmpeg.zip" && curl -fsSL "$FFPROBE_URL" -o "$TMP/ffprobe.zip"; then
    unzip -o "$TMP/ffmpeg.zip" -d "$VENDOR_BIN" >/dev/null
    unzip -o "$TMP/ffprobe.zip" -d "$VENDOR_BIN" >/dev/null
    chmod +x "$VENDOR_BIN/ffmpeg" "$VENDOR_BIN/ffprobe" || true
    rm -rf "$TMP"
    echo "    Downloaded arm64 ffmpeg/ffprobe."
    echo "    (If you ever need a specific build, drop ffmpeg/ffprobe into: $VENDOR_BIN)"
  else
    echo "!! Could not download ffmpeg automatically."
    echo "   Place arm64 'ffmpeg' and 'ffprobe' binaries in: $VENDOR_BIN"
    echo "   Then re-run this script."
    exit 1
  fi
fi
echo "==> ffmpeg arch: $(file "$VENDOR_BIN/ffmpeg" | sed 's/.*: //')"

# --- 4. build the .app -------------------------------------------------------
echo "==> Running PyInstaller"
pyinstaller "$HERE/Assembler.spec" --clean --noconfirm \
  --distpath "$REPO/dist" --workpath "$HERE/build/pyi"

APP="$REPO/dist/Assembler.app"
echo ""
echo "============================================================"
echo " Built: $APP"
echo "============================================================"
echo " It is UNSIGNED. On each colleague's Mac, first launch:"
echo "   right-click Assembler.app -> Open -> Open anyway"
echo " (or strip quarantine: xattr -dr com.apple.quarantine Assembler.app)"
echo ""
echo " To distribute: zip it ->"
echo "   ditto -c -k --keepParent \"$APP\" \"$REPO/dist/Assembler.zip\""
echo "============================================================"
