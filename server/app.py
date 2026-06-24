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
from engine import styles as st, compose, transcribe, ffmpeg_utils, localize, library, subtitles  # noqa: E402

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

# styles/fonts live next to the project root
os.environ.setdefault("CS_STYLES_DIR", os.path.join(ROOT, "styles"))
os.environ.setdefault("CS_FONTS_DIR", os.path.join(ROOT, "fonts"))
st.STYLES_DIR = os.environ["CS_STYLES_DIR"]
st.FONTS_DIR = os.environ["CS_FONTS_DIR"]

# Library lives in a configurable folder (point CS_LIBRARY_DIR / config.json
# "library_dir" at a shared Drive/Dropbox folder to share it across the team).
os.environ.setdefault("CS_LIBRARY_DIR", os.path.join(ROOT, "library"))
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
        if _cfg.get("library_dir") and not os.environ.get("CS_LIBRARY_DIR"):
            os.environ["CS_LIBRARY_DIR"] = os.path.expanduser(_cfg["library_dir"])
    except Exception:
        pass

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
    d = st.FONTS_DIR
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d) if f.lower().endswith((".ttf", ".otf", ".ttc")))


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
    }


def save_library_dir(path):
    """Persist the shared library folder to the per-machine config and apply it live."""
    path = os.path.expanduser((path or "").strip())
    if not path:
        return
    os.makedirs(path, exist_ok=True)
    os.environ["CS_LIBRARY_DIR"] = path
    library.LIBRARY_DIR = path
    os.makedirs(os.path.dirname(_PCFG), exist_ok=True)
    cur = {}
    if os.path.exists(_PCFG):
        try:
            cur = _json.load(open(_PCFG, encoding="utf-8"))
        except Exception:
            cur = {}
    cur["library_dir"] = path
    _json.dump(cur, open(_PCFG, "w", encoding="utf-8"), indent=2)


class KeysReq(BaseModel):
    elevenlabs_api_key: Optional[str] = None
    heygen_api_key: Optional[str] = None
    library_dir: Optional[str] = None


@app.get("/api/settings")
def get_settings():
    return {"machine_id": machine_id(), "allowed": license_ok(),
            "scribe_key_present": bool(os.environ.get("ELEVENLABS_API_KEY")),
            "heygen_key_present": bool(os.environ.get("HEYGEN_API_KEY")),
            "library_dir": os.environ.get("CS_LIBRARY_DIR", ""),
            "library_exists": os.path.isdir(os.environ.get("CS_LIBRARY_DIR", "")),
            "keys_location": _PCFG}


@app.post("/api/settings")
def post_settings(body: KeysReq):
    save_keys((body.elevenlabs_api_key or "").strip(), (body.heygen_api_key or "").strip())
    if body.library_dir is not None and body.library_dir.strip():
        save_library_dir(body.library_dir)
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


@app.get("/api/styles")
def get_styles():
    return {"styles": st.list_styles()}


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
    return {"saved": os.path.basename(path), "styles": st.list_styles()}


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

    import json
    with open(os.path.join(sdir, "words.json"), "w", encoding="utf-8") as wf:
        json.dump(result["words"], wf)

    return {
        "file_id": fid,
        "width": w, "height": h, "duration": dur,
        "language": result["language"],
        "words": result["words"],
    }


class PreviewFrameReq(BaseModel):
    style: dict
    words: Optional[List[dict]] = None
    time: float = 0.0
    format: str = "9:16"
    dur: float = 0.0   # clip duration, so the last caption "holds" like in the real render


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
    scale = st.DEFAULT_SCALE_FACTORS.get(req.format, 1.0)
    tagged = compose.build_timeline(req.words or [], [{"start": 0, "end": None, "style": style}])
    events = [e for e, _ in tagged]
    if events and req.dur:
        events[-1]["end"] = max(events[-1]["end"], req.dur)  # hold last caption to clip end
    ev = next((e for e in events if req.time >= e["start"] and req.time < e["end"]), None)
    if ev is None:
        return Response(status_code=204)  # nothing on screen at this moment
    import tempfile
    tmp = tempfile.mktemp(suffix=".png")
    font = st.resolve_font(style.get("font_name"))
    subtitles.render_subtitle_png(ev, tmp, TW, TH, font, style, scale)
    with open(tmp, "rb") as f:
        data = f.read()
    os.remove(tmp)
    return Response(content=data, media_type="image/png")


class RenderReq(BaseModel):
    file_id: str
    regions: list                  # [{"start","end","style"}]
    words: Optional[List[dict]] = None  # edited word timecodes; falls back to stored transcription
    formats: list = ["9:16"]
    smart_trim: bool = False
    use_body: bool = False          # use body files placed in the session dir / project root
    clip: str = "source.mp4"        # which clip in the session to caption (e.g. dub_es.mp4)
    trim: Optional[list] = None     # [start, end] seconds — same trim the Compose board applies


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

    try:
        outputs = compose.render(
            src, words or [], req.regions, req.formats, out_dir,
            bodies=bodies, default_body=default_body, smart_trim=req.smart_trim,
            out_prefix=out_prefix,
        )
    except Exception as e:
        raise HTTPException(500, f"Render failed: {e}")

    return {"outputs": [
        {"format": req.formats[i], "url": f"/download/{req.file_id}/{os.path.basename(p)}",
         "name": os.path.basename(p)}
        for i, p in enumerate(outputs)
    ]}


class DubReq(BaseModel):
    file_id: str
    target_langs: List[str]
    source_lang: str = "en"
    transcribe_engine: str = "whisper"
    model_size: str = "small"
    provider: str = "elevenlabs"  # elevenlabs (voice dub) | heygen (lip-sync)


@app.post("/api/dub")
def api_dub(req: DubReq):
    """Stage 1: dub the source into each language and re-transcribe it.

    Returns clean (caption-free) dubbed clips + editable transcripts. Captioning is
    done afterwards via /api/render with clip="dub_<lang>.mp4" and the (edited) words.
    """
    sdir = _session_dir(req.file_id)
    src = os.path.join(sdir, "source.mp4")
    if not os.path.exists(src):
        raise HTTPException(404, "Source video missing")
    try:
        results, errors = localize.dub_and_transcribe(
            src, req.target_langs, sdir,
            source_lang=req.source_lang, api_key=os.environ.get("ELEVENLABS_API_KEY"),
            transcribe_engine=req.transcribe_engine, model_size=req.model_size,
            provider=req.provider,
            provider_key=os.environ.get("HEYGEN_API_KEY") if req.provider == "heygen"
            else os.environ.get("ELEVENLABS_API_KEY"),
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
                                "w": o.get("w", 240), "angle": o.get("angle", 0)})
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
                    raise RuntimeError("a segment file is missing")
                # non-destructive: a segment may carry caption-data baked at assemble time
                segs.append({"clip": p, "words": s.get("words"), "style": s.get("style"),
                             "trim": s.get("trim"), "fade_in": s.get("fade_in"),
                             "overlays": resolve_overlays(s.get("overlays"))})
            if not segs:
                raise RuntimeError("recipe has no segments")
            safe = "".join(c for c in name if c.isalnum() or c in (" ", "_", "-")).strip().replace(" ", "_") or "creative"
            outp = os.path.join(out_dir, f"batch_{safe}.mp4")
            compose.assemble_recipe(segs, r.get("format", "9:16"), outp, music=music)
            results.append({"name": name, "url": f"/download/{req.file_id}/{os.path.basename(outp)}",
                            "file": os.path.basename(outp)})
        except Exception as e:
            errors[name] = str(e)
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
