# Assembler — packaging & distribution

This turns the project into **Assembler.app**, a real Mac app a colleague can
double-click. Their Mac needs **no Python, no ffmpeg, no Homebrew** — everything
is inside the bundle.

How it's split:
- **Runtime (heavy, shipped once in the .app):** a bundled Python, all libraries,
  `ffmpeg`/`ffprobe`, and the Whisper engine.
- **Code (light, auto-updates):** `engine/ server/ web/ …`. On launch the app
  pulls the latest code from GitHub into `~/.assembler/code` — gated by a
  healthcheck, with automatic rollback. So you ship features without rebuilding
  the .app for everyone.
- **Whisper models** are NOT bundled (keeps the download light). On first launch
  the app quietly downloads `small` + `medium` in the background.

---

## A. One-time setup (you, on your Mac)

1. **Shared keys** — paste the team's API keys:
   ```
   cp packaging/preconfig.example.json packaging/preconfig.json
   # edit packaging/preconfig.json → real elevenlabs + heygen keys
   ```
   `preconfig.json` is git-ignored and gets baked into the .app; on first launch
   it's written to `~/.assembler/config.json`. (Keys are extractable from any
   bundle — if a build leaks, rotate the keys.)

2. **Update URL** — point the app at your GitHub repo. Edit `packaging/update.py`,
   set `UPDATE_URL`. For a **public** repo (recommended — no token needed):
   ```
   https://github.com/<owner>/<repo>/archive/refs/heads/main.zip
   ```
   Keys are never in the repo, so public is safe. For a **private** repo, use a
   release-asset URL and add a token header in `_download()` (see the comment).

3. **(Optional) native ffmpeg** — for best M-series speed, drop arm64
   `ffmpeg` and `ffprobe` into `packaging/vendor/bin/`. If absent, the build
   script downloads a static build automatically (may be x86_64 via Rosetta).

---

## B. Build

```
bash packaging/build_app.sh
```
Produces `dist/Assembler.app`. Zip it for sharing:
```
ditto -c -k --keepParent dist/Assembler.app dist/Assembler.zip
```
Put `Assembler.zip` on your shared Drive/Dropbox link.

> The app is **unsigned** (no Apple Developer account). That's fine — see install.

---

## C. Install on a colleague's clean Mac

1. Download `Assembler.zip`, unzip, drag **Assembler.app** to Applications.
2. **First launch only:** right-click **Assembler.app → Open → Open anyway**.
   (Because it's unsigned. After this once, it opens normally.)
   - Alternative: `xattr -dr com.apple.quarantine /Applications/Assembler.app`
3. The app starts the local server, opens the browser, and in the background
   downloads Whisper `small` + `medium` once (needs internet that first time).
4. Done. No Python / ffmpeg / brew involved.

What it works without internet: rendering, captions, assembly. What needs
internet: localization (ElevenLabs/HeyGen), code auto-update, first model
download.

---

## D. Publishing an update (you)

You never type git commands if you use **GitHub Desktop**:
1. Make changes in the project folder.
2. GitHub Desktop shows what changed → write a short summary → **Commit** → **Push**.
3. Bump `version.json` so you can see the new version land (shown in the UI badge).

Colleagues get it automatically on their next launch (fetch → healthcheck → swap).
If a bad build slips through, their app keeps the last good copy and rolls back.

---

## Shared library on Google Drive

The body/packshot/music library is just a folder. To share it across the team
with no backend:

1. On Google Drive, create a folder named **exactly `Assembler Library`**
   (in a Shared drive, or in someone's My Drive shared with the team).
2. Inside it, the app uses subfolders `bodies/`, `packshots/`, `music/`. Files
   dropped straight into those subfolders are auto-detected (no manual import).
3. Everyone installs **Google Drive for Desktop** and sets that folder to
   **"Available offline" / Mirror** (so ffmpeg reads instantly, not on-demand).

The app finds it automatically: on launch it scans for a folder named
`Assembler Library` under any synced Google Drive and points the library there.
If it can't find one, each person can paste the path in **Settings → Shared
library folder** (or it falls back to `~/Documents/Assembler/library`).

> The auto-detect name is set by `LIBRARY_FOLDER_NAME` in `launcher.py`. If you
> rename the Drive folder, change it there too.

## Files in this folder

- `launcher.py` — the only frozen entry point (paths, keys, update, models, start).
- `update.py` — fetch + healthcheck-gated swap + rollback. **Set `UPDATE_URL`.**
- `Assembler.spec` — PyInstaller recipe (arm64, bundles deps + ffmpeg + code baseline).
- `build_app.sh` — one command to build the .app.
- `preconfig.example.json` — template for the shared keys (copy → `preconfig.json`).
- `vendor/bin/` — (git-ignored) `ffmpeg` + `ffprobe` shipped in the bundle.

## Where things live at runtime (on each Mac)

- `~/.assembler/config.json` — keys (preconfigured on first run).
- `~/.assembler/code/` — the live, auto-updating app code.
- `~/.assembler/launch.log` — startup log (handy if something won't open).
- `~/Documents/Assembler/library/` — the body/packshot/music library (override
  with `library_dir` in config.json to point at a shared folder). Survives updates.
