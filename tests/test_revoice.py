"""Re-voice — offline tests with REAL audio (ffmpeg), no network, no credits.

Run from the repo root:   python3 tests/test_revoice.py

A fake ElevenLabs answers the three calls exactly as the docs describe them (clone → voice_id,
TTS → base64 mp3 + per-character alignment, delete → ok). The "voice" is a tone: the clip says
300 Hz everywhere and the WRONG word at 500 Hz; the fake TTS speaks at 880 Hz. So after the
splice we can measure, second by second, what is actually in the audio.
"""
import base64
import json
import os
import struct
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["ELEVENLABS_API_KEY"] = "test-key"
from engine import revoice as RV  # noqa: E402

TMP = tempfile.mkdtemp(prefix="rv_test_")


def sh(*cmd):
    subprocess.run(cmd, capture_output=True, check=True)


def make_clip(path, dur=4.0, wrong=(1.6, 2.2), size="320x240"):
    """Video + audio: 300 Hz 'speech', the wrong word at 500 Hz, short gaps around it."""
    a, b = wrong
    expr = (f"if(between(t,{a},{b}),0.5*sin(2*PI*500*t),"
            f"if(between(t,{a-0.12},{a})+between(t,{b},{b+0.12}),0,0.5*sin(2*PI*300*t)))")
    sh("ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=25:duration={dur}",
       "-f", "lavfi", "-i", f"aevalsrc='{expr}':s=44100:d={dur}", "-shortest",
       "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "44100", "-ac", "2", path)


def tts_mp3(text, lead=0.10, speech=0.50):
    """What the fake ElevenLabs returns: silence, then the 'voice' (880 Hz) for `speech` s."""
    p = os.path.join(TMP, "tts.mp3")
    total = lead + speech + 0.15
    sh("ffmpeg", "-y", "-f", "lavfi", "-i",
       f"aevalsrc='if(between(t,{lead},{lead+speech}),0.5*sin(2*PI*880*t),0)':s=44100:d={total}",
       "-b:a", "128k", p)
    chars = list(text)
    n = len(chars)
    st = [lead + speech * i / n for i in range(n)]
    en = [lead + speech * (i + 1) / n for i in range(n)]
    return open(p, "rb").read(), {"characters": chars, "character_start_times_seconds": st,
                                  "character_end_times_seconds": en}


class _R:
    def __init__(self, code, js=None):
        self.status_code, self._js, self.text = code, js, json.dumps(js or {})

    def json(self):
        return self._js


class Fake11:
    def __init__(self, speech=0.50, fail_tts=False):
        self.speech, self.fail_tts, self.calls, self.deleted = speech, fail_tts, [], []

    def post(self, url, headers=None, data=None, files=None, params=None, timeout=None):
        self.calls.append(url)
        assert headers.get("xi-api-key") == "test-key"
        if url.endswith("/v1/voices/add"):
            assert files and files[0][0] == "files", "clone must upload the sample as `files`"
            return _R(200, {"voice_id": "v_tmp_123", "requires_verification": False})
        if "/with-timestamps" in url:
            if self.fail_tts:
                return _R(500, {"detail": "boom"})
            body = json.loads(data)
            self.last_body = body
            mp3, al = tts_mp3(body["text"], speech=self.speech)
            return _R(200, {"audio_base64": base64.b64encode(mp3).decode(), "alignment": al})
        return _R(404)

    def delete(self, url, headers=None, timeout=None):
        self.deleted.append(url.rsplit("/", 1)[-1])
        return _R(200, {"status": "ok"})


def freq(path, t, win=0.12):
    raw = subprocess.run(["ffmpeg", "-v", "quiet", "-ss", str(t), "-t", str(win), "-i", path,
                          "-ac", "1", "-ar", "44100", "-f", "s16le", "-"], capture_output=True).stdout
    v = struct.unpack("<%dh" % (len(raw) // 2), raw[:len(raw) // 2 * 2])
    if not v or max(abs(x) for x in v) < 300:
        return 0
    # crossings WITH HYSTERESIS: low-level codec noise in a quiet tail flips sign hundreds of
    # times and would read as a high pitch; only count swings past ±500
    crossings, state = 0, 0
    for x in v:
        if x > 500 and state <= 0:
            crossings += state < 0; state = 1
        elif x < -500 and state >= 0:
            crossings += state > 0; state = -1
    return round(crossings / 2 / (len(v) / 44100.0))


def tone_power(path, f, t, win=0.2):
    """Goertzel power of one frequency in a window (robust where zero-crossings are not)."""
    import math
    raw = subprocess.run(["ffmpeg", "-v", "quiet", "-ss", str(t), "-t", str(win), "-i", path, "-ac", "1",
                          "-ar", "8000", "-f", "s16le", "-"], capture_output=True).stdout
    x = struct.unpack("<%dh" % (len(raw) // 2), raw[:len(raw) // 2 * 2])
    k, s1, s2 = 2 * math.cos(2 * math.pi * f / 8000), 0.0, 0.0
    for v in x:
        s1, s2 = v + k * s1 - s2, s1
    return math.sqrt(max(0.0, s1 * s1 + s2 * s2 - k * s1 * s2)) / max(1, len(x))


SPAN = {"a": 1.6, "b": 2.2, "lo": 1.48, "hi": 2.32, "text": "Tai Chi", "say": "Tie Chee",
        "prev_text": "I start the", "next_text": "challenge this Monday"}


def test_plan_shorter_fits_without_tempo():
    p = RV.plan_patch(SPAN, 0.40, 4.0)
    assert p["tempo"] == 1.0 and abs(p["place"] - 1.58) < 1e-6
    assert abs(p["mute"][0] - 1.585) < 1e-9 and abs(p["mute"][1] - 2.215) < 1e-9


def test_plan_slightly_longer_is_sped_up_but_capped():
    p = RV.plan_patch(SPAN, 0.80, 4.0)
    assert 1.0 < p["tempo"] <= RV.MAX_TEMPO
    try:
        RV.plan_patch(SPAN, 2.0, 4.0)
    except RV.RevoiceError as e:
        assert "скороти текст" in str(e)
    else:
        raise AssertionError("a far-too-long phrase must be refused")


def test_splice_replaces_only_the_word_and_keeps_length():
    clip, out = os.path.join(TMP, "clip.mp4"), os.path.join(TMP, "out.mp4")
    make_clip(clip)
    fake = Fake11(speech=0.50)
    res = RV.revoice(clip, [SPAN], out, requests=fake)
    assert abs(RV.duration(out) - RV.duration(clip)) < 0.06, "clip length changed"
    for t in (0.3, 0.6, 1.0, 2.5, 3.2):
        assert abs(freq(out, t) - 300) < 15, f"audio outside the span was touched at {t}s: {freq(out, t)} Hz"
    for t in (1.65, 1.8, 1.95):
        f_new = freq(out, t, 0.1)
        assert abs(f_new - 880) < 40, f"span should hold ONLY the new voice at {t}s, got {f_new} Hz (old word leaking?)"
    assert abs(freq(clip, 1.75) - 500) < 15, "fixture sanity"


def test_slightly_longer_phrase_is_sped_up_and_still_fits():
    clip, out = os.path.join(TMP, "clip6.mp4"), os.path.join(TMP, "out6.mp4")
    make_clip(clip)
    res = RV.revoice(clip, [SPAN], out, requests=Fake11(speech=0.78))   # slot is ~0.71 s
    sp = res["spans"][0]
    assert 1.0 < sp["tempo"] <= RV.MAX_TEMPO, sp["tempo"]
    assert abs(RV.duration(out) - RV.duration(clip)) < 0.06, "clip length changed"
    assert sp["words"][-1]["end"] <= SPAN["hi"] + 1e-6, "sped-up words spill into the next word"
    assert abs(freq(out, 2.5) - 300) < 15, "the word after the span was overwritten"


def test_new_caption_timings_sit_inside_the_slot():
    clip, out = os.path.join(TMP, "clip2.mp4"), os.path.join(TMP, "out2.mp4")
    make_clip(clip)
    res = RV.revoice(clip, [SPAN], out, requests=Fake11(speech=0.50))
    ws = res["spans"][0]["words"]
    assert [w["word"] for w in ws] == ["Tai", "Chi"], "captions show the TEXT, not the phonetic `say`"
    assert SPAN["lo"] <= ws[0]["start"] < ws[-1]["end"] <= SPAN["hi"], ws


def test_stitching_context_and_phonetic_say_are_sent():
    clip, out = os.path.join(TMP, "clip3.mp4"), os.path.join(TMP, "out3.mp4")
    make_clip(clip)
    fake = Fake11()
    RV.revoice(clip, [SPAN], out, requests=fake)
    b = fake.last_body
    assert b["text"] == "Tie Chee", "the phonetic version is what gets spoken"
    assert b["previous_text"] == "I start the" and b["next_text"] == "challenge this Monday"
    assert b["model_id"] == RV.MODEL


def test_voice_clone_is_deleted_even_when_tts_fails():
    clip, out = os.path.join(TMP, "clip4.mp4"), os.path.join(TMP, "out4.mp4")
    make_clip(clip)
    fake = Fake11(fail_tts=True)
    try:
        RV.revoice(clip, [SPAN], out, requests=fake)
    except RV.RevoiceError:
        pass
    assert fake.deleted == ["v_tmp_123"], "a failed job must not leave a voice in the account"


def test_two_spans_one_clone_one_pass():
    clip, out = os.path.join(TMP, "clip5.mp4"), os.path.join(TMP, "out5.mp4")
    make_clip(clip, dur=6.0)
    s2 = dict(SPAN, a=4.0, b=4.6, lo=3.9, hi=4.75, text="this week", say="this week")
    fake = Fake11(speech=0.45)
    res = RV.revoice(clip, [SPAN, s2], out, requests=fake)
    assert sum(1 for c in fake.calls if c.endswith("/v1/voices/add")) == 1, "one clone per clip"
    assert len(res["spans"]) == 2 and abs(freq(out, 4.2) - 880) < 40


def test_late_recogniser_start_does_not_leak_the_old_onset():
    """1.9.69 in production: re-voicing a sentence left "A… Another". The recogniser put the
    first word 120 ms after it really started; muting from there kept the old onset audible."""
    clip, out = os.path.join(TMP, "clip8.mp4"), os.path.join(TMP, "out8.mp4")
    make_clip(clip)                                   # the old word REALLY sounds 1.60–2.20
    late = dict(SPAN, a=1.72, b=2.10)                 # …but the recogniser says 1.72–2.10
    res = RV.revoice(clip, [late], out, requests=Fake11(speech=0.50))
    for t in (1.60, 1.64, 2.12, 2.16):
        f = freq(out, t, 0.04)
        assert abs(f - 500) > 60, f"old word still audible at {t}s ({f} Hz)"
    assert abs(freq(out, 1.2) - 300) < 15 and abs(freq(out, 2.6) - 300) < 15, "neighbours touched"
    assert abs(RV.duration(out) - RV.duration(clip)) < 0.06


def test_snap_falls_back_to_a_margin_when_there_is_no_silence():
    clip = os.path.join(TMP, "clip9.mp4")
    sh("ffmpeg", "-y", "-f", "lavfi", "-i", "sine=f=300:d=3", "-ac", "2", "-c:a", "aac", clip)   # no dip anywhere
    s = RV.snap_to_audio(clip, {"a": 1.0, "b": 1.5, "lo": 0.9, "hi": 1.6}, 3.0)
    assert abs(s["a"] - 0.94) < 1e-6 and abs(s["b"] - 1.56) < 1e-6, s


def test_clone_is_made_from_the_speaker_who_says_the_span():
    """1.9.75 in production: a drama hook, the heroine's first line re-voiced in someone else's
    voice — the clone was made from the WHOLE clip (every character mixed). Now: diarize, and
    clone only from the speaker of the span. A speaks at 300 Hz, B (the span) at 500 Hz."""
    clip, out = os.path.join(TMP, "clip10.mp4"), os.path.join(TMP, "out10.mp4")
    expr = ("if(between(t,0,2)+between(t,5.2,7),0.5*sin(2*PI*300*t),"
            "if(between(t,2.2,5),0.5*sin(2*PI*500*t),0))")
    sh("ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=7", "-f", "lavfi",
       "-i", f"aevalsrc='{expr}':s=44100:d=7", "-shortest", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "44100", "-ac", "2", clip)
    words = []
    for s0, s1, spk in ((0.0, 2.0, "speaker_0"), (2.2, 5.0, "speaker_1"), (5.2, 7.0, "speaker_0")):
        t = s0
        while t + 0.4 <= s1 + 1e-6:
            words.append({"text": "w", "type": "word", "start": round(t, 2), "end": round(t + 0.35, 2), "speaker_id": spk})
            t += 0.4

    class F(Fake11):
        samples = []

        def post(self, url, headers=None, data=None, files=None, params=None, timeout=None):
            if url.endswith("/v1/speech-to-text"):
                assert data["diarize"] == "true"
                return _R(200, {"words": words})
            if url.endswith("/v1/voices/add"):
                p = os.path.join(TMP, f"sample_{len(F.samples)}.mp3")
                open(p, "wb").write(files[0][1][1].read())
                F.samples.append(p)
            return Fake11.post(self, url, headers=headers, data=data, files=files, params=params, timeout=timeout)

    span = dict(SPAN, a=3.0, b=3.6, lo=2.95, hi=3.8)
    res = RV.revoice(clip, [span], out, requests=F())
    assert len(F.samples) == 1 and res["spans"][0]["speaker"] == "speaker_1"
    d = RV.duration(F.samples[0])
    assert 2.5 < d < 3.2, f"sample should be ONLY speaker_1's ~2.8 s, got {d:.2f}"
    for t in (0.3, 1.2, 2.0):
        a, b = tone_power(F.samples[0], 300, t), tone_power(F.samples[0], 500, t)
        assert b > 20 * a, f"another character's voice leaked into the clone sample at {t}s (300 Hz {a:.0f} vs 500 Hz {b:.0f})"


def test_restricted_key_says_which_permission_is_missing():
    """1.9.65 in production: a key that transcribes fine got a bare '401' on clone."""
    class F(Fake11):
        def post(self, url, **k):
            return _R(401, {"detail": {"status": "missing_permissions",
                                       "message": "The API key you used is missing the permission voices_write to execute this operation."}})
    clip, out = os.path.join(TMP, "clip7.mp4"), os.path.join(TMP, "out7.mp4")
    make_clip(clip)
    try:
        RV.revoice(clip, [SPAN], out, requests=F())
    except RV.RevoiceError as e:
        assert "Voices: Write" in str(e) and "voices_write" in str(e), str(e)
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
