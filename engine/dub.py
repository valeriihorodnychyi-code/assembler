"""ElevenLabs dubbing -> a localized video clip.

Ported and cleaned from the original dubber.py. Key difference from a naive dub:
we keep the ORIGINAL video track and only swap in the AI audio (ffmpeg -c:v copy),
which avoids the black frames / re-encode the old script worked around.

The API key comes from the environment (ELEVENLABS_API_KEY) — never hardcoded,
because the project/library is shared across the team.
"""
import os
import time
import tempfile

from . import ffmpeg_utils as ff


DUBBING_URL = "https://api.elevenlabs.io/v1/dubbing"


def dub_clip(video_path, target_lang, source_lang="en", num_speakers=1,
             api_key=None, work_dir=None, poll_secs=10, timeout_secs=1800,
             progress=None):
    """Dub `video_path` into `target_lang`; return path to the localized video.

    Raises RuntimeError with a clean message on any failure so the UI can show it.
    """
    import requests  # lazy

    api_key = api_key or os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise RuntimeError("ElevenLabs API key missing (set ELEVENLABS_API_KEY).")
    work_dir = work_dir or tempfile.mkdtemp(prefix="cs_dub_")
    headers = {"xi-api-key": api_key}

    if progress:
        progress(f"Uploading for {target_lang} dubbing…")
    with open(video_path, "rb") as f:
        resp = requests.post(
            DUBBING_URL, headers=headers,
            files={"file": (os.path.basename(video_path), f, "video/mp4")},
            data={"target_lang": target_lang, "source_lang": source_lang,
                  "num_speakers": num_speakers},
            timeout=300,
        )
    if resp.status_code != 200:
        raise RuntimeError(f"Dub upload failed ({resp.status_code}): {resp.text[:300]}")
    dubbing_id = resp.json().get("dubbing_id")
    if not dubbing_id:
        raise RuntimeError(f"No dubbing_id in response: {resp.text[:300]}")

    # Poll until ready.
    started = time.time()
    while True:
        s = requests.get(f"{DUBBING_URL}/{dubbing_id}", headers=headers, timeout=60)
        status = s.json().get("status") if s.status_code == 200 else None
        if status == "dubbed":
            break
        if status == "failed":
            raise RuntimeError("ElevenLabs reported the dubbing failed.")
        if time.time() - started > timeout_secs:
            raise RuntimeError(f"Dubbing timed out after {timeout_secs}s (status={status}).")
        if progress:
            progress(f"{target_lang}: {status or 'working'}…")
        time.sleep(poll_secs)

    if progress:
        progress(f"Downloading {target_lang} audio…")
    audio = requests.get(f"{DUBBING_URL}/{dubbing_id}/audio/{target_lang}",
                         headers=headers, timeout=300)
    if audio.status_code != 200:
        raise RuntimeError(f"Dub download failed ({audio.status_code}): {audio.text[:200]}")

    base = os.path.splitext(os.path.basename(video_path))[0]
    tmp_audio = os.path.join(work_dir, f"_eltmp_{base}_{target_lang}.mp4")
    with open(tmp_audio, "wb") as f:
        f.write(audio.content)

    # Mux: original video + AI audio. Keeps perfect picture, just new voice.
    out = os.path.join(work_dir, f"{base}_{target_lang}.mp4")
    ff.run([
        "ffmpeg", "-i", video_path, "-i", tmp_audio,
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k", "-shortest", out, "-y",
    ])
    if os.path.exists(tmp_audio):
        os.remove(tmp_audio)
    return out
