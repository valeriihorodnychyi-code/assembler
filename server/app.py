"""Captions Studio — local FastAPI server.

Runs on the user's Mac. The browser handles design/preview; this server handles
the heavy lifting (transcription + the Pillow/ffmpeg render). Same code can later
be deployed to a VPS with no rewrite.

Endpoints:
    GET  /                       -> the web app
    GET  /api/info               -> encoder, engines, style list
    GET  /api/styles             -> list saved styles
    GET  /api/styles/{name}      -> one style
    POST /api/styles             -> save a style {name, style}
    POST /api/transcribe         -> upload video, get word timecodes + a file_id
    POST /api/render             -> render formats for a file_id, get download links
    GET  /download/{fid}/{name}  -> fetch a rendered file
"""
import os
import sys
import uuid
import shutil
import tempfile
from typing import Optional, List

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# allow "python -m server.app" and direct execution
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine import styles as st, compose, transcribe, ffmpeg_utils, localize, library, subtitles, textrules  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "web")


def app_version():
    try:
        import json as __j
        with open(os.path.join(ROOT, "version.json"), encoding="utf-8") as __f:
            return __j.load(__f).get("version", "dev")
    except Exception:
        return "dev"
WORK_ROOT = os.path.join(tempfile.gettempdir(), "captions_studio_work")
os.makedirs(WORK_ROOT, exist_ok=True)

# STYLES come in two tiers:
#   • OFFICIAL — curated presets shipped with the code (ROOT/styles), versioned & read-only.
#   • CUSTOM   — everyone's saved presets. These live NEXT TO the shared library folder
#                (<library>/_styles) so the whole team sees each other's styles the same way
#                they share bodies/packshots. If no shared library is set, they fall back to a
#                persistent per-machine folder (~/.assembler/styles) that survives updates.
# The UI merges both tiers (see styles.list_styles), so official + team customs both show up.
_ASSEMBLER_HOME = os.path.join(os.path.expanduser("~"), ".assembler")
os.environ.setdefault("CS_FONTS_DIR", os.path.join(ROOT, "fonts"))
st.FONTS_DIR = os.environ["CS_FONTS_DIR"]


def _seed_and_recover_styles():
    """Recover genuine custom presets from a previous code copy into the active custom dir.

    Only recovers user-made presets (skips names that are official, and never overwrites an
    existing file), so the shared folder isn't polluted with copies of shipped presets.
    """
    import glob as _glob, shutil as _sh
    dest = st.STYLES_DIR
    official = set(f[:-5] for f in os.listdir(st.OFFICIAL_STYLES_DIR)
                   if f.endswith(".json")) if os.path.isdir(st.OFFICIAL_STYLES_DIR) else set()
    try:
        os.makedirs(dest, exist_ok=True)
    except Exception:
        return
    # Also recover from the persistent local MIRROR (~/.assembler/styles). Every saved preset is
    # mirrored there (see post_style), so if the shared Drive folder gets wiped by corporate
    # retention/sync, the next launch re-seeds the customs from the local copy.
    sources = [os.path.join(_ASSEMBLER_HOME, "styles"),
               os.path.join(_ASSEMBLER_HOME, "code.prev", "styles"),
               os.path.join(_ASSEMBLER_HOME, "code", "styles")]
    for src in sources:
        if not os.path.isdir(src) or os.path.abspath(src) == os.path.abspath(dest):
            continue
        for f in _glob.glob(os.path.join(src, "*.json")):
            base = os.path.basename(f)
            if base[:-5] in official:      # official presets come via the merge, don't copy
                continue
            target = os.path.join(dest, base)
            if not os.path.exists(target):  # never overwrite a preset already there
                try:
                    _sh.copy2(f, target)
                except Exception:
                    pass


def _custom_styles_dir():
    """Where custom/team presets live. Next to a real shared library folder if one is set;
    otherwise a persistent per-machine folder (never inside the auto-updated code dir)."""
    lib = os.environ.get("CS_LIBRARY_DIR", "")
    in_code = os.path.abspath(lib).startswith(os.path.abspath(ROOT) + os.sep) if lib else True
    if lib and not in_code:
        return os.path.join(lib, "_styles")   # shared via the same Drive/Dropbox folder
    return os.path.join(_ASSEMBLER_HOME, "styles")


def _apply_styles_dirs():
    """(Re)point official + custom style dirs. Call after the library folder is resolved
    or changed at runtime, so styles always follow the shared library."""
    st.OFFICIAL_STYLES_DIR = os.path.join(ROOT, "styles")
    st.STYLES_DIR = _custom_styles_dir()
    os.environ["CS_OFFICIAL_STYLES_DIR"] = st.OFFICIAL_STYLES_DIR
    os.environ["CS_STYLES_DIR"] = st.STYLES_DIR
    _seed_and_recover_styles()
    _apply_caption_rules_path()


