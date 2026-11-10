"""Transcribe only the trim zone — offline (the recogniser is replaced by a stub).

Run from the repo root:   python3 tests/test_transcribe_trim.py

A trimmed 6-minute master must not send 6 minutes to Whisper / Scribe: only the kept part
+1 s on each side goes, and the words come back on the CLIP's clock.
"""
import os
import subprocess
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="txtrim_")
os.environ["CS_WORK_ROOT"] = os.path.join(TMP, "sessions")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient  # noqa: E402
import server.app as A  # noqa: E402
from engine import ffmpeg_utils as ff  # noqa: E402

C = TestClient(A.app)
SEEN = {}


def fake_transcribe(path, **k):
    SEEN["dur"] = ff.get_video_duration(path)
    return {"language": "en", "words": [{"word": "hello", "start": 0.5, "end": 0.9}]}


def _session(dur=20):
    clip = os.path.join(TMP, "long.mp4")
    subprocess.run(["ffmpeg", "-v", "quiet", "-y", "-f", "lavfi", "-i", f"testsrc=size=320x576:rate=25:duration={dur}",
                    "-f", "lavfi", "-i", f"sine=f=300:d={dur}", "-shortest", "-pix_fmt", "yuv420p", "-c:a", "aac", clip], check=True)
    up = C.post("/api/upload_clip", files={"file": ("long.mp4", open(clip, "rb"), "video/mp4")}).json()
    return up["file_id"], up["name"]


def test_only_the_trim_zone_is_sent_and_words_land_on_the_clip_clock():
    A.transcribe.transcribe = fake_transcribe
    fid, name = _session()
    d = C.post("/api/transcribe_clip", json={"file_id": fid, "clip": name, "trim": [5.0, 9.0]}).json()
    assert abs(SEEN["dur"] - 6.0) < 0.15, f"sent {SEEN['dur']:.2f}s, expected the 4-s zone + 2 s margin"
    assert d["cover"] == [4.0, 10.0]
    assert abs(d["words"][0]["start"] - 4.5) < 1e-6, d["words"]


def test_no_trim_sends_the_whole_clip():
    A.transcribe.transcribe = fake_transcribe
    fid, name = _session(8)
    d = C.post("/api/transcribe_clip", json={"file_id": fid, "clip": name}).json()
    assert abs(SEEN["dur"] - 8.0) < 0.15 and d["words"][0]["start"] == 0.5



def test_scribe_language_codes_are_made_whisper_safe():
    """Scribe gives 'eng'; re-transcribing with Whisper must not get it (it only knows 'en')."""
    from engine.transcribe import lang2
    assert [lang2(x) for x in ("eng", "ukr", "pt-BR", "EN", "", None, "zzz")] == ["en", "uk", "pt", "en", None, None, None]


def test_scribe_voices_come_free_with_the_transcript():
    """Scribe diarizes in the transcribe call → 🗣 Voices reads the cache, no second request."""
    from engine import revoice as RV
    fid, name = _session(6)
    A.transcribe.transcribe = lambda path, **k: {"language": "en", "words": [
        {"word": "Hi", "start": 0.5, "end": 0.9, "speaker": "speaker_0"},
        {"word": "you", "start": 2.0, "end": 2.4, "speaker": "speaker_1"}]}
    r = C.post("/api/transcribe_clip", json={"file_id": fid, "clip": name, "engine": "scribe"}).json()
    assert r["voices"] is True
    calls = []
    real = RV.requests if hasattr(RV, "requests") else None
    import requests as _rq
    orig = _rq.post
    _rq.post = lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(AssertionError("no Scribe call expected"))
    try:
        v = C.post("/api/voices", json={"file_id": fid, "clip": name}).json()
    finally:
        _rq.post = orig
    assert not calls and [s["id"] for s in v["speakers"]] == ["speaker_0", "speaker_1"], v

if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("  ✓", name)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print("  ✗", name, "—", type(e).__name__, e)
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
