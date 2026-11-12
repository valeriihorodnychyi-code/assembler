"""Hook render: trim + captions in ONE pass, and the baked-hook cache — offline, real video.

Run from the repo root:   python3 tests/test_render_onepass.py

The one-pass hook must be the same picture as the old two-pass one (trim → re-encode →
captions → re-encode), the same length, and a second assemble of the same hook must come
from the cache. Any change to the words must miss the cache.
"""
import os
import subprocess
import sys
import tempfile
import time

TMP = tempfile.mkdtemp(prefix="onepass_")
os.environ["CS_CACHE_DIR"] = os.path.join(TMP, "cache")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine import compose as C, styles as st, ffmpeg_utils as ff  # noqa: E402

SRC = os.path.join(TMP, "src.mp4")
subprocess.run(["ffmpeg", "-v", "quiet", "-y", "-f", "lavfi", "-i", "testsrc=size=540x960:rate=30:duration=7",
                "-f", "lavfi", "-i", "sine=f=440:d=7", "-shortest", "-pix_fmt", "yuv420p", "-c:v", "libx264",
                "-preset", "veryfast", "-c:a", "aac", SRC], check=True)
WORDS = [{"word": w, "start": 1.0 + i * 0.5, "end": 1.4 + i * 0.5} for i, w in enumerate("ONE PASS HOOK TEST".split())]
TRIM = (1.0, 5.0)


def psnr(a, b):
    r = subprocess.run(["ffmpeg", "-i", a, "-i", b, "-lavfi", "psnr", "-f", "null", "-"], capture_output=True, text=True).stderr
    line = [x for x in r.split("\n") if "PSNR" in x and "average:" in x][-1]
    v = line.split("average:")[1].split()[0]
    return 99.0 if v == "inf" else float(v)


def test_one_pass_matches_two_pass():
    style = st.normalize({})
    w1, w2 = tempfile.mkdtemp(dir=TMP), tempfile.mkdtemp(dir=TMP)
    cut = os.path.join(w1, "cut.mp4")
    C.trim_clip(SRC, *TRIM, cut)
    old, new = os.path.join(TMP, "old.mp4"), os.path.join(TMP, "new.mp4")
    C.caption_clip(cut, C.shift_words(WORDS, *TRIM), style, "9:16", old, w1)
    C.caption_clip(SRC, WORDS, style, "9:16", new, w2, src_trim=TRIM)   # clip-time words: the layout moves onto the trim
    assert abs(ff.get_video_duration(old) - ff.get_video_duration(new)) < 0.04
    p = psnr(old, new)
    assert p > 45, f"one-pass picture differs from two-pass (PSNR {p:.1f} dB)"


def test_same_hook_twice_comes_from_the_cache_and_a_change_misses_it():
    seg = {"clip": SRC, "words": WORDS, "style": st.normalize({}), "trim": list(TRIM)}
    t0 = time.time()
    C.assemble_recipe([dict(seg)], "9:16", os.path.join(TMP, "a1.mp4"))
    first = time.time() - t0
    hits0 = C.OP_TIMES["cache_hits"]
    t0 = time.time()
    C.assemble_recipe([dict(seg)], "9:16", os.path.join(TMP, "a2.mp4"))
    assert C.OP_TIMES["cache_hits"] == hits0 + 1 and time.time() - t0 < first
    changed = dict(seg, words=[dict(w) for w in WORDS])
    changed["words"][0]["word"] = "TWO"
    assert C._bake_key(changed, "9:16") != C._bake_key(seg, "9:16"), "an edited word must re-bake the hook"
    assert C._bake_key(dict(seg, trim=[1.0, 4.0]), "9:16") != C._bake_key(seg, "9:16")



def test_trim_keeps_the_preview_layout():
    """A word that ENDS just before In must not re-chunk the captions: the render shows the
    same caption the preview shows at In («I ALWAYS THOUGHT…», not «ALWAYS THOUGHT TAI…»)."""
    style = st.normalize({})
    ws = [{"word": w, "start": 0.30 + i * 0.4, "end": 0.6 + i * 0.4} for i, w in enumerate("I always thought Tai Chi was only".split())]
    full = C.build_timeline(ws, [{"start": 0, "end": None, "style": style}])
    lo = 0.62                                   # In after «I» ended
    tr = C.trim_tagged(full, lo, 5.0)
    first = " ".join(w["text"] for line in tr[0][0]["lines"] for w in line)
    at_in = [e for e, s in full if e["start"] <= lo < e["end"]][0]
    assert first == " ".join(w["text"] for line in at_in["lines"] for w in line), first
    assert tr[0][0]["start"] == 0.0 and all(e["start"] >= 0 for e, s in tr)


def test_titles_move_with_the_trim():
    h = C.shift_titles([{"text": "A", "in": 2.0, "out": 4.0}, {"text": "B", "in": 6.0}], 1.5, 3.0)
    assert h == [{"text": "A", "in": 0.5, "out": 2.5}], h


def test_joined_creative_starts_on_the_first_frame():
    """Stream-copy joins used to start the VIDEO 23 ms late (AAC priming): an empty edit that
    QuickTime shows as a black first frame. The video must start at 0."""
    out = os.path.join(TMP, "joined.mp4")
    C.assemble_segments([SRC, SRC], "9:16", out)
    v = float(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=start_time",
                              "-of", "csv=p=0", out], capture_output=True, text=True).stdout.strip())
    assert v < 0.001, f"video starts at {v:.3f} s — black first frame in QuickTime"
    assert C.CONCAT_STATS["copy"] >= 1

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
