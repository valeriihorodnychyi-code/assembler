"""Hook | Body boundary by comparing a pack — offline test with real video (ffmpeg).

Run from the repo root:   python3 tests/test_bodydetect.py

Three hooks (different pictures, different lengths) + ONE shared body, plus a "localization"
of hook 1 (same picture end to end). The boundary must land where each hook ends, and the
localization must get its project's boundary, not "no body".
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine import bodydetect as B  # noqa: E402

TMP = tempfile.mkdtemp(prefix="body_t_")


def make(name, hook_src, hook_len, body_len=6.0):
    out = os.path.join(TMP, name)
    subprocess.run(["ffmpeg", "-v", "quiet", "-y",
                    "-f", "lavfi", "-i", f"{hook_src}{':' if '=' in hook_src else '='}size=320x576:rate=25:duration={hook_len}",
                    "-f", "lavfi", "-i", f"testsrc=size=320x576:rate=25:duration={body_len}",
                    "-filter_complex", "[0:v]format=yuv420p[a];[1:v]format=yuv420p[b];[a][b]concat=n=2:v=1:a=0[v]",
                    "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", out], check=True)
    return out


def test_boundary_is_where_each_hook_ends():
    c1 = make("h1.mp4", "color=c=red", 3.0)
    c2 = make("h2.mp4", "smptebars", 2.5)
    c3 = make("h3.mp4", "color=c=blue", 4.0)
    loc = os.path.join(TMP, "h1_es.mp4")
    subprocess.run(["cp", c1, loc], check=True)          # a dub = the same picture
    res = B.detect([{"id": "1", "path": c1}, {"id": "2", "path": c2}, {"id": "3", "path": c3},
                    {"id": "1es", "path": loc}])
    for k, want in (("1", 3.0), ("2", 2.5), ("3", 4.0), ("1es", 3.0)):
        got = res[k]["body"]
        assert got is not None and abs(got - want) <= 0.3, f"{k}: body at {got}, expected ~{want}"
    assert res["1es"]["matched"] >= 2, "the localization is compared with the OTHER projects"


def test_a_clip_that_matches_nobody_gets_no_body():
    a = make("x1.mp4", "color=c=green", 3.0, body_len=3.0)
    b = os.path.join(TMP, "x2.mp4")
    subprocess.run(["ffmpeg", "-v", "quiet", "-y", "-f", "lavfi", "-i", "smptebars=size=320x576:rate=25:duration=5",
                    "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", b], check=True)
    res = B.detect([{"id": "a", "path": a}, {"id": "b", "path": b}])
    assert res["a"]["body"] is None and res["b"]["body"] is None


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
