"""sync. labs (sync.so) — dubbing + lipsync in ONE call -> a localized video clip.

Why it's the cheapest provider to add: sync.so can do the whole job itself. We upload the
clip and pass `dubParams`, so sync.so extracts the audio, dubs it into the target language
(via their ElevenLabs integration) and then runs lipsync on the dubbed audio. No separate
audio step on our side.

The signature mirrors dub.dub_clip / heygen.dub_clip, so the localization orchestration
stays provider-agnostic — the localized clip re-enters the SAME pipeline afterwards
(re-transcribe -> caption -> assemble).

API: POST https://api.sync.so/v2/generate  (multipart when uploading a local file)
     GET  https://api.sync.so/v2/generate/{id}  -> status + outputUrl
Auth: `x-api-key` header. Key from env SYNCLAB_API_KEY (or config.json "synclab_api_key").
Docs: https://sync.so/docs/api-reference/api/generate-api/create
"""
import os
import time
import json
import tempfile

BASE = os.environ.get("CS_SYNCLAB_BASE", "https://api.sync.so/v2")
GENERATE_URL = BASE + "/generate"
STATUS_URL = BASE + "/generate/{id}"

# lipsync-2 = fast + cost-efficient (their default recommendation). Override with
# CS_SYNCLAB_MODEL if you want lipsync-2-pro / sync-3 quality instead.
MODEL = os.environ.get("CS_SYNCLAB_MODEL", "lipsync-2")

# Direct file upload is capped at 20MB by the API (bigger media needs a public URL or
# their assets flow). Our hooks are a few MB, so the multipart path covers the real use.
MAX_UPLOAD = 20 * 1024 * 1024

_TERMINAL_OK = ("COMPLETED",)
_TERMINAL_BAD = ("FAILED", "REJECTED", "CANCELED", "CANCELLED")
_CT = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm", ".m4v": "video/x-m4v"}


def _key(api_key=None):
    api_key = api_key or os.environ.get("SYNCLAB_API_KEY")
    if not api_key:
        raise RuntimeError("Sync Lab API key missing (set it in ⚙ Settings, or SYNCLAB_API_KEY).")
    return api_key


def submit(video_path, target_lang, source_lang="en", api_key=None, model=None):
    """Start a dub+lipsync generation for one language. Returns the generation id."""
    import requests  # lazy
    api_key = _key(api_key)
    size = os.path.getsize(video_path)
    if size > MAX_UPLOAD:
        raise RuntimeError(
            f"Sync Lab accepts direct uploads up to 20MB; this clip is {size / 1e6:.1f}MB. "
            "Trim it (or localize the hook instead of the whole creative).")
    dub_params = {"providerName": "elevenlabs", "targetLang": target_lang}
    if source_lang:
        dub_params["sourceLang"] = source_lang
    name = os.path.basename(video_path)
    ct = _CT.get(os.path.splitext(name)[1].lower(), "video/mp4")
    with open(video_path, "rb") as fh:
        # Nested fields must be JSON *strings* in a multipart request (per their docs).
        r = requests.post(
            GENERATE_URL,
            headers={"x-api-key": api_key},
            files={"video": (name, fh, ct)},
            data={"model": (model or MODEL), "dubParams": json.dumps(dub_params)},
            timeout=600,
        )
    if r.status_code >= 300:
        raise RuntimeError(f"Sync Lab submit failed ({r.status_code}): {r.text[:300]}")
    data = r.json() or {}
    gen_id = data.get("id")
    if not gen_id:
        raise RuntimeError(f"Sync Lab submit: no id in response: {r.text[:300]}")
    return gen_id


def wait(gen_id, api_key=None, poll_secs=10, timeout_secs=1800, progress=None):
    """Poll until the generation finishes. Returns the output media URL."""
    import requests
    api_key = _key(api_key)
    t0 = time.time()
    last = ""
    while True:
        r = requests.get(STATUS_URL.format(id=gen_id),
                         headers={"x-api-key": api_key}, timeout=60)
        if r.status_code >= 300:
            raise RuntimeError(f"Sync Lab status failed ({r.status_code}): {r.text[:300]}")
        d = r.json() or {}
        status = (d.get("status") or "").upper()
        if status != last:
            last = status
            if progress:
                progress(f"Sync Lab: {status.lower() or 'queued'}…")
        if status in _TERMINAL_OK:
            url = d.get("outputUrl")
            if not url:
                raise RuntimeError("Sync Lab finished but returned no outputUrl.")
            return url
        if status in _TERMINAL_BAD:
            # errorCode is stable/machine-readable; the message is the human part
            raise RuntimeError("Sync Lab "
                               + status.lower()
                               + ": " + str(d.get("error") or d.get("errorCode") or "no reason given"))
        if time.time() - t0 > timeout_secs:
            raise RuntimeError(f"Sync Lab timed out after {int(timeout_secs / 60)} min (last status: {status}).")
        time.sleep(poll_secs)


def dub_clip(video_path, target_lang, source_lang="en", api_key=None, work_dir=None,
             progress=None, model=None, **_):
    """Dub + lipsync `video_path` into `target_lang`; return the path to the localized video.

    Mirrors dub.dub_clip / heygen.dub_clip so localize.py can swap providers freely.
    """
    import requests
    api_key = _key(api_key)
    work_dir = work_dir or tempfile.mkdtemp(prefix="cs_synclab_")
    os.makedirs(work_dir, exist_ok=True)
    if progress:
        progress(f"Uploading to Sync Lab for {target_lang}…")
    gen_id = submit(video_path, target_lang, source_lang=source_lang,
                    api_key=api_key, model=model)
    url = wait(gen_id, api_key=api_key, progress=progress)
    out = os.path.join(work_dir, f"synclab_{target_lang}_{gen_id[:8]}.mp4")
    with requests.get(url, stream=True, timeout=600) as r:
        if r.status_code >= 300:
            raise RuntimeError(f"Sync Lab download failed ({r.status_code}).")
        with open(out, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                if chunk:
                    f.write(chunk)
    if not os.path.exists(out) or os.path.getsize(out) < 1000:
        raise RuntimeError("Sync Lab download produced an empty file.")
    return out
