#!/usr/bin/env python3
"""Slice the hero sprite sheets in Heroes/ into individual transparent PNG frames.

Each sheet = one hero, 5 poses in a row on a white background. We:
  1) split into poses by detecting all-white gap columns,
  2) crop each pose to its content,
  3) remove the white background by flood-filling from the corners (so internal
     white details — sparkles, light fur, armor — are preserved),
  4) save Heroes/frames/<hero>/<state>.png  (states: idle, attack, hit, super, win)
"""
import glob
import os
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERO_DIR = os.path.join(ROOT, "Heroes")
OUT_DIR = os.path.join(HERO_DIR, "frames")
STATES = ["idle", "attack", "hit", "super", "win"]
WHITE = 244            # a pixel >= this on all channels counts as background
NFRAMES = 5            # each sheet = 5 poses in a row (split into 5 equal columns)


def is_white(p):
    return p[0] >= WHITE and p[1] >= WHITE and p[2] >= WHITE


def equal_slices(img):
    w, _ = img.size
    cw = w / NFRAMES
    return [(int(round(i * cw)), int(round((i + 1) * cw))) for i in range(NFRAMES)]


def content_bbox(img, x0, x1):
    """Tight bbox of non-white content inside the column [x0, x1) (sampled every 2px)."""
    px = img.load()
    w, h = img.size
    lx = rx = top = bot = None
    for y in range(0, h, 2):
        for x in range(x0, x1, 2):
            if not is_white(px[x, y]):
                lx = x if lx is None or x < lx else lx
                rx = x if rx is None or x > rx else rx
                top = y if top is None else top
                bot = y
    if lx is None:
        return None
    pad = 8
    return (max(x0, lx - pad), max(0, top - pad), min(x1, rx + pad), min(h, bot + pad))


def matte(im):
    rgb = im.convert("RGB")
    w, h = rgb.size
    SENT = (255, 0, 254)
    for seed in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]:
        try:
            ImageDraw.floodfill(rgb, seed, SENT, thresh=42)
        except Exception:
            pass
    out = im.convert("RGBA")
    op, sp = out.load(), rgb.load()
    for y in range(h):
        for x in range(w):
            if sp[x, y] == SENT:
                op[x, y] = (0, 0, 0, 0)
    return out


def main():
    files = sorted(glob.glob(os.path.join(HERO_DIR, "*.jpeg")) +
                   glob.glob(os.path.join(HERO_DIR, "*.jpg")) +
                   glob.glob(os.path.join(HERO_DIR, "*.png")))
    if not files:
        print("No sheets in", HERO_DIR)
        return
    for f in files:
        base = os.path.splitext(os.path.basename(f))[0]
        safe = "".join(c if c.isalnum() else "_" for c in base).strip("_").lower() or "hero"
        img = Image.open(f).convert("RGB")
        d = os.path.join(OUT_DIR, safe)
        os.makedirs(d, exist_ok=True)
        n = 0
        for i, (x0, x1) in enumerate(equal_slices(img)):
            bb = content_bbox(img, x0, x1)
            if not bb:
                continue
            fr = matte(img.crop(bb))
            st = STATES[i] if i < len(STATES) else f"f{i}"
            fr.save(os.path.join(d, st + ".png"))
            n += 1
        print(f"{os.path.basename(f)}  ->  {n} frames  ->  frames/{safe}/")
    print("done ->", OUT_DIR)


if __name__ == "__main__":
    main()
