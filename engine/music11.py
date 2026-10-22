"""Background music for HOOKS — one ElevenLabs music bed instead of a different one per shot.

Why: a hook is often stitched from several AI generations, each with its OWN generated music.
At every cut the vibe jumps, and a scene that is one scene reads as five. Bodies are not
touched — they come ready for the join.

Flow per hook:
  1. (optional) strip the old audio bed: ElevenLabs Audio Isolation keeps the voice only
     (it also removes the old SFX/ambience — that's the trade-off). Cached per clip, so
     re-applying another track doesn't pay for isolation again.
  2. lay ONE generated track under the voice: trimmed to the hook, short fade-in, a fade-out
     at the end so the hook hands over cleanly to the body, and ducked under the voice.
  The video stream is copied, the length never changes, the result is a NEW file.

Endpoints (docs checked Oct 2026):
  POST /v1/music            {prompt, music_length_ms 3000–600000, model_id, force_instrumental}
  POST /v1/audio-isolation  multipart `audio`
Key: ELEVENLABS_API_KEY (per machine). Needs the key permissions "Music Generation" and
"Audio Isolation" — they are OFF on restricted keys by default.
"""
import os
import subprocess

API = "https://api.elevenlabs.io"
MODEL = os.environ.get("CS_MUSIC_MODEL", "music_v1")
FADE_IN = 0.08
FADE_OUT = 0.6

# Templates the designer picks from. Instrumental always; written for ad hooks under a voice.
PRESETS = {
    "drama":    "Emotional cinematic drama underscore, warm strings and soft piano, slow build, intimate, "
                "leaves room for a voice-over, no vocals",
    "action":   "Driving action trailer underscore, punchy drums, pulsing synth bass, rising tension, "
                "energetic and tight, leaves room for a voice-over, no vocals",
    "epic":     "Epic orchestral trailer music, big brass and choir-like pads without lyrics, huge drums, "
                "heroic and uplifting build, no vocals",
    "sadness":  "Sad, melancholic ambient piano with soft cello, slow tempo, reflective and tender, "
                "sparse arrangement under a voice-over, no vocals",
    "uplifting": "Uplifting motivational pop underscore, light claps, bright plucks and warm pads, "
                 "positive and hopeful, mid tempo, no vocals",
    "suspense": "Dark suspense thriller underscore, low drones, ticking percussion, tense and mysterious, "
                "minimal so the voice stays clear, no vocals",
}


class MusicError(Exception):
    pass


def _key(api_key=None):
    k = api_key or os.environ.get("ELEVENLABS_API_KEY")
    if not k:
        raise MusicError("Немає ключа ElevenLabs — додай його в ⚙ Settings.")
    return k


def _auth(r, what):
    try:
        d = (r.json() or {}).get("detail") or {}
    except Exception:
        d = {}
    msg = (d.get("message") if isinstance(d, dict) else str(d)) or ""
    status = d.get("status") if isinstance(d, dict) else ""
    if status == "missing_permissions" or "permission" in msg.lower() or r.status_code in (401, 403):
        return (f"Ключ ElevenLabs без дозволу на {what}. elevenlabs.io → Developers → API Keys → цей ключ → "
                f"увімкни «{what}» (Access). " + (f"Відповідь: {msg[:160]}" if msg else f"({r.status_code})"))
    return f"ElevenLabs відмовив ({r.status_code}): {msg[:200] or (r.text or '')[:200]}"


def compose(prompt, length_s, requests, api_key=None, model=None):
    """→ mp3 bytes of an instrumental track exactly `length_s` long (clamped to the API's 3 s–10 min)."""
    ms = int(round(max(3.0, min(600.0, float(length_s))) * 1000))
    body = {"prompt": prompt, "music_length_ms": ms, "model_id": model or MODEL, "force_instrumental": True}
    r = requests.post(f"{API}/v1/music", headers={"xi-api-key": _key(api_key), "Content-Type": "application/json"},
                      json=body, params={"output_format": "mp3_44100_128"}, timeout=300)
    if r.status_code in (401, 403):
        raise MusicError(_auth(r, "Music Generation"))
    if r.status_code != 200:
        raise MusicError(_auth(r, "Music Generation"))
    if not r.content or len(r.content) < 1000:
        raise MusicError("ElevenLabs не повернув аудіо.")
    return r.content


def video_segment(clip_path, out_path, start=0.0, end=None):
    """The part of the hook the music is FOR (the trim), small and silent: video-to-music only
    needs the picture, and a 6-minute 1080p master would blow the 200 MB upload limit."""
    s = max(0.0, float(start or 0.0))
    cmd = ["ffmpeg", "-y", "-ss", f"{s:.3f}", "-i", clip_path]
    if end is not None:
        cmd += ["-t", f"{max(0.5, float(end) - s):.3f}"]
    cmd += ["-an", "-vf", "scale=-2:480", "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
            "-pix_fmt", "yuv420p", out_path]
    _run(cmd)
    return out_path


