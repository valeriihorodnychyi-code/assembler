"""Re-voice a changed word or phrase INSIDE a clip, in the clip's own voice (ElevenLabs).

The designer fixes a word in the transcript ("Tai Chai" → "Tai Chi", a new date) and this puts
the corrected words back into the AUDIO — no new generation of the video.

How, and why this way:
  1. Clone the voice from the clip's own audio (Instant Voice Clone). No voice_id has to be
     known in advance — the clip IS the reference. The clone is deleted when the job ends, so
     re-voicing never eats the account's voice slots.
  2. Synthesise only the changed words, passing the words before and after as previous_text /
     next_text: ElevenLabs then shapes the intonation to fit INTO the sentence, so the seam
     doesn't sound pasted. `say` may differ from the caption text ("Tie Chee" for "Tai Chi") —
     what is heard and what is shown are separate on purpose.
  3. Splice the result into exactly the time span the old words occupied (word timings come
     from the transcript). The clip's LENGTH never changes: a shorter phrase leaves a pause, a
     slightly longer one is sped up (up to 1.25x), anything longer is refused with the number,
     so nothing downstream — captions, trim, the board — shifts.
  4. Return the new words' timings from the TTS character alignment, so captions match the new
     audio without another transcribe.

Endpoints (checked against the docs, Oct 2026): POST /v1/voices/add · POST
/v1/text-to-speech/{voice_id}/with-timestamps · DELETE /v1/voices/{voice_id}.
Key: ELEVENLABS_API_KEY (per machine, never in the repo).
"""
import base64
import json
import os
import re
import subprocess
import tempfile
import uuid

API = "https://api.elevenlabs.io"
MODEL = os.environ.get("CS_REVOICE_MODEL", "eleven_multilingual_v2")   # supports previous/next_text
MAX_TEMPO = 1.25          # a replacement longer than its slot is sped up at most this much
LEAD = 0.02               # keep this much before the first phoneme (a clean attack)
TAIL = 0.04               # and this much after the last (natural release)
FADE = 0.015              # crossfade at every seam — no clicks
MUTE_PAD = 0.015          # mute the old words slightly beyond their edges


class RevoiceError(Exception):
    pass


def _key(api_key=None):
    k = api_key or os.environ.get("ELEVENLABS_API_KEY")
    if not k:
        raise RevoiceError("Немає ключа ElevenLabs — додай його в ⚙ Settings.")
    return k


def _run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RevoiceError("ffmpeg: " + (p.stderr or "")[-400:])
    return p


def duration(path):
    p = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
              "-of", "default=nw=1:nk=1", path])
    try:
        return float(p.stdout.strip())
    except ValueError:
        return 0.0


