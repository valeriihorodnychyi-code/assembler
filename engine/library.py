"""Body-part library: a folder of reusable (localized) clips + a JSON manifest.

The team's economy: long body parts are localized ONCE and reused. The library is
just a folder (ideally a shared Drive/Dropbox-synced folder so the whole team shares
it with zero backend) plus library.json describing each clip's name/language/format.
"""
import os
import json
import time
import uuid
import shutil

from . import ffmpeg_utils as ff

LIBRARY_DIR = os.environ.get("CS_LIBRARY_DIR", "library")
VIDEO_EXT = (".mp4", ".mov", ".m4v", ".webm")
AUDIO_EXT = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")


def _derive_lang(filename):
    """Pull a language tag from the filename.

    Two conventions are understood:
      our own : name_LANG.ext                    (serum_body_es.mp4)
      ADAM    : Kind_id_LANG_(n)Sub_WxH.ext      (Body_12_EN_Sub_1080x1080.mp4)
    """
    import re
    m = re.search(r"_([a-zA-Z]{2})_n?Sub_", filename, re.IGNORECASE)   # ADAM parts
    if m:
        return m.group(1).lower()
    m = re.search(r"_([a-zA-Z]{2})\.[^.]+$", filename)
    if m:
        return m.group(1).lower()
    if re.search(r"caption|_en\b", filename, re.IGNORECASE):
        return "en"
    return ""


def _bucket(w, h):
    if not w or not h:
        return ""
    return "16:9" if w > h else ("1:1" if w == h else "9:16")


# Probe results are cached LOCALLY (never in the shared folder): probing every loose file on
# every library listing meant one ffprobe subprocess per file, and on a Google Drive stream
# mount each of those pulls data over the network — that's what made the app crawl when Drive
# was slow or offline.
_PROBE_CACHE = None
_PROBE_DIRTY = False


def _probe_cache_path():
    return os.path.join(os.path.expanduser("~"), ".assembler", "library_probe.json")


def _probe_cache():
    global _PROBE_CACHE
    if _PROBE_CACHE is None:
        try:
            with open(_probe_cache_path(), encoding="utf-8") as f:
                _PROBE_CACHE = json.load(f)
        except Exception:
            _PROBE_CACHE = {}
    return _PROBE_CACHE


def flush_probe_cache():
    global _PROBE_DIRTY
    if not _PROBE_DIRTY:
        return
    try:
        p = _probe_cache_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(_PROBE_CACHE or {}, f)
        _PROBE_DIRTY = False
    except Exception:
        pass


def _derive_format(path, stat=None):
    """Aspect-ratio bucket for a file. Order matters — cheapest first:
       1) the size baked into the NAME (…_1080x1920.mp4 — ADAM assets and our exports have it)
       2) a cached probe (keyed by size+mtime)
       3) ffprobe, then cache it
    """
    global _PROBE_DIRTY
    import re
    m = re.search(r"(\d{3,4})\s*[xX]\s*(\d{3,4})", os.path.basename(path))
    if m:
        return _bucket(int(m.group(1)), int(m.group(2)))
    try:
        stt = stat or os.stat(path)
        key = os.path.abspath(path)
        sig = f"{stt.st_size}|{int(stt.st_mtime)}"
        hit = _probe_cache().get(key)
        if hit and hit.get("sig") == sig:
            return hit.get("format", "")
    except OSError:
        return ""
    try:
        w, h = ff.get_video_size(path)
        fmt = _bucket(w, h)
    except Exception:
        fmt = ""
    _probe_cache()[key] = {"sig": sig, "format": fmt}
    _PROBE_DIRTY = True
    return fmt


def _manifest_path():
    return os.path.join(LIBRARY_DIR, "library.json")


def _load():
    p = _manifest_path()   # read-only: never create the folder here (a missing Drive mount
                           # would get a local shadow folder that Drive then fights over)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"items": []}


def _save(data):
    os.makedirs(LIBRARY_DIR, exist_ok=True)
    with open(_manifest_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# Subfolders so the team can sort files; the app knows which is which by folder.
SUBDIRS = {"body": "bodies", "packshot": "packshots", "music": "music"}


def _scan_folder(sub, typ, exts=VIDEO_EXT):
    items = []
    d = os.path.join(LIBRARY_DIR, sub) if sub else LIBRARY_DIR
    if os.path.isdir(d):
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)   # one syscall for name+stat
        except OSError:
            return items
        for e in entries:
            f = e.name
            if not f.lower().endswith(exts):
                continue
            try:
                stt = e.stat()
            except OSError:
                stt = None
            rel = (sub + "/" + f) if sub else f
            items.append({"id": rel, "name": os.path.splitext(f)[0], "lang": _derive_lang(f),
                          "format": _derive_format(os.path.join(d, f), stat=stt),
                          "size_mb": round((stt.st_size / 1e6), 1) if stt else 0,
                          "file": rel, "type": typ, "added": "(in folder)"})
    return items


