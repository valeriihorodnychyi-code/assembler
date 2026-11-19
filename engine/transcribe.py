"""Pluggable transcription -> normalized word timecodes.

Two backends:
  - "whisper": faster-whisper, local, free, offline (default). Slower on CPU.
  - "scribe" : ElevenLabs Scribe, cloud, very fast + accurate. Needs API key + net.

Both return the same shape:
    {"language": "en", "words": [{"word": "hello", "start": 0.12, "end": 0.34}, ...]}

Heavy/optional deps (faster_whisper, requests) are imported lazily so this module
always imports cleanly even when a backend isn't installed.
"""
import os
import subprocess
import tempfile

# Scribe speaks ISO 639-3 ("eng", "ukr"), Whisper only 639-1 ("en", "uk"). A clip transcribed with
# Scribe and RE-transcribed with Whisper passed "eng" and Whisper refused. Everything is stored and
# passed as the 2-letter code; anything unknown → None (auto-detect).
_ISO3 = {"eng": "en", "ukr": "uk", "rus": "ru", "spa": "es", "fra": "fr", "fre": "fr", "deu": "de", "ger": "de",
         "ita": "it", "por": "pt", "pol": "pl", "hun": "hu", "ron": "ro", "rum": "ro", "fin": "fi", "nor": "no",
         "nob": "no", "nno": "nn", "hrv": "hr", "jpn": "ja", "ces": "cs", "cze": "cs", "dan": "da", "bul": "bg",
         "tur": "tr", "nld": "nl", "dut": "nl", "ara": "ar", "zho": "zh", "chi": "zh", "cmn": "zh", "kor": "ko",
         "swe": "sv", "ell": "el", "gre": "el", "heb": "he", "hin": "hi", "ind": "id", "vie": "vi", "tha": "th",
         "slk": "sk", "slo": "sk", "slv": "sl", "srp": "sr", "lit": "lt", "lav": "lv", "est": "et", "cat": "ca",
         "msa": "ms", "may": "ms", "fil": "tl", "tgl": "tl", "fas": "fa", "per": "fa", "urd": "ur", "ben": "bn",
         "kaz": "kk", "bel": "be", "aze": "az", "kat": "ka", "geo": "ka", "hye": "hy", "arm": "hy"}


def lang2(code):
    """'eng' / 'en-US' / 'EN' / 'pt_BR' → 'en' / 'pt'; '' / unknown → None."""
    c = (code or "").strip().lower().replace("_", "-").split("-")[0]
    if not c:
        return None
    if len(c) == 2:
        return c
    return _ISO3.get(c)


# Cache loaded whisper models by size so repeated calls are fast.
_WHISPER_CACHE = {}


def extract_audio(video_path: str) -> str:
    """Pull a 16kHz mono wav out of the video for transcription."""
    out = tempfile.mktemp(suffix=".wav")
    subprocess.run(
        ["ffmpeg", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000", out, "-y"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
    )
    return out


def transcribe_whisper(video_path: str, model_size: str = "small",
                       language: str = None, device: str = "cpu") -> dict:
    from faster_whisper import WhisperModel  # lazy

    key = (model_size, device)
    if key not in _WHISPER_CACHE:
        _WHISPER_CACHE[key] = WhisperModel(model_size, device=device, compute_type="int8")
    model = _WHISPER_CACHE[key]
    language = lang2(language)

    def _collect(segs):
        out = []
        for seg in segs:
            for w in (seg.words or []):
                out.append({"word": w.word.strip(), "start": float(w.start), "end": float(w.end)})
        return out

    # vad_filter trims non-speech so the FIRST word's timestamp lands on the real
    # speech onset (otherwise faster-whisper anchors it to ~0 and the first caption
    # shows from frame 0 over a silent/music intro). Timestamps stay in video time.
    # Fall back gracefully if VAD deps (onnxruntime) aren't available.
    try:
        segments, info = model.transcribe(video_path, word_timestamps=True,
                                          language=language, vad_filter=True)
        words = _collect(segments)
    except Exception:
        segments, info = model.transcribe(video_path, word_timestamps=True, language=language)
        words = _collect(segments)
    return {"language": getattr(info, "language", language) or "en", "words": words}


def dictate_english(audio_path: str, model_size: str = None, device: str = "cpu") -> dict:
    """🎙 Dictation for prompt fields: speak in ANY language → English text (Whisper's own
    translate task — local, free, no key). → {"text", "language"}."""
    from faster_whisper import WhisperModel  # lazy
    size = model_size or os.environ.get("CS_DICTATE_MODEL", "small")
    key = (size, device)
    if key not in _WHISPER_CACHE:
        _WHISPER_CACHE[key] = WhisperModel(size, device=device, compute_type="int8")
    segs, info = _WHISPER_CACHE[key].transcribe(audio_path, task="translate", beam_size=5, vad_filter=False)
    text = " ".join(seg.text.strip() for seg in segs).strip()
    return {"text": text, "language": getattr(info, "language", "") or ""}


def transcribe_scribe(video_path: str, api_key: str = None, language: str = None) -> dict:
    """ElevenLabs Scribe speech-to-text with word timestamps."""
    import requests  # lazy

    api_key = api_key or os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise RuntimeError("ElevenLabs API key missing (set ELEVENLABS_API_KEY).")

    audio = extract_audio(video_path)
    try:
        with open(audio, "rb") as f:
            # diarize in the SAME request: who speaks when comes free with the words (🗣 Voices)
            data = {"model_id": "scribe_v1", "timestamps_granularity": "word", "diarize": "true"}
            if language:
                data["language_code"] = language
            resp = requests.post(
                "https://api.elevenlabs.io/v1/speech-to-text",
                headers={"xi-api-key": api_key},
                files={"file": ("audio.wav", f, "audio/wav")},
                data=data,
                timeout=300,
            )
    finally:
        if os.path.exists(audio):
            os.remove(audio)

    if resp.status_code != 200:
        raise RuntimeError(f"Scribe failed ({resp.status_code}): {resp.text[:500]}")

    payload = resp.json()
    words = []
    for w in payload.get("words", []):
        # Scribe returns spacing/audio_event tokens too; keep only real words.
        if w.get("type", "word") != "word":
            continue
        ww = {"word": (w.get("text") or "").strip(), "start": float(w.get("start", 0.0)), "end": float(w.get("end", 0.0))}
        if w.get("speaker_id"):
            ww["speaker"] = w["speaker_id"]
        words.append(ww)
    return {"language": lang2(payload.get("language_code")) or lang2(language) or "en", "words": words}


def transcribe(video_path: str, engine: str = "whisper", **kwargs) -> dict:
    if engine == "scribe":
        return transcribe_scribe(video_path,
                                 api_key=kwargs.get("api_key"),
                                 language=kwargs.get("language"))
    return transcribe_whisper(video_path,
                              model_size=kwargs.get("model_size", "small"),
                              language=kwargs.get("language"),
                              device=kwargs.get("device", "cpu"))
