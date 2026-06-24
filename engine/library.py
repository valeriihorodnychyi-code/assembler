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
    """Pull a language tag from a name_LANG.ext filename (the naming convention)."""
    import re
    m = re.search(r"_([a-zA-Z]{2})\.[^.]+$", filename)
    if m:
        return m.group(1).lower()
    if re.search(r"caption|_en\b", filename, re.IGNORECASE):
        return "en"
    return ""


def _derive_format(path):
    """Guess aspect-ratio bucket from a video file (for files dropped in directly)."""
    try:
        w, h = ff.get_video_size(path)
        if w > h:
            return "16:9"
        if w == h:
            return "1:1"
        return "9:16"
    except Exception:
        return ""


def _manifest_path():
    return os.path.join(LIBRARY_DIR, "library.json")


def _load():
    os.makedirs(LIBRARY_DIR, exist_ok=True)
    p = _manifest_path()
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
        for f in sorted(os.listdir(d)):
            if f.lower().endswith(exts):
                rel = (sub + "/" + f) if sub else f
                items.append({"id": rel, "name": os.path.splitext(f)[0], "lang": _derive_lang(f),
                              "format": _derive_format(os.path.join(d, f)),
                              "file": rel, "type": typ, "added": "(in folder)"})
    return items


def list_items(fmt=None, lang=None, type=None):
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
    return items


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
