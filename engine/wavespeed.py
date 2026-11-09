"""WaveSpeed AI (Seedance 2.5 video-edit for ⧉ parts). The key lives ONLY in
~/.assembler/config.json (wavespeed_api_key) / env WAVESPEED_API_KEY — never in the repo."""
import os

API = "https://api.wavespeed.ai/api/v3"


class WaveSpeedError(Exception):
    pass


def _key(api_key=None):
    k = (api_key or os.environ.get("WAVESPEED_API_KEY") or "").strip()
    if not k:
        raise WaveSpeedError("Немає ключа WaveSpeed — додай його в ⚙ Settings.")
    return k


def balance(requests, api_key=None):
    """→ USD balance (float). Also the key check: 401 = wrong key."""
    h = {"Authorization": f"Bearer {_key(api_key)}"}
    last = None
    for path in ("/balance", "/account/balance"):
        r = requests.get(API + path, headers=h, timeout=20)
        if r.status_code in (401, 403):
            raise WaveSpeedError("WaveSpeed не приймає ключ (401) — перевір, що скопійовано повністю.")
        if r.status_code == 200:
            d = r.json() or {}
            v = (d.get("data") or {}).get("balance", d.get("balance"))
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        last = r.status_code
    raise WaveSpeedError(f"WaveSpeed: не вдалося прочитати баланс (HTTP {last}).")


# ---------------------------------------------------------------- Seedance 2.5 video-edit
# The clip goes up to WaveSpeed storage (kept there 7 days), Seedance rewrites what the prompt
# names and keeps motion / composition; output length and aspect follow the input (4–30 s:
# shorter is padded to 4 s, longer cut at 30 s). Priced on input + output seconds.
MODEL = os.environ.get("CS_SEEDANCE_MODEL", "bytedance/seedance-2.5/video-edit")
RATES = {"480p": 0.11, "720p": 0.22, "1080p": 0.55}


def estimate(duration_s, resolution="720p"):
    """→ (usd, billed_seconds). Input is billed within 4–30 s; output = input length."""
    d = max(4.0, min(30.0, float(duration_s or 0)))
    rate = RATES.get(resolution, RATES["720p"])
    return round(2 * d * rate, 2), round(2 * d, 1)


def _j(r):
    try:
        d = r.json()
    except Exception:
        d = {}
    return d.get("data", d) if isinstance(d, dict) else {}


def _err(r, what):
    if r.status_code in (401, 403):
        return "WaveSpeed не приймає ключ (401)."
    if r.status_code == 402:
        return "WaveSpeed: недостатньо коштів на балансі."
    if r.status_code == 429:
        return "WaveSpeed: ліміт запитів — спробуй за хвилину."
    try:
        msg = (r.json() or {}).get("message") or r.text[:200]
    except Exception:
        msg = r.text[:200]
    return f"WaveSpeed {what}: HTTP {r.status_code} · {msg}"


def upload(path, requests, api_key=None):
    """Local file → WaveSpeed download_url (direct upload; legacy binary as a fallback)."""
    import mimetypes
    h = {"Authorization": f"Bearer {_key(api_key)}"}
    name = os.path.basename(path)
    size = os.path.getsize(path)
    ct = mimetypes.guess_type(name)[0] or "video/mp4"
    r = requests.post(API + "/media/uploads", headers=h, json={"filename": name, "size": size, "content_type": ct}, timeout=30)
    if r.status_code == 200:
        t = _j(r)
        up = t.get("upload") or {}
        with open(path, "rb") as f:
            p = requests.put(up["url"], headers=up.get("headers") or {}, data=f, timeout=600)
        if not (200 <= p.status_code < 300):
            raise WaveSpeedError(f"WaveSpeed upload: PUT HTTP {p.status_code}")
        return t["download_url"]
    if r.status_code in (401, 403, 402, 413, 429):
        raise WaveSpeedError(_err(r, "upload"))
    with open(path, "rb") as f:   # older accounts / API: one multipart request
        r = requests.post(API + "/media/upload/binary", headers=h, files={"file": (name, f, ct)}, timeout=600)
    if r.status_code != 200:
        raise WaveSpeedError(_err(r, "upload"))
    return _j(r)["download_url"]


def submit_edit(video_url, prompt, requests, resolution="720p", keep_audio=True, api_key=None, model=None, refs=None):
    """→ prediction id. refs = reference image URLs (@Image 1, @Image 2… in the prompt, ≤ 30)."""
    body = {"prompt": (prompt or "").strip(), "video": video_url, "resolution": resolution,
            "generate_audio": not keep_audio}
    if refs:
        body["reference_images"] = list(refs)[:30]
    if not body["prompt"]:
        raise WaveSpeedError("Опиши, що змінити в кадрі.")
    r = requests.post(f"{API}/{model or MODEL}", headers={"Authorization": f"Bearer {_key(api_key)}",
                                                          "Content-Type": "application/json"}, json=body, timeout=60)
    if r.status_code != 200:
        raise WaveSpeedError(_err(r, "submit"))
    pid = _j(r).get("id")
    if not pid:
        raise WaveSpeedError("WaveSpeed не повернув id задачі.")
    return pid


def result(pid, requests, api_key=None):
    """→ {"status", "outputs", "error"} — status: created / processing / completed / failed…"""
    r = requests.get(f"{API}/predictions/{pid}/result", headers={"Authorization": f"Bearer {_key(api_key)}"}, timeout=30)
    if r.status_code != 200:
        raise WaveSpeedError(_err(r, "result"))
    d = _j(r)
    return {"status": d.get("status") or "", "outputs": d.get("outputs") or [], "error": d.get("error") or ""}


# ---------------------------------------------------------------- video → foley (auto SFX)
# HunyuanVideo-Foley: watches a piece of video and writes the matching sounds (door, steps, cloth,
# curtains…) synced to the picture, optionally steered by a short prompt. ~$0.05 a run.
FOLEY_MODEL = os.environ.get("CS_FOLEY_MODEL", "wavespeed-ai/hunyuan-video-foley")
FOLEY_PRICE = 0.05


def submit_foley(video_url, prompt, requests, api_key=None, seed=-1, model=None):
    body = {"video": video_url, "seed": int(seed)}
    if (prompt or "").strip():
        body["prompt"] = prompt.strip()
    r = requests.post(f"{API}/{model or FOLEY_MODEL}", headers={"Authorization": f"Bearer {_key(api_key)}",
                                                                "Content-Type": "application/json"}, json=body, timeout=60)
    if r.status_code != 200:
        raise WaveSpeedError(_err(r, "foley"))
    pid = _j(r).get("id")
    if not pid:
        raise WaveSpeedError("WaveSpeed не повернув id задачі.")
    return pid