def video_to_music(video_path, description, tags, requests, api_key=None, model=None):
    """→ mp3 bytes scored TO the video (its cuts, pace and mood). Template text steers the style."""
    data = {"description": (description or "")[:4100]}
    if tags:
        data["tags"] = list(tags)[:10]
    if model:
        data["model_id"] = model
    with open(video_path, "rb") as f:
        r = requests.post(f"{API}/v1/music/video-to-music", headers={"xi-api-key": _key(api_key)},
                          data=data, files=[("videos", (os.path.basename(video_path), f, "video/mp4"))],
                          params={"output_format": "mp3_44100_128"}, timeout=600)
    if r.status_code == 403 and "permission" not in (r.text or "").lower():
        raise MusicError("Video to Music недоступний на цьому тарифі ElevenLabs (403 Subscription required).")
    if r.status_code != 200:
        raise MusicError(_auth(r, "Music Generation"))
    if not r.content or len(r.content) < 1000:
        raise MusicError("ElevenLabs не повернув аудіо.")
    return r.content


def isolate_voice(clip_path, out_path, requests, api_key=None, start=0.0, end=None):
    """Voice only (old music / ambience / SFX removed) → out_path (wav, clip time-base).
    Only [start, end] (the trim) is sent — isolation is billed per minute, and a trimmed-off
    body is not going into the creative anyway."""
    wav = out_path + ".in.mp3"
    s = max(0.0, float(start or 0.0))
    cut = ["-ss", f"{s:.3f}"] + (["-t", f"{max(0.5, float(end) - s):.3f}"] if end is not None else [])
    _run(["ffmpeg", "-y", *cut, "-i", clip_path, "-vn", "-ac", "1", "-ar", "44100", "-b:a", "192k", wav])
    try:
        with open(wav, "rb") as f:
            r = requests.post(f"{API}/v1/audio-isolation", headers={"xi-api-key": _key(api_key)},
                              files=[("audio", (os.path.basename(wav), f, "audio/mpeg"))], timeout=300)
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass
    if r.status_code != 200:
        raise MusicError(_auth(r, "Audio Isolation"))
    raw = out_path + ".raw"
    with open(raw, "wb") as f:
        f.write(r.content)
    d_ms = int(round(s * 1000))
    _run(["ffmpeg", "-y", "-i", raw, "-af", f"adelay={d_ms}|{d_ms}", "-ac", "2", "-ar", "44100", out_path])   # back on the clip's clock
    os.remove(raw)
    return out_path


def _run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise MusicError("ffmpeg: " + (p.stderr or "")[-400:])
    return p


def duration(path):
    p = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path])
    try:
        return float(p.stdout.strip())
    except ValueError:
        return 0.0


def has_audio(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                        "-of", "csv=p=0", path], capture_output=True, text=True)
    return bool((p.stdout or "").strip())


def mix(clip_path, music_path, out_path, volume=0.35, duck=True, voice_path=None, offset=0.0,
        start_at=0.0, end_at=None):
    """Lay `music_path` under the hook. Base = isolated voice (voice_path) or the clip's own audio.
    Music covers [start_at, end_at] of the clip (= the trim: what is cut off gets no music, and the
    fade-out lands where the hook really ends), from `offset` s into the track, ducked under voice."""
    dur = duration(clip_path)
    if dur <= 0:
        raise MusicError("Не вдалося прочитати довжину кліпу.")
    s_at = max(0.0, min(dur, float(start_at or 0.0)))
    e_at = dur if end_at is None else max(s_at + 0.5, min(dur, float(end_at)))
    span = e_at - s_at
    fo = max(0.0, span - FADE_OUT)
    d_ms = int(round(s_at * 1000))
    vol = max(0.0, min(1.5, float(volume)))
    cmd = ["ffmpeg", "-y", "-i", clip_path]
    if voice_path:
        cmd += ["-i", voice_path]
    elif not has_audio(clip_path):          # a silent hook: the music is the only sound
        cmd += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=44100:cl=stereo"]
    base = "[1:a]" if (voice_path or not has_audio(clip_path)) else "[0:a]"
    mi = 2 if base == "[1:a]" else 1
    cmd += ["-ss", f"{max(0.0, float(offset)):.3f}", "-i", music_path]
    fc = [f"{base}aformat=sample_rates=44100:channel_layouts=stereo,apad,atrim=0:{dur:.3f}[v]",
          f"[{mi}:a]aformat=sample_rates=44100:channel_layouts=stereo,atrim=0:{span:.3f},asetpts=PTS-STARTPTS,"
          f"volume={vol:.3f},afade=t=in:d={FADE_IN},afade=t=out:st={fo:.3f}:d={FADE_OUT},"
          f"adelay={d_ms}|{d_ms},apad,atrim=0:{dur:.3f}[m]"]
    if duck:
        fc.append("[v]asplit=2[v1][v2]")
        fc.append("[m][v2]sidechaincompress=threshold=0.04:ratio=6:attack=25:release=450[mz]")
        fc.append("[v1][mz]amix=inputs=2:duration=first:normalize=0[aout]")
    else:
        fc.append("[v][m]amix=inputs=2:duration=first:normalize=0[aout]")
    cmd += ["-filter_complex", "; ".join(fc), "-map", "0:v?", "-map", "[aout]", "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2", "-t", f"{dur:.3f}", out_path]
    _run(cmd)
    return out_path
