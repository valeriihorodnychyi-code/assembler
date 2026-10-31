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
    C.caption_clip(SRC, C.shift_words(WORDS, *TRIM), style, "9:16", new, w2, src_trim=TRIM)
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
