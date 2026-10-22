"""Hook music — offline tests with REAL audio (ffmpeg), no network, no credits.

Run from the repo root:   python3 tests/test_hookmusic.py

The hook "speaks" at 300 Hz and carries an OLD per-shot music bed at 500 Hz. A fake
ElevenLabs returns a 880 Hz "track" for /v1/music and the voice alone for /v1/audio-isolation.
So we can measure what is really in the result: voice kept, old bed gone, new bed in.
"""
import json
import math
import os
import struct
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["ELEVENLABS_API_KEY"] = "test-key"
from engine import music11 as M  # noqa: E402

TMP = tempfile.mkdtemp(prefix="hm_test_")


def sh(*cmd):
    subprocess.run(cmd, capture_output=True, check=True)


def make_hook(path, dur=4.0):
    sh("ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=size=320x568:rate=30:duration={dur}",
       "-f", "lavfi", "-i", f"aevalsrc='0.4*sin(2*PI*300*t)+0.25*sin(2*PI*500*t)':s=44100:d={dur}",
       "-shortest", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "44100", "-ac", "2", path)


def tone_mp3(freq, dur, path):
    sh("ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=f={freq}:d={dur}:sample_rate=44100", "-b:a", "128k", path)
    return open(path, "rb").read()


def level(path, freq, t, win=0.2):
    """Goertzel power of `freq` in a window — enough to tell 300 / 500 / 880 Hz apart."""
    raw = subprocess.run(["ffmpeg", "-v", "quiet", "-ss", str(t), "-t", str(win), "-i", path, "-ac", "1",
                          "-ar", "8000", "-f", "s16le", "-"], capture_output=True).stdout
    x = struct.unpack("<%dh" % (len(raw) // 2), raw[:len(raw) // 2 * 2])
    if not x:
        return 0.0
    k = 2 * math.cos(2 * math.pi * freq / 8000)
    s1 = s2 = 0.0
    for v in x:
        s1, s2 = v + k * s1 - s2, s1
    return math.sqrt(max(0.0, s1 * s1 + s2 * s2 - k * s1 * s2)) / len(x)


class _R:
    def __init__(self, code, content=b"", js=None):
        self.status_code, self.content, self._js, self.text = code, content, js, json.dumps(js or {})

    def json(self):
        return self._js


class Fake11:
    def __init__(self, deny=None):
        self.deny, self.calls = deny, []

    def post(self, url, headers=None, json=None, params=None, files=None, data=None, timeout=None):
        self.calls.append(url)
        assert headers.get("xi-api-key") == "test-key"
        if self.deny and self.deny in url:
            return _R(401, js={"detail": {"status": "missing_permissions", "message": "missing the permission"}})
        if url.endswith("/v1/music"):
            assert json["force_instrumental"] is True and 3000 <= json["music_length_ms"] <= 600000
            self.length = json["music_length_ms"] / 1000
            return _R(200, tone_mp3(880, self.length, os.path.join(TMP, "trk.mp3")))
        if url.endswith("/v1/audio-isolation"):
            return _R(200, tone_mp3(300, 4.0, os.path.join(TMP, "voice.mp3")))
        return _R(404)


def test_compose_asks_for_an_instrumental_of_the_hook_length():
    f = Fake11()
    mp3 = M.compose(M.PRESETS["epic"], 6.3, f)
    assert len(mp3) > 1000 and abs(f.length - 6.3) < 0.01


def test_strip_original_replaces_the_old_bed_and_keeps_the_voice():
    hook, out = os.path.join(TMP, "hook.mp4"), os.path.join(TMP, "out.mp4")
    make_hook(hook)
    f = Fake11()
    trk = os.path.join(TMP, "bed.mp3")
    open(trk, "wb").write(M.compose("x", 4.5, f))
    voice = M.isolate_voice(hook, os.path.join(TMP, "v.wav"), f)
    M.mix(hook, trk, out, volume=0.5, duck=False, voice_path=voice)
    assert abs(M.duration(out) - M.duration(hook)) < 0.06, "hook length changed"
    t = 1.5
    assert level(out, 300, t) > 1000, "voice lost"
    assert level(out, 880, t) > 500, "new music missing"
    assert level(out, 500, t) < level(hook, 500, t) * 0.1, "old music still there"


def test_without_strip_the_original_audio_stays_under_the_new_bed():
    hook, out = os.path.join(TMP, "hook2.mp4"), os.path.join(TMP, "out2.mp4")
    make_hook(hook)
    trk = os.path.join(TMP, "bed2.mp3")
    tone_mp3(880, 5, trk)
    M.mix(hook, trk, out, volume=0.5, duck=False)
    assert level(out, 500, 1.5) > level(hook, 500, 1.5) * 0.5 and level(out, 880, 1.5) > 500


def test_music_fades_out_at_the_end_of_the_hook():
    hook, out = os.path.join(TMP, "hook3.mp4"), os.path.join(TMP, "out3.mp4")
    make_hook(hook)
    trk = os.path.join(TMP, "bed3.mp3")
    tone_mp3(880, 5, trk)
    M.mix(hook, trk, out, volume=0.5, duck=False)
    assert level(out, 880, 3.85, 0.1) < level(out, 880, 2.0, 0.1) * 0.35, "no fade-out before the body"


def test_missing_permission_names_the_switch_to_turn_on():
    try:
        M.compose("x", 5, Fake11(deny="/v1/music"))
    except M.MusicError as e:
        assert "Music Generation" in str(e)
    else:
        raise AssertionError("must fail")
    hook = os.path.join(TMP, "hook4.mp4")
    make_hook(hook)
    try:
        M.isolate_voice(hook, os.path.join(TMP, "v2.wav"), Fake11(deny="isolation"))
    except M.MusicError as e:
        assert "Audio Isolation" in str(e)
    else:
        raise AssertionError("must fail")


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
