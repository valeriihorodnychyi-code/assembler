"""HeyGen video translation (lip-sync) -> a localized video clip.

Unlike ElevenLabs (which takes a file upload directly), HeyGen translates from a
URL. For a local app we therefore: upload the clip to HeyGen's asset endpoint to
get a URL (<=32MB), submit a translation, poll, then download the result.

The localized clip then re-enters the SAME pipeline as ElevenLabs dubs
(re-transcribe -> caption), because the engine is provider-agnostic.

API key from env HEYGEN_API_KEY (or config.json "heygen_api_key").
"""
import os
import time
import tempfile

UPLOAD_URL = "https://upload.heygen.com/v1/asset"
TRANSLATE_URL = "https://api.heygen.com/v2/video_translate"
STATUS_URL = "https://api.heygen.com/v2/video_translate/{id}"

# HeyGen wants language *names*, not codes.
LANG_NAMES = {
    "en": "English", "es": "Spanish", "de": "German", "fr": "French",
    "it": "Italian", "pt": "Portuguese", "pl": "Polish", "nl": "Dutch",
    "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "uk": "Ukrainian",
}
_CT = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}
_UPLOAD_CACHE = {}  # local path -> HeyGen asset URL (avoid re-uploading per language)


def _key(api_key):
    api_key = api_key or os.environ.get("HEYGEN_API_KEY")
    if not api_key:
        raise RuntimeError("HeyGen API key missing (set HEYGEN_API_KEY or config.json).")
    return api_key


def upload_asset(video_path, api_key=None):
    """Upload a local clip to HeyGen, return a usable video URL (cached per path)."""
    import requests  # lazy
    if video_path in _UPLOAD_CACHE:
        return _UPLOAD_CACHE[video_path]
    api_key = _key(api_key)
    if os.path.getsize(video_path) > 32 * 1024 * 1024:
        raise RuntimeError("HeyGen upload limit is 32MB — trim/compress the clip first.")
    ct = _CT.get(os.path.splitext(video_path)[1].lower(), "video/mp4")
    with open(video_path, "rb") as f:
        resp = requests.post(UPLOAD_URL, headers={"x-api-key": api_key, "Content-Type": ct},
                             data=f.read(), timeout=300)
    if resp.status_code >= 300:
        raise RuntimeError(f"HeyGen upload failed ({resp.status_code}): {resp.text[:300]}")
    data = resp.json().get("data", {})
    url = data.get("url")
    if not url:
        raise RuntimeError(f"HeyGen upload: no url in response: {resp.text[:300]}")
    _UPLOAD_CACHE[video_path] = url
    return url


def _translate(video_url, lang_name, api_key, title="Captions Studio"):
    import requests
    resp = requests.post(TRANSLATE_URL, headers={"x-api-key": api_key, "Content-Type": "application/json"},
                         json={"video_url": video_url, "output_language": lang_name, "title": title},
                         timeout=120)
    if resp.status_code >= 300:  # HeyGen returns 202 Accepted for async jobs — that's success
        raise RuntimeError(f"HeyGen translate failed ({resp.status_code}): {resp.text[:300]}")
    data = resp.json().get("data", {})
    tid = data.get("video_translate_id") or (data.get("video_translate_ids") or [None])[0]
    if not tid:
        raise RuntimeError(f"HeyGen: no video_translate_id: {resp.text[:300]}")
    return tid


def _poll(tid, api_key, poll_secs=15, timeout_secs=3600, progress=None, lang=""):
    import requests
    started = time.time()
    while True:
        r = requests.get(STATUS_URL.format(id=tid), headers={"x-api-key": api_key}, timeout=60)
        data = r.json().get("data", {}) if r.status_code < 300 else {}
        status = data.get("status")
        if status == "success":
            url = data.get("url")
            if not url:
                raise RuntimeError("HeyGen reported success but no video url.")
            return url
        if status == "failed":
            raise RuntimeError("HeyGen translation failed: " + str(data.get("message", "")))
        if time.time() - started > timeout_secs:
            raise RuntimeError(f"HeyGen timed out (status={status}).")
        if progress:
            progress(f"{lang}: {status or 'working'}…")
        time.sleep(poll_secs)


def dub_clip(video_path, target_lang, api_key=None, work_dir=None, progress=None, **_):
    """Lip-sync-translate a clip into target_lang; return path to the localized video.
    Signature mirrors dub.dub_clip so the localization orchestration is provider-agnostic."""
    import requests
    api_key = _key(api_key)
    work_dir = work_dir or tempfile.mkdtemp(prefix="cs_heygen_")
    lang_name = LANG_NAMES.get(target_lang, target_lang)

    if progress:
        progress(f"Uploading to HeyGen for {target_lang}…")
    url = upload_asset(video_path, api_key)
    if progress:
        progress(f"Translating {target_lang} (lip-sync, can take minutes)…")
    tid = _translate(url, lang_name, api_key)
    out_url = _poll(tid, api_key, progress=progress, lang=target_lang)

    base = os.path.splitext(os.path.basename(video_path))[0]
    out = os.path.join(work_dir, f"{base}_{target_lang}.mp4")
    dl = requests.get(out_url, timeout=600)
    if dl.status_code >= 300:
        raise RuntimeError(f"HeyGen download failed ({dl.status_code}).")
    with open(out, "wb") as f:
        f.write(dl.content)
    return out