def _with_size(it):
    """Attach the file size (free — no ffprobe). Lets the UI show WHY one language version of a
    creative comes out heavier: a body that already matches the render params is stream-copied
    untouched, so its own bitrate carries straight into the final file."""
    if it.get("size_mb"):
        return it
    try:
        it["size_mb"] = round(os.path.getsize(os.path.join(LIBRARY_DIR, it["file"])) / 1e6, 1)
    except OSError:
        it["size_mb"] = 0
    return it


def dir_ok():
    """Is the library folder actually reachable right now? (Google Drive not mounted / signed
    out / renamed folder → False, and the app must say so instead of showing an empty list.)"""
    try:
        return os.path.isdir(LIBRARY_DIR)
    except OSError:
        return False


def list_items(fmt=None, lang=None, type=None):
    if not dir_ok():
        return []          # folder gone → return fast, don't stat/probe anything
    data = _load()
    items = [it for it in data["items"] if os.path.exists(os.path.join(LIBRARY_DIR, it["file"]))]
    for it in items:
        it.setdefault("type", "body")
    known = {it["file"] for it in items}
    # pick up loose files: bodies/ -> body, packshots/ -> packshot, root -> body
    for it in (_scan_folder("bodies", "body") + _scan_folder("packshots", "packshot")
               + _scan_folder("music", "music", AUDIO_EXT) + _scan_folder("", "body")):
        if it["file"] not in known:
            items.append(it)
            known.add(it["file"])
    if fmt:
        items = [it for it in items if it.get("format") == fmt]
    if lang:
        items = [it for it in items if it.get("lang") == lang]
    if type:
        items = [it for it in items if it.get("type") == type]
    out = [_with_size(it) for it in items]
    flush_probe_cache()   # persist any new probes so the next listing is instant
    return out


def add_item(src_path, name, lang, fmt, kind="body"):
    """Copy a clip into the library (into bodies/ or packshots/) and register it."""
    if not os.path.exists(src_path):
        raise FileNotFoundError(src_path)
    sub = SUBDIRS.get(kind, "bodies")
    dest_dir = os.path.join(LIBRARY_DIR, sub)
    os.makedirs(dest_dir, exist_ok=True)
    item_id = uuid.uuid4().hex[:10]
    safe = "".join(c for c in (name or kind) if c.isalnum() or c in (" ", "_", "-")).strip().replace(" ", "_") or kind
    ext = os.path.splitext(src_path)[1].lower() or (".mp3" if kind == "music" else ".mp4")
    fname = f"{item_id}__{safe}{ext}"
    rel = sub + "/" + fname
    shutil.copy(src_path, os.path.join(LIBRARY_DIR, rel))
    item = {"id": rel, "name": name or safe, "lang": lang or "", "format": fmt or "",
            "file": rel, "type": kind, "added": time.strftime("%Y-%m-%d %H:%M")}
    data = _load()
    data["items"].append(item)
    _save(data)
    return item


def item_path(item_id):
    for it in _load()["items"]:
        if it["id"] == item_id:
            p = os.path.join(LIBRARY_DIR, it["file"])
            if os.path.exists(p):
                return p
    # loose file: id may be "bodies/x.mp4", "packshots/x.mp4" or just "x.mp4"
    for cand in (item_id, os.path.join("bodies", os.path.basename(item_id)),
                 os.path.join("packshots", os.path.basename(item_id)),
                 os.path.basename(item_id)):
        p = os.path.join(LIBRARY_DIR, cand)
        if os.path.exists(p):
            return p
    return None


def delete_item(item_id):
    data = _load()
    keep, removed = [], None
    for it in data["items"]:
        if it["id"] == item_id:
            removed = it
        else:
            keep.append(it)
    if removed:
        fp = os.path.join(LIBRARY_DIR, removed["file"])
        if os.path.exists(fp):
            try:
                os.remove(fp)
            except OSError:
                pass
        data["items"] = keep
        _save(data)
        return True
    p = item_path(item_id)  # loose file
    if p and os.path.exists(p):
        try:
            os.remove(p)
            return True
        except OSError:
            pass
    return False
