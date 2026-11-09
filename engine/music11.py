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


# Video-to-Music lets the PICTURE drive the mood; the description alone was too weak (an "Epic"
# request came back as curious bells and xylophone). Tags are the strongest steer the endpoint
# has (max 10) — so each template carries a full, unambiguous set.
PRESET_TAGS = {
    "drama":    ["cinematic", "drama", "emotional", "strings", "piano", "underscore", "serious"],
    "action":   ["action", "trailer", "intense", "driving drums", "synth bass", "tension", "aggressive"],
    "epic":     ["epic", "orchestral", "trailer", "brass", "big drums", "heroic", "powerful", "dark"],
    "sadness":  ["sad", "melancholic", "piano", "cello", "slow", "emotional", "minimal"],
    "uplifting": ["uplifting", "motivational", "hopeful", "pop", "bright", "positive"],
    "suspense": ["suspense", "thriller", "dark", "tense", "drones", "mysterious", "ominous"],
}
NOT_PLAYFUL = " Serious tone — not playful, not whimsical, no bells, no xylophone, no music-box."


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


def video_to_music(video_path, description, tags, requests, api_key=None, model=None, sfx=False):
    """→ mp3 bytes scored TO the video (its cuts, pace and mood). Template text steers the style.
    sfx=True → (music_bytes, sfx_bytes|None): SFX synced to the picture (steps, doors, hits,
    whooshes) come back as a SEPARATE track, so the designer sets their level apart from the music."""
    data = {"description": (description or "")[:4100]}
    if sfx:
        data["add_sfx"] = "true"
        data["separate_tracks"] = "true"
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
    if not sfx:
        return r.content
    return split_tracks(r.content)


def split_tracks(content):
    """separate_tracks answer: a ZIP with a music file and an SFX file → (music, sfx). Names are not
    documented, so: a file whose name says sfx/effect is the SFX, the other one is the music. A
    plain audio answer (no ZIP) = music only."""
    import io
    import zipfile
    if not content.startswith(b"PK"):
        return content, None
    music, fx = None, None
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        files = [n for n in z.namelist() if not n.endswith("/") and n.lower().rsplit(".", 1)[-1] in ("mp3", "wav", "m4a", "aac", "ogg", "flac")]
        for n in files:
            low = os.path.basename(n).lower()
            if fx is None and any(k in low for k in ("sfx", "effect", "fx")):
                fx = z.read(n)
            elif music is None:
                music = z.read(n)
    if music is None:
        raise MusicError("ElevenLabs повернув архів без музичної доріжки.")
    return music, fx


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
        start_at=0.0, end_at=None, sfx_path=None, sfx_volume=0.8):
    """Lay `music_path` under the hook (kept for the old ♪ Apply path and the tests)."""
    return mix_layers(clip_path, out_path, voice_path=voice_path,
                      music={"path": music_path, "volume": volume, "duck": duck, "offset": offset,
                             "start": start_at, "end": end_at, "sfx_path": sfx_path, "sfx_volume": sfx_volume})


