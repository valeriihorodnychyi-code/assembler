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

    segments, info = model.transcribe(video_path, word_timestamps=True, language=language)
    words = []
    for seg in segments:
        for w in (seg.words or []):
            words.append({"word": w.word.strip(), "start": float(w.start), "end": float(w.end)})
    return {"language": getattr(info, "language", language) or "en", "words": words}


def transcribe_scribe(video_path: str, api_key: str = None, language: str = None) -> dict:
    """ElevenLabs Scribe speech-to-text with word timestamps."""
    import requests  # lazy

    api_key = api_key or os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise RuntimeError("ElevenLabs API key missing (set ELEVENLABS_API_KEY).")

    audio = extract_audio(video_path)
    try:
        with open(audio, "rb") as f:
            data = {"model_id": "scribe_v1", "timestamps_granularity": "word"}
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
        words.append({"word": (w.get("text") or "").strip(),
                      "start": float(w.get("start", 0.0)),
                      "end": float(w.get("end", 0.0))})
    return {"language": payload.get("language_code", language) or "en", "words": words}


def transcribe(video_path: str, engine: str = "whisper", **kwargs) -> dict:
    if engine == "scribe":
        return transcribe_scribe(video_path,
                                 api_key=kwargs.get("api_key"),
                                 language=kwargs.get("language"))
    return transcribe_whisper(video_path,
                              model_size=kwargs.get("model_size", "small"),
                              language=kwargs.get("language"),
                              device=kwargs.get("device", "cpu"))