def mean_volume(path, start=None, length=None):
    """Mean loudness in dB (ffmpeg volumedetect) of a window, or None for digital silence."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats"]
    if start is not None:
        cmd += ["-ss", f"{max(0.0, start):.3f}"]
    if length is not None:
        cmd += ["-t", f"{max(0.05, length):.3f}"]
    cmd += ["-i", path, "-vn", "-af", "volumedetect", "-f", "null", "-"]
    p = subprocess.run(cmd, capture_output=True, text=True)
    m = re.search(r"mean_volume:\s*(-?[\d.]+|-inf)\s*dB", p.stderr or "")
    if not m or m.group(1) == "-inf":
        return None
    return float(m.group(1))


# ---------------------------------------------------------------- ElevenLabs calls

def _auth_error(r, what):
    """401/403 → a message a designer can act on. ElevenLabs keys are RESTRICTED by default: a key
    that transcribes and dubs fine can still lack the Voices / Text-to-Speech permissions that
    re-voice needs — the answer then is {"detail": {"status": "missing_permissions", ...}}."""
    try:
        d = (r.json() or {}).get("detail") or {}
    except Exception:
        d = {}
    status = d.get("status") if isinstance(d, dict) else ""
    msg = (d.get("message") if isinstance(d, dict) else str(d)) or ""
    if status == "missing_permissions" or "permission" in msg.lower():
        return (f"Ключ ElevenLabs робочий, але без дозволу на {what}. Відкрий elevenlabs.io → "
                "Developers → API Keys → цей ключ → увімкни Voices: Write і Text to Speech "
                "(або зроби ключ без обмежень). Відповідь ElevenLabs: " + msg[:160])
    if status in ("invalid_api_key", "") and r.status_code == 401:
        return ("ElevenLabs не прийняв ключ (401" + (f", {status}" if status else "") + "). Перевір ключ у ⚙ Settings — "
                "без пробілів, той самий, що в elevenlabs.io → Developers → API Keys." + (f" ({msg[:120]})" if msg else ""))
    return f"ElevenLabs відмовив ({r.status_code}{', ' + status if status else ''}): {msg[:200]}"

def clone_voice(sample_path, requests, api_key=None, name=None):
    """Instant voice clone from the clip's own audio → voice_id. Background-noise removal stays
    OFF by default: it degrades clean samples, and the endpoint failed with it on (status
    incident, 13 Oct 2026)."""
    with open(sample_path, "rb") as f:
        r = requests.post(f"{API}/v1/voices/add", headers={"xi-api-key": _key(api_key)},
                          data={"name": name or f"assembler-revoice-{uuid.uuid4().hex[:8]}",
                                "description": "Temporary clone for an Assembler re-voice. Deleted after use.",
                                "remove_background_noise": "false"},
                          files=[("files", (os.path.basename(sample_path), f, "audio/mpeg"))],
                          timeout=120)
    if r.status_code in (401, 403):
        raise RevoiceError(_auth_error(r, "клонування голосу (Voices → Write)"))
    if r.status_code != 200:
        raise RevoiceError(f"Клон голосу не вдався ({r.status_code}): {(r.text or '')[:200]}")
    vid = (r.json() or {}).get("voice_id")
    if not vid:
        raise RevoiceError("ElevenLabs не повернув voice_id для клону.")
    return vid


def delete_voice(voice_id, requests, api_key=None):
    try:
        r = requests.delete(f"{API}/v1/voices/{voice_id}", headers={"xi-api-key": _key(api_key)}, timeout=30)
        return r.status_code == 200
    except Exception:
        return False


def synth(voice_id, text, requests, previous_text="", next_text="", api_key=None, model=None):
    """→ (mp3_bytes, alignment) — alignment has per-CHARACTER start/end times."""
    body = {"text": text, "model_id": model or MODEL,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.9, "style": 0.0,
                               "use_speaker_boost": True}}
    if previous_text:
        body["previous_text"] = previous_text[-600:]
    if next_text:
        body["next_text"] = next_text[:600]
    r = requests.post(f"{API}/v1/text-to-speech/{voice_id}/with-timestamps",
                      params={"output_format": "mp3_44100_128"},
                      headers={"xi-api-key": _key(api_key), "Content-Type": "application/json"},
                      data=json.dumps(body), timeout=120)
    if r.status_code in (401, 403):
        raise RevoiceError(_auth_error(r, "озвучки (Text to Speech)"))
    if r.status_code != 200:
        raise RevoiceError(f"Генерація не вдалась ({r.status_code}): {(r.text or '')[:200]}")
    d = r.json() or {}
    if not d.get("audio_base64"):
        raise RevoiceError("ElevenLabs не повернув аудіо.")
    return base64.b64decode(d["audio_base64"]), (d.get("alignment") or d.get("normalized_alignment") or {})


# ---------------------------------------------------------------- alignment → words

def alignment_words(al):
    """Per-character alignment → [{word, start, end}] (split on whitespace)."""
    chars = al.get("characters") or []
    st = al.get("character_start_times_seconds") or []
    en = al.get("character_end_times_seconds") or []
    out, cur = [], None
    for i, ch in enumerate(chars):
        if i >= len(st) or i >= len(en):
            break
        if ch.isspace():
            if cur:
                out.append(cur)
                cur = None
            continue
        if cur is None:
            cur = {"word": ch, "start": float(st[i]), "end": float(en[i])}
        else:
            cur["word"] += ch
            cur["end"] = float(en[i])
    if cur:
        out.append(cur)
    return out


def caption_words(text, said, t0, t1):
    """Caption words for the new audio. Same word count as what was said → each caption word
    takes its spoken word's time; otherwise (a phonetic `say` like "Tie Chee" for "Tai Chi")
    the caption words share the spoken span, weighted by length."""
    toks = text.split()
    if not toks:
        return []
    if len(said) == len(toks):
        return [{"word": t, "start": round(w["start"], 3), "end": round(w["end"], 3)} for t, w in zip(toks, said)]
    tot = sum(max(1, len(t)) for t in toks)
    out, t = [], t0
    for tok in toks:
        d = (t1 - t0) * max(1, len(tok)) / tot
        out.append({"word": tok, "start": round(t, 3), "end": round(t + d, 3)})
        t += d
    return out


# ---------------------------------------------------------------- the job

def plan_patch(span, speech_len, clip_dur):
    """Where the new audio goes and how fast. Pure arithmetic, so it is testable alone.

    span: {a, b, lo, hi} — a/b = the OLD words' audio, lo/hi = the neighbouring words (the
    patch may use the pauses up to them, never cross them).
    """
    a, b = float(span["a"]), float(span["b"])
    lo, hi = float(span.get("lo", 0.0)), float(span.get("hi", clip_dur))
    if not (0 <= lo <= a + 1e-6 and a <= b + 1e-6 and b <= hi + 1e-6 and hi <= clip_dur + 0.05):
        raise RevoiceError(f"Некоректний відрізок {a:.2f}–{b:.2f} с.")
    if b > a:                       # replacing words: start where the old ones started
        place = max(lo, a - LEAD)
    else:                           # pure insertion into a pause: just after the previous word
        place = max(lo, a) + 0.03
    slot = max(0.05, (hi - 0.03) - place)
    tempo = 1.0
    if speech_len > slot:
        tempo = speech_len / slot
        if tempo > MAX_TEMPO:
            raise RevoiceError(f"Нова фраза довша за місце в аудіо на {speech_len / MAX_TEMPO - slot:.2f} с — "
                               f"скороти текст (місце {slot:.2f} с, фраза {speech_len:.2f} с).")
    mute = (max(lo, a - MUTE_PAD), min(hi, b + MUTE_PAD)) if b > a else None
    return {"place": round(place, 3), "slot": round(slot, 3), "tempo": round(tempo, 4), "mute": mute}


def revoice(clip_path, spans, out_path, requests=None, api_key=None, workdir=None):
    """Re-voice every span in ONE clip with ONE voice clone, then splice them all in one pass.

    spans: [{a, b, lo, hi, text, say?, prev_text?, next_text?}] in clip seconds.
    Returns {"spans": [{"words": [...], "tempo", "gain_db", "place"}], "voice_deleted": bool}.
    """
    if requests is None:
        import requests  # noqa: F811 — lazy, injectable for tests
    if not spans:
        raise RevoiceError("Нема що переозвучувати.")
    clip_dur = duration(clip_path)
    wd = workdir or tempfile.mkdtemp(prefix="cs_rv_")
    sample = os.path.join(wd, "voice_sample.mp3")
    # the clip itself is the voice reference — up to 3 minutes is plenty for an instant clone
    _run(["ffmpeg", "-y", "-i", clip_path, "-vn", "-ac", "1", "-ar", "44100", "-t", "180",
          "-b:a", "128k", sample])
    voice_id = clone_voice(sample, requests, api_key)
    results, patches = [], []
    try:
        for k, sp in enumerate(spans):
            say = (sp.get("say") or sp.get("text") or "").strip()
            text = (sp.get("text") or say).strip()
            if not say:
                raise RevoiceError("Порожній текст для переозвучки.")
            mp3, al = synth(voice_id, say, requests, sp.get("prev_text", ""), sp.get("next_text", ""), api_key)
            raw = os.path.join(wd, f"tts_{k}.mp3")
            with open(raw, "wb") as f:
                f.write(mp3)
            said = alignment_words(al)
            if said:
                s0, s1 = said[0]["start"], said[-1]["end"]
            else:                                   # no alignment: trust the file
                s0, s1 = 0.0, duration(raw)
            cut0, cut1 = max(0.0, s0 - LEAD), s1 + TAIL
            speech_len = cut1 - cut0
            pl = plan_patch(sp, speech_len, clip_dur)
            # loudness: match the voice around the old words (a window, so an insertion into a
            # pause still has a reference)
            ref_db = mean_volume(clip_path, float(sp["a"]) - 1.0, (float(sp["b"]) - float(sp["a"])) + 2.0)
            patch = os.path.join(wd, f"patch_{k}.wav")
            filt = [f"atempo={pl['tempo']:.4f}"] if pl["tempo"] > 1.0001 else []
            _run(["ffmpeg", "-y", "-ss", f"{cut0:.3f}", "-to", f"{cut1:.3f}", "-i", raw,
                  "-ac", "2", "-ar", "44100"] + (["-af", ",".join(filt)] if filt else []) + [patch])
            pat_db = mean_volume(patch)
            gain = 0.0 if (ref_db is None or pat_db is None) else max(-12.0, min(12.0, ref_db - pat_db))
            plen = duration(patch)
            patches.append({"path": patch, "place": pl["place"], "len": plen, "gain": gain, "mute": pl["mute"]})
            # caption timings in CLIP time
            to_clip = lambda t: pl["place"] + (t - cut0) / pl["tempo"]
            said_clip = [{"word": w["word"], "start": to_clip(w["start"]), "end": to_clip(w["end"])} for w in said]
            t0 = said_clip[0]["start"] if said_clip else pl["place"]
            t1 = said_clip[-1]["end"] if said_clip else pl["place"] + plen
            results.append({"words": caption_words(text, said_clip, t0, t1), "text": text, "say": say,
                            "tempo": pl["tempo"], "gain_db": round(gain, 1), "place": pl["place"]})
    finally:
        deleted = delete_voice(voice_id, requests, api_key)
    splice(clip_path, patches, out_path)
    return {"spans": results, "voice_deleted": deleted, "voice_id": None if deleted else voice_id}


def splice(clip_path, patches, out_path):
    """One ffmpeg pass: the old words muted with short ramps, every patch laid in at its place
    with crossfades and matched gain, the video stream copied untouched, the length kept."""
    fc, mix = [], []
    # mute envelope on the original (one volume filter per span, ramps on both sides)
    chain = []
    for p in patches:
        if not p["mute"]:
            continue
        a, b = p["mute"]
        lo = max(0.0, a - FADE)
        chain.append(
            f"volume=volume='if(between(t,{a:.4f},{b:.4f}),0,"
            f"if(between(t,{lo:.4f},{a:.4f}),({a:.4f}-t)/{FADE},"
            f"if(between(t,{b:.4f},{b + FADE:.4f}),(t-{b:.4f})/{FADE},1)))':eval=frame")
    fc.append("[0:a]aformat=sample_rates=44100:channel_layouts=stereo"
              + ("," + ",".join(chain) if chain else "") + "[base]")
    mix.append("[base]")
    for i, p in enumerate(patches, start=1):
        d_ms = int(round(p["place"] * 1000))
        fo = max(0.0, p["len"] - FADE)
        fc.append(f"[{i}:a]aformat=sample_rates=44100:channel_layouts=stereo,volume={p['gain']:.2f}dB,"
                  f"afade=t=in:d={FADE},afade=t=out:st={fo:.4f}:d={FADE},adelay={d_ms}|{d_ms}[p{i}]")
        mix.append(f"[p{i}]")
    fc.append("".join(mix) + f"amix=inputs={len(mix)}:normalize=0:duration=first:dropout_transition=0[aout]")
    cmd = ["ffmpeg", "-y", "-i", clip_path]
    for p in patches:
        cmd += ["-i", p["path"]]
    cmd += ["-filter_complex", "; ".join(fc), "-map", "0:v?", "-map", "[aout]", "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2", "-shortest", out_path]
    _run(cmd)
    return out_path
