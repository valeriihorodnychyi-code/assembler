"""Where does the BODY start? — found by comparing the clips of a pack.

A pack is usually N hooks glued to ONE shared body part: the tails are the same picture, the
heads differ. So: take a tiny fingerprint of every clip (16x16 grey frame, 4 per second), line
the clips up by their END, and walk backwards while the frames still match. Where two clips
stop matching is where the body starts. No library, no network, no AI.

Clips that match over (nearly) their WHOLE length are versions of the same project (a dub of
the same picture) — they tell nothing about the boundary, so they are compared only with clips
of OTHER projects. A clip that matches nobody gets no body (the UI says so; the designer can
set it by hand).
"""
import subprocess

from . import ffmpeg_utils as ff

FPS = 4               # 0.25 s steps; the UI then snaps to the nearest scene cut (frame-exact)
SIDE = 16
THR = 14.0            # mean abs difference (0..255) below which two frames are "the same"
MIN_BODY = 1.0        # seconds — a shorter common tail is a coincidence (a black frame, a logo)
SAME_PROJECT = 0.95   # matching over 95 % of the shorter clip = the same picture (a localization)


def fingerprint(path, fps=FPS):
    """→ list of frames, each a bytes object of SIDE*SIDE grey pixels."""
    cmd = [ff.FFMPEG, "-v", "error", "-i", path, "-an",
           "-vf", f"fps={fps},scale={SIDE}:{SIDE}:flags=area,format=gray", "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout or b""
    n = SIDE * SIDE
    return [raw[i:i + n] for i in range(0, len(raw) - n + 1, n)]


def _mad(a, b):
    return sum(abs(x - y) for x, y in zip(a, b)) / float(len(a) or 1)


def tail_match(a, b, thr=THR):
    """How many trailing frames of a and b are the same picture (1 noisy frame is forgiven)."""
    k, miss, best = 0, 0, 0
    while k < len(a) and k < len(b):
        if _mad(a[-1 - k], b[-1 - k]) < thr:
            miss = 0
            best = k + 1
        else:
            miss += 1
            if miss > 1:
                break
        k += 1
    return best


def detect(clips, fps=FPS):
    """clips: [{"id", "path"}] → {id: {"body": seconds|None, "matched": n, "length": s}}."""
    prints = {c["id"]: fingerprint(c["path"], fps) for c in clips}
    ids = [c["id"] for c in clips]
    out = {}
    for i in ids:
        a = prints[i]
        lens = []
        for j in ids:
            if j == i:
                continue
            b = prints[j]
            if not a or not b:
                continue
            m = tail_match(a, b)
            if m >= SAME_PROJECT * min(len(a), len(b)):
                continue                      # same picture end to end → a version of this project
            if m / fps >= MIN_BODY:
                lens.append(m)
        if lens:
            L = max(lens)
            out[i] = {"body": round((len(a) - L) / fps, 3), "matched": len(lens), "length": round(len(a) / fps, 3)}
        else:
            out[i] = {"body": None, "matched": 0, "length": round(len(a) / fps, 3)}
    return out
