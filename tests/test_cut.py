"""✂ Cut segment — offline test with real audio/video (ffmpeg), no network.

Run from the repo root:   python3 tests/test_cut.py

The fixture "speaks" 11 words as 300 Hz bursts with silent gaps, exactly where the word list
says. So we can check that a cut lands in the silence (never through a word) and that the
caption words after it move earlier by exactly the removed time.
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient  # noqa: E402
import server.app as A  # noqa: E402
from engine import revoice as RV  # noqa: E402

C = TestClient(A.app)
WORDS = [{"word": w, "start": round(0.2 + i * 0.5, 2), "end": round(0.55 + i * 0.5, 2)}
         for i, w in enumerate("One two three. Four five six seven. Eight nine ten eleven.".split())]


def _clip():
    p = os.path.join(tempfile.mkdtemp(prefix="cut_t_"), "words.mp4")
    expr = "+".join(f"between(t,{w['start']},{w['end']})" for w in WORDS)
    subprocess.run(["ffmpeg", "-v", "quiet", "-y", "-f", "lavfi", "-i", "testsrc=size=320x568:rate=30:duration=6",
                    "-f", "lavfi", "-i", f"aevalsrc='if({expr},0.5*sin(2*PI*300*t),0)':s=44100:d=6", "-shortest",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "44100", "-ac", "2", p], check=True)
    up = C.post("/api/upload_clip", files={"file": ("words.mp4", open(p, "rb"), "video/mp4")}).json()
    return up["file_id"], up["name"]


def _cut(fid, name, spans, speech):
    r = C.post("/api/cut", json={"file_id": fid, "clip": name, "spans": spans, "speech": speech,
                                 "words": WORDS, "orig_words": WORDS})
    assert r.status_code == 200, r.text
    return r.json()


def test_timeline_cut_inside_words_moves_to_the_silence():
    fid, name = _clip()
    d = _cut(fid, name, [[1.8, 2.9]], False)            # 1.8 is inside "Four", 2.9 inside "six"
    (a, b), = d["spans"]
    assert 1.55 <= a <= 1.70 and 3.05 <= b <= 3.20, d["spans"]
    got = [w["word"] for w in d["words"]]
    assert "Four" not in got and "six" not in got and "seven." in got
    out = os.path.join(A._session_dir(fid), d["clip"])
    assert abs(RV.duration(out) - (6 - d["removed"])) < 0.06
    seven = next(w for w in d["words"] if w["word"] == "seven.")
    assert abs(seven["start"] - (3.2 - d["removed"])) < 0.04


def test_timeline_cut_in_a_pause_stays_frame_exact():
    fid, name = _clip()
    d = _cut(fid, name, [[1.6, 1.667]], False)          # wholly inside the pause after "three."
    (a, b), = d["spans"]
    assert abs(a - 1.6) < 0.02 and abs(b - 1.667) < 0.02, d["spans"]
    assert len(d["words"]) == len(WORDS)


def test_sentence_cut_keeps_one_natural_pause():
    fid, name = _clip()
    d = _cut(fid, name, [[1.7, 3.55]], True)            # "Four five six seven."
    got = [w["word"] for w in d["words"]]
    assert got == ["One", "two", "three.", "Eight", "nine", "ten", "eleven."], got
    three, eight = d["words"][2], d["words"][3]
    gap = eight["start"] - three["end"]
    assert 0.08 <= gap <= 0.25, f"join should keep ONE normal pause, got {gap:.3f}s"


def test_cutting_almost_everything_is_refused():
    fid, name = _clip()
    r = C.post("/api/cut", json={"file_id": fid, "clip": name, "spans": [[0.0, 5.9]], "words": WORDS})
    assert r.status_code == 400


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