def _apply_caption_rules_path():
    """Keep the caption-rules file (keep-together / glue / widows) OUTSIDE the auto-updated
    code dir so custom phrases survive updates. Lives next to the shared library when one is
    set (so the whole team shares them), else in the persistent per-machine home. Seeded once
    from the shipped defaults so built-in rules aren't lost."""
    base = os.path.dirname(_custom_styles_dir())            # <library> or ~/.assembler
    dst = os.path.join(base, "caption_rules.json")
    shipped = os.path.join(ROOT, "caption_rules.json")
    try:
        os.makedirs(base, exist_ok=True)
        if not os.path.exists(dst):
            if os.path.exists(shipped):
                shutil.copy(shipped, dst)
        elif os.path.exists(shipped):
            # MERGE, don't just seed: rules shipped with the code (team-wide, edited on GitHub)
            # must reach everyone on update, while each machine keeps its own additions.
            # Local values win per key; new keys/entries from the code are added in.
            try:
                import json as _j
                ship = _j.load(open(shipped, encoding="utf-8"))
                local = _j.load(open(dst, encoding="utf-8"))

                def _merge(s, l):
                    if isinstance(s, dict) and isinstance(l, dict):
                        out = dict(s)
                        for k, v in l.items():
                            out[k] = _merge(s.get(k), v) if k in s else v
                        return out
                    if isinstance(s, list) and isinstance(l, list):
                        seen, out = set(), []
                        for x in s + l:                       # shipped first, then local extras
                            key = str(x).lower()
                            if key not in seen:
                                seen.add(key)
                                out.append(x)
                        return out
                    return l if l is not None else s

                merged = _merge(ship, local)
                if merged != local:
                    _j.dump(merged, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
                    print("[rules] merged shipped caption rules into the local file", flush=True)
            except Exception as e:
                print(f"[rules] merge skipped ({type(e).__name__}: {e})", flush=True)
        os.environ["CS_CAPTION_RULES"] = dst
    except OSError:
        pass  # fall back to the shipped path (subtitles._rules_path default)

# Library lives in a configurable folder (point CS_LIBRARY_DIR / config.json
# "library_dir" at a shared Drive/Dropbox folder to share it across the team).
os.environ.setdefault("CS_LIBRARY_DIR", os.path.join(ROOT, "library"))
if "://" in os.environ.get("CS_LIBRARY_DIR", ""):   # a web link can never be a library folder
    os.environ["CS_LIBRARY_DIR"] = os.path.join(ROOT, "library")
library.LIBRARY_DIR = os.environ["CS_LIBRARY_DIR"]

import json as _json
import uuid as _uuid, hashlib as _hashlib

# Per-machine key store, OUTSIDE the shared project folder (~/.assembler/config.json).
# This is the preferred place for API keys so they aren't sitting in a folder the
# whole team can copy. It WINS over the legacy shared config.json below.
_PCFG = os.path.join(os.path.expanduser("~"), ".assembler", "config.json")
if os.path.exists(_PCFG):
    try:
        _pc = _json.load(open(_PCFG, encoding="utf-8"))
        if _pc.get("elevenlabs_api_key"):
            os.environ.setdefault("ELEVENLABS_API_KEY", _pc["elevenlabs_api_key"])
        if _pc.get("heygen_api_key"):
            os.environ.setdefault("HEYGEN_API_KEY", _pc["heygen_api_key"])
        # persist the chosen library folder across restarts — but ignore a stored web link
        # (an older build accepted URLs and pointed the whole library at a bogus folder)
        _lib = (_pc.get("library_dir") or "").strip()
        if _lib and "://" not in _lib:
            os.environ["CS_LIBRARY_DIR"] = os.path.expanduser(_lib)
            library.LIBRARY_DIR = os.environ["CS_LIBRARY_DIR"]
    except Exception:
        pass


def _log_timing(op, secs, meta=""):
    """Append one timing row to ~/.assembler/timings.csv.

    On-screen timers vanish with the toast; this file is the durable record used to
    benchmark machines (local Mac vs a cloud worker) and to size render capacity.
    Columns: when, op, seconds, encoder, machine, notes
    """
    try:
        os.makedirs(_ASSEMBLER_HOME, exist_ok=True)
        p = os.path.join(_ASSEMBLER_HOME, "timings.csv")
        new = not os.path.exists(p)
        with open(p, "a", encoding="utf-8") as f:
            if new:
                f.write("timestamp,op,seconds,encoder,machine,notes\n")
            import datetime as _dt
            enc = ffmpeg_utils.video_encoder()
            note = str(meta).replace(",", ";").replace("\n", " ")[:300]
            f.write(f"{_dt.datetime.now().isoformat(timespec='seconds')},{op},{secs:.2f},{enc},{machine_id()},{note}\n")
    except Exception:
        pass


def machine_id():
    """Short stable per-laptop id (from the MAC). A soft licensing deterrent — not real DRM."""
    return _hashlib.sha256(str(_uuid.getnode()).encode()).hexdigest()[:12]


def license_ok():
    """Open by default. If license.json exists with a non-empty 'allowed' list, only those
    machine ids may use the app. Admin (Val) maintains the list."""
    p = os.path.join(ROOT, "license.json")
    if not os.path.exists(p):
        return True
    try:
        d = _json.load(open(p, encoding="utf-8"))
        al = d.get("allowed", []) if isinstance(d, dict) else d
        return (not al) or (machine_id() in al)
    except Exception:
        return True


def save_keys(eleven, heygen):
    os.makedirs(os.path.dirname(_PCFG), exist_ok=True)
    cur = {}
    if os.path.exists(_PCFG):
        try:
            cur = _json.load(open(_PCFG, encoding="utf-8"))
        except Exception:
            cur = {}
    if eleven:
        cur["elevenlabs_api_key"] = eleven
        os.environ["ELEVENLABS_API_KEY"] = eleven
    if heygen:
        cur["heygen_api_key"] = heygen
        os.environ["HEYGEN_API_KEY"] = heygen
    _json.dump(cur, open(_PCFG, "w", encoding="utf-8"), indent=2)


# Optional shared config.json (legacy fallback) so the team can set the key once in the
# project folder. Per-machine store above takes precedence.
_CFG = os.path.join(ROOT, "config.json")
if os.path.exists(_CFG):
    try:
        import json as _json
        _cfg = _json.load(open(_CFG, encoding="utf-8"))
        if _cfg.get("elevenlabs_api_key") and not os.environ.get("ELEVENLABS_API_KEY"):
            os.environ["ELEVENLABS_API_KEY"] = _cfg["elevenlabs_api_key"]
        if _cfg.get("heygen_api_key") and not os.environ.get("HEYGEN_API_KEY"):
            os.environ["HEYGEN_API_KEY"] = _cfg["heygen_api_key"]
        if _cfg.get("library_dir") and "://" not in _cfg["library_dir"] and not os.environ.get("CS_LIBRARY_DIR"):
            os.environ["CS_LIBRARY_DIR"] = os.path.expanduser(_cfg["library_dir"])
    except Exception:
        pass

# Library folder is now final → point styles next to it (or the local fallback) + recover.
_apply_styles_dirs()
try:      # keep the normalized-clip cache from growing forever (entries are rebuilt on demand)
    compose.prune_cache()
except Exception:
    pass


# --- delivery: drop finished creatives into the team "Finished Work" Drive folder ----
# Local method: the file is copied into the Google-Drive-synced folder; Drive uploads it.
# No rclone / OAuth needed on a Mac that already syncs Drive (that's the headless-VPS path).
def _detect_drive_folder(folder_name):
    import glob as _g
    home = os.path.expanduser("~")
    roots = (_g.glob(os.path.join(home, "Library", "CloudStorage", "GoogleDrive-*"))
             + _g.glob(os.path.join(home, "Library", "CloudStorage", "Dropbox*"))
             + _g.glob(os.path.join(home, "Google Drive*")))
    mids = ["Shared drives/*", "Shared drives/*/*", "Спільні диски/*", "Спільні диски/*/*",
            "My Drive", "My Drive/*", "My Drive/*/*", "", "*"]
    for root in roots:
        if not os.path.isdir(root):
            continue
        for mid in mids:
            parts = [p for p in mid.split("/") if p]
            pat = os.path.join(root, *parts, folder_name) if parts else os.path.join(root, folder_name)
            hits = sorted(_g.glob(pat))
            if hits:
                return hits[0]
    return None


def _resolve_finished_dir(scan=False):
    """Where delivered creatives go. Reads env, then config — FAST, no disk scan.
    Only globs the Drive tree when scan=True, which happens lazily on the first delivery
    (never at startup), and the result is CACHED to config so it's never scanned again.
    (Scanning Google Drive at every launch was making the app slow to open.)"""
    d = os.environ.get("CS_FINISHED_DIR", "")
    if not d:
        try:
            if os.path.exists(_PCFG):
                d = (_json.load(open(_PCFG, encoding="utf-8")).get("finished_dir") or "")
        except Exception:
            d = ""
    if not d and scan:
        d = _detect_drive_folder("Finished Work") or ""
        if d:
            try:
                save_finished_dir(d)   # cache the hit so we never scan the Drive again
            except Exception:
                pass
    if d:
        os.environ["CS_FINISHED_DIR"] = os.path.expanduser(d)
    return os.environ.get("CS_FINISHED_DIR", "")


def save_finished_dir(path):
    path = os.path.expanduser((path or "").strip())
    if path:
        os.makedirs(path, exist_ok=True)
        os.environ["CS_FINISHED_DIR"] = path
    os.makedirs(os.path.dirname(_PCFG), exist_ok=True)
    cur = {}
    if os.path.exists(_PCFG):
        try:
            cur = _json.load(open(_PCFG, encoding="utf-8"))
        except Exception:
            cur = {}
    cur["finished_dir"] = path
    _json.dump(cur, open(_PCFG, "w", encoding="utf-8"), indent=2)


def _deliver_to_finished(named_paths):
    """named_paths = [(src_path, dest_filename)]. Copy each into the Finished Work folder."""
    dst = os.environ.get("CS_FINISHED_DIR", "")
    if not dst or not os.path.isdir(dst):
        return {"ok": False, "delivered": 0, "dir": dst, "reason": "no Finished Work folder set/synced"}
    n = 0
    for src, name in named_paths:
        try:
            shutil.copy2(src, os.path.join(dst, os.path.basename(name)))
            n += 1
        except Exception:
            pass
    return {"ok": True, "delivered": n, "dir": dst}


_resolve_finished_dir()

app = FastAPI(title="Captions Studio", version="0.1.0")


@app.middleware("http")
async def no_cache_html(request, call_next):
    """Never cache the app shell, so code updates always show after a restart."""
    resp = await call_next(request)
    p = request.url.path
    if p == "/" or p.endswith(".html") or p.endswith(".js"):
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


def _session_dir(fid: str) -> str:
    d = os.path.join(WORK_ROOT, fid)
    if not os.path.isdir(d):
        raise HTTPException(404, "Unknown file_id (session expired?)")
    return d


def list_fonts():
    """Every font in fonts/, including SUBFOLDERS — so a family can live in its own folder
    (fonts/Poppins/Poppins-SemiBold.ttf) and stay tidy on GitHub. Returns paths relative to
    fonts/ ('Poppins/Poppins-Bold.ttf' or just 'Oswald-Bold.ttf' for flat files)."""
    d = st.FONTS_DIR
    if not os.path.isdir(d):
        return []
    out = []
    for base, dirs, files in os.walk(d):
        dirs[:] = [x for x in dirs if not x.startswith(".")]
        for f in files:
            if f.lower().endswith((".ttf", ".otf", ".ttc")) and not f.startswith("."):
                out.append(os.path.relpath(os.path.join(base, f), d))
    return sorted(out, key=str.lower)


@app.get("/api/info")
def info():
    return {
        "encoder": ffmpeg_utils.video_encoder(),
        "engines": ["whisper", "scribe"],
        "scribe_key_present": bool(os.environ.get("ELEVENLABS_API_KEY")),
        "heygen_key_present": bool(os.environ.get("HEYGEN_API_KEY")),
        "formats": list(ffmpeg_utils.DIMS.keys()),
        "styles": st.list_styles(),
        "fonts": list_fonts(),
        "default_style": st.DEFAULT_STYLE,
        "machine_id": machine_id(),
        "allowed": license_ok(),
        "version": app_version(),
        # true only in the maintainer's git clone (run.command from the repo). The packaged .app
        # and auto-updated code have no .git, so the 'Save as app default' button is hidden there
        # (it would write into the bundle and never reach GitHub).
        "maintainer": os.path.isdir(os.path.join(ROOT, ".git")),
    }


def _looks_like_url(p):
    import re as _re
    return bool(_re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", (p or "").strip()))


def valid_lib_dir(p):
    """A usable library folder = a real LOCAL directory. A drive.google.com link is not one."""
    p = (p or "").strip()
    return bool(p) and not _looks_like_url(p) and os.path.isdir(os.path.expanduser(p))


@app.get("/api/drive_roots")
def drive_roots():
    """Candidate LOCAL paths of synced cloud folders on this Mac, so the user can pick one
    instead of pasting a web link (Google Drive for desktop mounts under ~/Library/CloudStorage)."""
    home = os.path.expanduser("~")
    out, seen = [], set()

    def add(p):
        if os.path.isdir(p) and p not in seen:
            seen.add(p)
            out.append(p)
    cs = os.path.join(home, "Library", "CloudStorage")
    if os.path.isdir(cs):
        for e in sorted(os.listdir(cs)):
            p = os.path.join(cs, e)
            add(p)
            for sub in ("My Drive", "Мій диск", "Shared drives", "Спільні диски"):
                add(os.path.join(p, sub))
    for e in sorted(os.listdir(home)) if os.path.isdir(home) else []:
        low = e.lower()
        if low.startswith("google drive") or low.startswith("dropbox"):
            add(os.path.join(home, e))
    return {"roots": out[:40], "current": os.environ.get("CS_LIBRARY_DIR", "")}


def save_library_dir(path):
    """Persist the shared library folder to the per-machine config and apply it live."""
    path = os.path.expanduser((path or "").strip().strip('"').strip("'"))
    if not path:
        return
    # A web link (https://drive.google.com/…) can't be read by ffmpeg or Python. It used to be
    # accepted and os.makedirs() then CREATED a folder literally named after the URL — the
    # library looked "✓ set" and was permanently empty. Refuse it with a usable hint instead.
    if _looks_like_url(path):
        raise HTTPException(400,
                            "Це веб-посилання, а не папка на цьому Mac. Відкрий папку в Finder "
                            "(Google Drive), правий клік → «Copy as Pathname» (⌥⌘C) і вставь ШЛЯХ, "
                            "напр. /Users/…/Library/CloudStorage/GoogleDrive-…/My Drive/Assembler_Library")
    if not os.path.isdir(path):
        parent = os.path.dirname(path.rstrip(os.sep))
        if not (parent and os.path.isdir(parent)):
            raise HTTPException(400, f"Папки не існує і батьківської теки теж немає: {path}")
        os.makedirs(path, exist_ok=True)   # only one new level, inside a folder that really exists
    os.environ["CS_LIBRARY_DIR"] = path
    library.LIBRARY_DIR = path
    _apply_styles_dirs()   # styles follow the shared library folder (or fall back locally)
    os.makedirs(os.path.dirname(_PCFG), exist_ok=True)
    cur = {}
    if os.path.exists(_PCFG):
        try:
            cur = _json.load(open(_PCFG, encoding="utf-8"))
        except Exception:
            cur = {}
    cur["library_dir"] = path
    _json.dump(cur, open(_PCFG, "w", encoding="utf-8"), indent=2)


class DeliverReq(BaseModel):
    file_id: str
    shots: List[dict] = []   # [{"file": "batch_x.mp4", "name": "PROJ-123_hook1_es"}]


@app.post("/api/deliver_finished")
def deliver_finished(req: DeliverReq):
    """Copy the named finished creatives into the team 'Finished Work' Drive folder
    (Google Drive for Desktop uploads them). Names use the same creative names as export."""
    sdir = _session_dir(req.file_id)
    out = os.path.join(sdir, "output")
    if not os.environ.get("CS_FINISHED_DIR", ""):
        _resolve_finished_dir(scan=True)   # lazy one-time Drive scan on first delivery (then cached)
    named = []
    for s in (req.shots or []):
        vf = os.path.join(out, os.path.basename(s.get("file", "")))
        if not os.path.exists(vf):
            continue
        nm = _safe_name(s.get("name"), os.path.splitext(os.path.basename(vf))[0])
        named.append((vf, nm + ".mp4"))
    if not named:  # no naming info → deliver whatever finals exist, as-is
        import glob as _g
        for v in (sorted(_g.glob(os.path.join(out, "batch_*.mp4"))) or sorted(_g.glob(os.path.join(out, "*.mp4")))):
            named.append((v, os.path.basename(v)))
    res = _deliver_to_finished(named)
    if not res["ok"]:
        raise HTTPException(400, res.get("reason", "delivery unavailable"))
    return res


class KeysReq(BaseModel):
    elevenlabs_api_key: Optional[str] = None
    heygen_api_key: Optional[str] = None
    library_dir: Optional[str] = None
    finished_dir: Optional[str] = None


@app.get("/api/settings")
def get_settings():
    return {"machine_id": machine_id(), "allowed": license_ok(),
            "scribe_key_present": bool(os.environ.get("ELEVENLABS_API_KEY")),
            "heygen_key_present": bool(os.environ.get("HEYGEN_API_KEY")),
            "library_dir": os.environ.get("CS_LIBRARY_DIR", ""),
            "library_exists": os.path.isdir(os.environ.get("CS_LIBRARY_DIR", "")),
            "finished_dir": os.environ.get("CS_FINISHED_DIR", ""),
            "finished_exists": os.path.isdir(os.environ.get("CS_FINISHED_DIR", "")),
            "keys_location": _PCFG}


@app.post("/api/settings")
def post_settings(body: KeysReq):
    save_keys((body.elevenlabs_api_key or "").strip(), (body.heygen_api_key or "").strip())
    if body.library_dir is not None and body.library_dir.strip():
        save_library_dir(body.library_dir)
    if body.finished_dir is not None:
        save_finished_dir(body.finished_dir)
    return {"saved": True,
            "scribe_key_present": bool(os.environ.get("ELEVENLABS_API_KEY")),
            "heygen_key_present": bool(os.environ.get("HEYGEN_API_KEY")),
            "library_dir": os.environ.get("CS_LIBRARY_DIR", ""),
            "library_exists": os.path.isdir(os.environ.get("CS_LIBRARY_DIR", ""))}


@app.get("/api/fonts")
def get_fonts():
    return {"fonts": list_fonts()}


@app.post("/api/shutdown")
def shutdown():
    """Cleanly stop the local server (the Quit button) instead of force-killing the terminal."""
    import threading
    threading.Timer(0.4, lambda: os._exit(0)).start()
    return {"ok": True}


def _official_names():
    d = st.OFFICIAL_STYLES_DIR
    if not d or not os.path.isdir(d):
        return []
    return sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json"))


@app.get("/api/styles")
def get_styles():
    # `official` = the shipped defaults (repo styles/); the UI shows these WITHOUT a ★.
    return {"styles": st.list_styles(), "official": _official_names()}


@app.get("/api/styles/{name}")
def get_style(name: str):
    try:
        return st.load_style(name)
    except FileNotFoundError:
        raise HTTPException(404, "Style not found")


class SaveStyle(BaseModel):
    name: str
    style: dict


@app.post("/api/styles")
def post_style(body: SaveStyle):
    path = st.save_style(body.name, body.style)
    # Durability: always keep a local mirror (~/.assembler/styles) so a wiped shared Drive
    # folder can't lose the team's presets — they re-seed from here on the next launch.
    try:
        mirror = os.path.join(_ASSEMBLER_HOME, "styles")
        if os.path.abspath(mirror) != os.path.abspath(st.STYLES_DIR):
            os.makedirs(mirror, exist_ok=True)
            shutil.copy2(path, os.path.join(mirror, os.path.basename(path)))
    except Exception:
        pass
    return {"saved": os.path.basename(path), "styles": st.list_styles()}


@app.delete("/api/styles/{name}")
def delete_style(name: str):
    st.delete_style(name)
    st.delete_style(name, styles_dir=os.path.join(_ASSEMBLER_HOME, "styles"))  # also drop the local mirror so it doesn't re-seed
    return {"deleted": name, "styles": st.list_styles()}


@app.post("/api/styles/official")
def post_official_style(body: SaveStyle):
    """Save a style into the SHIPPED defaults (repo styles/) so it becomes a permanent base
    preset — it travels with every code update and gets bundled into every repackaged .app.
    Writes into the code's styles/ folder; the maintainer then Commits + Pushes it on GitHub."""
    d = st.OFFICIAL_STYLES_DIR
    os.makedirs(d, exist_ok=True)
    path = st.save_style(body.name, body.style, styles_dir=d)
    return {"saved": os.path.basename(path), "dir": d,
            "official": _official_names(), "styles": st.list_styles()}


class DetectCutsReq(BaseModel):
    file_id: str
    clip: str = "source.mp4"
    threshold: float = 0.3   # lower = more sensitive (catches cuts between similar shots)


@app.post("/api/detect_cuts")
def api_detect_cuts(req: DetectCutsReq):
    """Scene-cut timestamps for a session clip, so captions can be clipped at cuts."""
    sdir = _session_dir(req.file_id)
    src = os.path.join(sdir, os.path.basename(req.clip))
    if not os.path.exists(src):
        raise HTTPException(404, f"Clip '{os.path.basename(req.clip)}' not found in session")
    try:
        cuts = ffmpeg_utils.detect_scene_cuts(src, threshold=req.threshold)
    except Exception as e:
        raise HTTPException(500, f"Cut detection failed: {e}")
    return {"cuts": cuts}


@app.get("/api/caption_rules")
def get_caption_rules():
    """Shared auto-caption rules (keep_together / glue / no_line_end / widow control)."""
    return subtitles.load_rules()


@app.post("/api/caption_rules")
def post_caption_rules(body: dict):
    """Save the shared caption rules. Body is the full rules object."""
    if not isinstance(body, dict):
        raise HTTPException(400, "Rules must be an object")
    path = subtitles._rules_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        _json.dump(body, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return {"saved": True, "rules": subtitles.load_rules()}


@app.post("/api/transcribe")
async def api_transcribe(
    file: UploadFile = File(...),
    engine: str = Form("whisper"),
    model_size: str = Form("small"),
    language: str = Form(""),
):
    fid = uuid.uuid4().hex[:12]
    sdir = os.path.join(WORK_ROOT, fid)
    os.makedirs(sdir, exist_ok=True)
    src = os.path.join(sdir, "source.mp4")
    with open(src, "wb") as out:
        shutil.copyfileobj(file.file, out)

    try:
        w, h = ffmpeg_utils.get_video_size(src)
        dur = ffmpeg_utils.get_video_duration(src)
        result = transcribe.transcribe(
            src, engine=engine,
            model_size=model_size,
            language=language or None,
            api_key=os.environ.get("ELEVENLABS_API_KEY"),
        )
    except Exception as e:  # surface a clean message to the UI
        raise HTTPException(500, f"Transcription failed: {e}")

    result["words"] = _clean_transcript(result["words"], result.get("language"))

    import json
    with open(os.path.join(sdir, "words.json"), "w", encoding="utf-8") as wf:
        json.dump(result["words"], wf)

    return {
        "file_id": fid,
        "width": w, "height": h, "duration": dur,
        "language": result["language"],
        "words": result["words"],
    }


def _clean_transcript(words, lang=None):
    """Caption-ready clean-up of raw ASR words (numbers -> digits, unit/word swaps).

    Applied to BOTH engines right after transcription, so Whisper and Scribe give the same
    caption-ready text and the transcript box shows exactly what will be burned in.
    """
    try:
        return textrules.clean_words(words or [], lang or "en", subtitles.load_rules())
    except Exception as e:
        print(f"[textrules] skipped ({type(e).__name__}: {e})", flush=True)
        return words


class TranscribeClipReq(BaseModel):
    file_id: str
    clip: str = "source.mp4"
    engine: str = "whisper"
    model_size: str = "small"
    language: str = ""


@app.post("/api/transcribe_clip")
def api_transcribe_clip(req: TranscribeClipReq):
    """Transcribe a clip that is ALREADY in the session (uploaded via /api/upload_clip).
    Lets the EDIT batch flow upload each hook once, then caption them all in one session."""
    sdir = _session_dir(req.file_id)
    clip = os.path.basename(req.clip)
    src = os.path.join(sdir, clip)
    if not os.path.exists(src):
        raise HTTPException(404, f"Clip '{clip}' not found in session")
    try:
        result = transcribe.transcribe(
            src, engine=req.engine, model_size=req.model_size,
            language=req.language or None,
            api_key=os.environ.get("ELEVENLABS_API_KEY"),
        )
    except Exception as e:
        raise HTTPException(500, f"Transcription failed: {e}")
    return {"language": result["language"],
            "words": _clean_transcript(result["words"], result.get("language"))}


class PreviewFrameReq(BaseModel):
    style: dict
    words: Optional[List[dict]] = None
    time: float = 0.0
    format: str = "9:16"
    dur: float = 0.0   # clip duration, so the last caption "holds" like in the real render
    cap_in: Optional[float] = None    # captions only shown within [cap_in, cap_out]
    cap_out: Optional[float] = None
    lang: str = "en"                  # language for auto-caption stopword rules
    cuts: Optional[List[float]] = None  # scene-cut times → captions don't cross a cut
    titles: Optional[list] = None      # static text overlays, so 'exact' shows them like the render


@app.get("/api/font_metrics")
def api_font_metrics(font: str = "", size: int = 80):
    """Real Pillow metrics for the live Canvas to match the engine's line layout."""
    f = subtitles.load_font(st.resolve_font(font), max(1, int(size)))
    ascent, descent = f.getmetrics()
    return {"ascent": ascent, "descent": descent, "space": f.getlength(" ")}


@app.post("/api/preview_frame")
def api_preview_frame(req: PreviewFrameReq):
    """Render the EXACT subtitle overlay (Pillow — the real engine) for one moment,
    so the on-screen preview matches the final render pixel-for-pixel."""
    TW, TH = ffmpeg_utils.DIMS.get(req.format, (1080, 1920))
    style = st.normalize(req.style)
    scale = 1.0   # NO per-format scaling — font px absolute, same caption in every format
    # caption window: nothing shown outside [cap_in, cap_out]
    if (req.cap_in is not None and req.time < req.cap_in) or (req.cap_out is not None and req.time >= req.cap_out):
        return Response(status_code=204)
    tagged = compose.build_timeline(req.words or [], [{"start": 0, "end": None, "style": style}],
                                    lang=(req.lang or "en"), cuts=req.cuts)
    events = [e for e, _ in tagged]
    # By default do NOT extend the last caption to the clip end — build_events already gives it a
    # readable tail (and trims at a scene cut); extending unconditionally made 'exact' hang the
    # last caption over trailing footage / the packshot. The style switch opts back in.
    if events and req.dur and style.get("hold_last"):
        # +0.2s of slack: the browser's duration can be a hair short of the real file length,
        # so without it the caption could blink off on the very last frames of the preview.
        events[-1]["end"] = max(float(events[-1]["end"]), float(req.dur) + 0.2)
    ev = next((e for e in events if req.time >= e["start"] and req.time < e["end"]), None)
    # titles visible at this moment — 'exact' must show them too, otherwise the preview looks
    # emptier than the render (which does burn them in)
    act_titles = [t for t in (req.titles or [])
                  if t and str(t.get("text", "")).strip()
                  and (t.get("in") is None or req.time >= float(t["in"]))
                  and (t.get("out") is None or req.time <= float(t["out"]))]
    if ev is None and not act_titles:
        return Response(status_code=204)  # nothing on screen at this moment
    import tempfile
    from PIL import Image as _Img
    tmp = tempfile.mktemp(suffix=".png")
    font = st.resolve_font(style.get("font_name"))
    if ev is not None:
        subtitles.render_subtitle_png(ev, tmp, TW, TH, font, style, scale)
    else:
        _Img.new("RGBA", (TW, TH), (0, 0, 0, 0)).save(tmp)
    if act_titles:                                   # composite each title over the caption layer
        base = _Img.open(tmp).convert("RGBA")
        for _i, t in enumerate(act_titles):
            tp = tempfile.mktemp(suffix=".png")
            subtitles.render_headline_png(t["text"], tp, TW, TH, st.resolve_font(t.get("font_name")), t)
            base.alpha_composite(_Img.open(tp).convert("RGBA"))
            os.remove(tp)
        base.save(tmp)
    with open(tmp, "rb") as f:
        data = f.read()
    os.remove(tmp)
    return Response(content=data, media_type="image/png")


class LayoutReq(BaseModel):
    words: list = []                              # word dicts (with optional brk="soft")
    regions: Optional[list] = None                # [{"start","end","style"}]; else single base_style region
    base_style: Optional[dict] = None
    cuts: Optional[List[float]] = None
    lang: str = "en"
    duration: float = 0.0                         # clip length — needed for "hold last caption to end"


@app.post("/api/layout")
def api_layout(req: LayoutReq):
    """SINGLE SOURCE OF TRUTH for caption layout. The browser preview calls this to get the
    exact same event/line chunking the final render uses (build_timeline → build_events), so
    what you see == what you get. The preview only DRAWS these events; it no longer computes
    its own chunking. Styling stays client-side (colors/stroke/shadow/animation)."""
    base = st.normalize(req.base_style or {})
    if req.regions:
        regions = [{"start": float(r.get("start", 0)), "end": r.get("end"),
                    "style": st.normalize(r.get("style") or (req.base_style or {}))} for r in req.regions]
    else:
        regions = [{"start": 0, "end": None, "style": base}]
    tagged = compose.build_timeline(req.words or [], regions, lang=(req.lang or "en"), cuts=req.cuts)
    # mirror the render's "hold last caption to end" so the preview shows the same thing
    if tagged and req.duration and base.get("hold_last"):
        tagged[-1][0]["end"] = max(float(tagged[-1][0]["end"]), float(req.duration) + 0.2)
    events = []
    for e, _s in tagged:
        events.append({
            "start": e["start"], "end": e["end"],
            "active": e.get("active_word_index", 0), "manual": bool(e.get("manual", False)),
            "lines": [[{"text": w.get("text", ""), "start": w.get("start", 0), "end": w.get("end", 0)}
                       for w in line] for line in e.get("lines", [])],
        })
    return {"events": events}


class RenderReq(BaseModel):
    file_id: str
    regions: list                  # [{"start","end","style"}]
    words: Optional[List[dict]] = None  # edited word timecodes; falls back to stored transcription
    formats: list = ["9:16"]
    smart_trim: bool = False
    use_body: bool = False          # use body files placed in the session dir / project root
    clip: str = "source.mp4"        # which clip in the session to caption (e.g. dub_es.mp4)
    trim: Optional[list] = None     # [start, end] seconds — same trim the Compose board applies
    cuts: Optional[List[float]] = None  # scene cuts → captions clipped at cuts
    headline: Optional[dict] = None     # static TEXT overlay {text,size,color,y,in,out,...} — localizable per language


@app.post("/api/render")
def api_render(req: RenderReq):
    sdir = _session_dir(req.file_id)
    clip = os.path.basename(req.clip)  # prevent path traversal
    src = os.path.join(sdir, clip)
    if not os.path.exists(src):
        raise HTTPException(404, f"Clip '{clip}' not found in session")
    out_dir = os.path.join(sdir, "output")
    stem = os.path.splitext(clip)[0]
    out_prefix = "caption" if stem == "source" else stem  # e.g. dub_es -> 9x16_dub_es.mp4

    # locate body clips (optional): per-format body_<fmt>.mp4 then body.mp4
    bodies, default_body = {}, None
    if req.use_body:
        for fmt in req.formats:
            tag = {"16:9": "16x9", "1:1": "1x1", "9:16": "9x16"}[fmt]
            for base in (sdir, ROOT):
                p = os.path.join(base, f"body_{tag}.mp4")
                if os.path.exists(p):
                    bodies[fmt] = p
                    break
        for base in (sdir, ROOT):
            p = os.path.join(base, "body.mp4")
            if os.path.exists(p):
                default_body = p
                break

    # real words for the timeline: prefer edited words from the request, else stored
    words = req.words
    if words is None:
        words_path = os.path.join(sdir, "words.json")
        if os.path.exists(words_path):
            import json
            words = json.load(open(words_path))

    # apply trim (cut clip + re-base caption timecodes) — identical to the Compose board
    if req.trim and len(req.trim) == 2:
        import tempfile
        tdir = tempfile.mkdtemp(prefix="cs_rtrim_", dir=sdir)
        trimmed = os.path.join(tdir, "trimmed.mp4")
        compose.trim_clip(src, req.trim[0], req.trim[1], trimmed)
        src = trimmed
        words = compose.shift_words(words or [], float(req.trim[0]), float(req.trim[1]))
        if req.cuts:   # re-base scene cuts to the trimmed clip too
            lo, hi = float(req.trim[0]), float(req.trim[1])
            req.cuts = [c - lo for c in req.cuts if lo < c < hi]

    import time as _t
    _t0 = _t.time()
    try:
        outputs = compose.render(
            src, words or [], req.regions, req.formats, out_dir,
            bodies=bodies, default_body=default_body, smart_trim=req.smart_trim,
            out_prefix=out_prefix, cuts=req.cuts, headline=req.headline,
        )
    except Exception as e:
        raise HTTPException(500, f"Render failed: {e}")
    try:   # durable timing record (see ~/.assembler/timings.csv)
        _dur = ffmpeg_utils.get_video_duration(src) or 0
        _rt = (_t.time() - _t0) / _dur if _dur else 0
        _log_timing("render", _t.time() - _t0,
                    f"clip={clip} fmts={'+'.join(req.formats)} vid_s={_dur:.1f} realtime_x={_rt:.2f} words={len(words or [])}")
    except Exception:
        pass

    return {"outputs": [
        {"format": req.formats[i], "url": f"/download/{req.file_id}/{os.path.basename(p)}",
         "name": os.path.basename(p)}
        for i, p in enumerate(outputs)
    ]}


@app.get("/api/stats")
def api_stats():
    """Aggregate of ~/.assembler/timings.csv — today / last 7 days / all time.

    Per operation: how many times it ran and how long it took (total + median), plus the
    assembly cache hit rate and an estimate of the encoding time the cache avoided.
    """
    import csv as _csv
    import datetime as _dt
    import re as _re
    p = os.path.join(_ASSEMBLER_HOME, "timings.csv")
    empty = {"today": {}, "week": {}, "all": {}, "rows": 0, "file": p}
    if not os.path.exists(p):
        return empty
    now = _dt.datetime.now()
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week0 = now - _dt.timedelta(days=7)
    buckets = {"today": {}, "week": {}, "all": {}}
    joined_seconds = 0.0
    fast = reenc = 0
    bake_rt = []          # measured encode cost (seconds of compute per second of video)
    rows = 0
    try:
        with open(p, newline="", encoding="utf-8") as fh:
            for r in _csv.DictReader(fh):
                try:
                    when = _dt.datetime.fromisoformat(r.get("timestamp", ""))
                except Exception:
                    continue
                op = (r.get("op") or "?").strip()
                try:
                    secs = float(r.get("seconds") or 0)
                except Exception:
                    continue
                rows += 1
                notes = r.get("notes") or ""
                kv = dict(_re.findall(r"([a-z_]+)=([^\s]+)", notes))
                if op == "batch":
                    fast += int(float(kv.get("fast_joins", 0) or 0)) if "fast_joins" in kv else 0
                    reenc += int(float(kv.get("reencoded_joins", 0) or 0)) if "reencoded_joins" in kv else 0
                    try:
                        joined_seconds += float(kv.get("out_s_total", 0) or 0)
                    except Exception:
                        pass
                if op == "render":
                    try:
                        rt = float(kv.get("realtime_x", 0) or 0)
                        if rt > 0:
                            bake_rt.append(rt)
                    except Exception:
                        pass
                for name, since in (("today", today0), ("week", week0), ("all", None)):
                    if since is None or when >= since:
                        b = buckets[name].setdefault(op, {"count": 0, "total": 0.0, "samples": []})
                        b["count"] += 1
                        b["total"] += secs
                        b["samples"].append(secs)
    except Exception:
        return empty

    def finish(b):
        out = {}
        for op, v in b.items():
            s = sorted(v["samples"])
            out[op] = {"count": v["count"], "total_seconds": round(v["total"], 1),
                       "median_seconds": round(s[len(s) // 2], 2) if s else 0}
        return out

    med_rt = sorted(bake_rt)[len(bake_rt) // 2] if bake_rt else 0
    total_joins = fast + reenc
    return {"today": finish(buckets["today"]), "week": finish(buckets["week"]),
            "all": finish(buckets["all"]), "rows": rows, "file": p,
            "cache": {"fast_joins": fast, "reencoded_joins": reenc,
                      "hit_rate_pct": round(100.0 * fast / total_joins, 1) if total_joins else None,
                      # estimate: those joined seconds would have been re-encoded at this machine's
                      # own measured encode speed (median realtime factor from real renders)
                      "encoding_avoided_seconds": round(joined_seconds * med_rt, 1) if med_rt else None,
                      "encode_realtime_factor": round(med_rt, 3) if med_rt else None}}


@app.get("/api/cache")
def api_cache_info():
    """Size of the normalized-clip cache (what makes repeat assembles fast)."""
    d = compose._cache_dir()
    n = tot = 0
    for f in os.listdir(d) if os.path.isdir(d) else []:
        p = os.path.join(d, f)
        if os.path.isfile(p):
            n += 1
            tot += os.path.getsize(p)
    return {"dir": d, "files": n, "mb": round(tot / 1e6, 1)}


@app.post("/api/cache/clear")
def api_cache_clear():
    """Wipe the cache. Safe: clips are simply normalized again on the next assemble."""
    d = compose._cache_dir()
    freed = n = 0
    for f in os.listdir(d) if os.path.isdir(d) else []:
        p = os.path.join(d, f)
        try:
            if os.path.isfile(p):
                sz = os.path.getsize(p)
                os.remove(p)
                freed += sz
                n += 1
        except Exception:
            pass
    _log_timing("cache_clear", 0, f"removed={n} freed_mb={round(freed/1e6,1)}")
    return {"removed": n, "freed_mb": round(freed / 1e6, 1), "dir": d}


@app.get("/api/clip_info")
def api_clip_info(file_id: str, clip: str = "source.mp4"):
    """Real stream params of a session clip — the UI needs the TRUE fps to snap the trim
    handles to whole frames (a 24 fps source on a 30 fps grid is off by up to a frame)."""
    sdir = _session_dir(file_id)
    src = os.path.join(sdir, os.path.basename(clip))
    if not os.path.exists(src):
        raise HTTPException(404, f"Clip '{clip}' not found in session")
    p = compose._stream_params(src) or (None,) * 8
    try:
        dur = ffmpeg_utils.get_video_duration(src) or 0.0
    except Exception:
        dur = 0.0
    return {"width": p[0], "height": p[1], "fps": p[2] or 30.0, "vcodec": p[3],
            "acodec": p[5], "sample_rate": p[6], "channels": p[7],
            "duration": round(float(dur), 3)}


class NormalizeReq(BaseModel):
    file_id: str
    clip: str = "source.mp4"
    format: str = "9:16"
    trim: Optional[List[float]] = None    # [in, out] seconds — cut FIRST, then conform


@app.post("/api/normalize")
def api_normalize(req: NormalizeReq):
    """Prepare a 'wild' clip for assembly — no captions, just conform it to the project's
    render parameters (size / 30fps / h264 yuv420p / aac 44.1k stereo).

    Why: clips straight out of Kling / Freepik / Higgsfield can be 720p, 24fps, odd audio.
    Normalizing once means the board can join them with STREAM COPY (fast, no quality loss)
    instead of re-encoding everything. Cached, so re-running is instant.
    """
    import time as _t
    sdir = _session_dir(req.file_id)
    src = os.path.join(sdir, os.path.basename(req.clip))
    if not os.path.exists(src):
        raise HTTPException(404, f"Clip '{req.clip}' not found in session")
    out_dir = os.path.join(sdir, "output")
    os.makedirs(out_dir, exist_ok=True)
    t0 = _t.time()
    stem = os.path.splitext(os.path.basename(src))[0]
    cut = None
    # Trim FIRST when asked, so "Normalize" delivers the actual body part you cut on the bar
    # (before this, trim was only applied during the final assemble, and a trimmed body could
    # not be handed over as a ready clip / library item). The cut file gets a deterministic
    # name so normalize_clip's content cache still works on repeat runs.
    if req.trim and len(req.trim) == 2:
        t_in, t_out = float(req.trim[0]), float(req.trim[1])
        if t_out - t_in > 0.05:
            cut = os.path.join(out_dir, f"{stem}_cut_{int(t_in * 1000)}_{int(t_out * 1000)}.mp4")
            if not (os.path.exists(cut) and os.path.getsize(cut) > 1000):
                try:
                    compose.trim_clip(src, t_in, t_out, cut)
                except Exception as e:
                    raise HTTPException(500, f"Trim failed: {e}")
            stem = f"{stem}_cut"
    try:
        norm = compose.normalize_clip(cut or src, req.format)
    except Exception as e:
        raise HTTPException(500, f"Normalize failed: {e}")
    name = f"{stem}_norm.mp4"
    dst = os.path.join(out_dir, name)
    if os.path.abspath(norm) != os.path.abspath(dst):
        shutil.copyfile(norm, dst)
    el = _t.time() - t0
    already = (os.path.abspath(norm) == os.path.abspath(cut or src))   # already matched the target
    _log_timing("normalize", el,
                f"clip={os.path.basename(src)} fmt={req.format} already_ok={already} trimmed={bool(cut)}")
    return {"name": name, "url": f"/download/{req.file_id}/{name}",
            "seconds": round(el, 2), "already_conformed": already,
            "size_mb": round(os.path.getsize(dst) / 1e6, 2) if os.path.exists(dst) else 0}


class BenchReq(BaseModel):
    file_id: str
    clip: str = "source.mp4"
    repeat: int = 2
    concurrency: int = 1     # 1 = sequential; >1 renders that many copies at once


@app.post("/api/benchmark")
def api_benchmark(req: BenchReq):
    """Measure this machine's render speed on a REAL clip from the session.

    Returns the two numbers used to size render capacity:
      realtime_factor  — render seconds per second of video (lower = faster)
      clips_per_min    — throughput at the requested concurrency
    Also appends to ~/.assembler/timings.csv so results accumulate per machine.
    """
    import time as _t
    import concurrent.futures as _cf
    sdir = _session_dir(req.file_id)
    src = os.path.join(sdir, os.path.basename(req.clip))
    if not os.path.exists(src):
        raise HTTPException(404, f"Clip '{req.clip}' not found in session")
    dur = ffmpeg_utils.get_video_duration(src) or 0
    if dur <= 0:
        raise HTTPException(400, "Could not read clip duration")
    # A realistic, machine-independent caption load derived from the clip length.
    n_words = max(1, int(dur * 2.2))
    slot = dur / n_words
    sample = ["THIS", "IS", "YOUR", "BODY", "AFTER", "REGULAR", "WALKING", "AND", "IT", "WORKS"]
    words = [{"word": sample[i % len(sample)], "start": round(i * slot, 3),
              "end": round((i + 1) * slot, 3)} for i in range(n_words)]
    style = st.normalize({"font_size": 90, "max_chars_per_line": 16, "max_lines": 2,
                          "stroke_on": True, "stroke_outer": {"width": 8, "color": [0, 0, 0, 255]},
                          "karaoke": {"enabled": True}})

    def one():
        work = tempfile.mkdtemp(prefix="cs_bench_", dir=WORK_ROOT)
        out = os.path.join(work, "b.mp4")
        t0 = _t.time()
        compose.caption_clip(src, words, style, "9:16", out, work)
        el = _t.time() - t0
        try:
            shutil.rmtree(work, ignore_errors=True)
        except Exception:
            pass
        return el

    reps = max(1, min(5, int(req.repeat)))
    conc = max(1, min(8, int(req.concurrency)))
    t_all = _t.time()
    times = []
    if conc == 1:
        for _ in range(reps):
            times.append(one())
    else:
        with _cf.ThreadPoolExecutor(max_workers=conc) as ex:   # ffmpeg runs as a subprocess
            times = list(ex.map(lambda _i: one(), range(reps * conc)))
    wall = _t.time() - t_all
    times.sort()
    med = times[len(times) // 2]
    jobs = len(times)
    res = {"clip": os.path.basename(src), "video_seconds": round(dur, 2),
           "runs": jobs, "concurrency": conc,
           "median_render_seconds": round(med, 2),
           "realtime_factor": round(med / dur, 3),
           "wall_seconds": round(wall, 2),
           "clips_per_min": round(jobs / (wall / 60), 1),
           "encoder": ffmpeg_utils.video_encoder(),
           "cores": os.cpu_count(), "machine": machine_id()}
    _log_timing("benchmark", wall,
                f"clip={res['clip']} vid_s={res['video_seconds']} runs={jobs} conc={conc} "
                f"median_s={res['median_render_seconds']} realtime_x={res['realtime_factor']} "
                f"clips_per_min={res['clips_per_min']} cores={res['cores']}")
    return res


class DubReq(BaseModel):
    file_id: str
    target_langs: List[str]
    source_lang: str = "en"
    transcribe_engine: str = "whisper"
    model_size: str = "small"
    provider: str = "elevenlabs"  # elevenlabs (voice dub) | heygen (lip-sync)
    clip: str = "source.mp4"      # which session clip to localize (a pack hook, e.g. 1_hook.mp4)


@app.post("/api/dub")
def api_dub(req: DubReq):
    """Stage 1: dub the chosen clip into each language and re-transcribe it.

    Returns clean (caption-free) dubbed clips + editable transcripts. Captioning is
    done afterwards via /api/render with clip="dub_<...>_<lang>.mp4" and the (edited) words.
    """
    sdir = _session_dir(req.file_id)
    clip = os.path.basename(req.clip)
    src = os.path.join(sdir, clip)
    if not os.path.exists(src):
        raise HTTPException(404, f"Clip '{clip}' missing in session")
    # unique output prefix per source clip so multiple hooks' dubs never collide
    stem = os.path.splitext(clip)[0]
    prefix = "" if clip == "source.mp4" else (stem + "_")
    try:
        results, errors = localize.dub_and_transcribe(
            src, req.target_langs, sdir,
            source_lang=req.source_lang, api_key=os.environ.get("ELEVENLABS_API_KEY"),
            transcribe_engine=req.transcribe_engine, model_size=req.model_size,
            provider=req.provider,
            provider_key=os.environ.get("HEYGEN_API_KEY") if req.provider == "heygen"
            else os.environ.get("ELEVENLABS_API_KEY"),
            name_prefix=prefix,
        )
    except Exception as e:
        raise HTTPException(500, f"Dub failed: {e}")

    import json
    for r in results:
        with open(os.path.join(sdir, f"words_{r['lang']}.json"), "w", encoding="utf-8") as wf:
            json.dump(r["words"], wf)
        r["url"] = f"/clip/{req.file_id}/{r['clip']}"  # download the caption-free clip
    return {"results": results, "errors": errors}


# ----- Library (reusable body parts) -----
@app.get("/api/library")
def api_library(format: str = "", lang: str = ""):
    return {"items": library.list_items(fmt=format or None, lang=lang or None),
            "dir": library.LIBRARY_DIR}


class AddLibraryReq(BaseModel):
    file_id: str
    name: str
    lang: str = ""
    format: str = "9:16"
    output_name: str  # a file in the session's output/ dir (a rendered clip)
    kind: str = "body"  # body | packshot


@app.post("/api/library/add")
def api_library_add(req: AddLibraryReq):
    sdir = _session_dir(req.file_id)
    src = os.path.join(sdir, "output", os.path.basename(req.output_name))
    if not os.path.exists(src):
        raise HTTPException(404, "Rendered clip not found in session")
    item = library.add_item(src, req.name, req.lang, req.format, kind=req.kind)
    return {"item": item, "items": library.list_items()}


@app.post("/api/library/upload")
async def api_library_upload(file: UploadFile = File(...), name: str = Form(...),
                             lang: str = Form(""), format: str = Form("9:16"),
                             kind: str = Form("body")):
    ext = os.path.splitext(file.filename or "")[1].lower() or ".mp4"
    tmp = os.path.join(WORK_ROOT, f"_up_{uuid.uuid4().hex[:8]}{ext}")
    with open(tmp, "wb") as out:
        shutil.copyfileobj(file.file, out)
    try:
        item = library.add_item(tmp, name, lang, format, kind=kind)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return {"item": item, "items": library.list_items()}


@app.post("/api/library/delete")
def api_library_delete(body: dict):
    return {"deleted": library.delete_item(body.get("id", "")), "items": library.list_items()}


@app.get("/library/{name}")
def library_file(name: str):
    p = os.path.join(library.LIBRARY_DIR, os.path.basename(name))
    if not os.path.exists(p):
        raise HTTPException(404, "Library file not found")
    return FileResponse(p, media_type="video/mp4", filename=name)


# ----- Assemble (hook render + library body) -----
class AssembleReq(BaseModel):
    file_id: str
    hook_name: str   # a rendered clip in the session output/ dir (the hook)
    body_id: str     # a library item id
    format: str = "9:16"


@app.post("/api/assemble")
def api_assemble(req: AssembleReq):
    sdir = _session_dir(req.file_id)
    hook = os.path.join(sdir, "output", os.path.basename(req.hook_name))
    if not os.path.exists(hook):
        raise HTTPException(404, "Hook render not found in session")
    body = library.item_path(req.body_id)
    if not body:
        raise HTTPException(404, "Library body not found")
    out_dir = os.path.join(sdir, "output")
    os.makedirs(out_dir, exist_ok=True)
    hook_stem = os.path.splitext(os.path.basename(req.hook_name))[0]
    out = os.path.join(out_dir, f"assembled_{hook_stem}.mp4")
    try:
        compose.assemble(hook, body, req.format, out)
    except Exception as e:
        raise HTTPException(500, f"Assemble failed: {e}")
    return {"output": {"url": f"/download/{req.file_id}/{os.path.basename(out)}",
                       "name": os.path.basename(out), "format": req.format}}


@app.post("/api/upload_clip")
async def upload_clip(file: UploadFile = File(...), file_id: str = Form("")):
    """Upload a ready clip (e.g. an external hook) into a session for the batch board.
    Creates a session on the fly if none is given, so batch works without transcribing."""
    if not file_id or not os.path.isdir(os.path.join(WORK_ROOT, file_id)):
        file_id = uuid.uuid4().hex[:12]
    sdir = os.path.join(WORK_ROOT, file_id)
    os.makedirs(os.path.join(sdir, "output"), exist_ok=True)
    name = os.path.basename(file.filename or "clip.mp4")
    with open(os.path.join(sdir, name), "wb") as out:
        shutil.copyfileobj(file.file, out)
    return {"file_id": file_id, "name": name}


@app.get("/api/download_all/{file_id}")
def download_all(file_id: str):
    """Zip every finished creative in the session (the batch finals) → one download."""
    sdir = _session_dir(file_id)
    out = os.path.join(sdir, "output")
    import glob as _glob, zipfile as _zip
    files = sorted(_glob.glob(os.path.join(out, "batch_*.mp4"))) or sorted(_glob.glob(os.path.join(out, "*.mp4")))
    if not files:
        raise HTTPException(404, "No finished videos yet")
    zpath = os.path.join(sdir, "assembler_finals.zip")
    with _zip.ZipFile(zpath, "w", _zip.ZIP_STORED) as z:
        for f in files:
            z.write(f, os.path.basename(f))
    return FileResponse(zpath, filename="assembler_finals.zip", media_type="application/zip")


@app.get("/api/download_locs/{file_id}")
def download_locs(file_id: str):
    """Backup zip of every localized (caption-free) clip + its transcript in the session,
    so a session crash never means re-paying HeyGen/ElevenLabs to regenerate them."""
    sdir = _session_dir(file_id)
    import glob as _glob, zipfile as _zip
    clips = sorted(_glob.glob(os.path.join(sdir, "dub_*.mp4")))
    if not clips:
        raise HTTPException(404, "No localized clips in this session yet")
    zpath = os.path.join(sdir, "assembler_localized.zip")
    with _zip.ZipFile(zpath, "w", _zip.ZIP_STORED) as z:
        for f in clips:
            z.write(f, os.path.basename(f))
        for j in sorted(_glob.glob(os.path.join(sdir, "words_*.json"))):   # transcripts too (cheap, enables full restore)
            z.write(j, os.path.basename(j))
    return FileResponse(zpath, filename="assembler_localized.zip", media_type="application/zip")


class PosterReq(BaseModel):
    file_id: str
    shots: List[dict]   # [{"file": "batch_x.mp4", "t": 1.2}]  t = seconds (e.g. when the first caption appears)


def _render_poster(out_dir, fname, t):
    """One poster frame: PNG in ORIGINAL resolution; if >1 MB shrink the palette, never the size."""
    import subprocess
    f = os.path.join(out_dir, os.path.basename(fname))
    if not os.path.exists(f):
        return None
    png = os.path.join(out_dir, os.path.splitext(os.path.basename(f))[0] + ".png")
    subprocess.run([ffmpeg_utils.FFMPEG, "-y", "-ss", f"{max(0.1, float(t))}", "-i", f,
                    "-frames:v", "1", "-compression_level", "100", png],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.path.exists(png) and os.path.getsize(png) > 1_000_000:
        try:
            from PIL import Image
            im = Image.open(png).convert("RGB")
            for colors in (256, 128, 64, 32):
                im.quantize(colors=colors, method=Image.FASTOCTREE).save(png, optimize=True)
                if os.path.getsize(png) <= 1_000_000:
                    break
        except Exception:
            pass
    return png if os.path.exists(png) else None


@app.post("/api/posters")
def posters(req: PosterReq):
    """Zip of poster frames for every final video."""
    sdir = _session_dir(req.file_id)
    out = os.path.join(sdir, "output")
    import zipfile as _zip
    made = [p for s in req.shots if (p := _render_poster(out, s.get("file", ""), s.get("t", 1.0)))]
    if not made:
        raise HTTPException(404, "No frames to capture (render first)")
    zpath = os.path.join(sdir, "assembler_posters.zip")
    with _zip.ZipFile(zpath, "w", _zip.ZIP_STORED) as z:
        for p in made:
            z.write(p, os.path.basename(p))
    return FileResponse(zpath, filename="assembler_posters.zip", media_type="application/zip")


def _safe_name(name, fallback="creative"):
    n = "".join(ch for ch in (name or "") if ch.isalnum() or ch in " _-.").strip()
    return n or fallback


class PosterOneReq(BaseModel):
    file_id: str
    file: str
    t: float = 1.0
    name: Optional[str] = None   # custom export filename (without extension)


@app.post("/api/poster_one")
def poster_one(req: PosterOneReq):
    """A single poster PNG (original resolution) — no zip, for one preview."""
    sdir = _session_dir(req.file_id)
    png = _render_poster(os.path.join(sdir, "output"), req.file, req.t)
    if not png:
        raise HTTPException(404, "Clip not found (render first)")
    dl = _safe_name(req.name, os.path.splitext(os.path.basename(png))[0]) + ".png"
    return FileResponse(png, filename=dl, media_type="image/png")


@app.post("/api/export_all")
def export_all(req: PosterReq):
    """One archive: final videos + auto-generated PNG posters (in /videos and /posters),
    named with the user's creative names (video and poster share the same name)."""
    sdir = _session_dir(req.file_id)
    out = os.path.join(sdir, "output")
    import glob as _glob, zipfile as _zip
    zpath = os.path.join(sdir, "assembler_export.zip")
    n = 0
    used = set()
    with _zip.ZipFile(zpath, "w", _zip.ZIP_STORED) as z:
        shots = req.shots or []
        if shots:
            for s in shots:
                vf = os.path.join(out, os.path.basename(s.get("file", "")))
                if not os.path.exists(vf):
                    continue
                nm = _safe_name(s.get("name"), os.path.splitext(os.path.basename(vf))[0])
                base = nm
                k = 2
                while nm in used:           # avoid duplicate names colliding in the zip
                    nm = f"{base}_{k}"; k += 1
                used.add(nm)
                z.write(vf, f"videos/{nm}.mp4"); n += 1
                p = _render_poster(out, s.get("file", ""), s.get("t", 1.0))
                if p:
                    z.write(p, f"posters/{nm}.png")
        else:  # no naming info — just zip whatever finals exist
            for v in (sorted(_glob.glob(os.path.join(out, "batch_*.mp4"))) or sorted(_glob.glob(os.path.join(out, "*.mp4")))):
                z.write(v, "videos/" + os.path.basename(v)); n += 1
    if not n:
        raise HTTPException(404, "No finished videos yet")
    return FileResponse(zpath, filename="assembler_export.zip", media_type="application/zip")


@app.get("/api/session_outputs/{fid}")
def session_outputs(fid: str):
    """List rendered clips already in this session (to pull localized hooks onto the board)."""
    sdir = _session_dir(fid)
    od = os.path.join(sdir, "output")
    files = [f for f in os.listdir(od) if f.endswith(".mp4")] if os.path.isdir(od) else []
    files = [f for f in files if not f.startswith(("batch_", "assembled_"))]
    return {"outputs": sorted(files)}


class BatchReq(BaseModel):
    file_id: str
    recipes: List[dict]  # [{"name","format","segments":[{"type":"library|session","ref":"..."}]}]
    music: Optional[dict] = None  # {"name"(session file),"volume"0-1,"start"sec,"duck"bool}


@app.post("/api/batch_assemble")
def api_batch_assemble(req: BatchReq):
    """Assemble many creatives at once. Each recipe = an ordered list of segments
    (each pulled from the library or the current session)."""
    import time as _tb
    _batch_t0 = _tb.time()
    try:
        compose.reset_concat_stats()   # so the response can report whether the fast join ran
    except Exception:
        pass
    sdir = _session_dir(req.file_id)
    out_dir = os.path.join(sdir, "output")
    os.makedirs(out_dir, exist_ok=True)

    def resolve(seg):
        t, ref = seg.get("type"), os.path.basename(seg.get("ref", ""))
        if t == "library":
            return library.item_path(seg.get("ref", ""))
        for base in (out_dir, sdir):  # session render dir, then session root (dub_xx.mp4)
            p = os.path.join(base, ref)
            if os.path.exists(p):
                return p
        return None

    def resolve_overlays(ovs):
        out = []
        for o in (ovs or []):
            nm = os.path.basename(o.get("name", ""))
            for base in (sdir, out_dir):
                cand = os.path.join(base, nm)
                if nm and os.path.exists(cand):
                    out.append({"path": cand, "x": o.get("x", 0), "y": o.get("y", 0),
                                "w": o.get("w", 240), "angle": o.get("angle", 0),
                                "in": o.get("in"), "out": o.get("out")})
                    break
        return out

    # optional music bed (a track uploaded into the session)
    def _resolve_track_name(raw):
        for base in (sdir, out_dir):  # a track uploaded into this session
            cand = os.path.join(base, os.path.basename(raw))
            if os.path.exists(cand):
                return cand
        lp = library.item_path(raw)  # or a saved track from the music library (raw == id)
        return lp if (lp and os.path.exists(lp)) else None

    music = None
    if req.music and req.music.get("tracks"):  # multi-track timeline
        tr = []
        for t in req.music["tracks"]:
            mp = _resolve_track_name(t.get("name", ""))
            if mp:
                tr.append({"path": mp, "startSeg": int(t.get("startSeg", 0)),
                           "in": float(t.get("in", 0)), "out": float(t.get("out", 1e6)),
                           "volume": float(t.get("volume", 0.25)), "duck": bool(t.get("duck", True))})
        if tr:
            music = {"tracks": tr}
    elif req.music and req.music.get("name"):  # legacy single track
        mp = _resolve_track_name(req.music["name"])
        if mp:
            music = {"path": mp, "volume": float(req.music.get("volume", 0.25)),
                     "start": float(req.music.get("start", 0.0)),
                     "duck": bool(req.music.get("duck", True))}

    results, errors = [], {}
    for r in req.recipes:
        name = r.get("name", "creative")
        try:
            segs = []
            for s in r.get("segments", []):
                p = resolve(s)
                if p is None:
                    raise RuntimeError(f"segment file not found in this session: '{os.path.basename(s.get('ref',''))}' "
                                       f"(type={s.get('type')}). The clip may belong to a different/older session.")
                # non-destructive: a segment may carry caption-data baked at assemble time
                segs.append({"clip": p, "words": s.get("words"), "style": s.get("style"),
                             "regions": s.get("regions"),   # #10 per-phrase style overrides (optional)
                             "headline": s.get("headline"),  # static localizable text overlay
                             "trim": s.get("trim"), "fade_in": s.get("fade_in"),
                             "cap_in": s.get("cap_in"), "cap_out": s.get("cap_out"),
                             "cuts": s.get("cuts"),
                             "overlays": resolve_overlays(s.get("overlays"))})
            if not segs:
                raise RuntimeError("recipe has no segments")
            safe = "".join(c for c in name if c.isalnum() or c in (" ", "_", "-")).strip().replace(" ", "_") or "creative"
            outp = os.path.join(out_dir, f"batch_{safe}.mp4")
            import time as _t
            _s0 = _t.time()
            compose.assemble_recipe(segs, r.get("format", "9:16"), outp, music=music)
            try:
                _od = ffmpeg_utils.get_video_duration(outp) or 0
                _log_timing("assemble", _t.time() - _s0,
                            f"creative={safe} segs={len(segs)} out_s={_od:.1f} "
                            f"realtime_x={((_t.time()-_s0)/_od if _od else 0):.2f} fmt={r.get('format','9:16')}")
            except Exception:
                pass
            results.append({"name": name, "url": f"/download/{req.file_id}/{os.path.basename(outp)}",
                            "file": os.path.basename(outp)})
        except Exception as e:
            errors[name] = str(e)
    # BATCH TIMING — this is the number that matters for capacity: the real production job
    # (N hooks with captions + a long body → N finished creatives). Note the body is
    # re-encoded once per creative, so a long body dominates the cost.
    try:
        import time as _t2
        _wall = _t2.time() - _batch_t0
        _tot_out = 0.0
        for _r in results:
            try:
                _tot_out += ffmpeg_utils.get_video_duration(os.path.join(out_dir, _r["file"])) or 0
            except Exception:
                pass
        _n = max(1, len(results))
        _stats = {"creatives": len(results), "wall_seconds": round(_wall, 2),
                  "seconds_per_creative": round(_wall / _n, 2),
                  "output_seconds_total": round(_tot_out, 1),
                  "realtime_factor": round(_wall / _tot_out, 3) if _tot_out else None,
                  "creatives_per_min": round(_n / (_wall / 60), 1) if _wall > 0 else None,
                  "encoder": ffmpeg_utils.video_encoder(), "cores": os.cpu_count()}
        try:   # did the fast (stream-copy) join actually run, or did we fall back?
            _cs = getattr(compose, "CONCAT_STATS", {})
            _stats["fast_joins"] = _cs.get("copy", 0)
            _stats["reencoded_joins"] = _cs.get("reencode", 0)
            _stats["fallback_reason"] = _cs.get("why", "")
            # per-operation breakdown: where the time actually went this run
            _ot = dict(getattr(compose, "OP_TIMES", {}))
            _stats["ops"] = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in _ot.items()}
            _stats["other_seconds"] = round(max(0.0, _wall - sum(
                float(_ot.get(k, 0) or 0) for k in ("captions_bake", "normalize", "join_copy", "join_reencode"))), 2)
        except Exception:
            pass
        _log_timing("batch", _wall,
                    f"creatives={_stats['creatives']} per_creative_s={_stats['seconds_per_creative']} "
                    f"out_s_total={_stats['output_seconds_total']} realtime_x={_stats['realtime_factor']} "
                    f"per_min={_stats['creatives_per_min']} cores={_stats['cores']}")
        return {"results": results, "errors": errors, "stats": _stats}
    except Exception:
        return {"results": results, "errors": errors}


@app.get("/download/{fid}/{name}")
def download(fid: str, name: str):
    sdir = _session_dir(fid)
    path = os.path.join(sdir, "output", os.path.basename(name))
    if not os.path.exists(path):
        raise HTTPException(404, "File not found")
    return FileResponse(path, media_type="video/mp4", filename=name)


@app.get("/clip/{fid}/{name}")
def clip(fid: str, name: str):
    """Serve a session clip (a caption-free dubbed video, or an uploaded music track)."""
    sdir = _session_dir(fid)
    path = os.path.join(sdir, os.path.basename(name))
    if not os.path.exists(path):
        raise HTTPException(404, "Clip not found")
    import mimetypes
    mt = mimetypes.guess_type(path)[0] or "video/mp4"
    return FileResponse(path, media_type=mt, filename=name)


@app.get("/library_audio/{item_id:path}")
def library_audio(item_id: str):
    """Serve a library music track (for the waveform + preview)."""
    p = library.item_path(item_id)
    if not p or not os.path.exists(p):
        raise HTTPException(404, "Track not found")
    import mimetypes
    return FileResponse(p, media_type=mimetypes.guess_type(p)[0] or "audio/mpeg")


@app.get("/library_thumb/{item_id:path}")
def library_thumb(item_id: str):
    """A small JPEG poster frame for a library body/packshot (cached next to the file)."""
    p = library.item_path(item_id)
    if not p or not os.path.exists(p):
        raise HTTPException(404, "Library item not found")
    cache = p + ".thumb.jpg"
    if (not os.path.exists(cache)) or os.path.getmtime(cache) < os.path.getmtime(p):
        import subprocess
        subprocess.run(["ffmpeg", "-y", "-ss", "0.5", "-i", p, "-frames:v", "1",
                        "-vf", "scale=160:-2", cache],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    if not os.path.exists(cache):
        raise HTTPException(500, "Could not make thumbnail")
    return FileResponse(cache, media_type="image/jpeg")


# Serve font files so the browser preview can use the EXACT same font as the render.
if os.path.isdir(st.FONTS_DIR):
    app.mount("/fonts", StaticFiles(directory=st.FONTS_DIR), name="fonts")

# Serve the web app last so /api and /fonts routes win.
if os.path.isdir(WEB_DIR):
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")


def main():
    import webbrowser
    import threading
    import uvicorn

    host, port = "127.0.0.1", int(os.environ.get("CS_PORT", "8765"))
    threading.Timer(1.2, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    print(f"\n  Captions Studio -> http://{host}:{port}  (encoder: {ffmpeg_utils.video_encoder()})\n")
    # log_config=None: don't let uvicorn run its dictConfig (fails in a frozen .app
    # with "Unable to configure formatter 'default'"). loop/http forced to pure-Python
    # implementations so the bundle needs no uvloop/httptools C extensions.
    uvicorn.run(app, host=host, port=port, loop="asyncio", http="h11",
                log_config=None, log_level="warning")


if __name__ == "__main__":
    main()
