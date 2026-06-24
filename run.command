#!/bin/bash
# Captions Studio launcher — double-click on macOS.
cd "$(dirname "$0")"
ulimit -n 4096 2>/dev/null

# First run: create venv + install deps.
if [ ! -d "venv" ]; then
  echo "First run — setting up (this happens only once)…"
  python3 -m venv venv
  source venv/bin/activate
  pip install --upgrade pip >/dev/null
  pip install -r requirements.txt
else
  source venv/bin/activate
fi

# ElevenLabs API key is read from config.json in this folder — nothing to set here.
# (If you prefer, you can override it by uncommenting the next line instead.)
# export ELEVENLABS_API_KEY="sk_..."

echo ""
echo "Starting Captions Studio… your browser will open automatically."
python3 -m server.app

read -p "Press Enter to close…"
