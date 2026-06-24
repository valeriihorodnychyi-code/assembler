# Captions Studio

An internal tool for the WF motion team: design subtitle styles live in the browser,
then render fast with the ffmpeg engine. One unified pipeline (the successor to
`magic_en.py` + `magic_loc.py`), wrapped in a UI so no one touches the terminal.

## What's inside

```
captions_studio/
├─ engine/                 # the unified render pipeline (no UI)
│  ├─ ffmpeg_utils.py       # ffprobe + platform-aware codec (videotoolbox on Mac)
│  ├─ styles.py             # style schema, load/save, defaults
│  ├─ subtitles.py          # karaoke event building + Pillow PNG rendering
│  ├─ transcribe.py         # Whisper (local) + ElevenLabs Scribe (cloud)
│  └─ compose.py            # subtitles → hook → body concat, multi-format
├─ server/app.py           # FastAPI local server (transcribe / render / styles)
├─ web/index.html          # the browser UI (live preview + design editor)
├─ styles/                 # *.json style presets (editor-compatible)
├─ fonts/                  # drop your .ttf/.otf here
├─ requirements.txt
└─ run.command             # double-click launcher (macOS)
```

## Run it (dev / first version)

Requires **Python 3.9+** and **ffmpeg** on PATH (`brew install ffmpeg`).

Easiest: double-click **`run.command`** in Finder. First launch creates a virtual
environment and installs dependencies (one-time), then opens the browser.

Manual:
```bash
cd captions_studio
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python3 -m server.app          # opens http://127.0.0.1:8765
```

### ElevenLabs Scribe (optional, fast transcription)
```bash
export ELEVENLABS_API_KEY="sk_..."   # then start the app
```
Without a key the app still works fully on local Whisper. **Do not hardcode the key
in source** — the old `dubber.py` had it inline; here it comes from the environment.

## How it works

- **Design** happens in the browser canvas (instant, no render).
- **Transcribe** uploads the video to the local server → Whisper or Scribe returns
  word-level timecodes → the preview switches to real timing.
- **Render** sends the style JSON + timecodes to the engine, which generates the
  subtitle PNGs (Pillow) and burns them with **ffmpeg**. On Apple Silicon it uses
  `h264_videotoolbox` (hardware) — the same "renders in seconds" path you had.
  On non-Mac it falls back to `libx264` automatically.

The browser preview and the engine read the **same style JSON**, so what you see is
what you export. The exported JSON is drop-in compatible with `styles/*.json`.

## Hook / body different styles
Toggle **"Different style for body"**, set a split time, and edit the Hook and Body
styles separately. Both are rendered in a single ffmpeg pass (no cut-and-stitch).

## Notes / next steps
- Body clips: enable **"Append body clip"** and place `body.mp4` (or per-format
  `body_9x16.mp4`, `body_1x1.mp4`, `body_16x9.mp4`) next to the app or in the
  session temp dir.
- Packaging into a single double-click `.app` (PyInstaller/py2app, bundling ffmpeg)
  is the planned final step so teammates install nothing.
- Cloud/VPS deployment later reuses this exact server with a different codec + auth.