def mix_layers(clip_path, out_path, base_volume=1.0, voice_path=None, music=None, sfx=None):
    """THE audio mix — one ffmpeg pass, rebuilt from scratch every time a lane changes, so nothing
    is ever baked for good: the clip's own audio (or the isolated voice) × base_volume, + the music
    over its zone (ducked under the voice, faded at the zone ends), + every SFX at its time.
      music: {path, volume, duck, offset, start, end, sfx_path?, sfx_volume?} or None
      sfx:   [{path, at, volume}]
    Video copied, length kept."""
    dur = duration(clip_path)
    if dur <= 0:
        raise MusicError("Не вдалося прочитати довжину кліпу.")
    sfx = [it for it in (sfx or []) if it.get("path") and os.path.exists(it["path"]) and 0 <= float(it.get("at", 0)) < dur]
    cmd = ["ffmpeg", "-y", "-i", clip_path]
    n_in = 1
    if voice_path:
        cmd += ["-i", voice_path]
        base = f"[{n_in}:a]"
        n_in += 1
    elif not has_audio(clip_path):          # a silent hook
        cmd += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=44100:cl=stereo"]
        base = f"[{n_in}:a]"
        n_in += 1
    else:
        base = "[0:a]"
    bv = max(0.0, min(2.0, float(base_volume)))
    fc = [f"{base}aformat=sample_rates=44100:channel_layouts=stereo,volume={bv:.3f},apad,atrim=0:{dur:.3f}[v]"]
    layers, voice_lbl = [], "[v]"
    if music and music.get("path"):
        s_at = max(0.0, min(dur, float(music.get("start") or 0.0)))
        e = music.get("end")
        e_at = dur if e is None else max(s_at + 0.5, min(dur, float(e)))
        span = e_at - s_at
        fo = max(0.0, span - FADE_OUT)
        d_ms = int(round(s_at * 1000))
        vol = max(0.0, min(1.5, float(music.get("volume", 0.35))))
        cmd += ["-ss", f"{max(0.0, float(music.get('offset') or 0)):.3f}", "-i", music["path"]]
        mi = n_in
        n_in += 1
        fc.append(f"[{mi}:a]aformat=sample_rates=44100:channel_layouts=stereo,atrim=0:{span:.3f},asetpts=PTS-STARTPTS,"
                  f"volume={vol:.3f},afade=t=in:d={FADE_IN},afade=t=out:st={fo:.3f}:d={FADE_OUT},"
                  f"adelay={d_ms}|{d_ms},apad,atrim=0:{dur:.3f}[m]")
        if music.get("duck", True) and bv > 0:
            fc.append("[v]asplit=2[v1][v2]")
            fc.append("[m][v2]sidechaincompress=threshold=0.04:ratio=6:attack=25:release=450[mz]")
            voice_lbl = "[v1]"
            layers.append("[mz]")
        else:
            layers.append("[m]")
        if music.get("sfx_path"):
            # the SFX stem that came WITH a Video-to-Music take: same window, never ducked
            cmd += ["-i", music["sfx_path"]]
            si = n_in
            n_in += 1
            sv = max(0.0, min(2.0, float(music.get("sfx_volume", 0.8))))
            fc.append(f"[{si}:a]aformat=sample_rates=44100:channel_layouts=stereo,atrim=0:{span:.3f},asetpts=PTS-STARTPTS,"
                      f"volume={sv:.3f},afade=t=in:d=0.03,afade=t=out:st={max(0.0, span - 0.15):.3f}:d=0.15,"
                      f"adelay={d_ms}|{d_ms},apad,atrim=0:{dur:.3f}[fx]")
            layers.append("[fx]")
    for k, it in enumerate(sfx):
        cmd += ["-i", it["path"]]
        d_ms = int(round(float(it["at"]) * 1000))
        v = max(0.0, min(2.0, float(it.get("volume", 0.9))))
        fc.append(f"[{n_in}:a]aformat=sample_rates=44100:channel_layouts=stereo,volume={v:.3f},"
                  f"afade=t=in:d=0.01,adelay={d_ms}|{d_ms},apad,atrim=0:{dur:.3f}[s{k}]")
        n_in += 1
        layers.append(f"[s{k}]")
    if layers:
        fc.append(voice_lbl + "".join(layers) + f"amix=inputs={len(layers) + 1}:duration=first:normalize=0[aout]")
    else:
        fc[-1] = fc[-1][:-3] + "[aout]"
    if out_path.lower().endswith(".wav"):   # a stem: audio only, lossless, same length as the clip
        cmd += ["-filter_complex", "; ".join(fc), "-map", "[aout]", "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "2",
                "-t", f"{dur:.3f}", out_path]
    else:
        cmd += ["-filter_complex", "; ".join(fc), "-map", "0:v?", "-map", "[aout]", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2", "-t", f"{dur:.3f}", out_path]
    _run(cmd)
    return out_path


def peaks(path, n=800):
    """Waveform for the timeline lanes: n values 0..1 (max |sample| per bucket)."""
    import array
    p = subprocess.run(["ffmpeg", "-v", "quiet", "-i", path, "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                       capture_output=True)
    a = array.array("h")
    a.frombytes(p.stdout[: len(p.stdout) // 2 * 2])
    if not a:
        return [0.0] * n
    step = max(1, len(a) // n)
    out = []
    for i in range(0, len(a), step):
        chunk = a[i:i + step]
        out.append(max(abs(min(chunk)), abs(max(chunk))) / 32768.0)
    top = max(out) or 1.0
    return [round(x / top, 3) for x in out[:n]]


# ---------------------------------------------------------------- SFX only (the music stays)
# Two ways to add sound effects to a hook that already has the music you like:
#   • text → one effect at the playhead (ElevenLabs Sound Effects, eleven_text_to_sound_v2,
#     0.5–30 s, optional seamless loop) — "door slam", "whoosh", "heartbeat";
#   • auto from the picture: Video-to-Music with SFX as a separate track — we keep ONLY the SFX
#     stem and throw the music away, so the bed you already chose is not touched.
SFX_MODEL = os.environ.get("CS_SFX_MODEL", "eleven_text_to_sound_v2")


def sound_effect(prompt, requests, duration=None, loop=False, influence=0.4, api_key=None):
    """→ mp3 bytes of one sound effect."""
    body = {"text": (prompt or "").strip()[:450], "model_id": SFX_MODEL, "prompt_influence": float(influence)}
    if not body["text"]:
        raise MusicError("Опиши звук, наприклад «door slam» або «whoosh».")
    if duration:
        body["duration_seconds"] = max(0.5, min(30.0, float(duration)))
    if loop:
        body["loop"] = True
    r = requests.post(f"{API}/v1/sound-generation", headers={"xi-api-key": _key(api_key), "Content-Type": "application/json"},
                      json=body, params={"output_format": "mp3_44100_128"}, timeout=120)
    if r.status_code != 200:
        raise MusicError(_auth(r, "Sound Effects"))
    if not r.content or len(r.content) < 500:
        raise MusicError("ElevenLabs не повернув звук.")
    return r.content


def mix_sfx(clip_path, items, out_path):
    """Lay effects ON TOP of the clip's current audio (voice + music stay as they are)."""
    ok = [it for it in items if it.get("path") and os.path.exists(it["path"])]
    if not ok:
        raise MusicError("Немає SFX, які можна покласти в межах кліпу.")
    return mix_layers(clip_path, out_path, sfx=ok)
