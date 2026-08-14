#!/usr/bin/env python3
"""Assembler render benchmark — measures REAL creatives, so the numbers are a usable standard.

Why: to size cloud render capacity you need two numbers per machine:
  1) realtime factor  = render seconds / video seconds   (lower is better; 0.5x = 2x faster than realtime)
  2) parallel scaling = how much throughput you gain running N renders at once

Usage
-----
  # one or more real files (or a folder of them)
  python3 tools/benchmark.py /path/to/creatives                     # sequential pass
  python3 tools/benchmark.py clip1.mp4 clip2.mp4 --repeat 3         # median of 3
  python3 tools/benchmark.py /path/to/creatives --parallel 1,2,4    # concurrency sweep

Writes benchmark_<host>_<timestamp>.csv next to the outputs and prints a summary
table you can hand straight to the architect.

Notes
  * Captions are synthesized from the clip's own length (a realistic caption load,
    ~2.2 words/sec) so every machine renders the SAME work for a given file — no
    transcription API needed and results are reproducible.
  * Transcription is deliberately NOT measured here: it's a different workload
    (Whisper/Scribe) and would hide the ffmpeg/Pillow cost we're sizing.
"""
import argparse
import concurrent.futures as cf
import csv
import os
import platform
import socket
import statistics
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import compose, styles as st, ffmpeg_utils as ff  # noqa: E402

VIDEO_EXT = (".mp4", ".mov", ".m4v", ".webm")


def collect(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            out += [os.path.join(p, f) for f in sorted(os.listdir(p))
                    if f.lower().endswith(VIDEO_EXT) and not f.startswith(".")]
        elif p.lower().endswith(VIDEO_EXT):
            out.append(p)
    return out


def fake_words(duration, wps=2.2):
    """A realistic caption load derived from the clip length (same work on every machine)."""
    n = max(1, int(duration * wps))
    slot = duration / n
    sample = ["THIS", "IS", "YOUR", "BODY", "AFTER", "REGULAR", "WALKING", "AND", "IT", "WORKS"]
    return [{"word": sample[i % len(sample)], "start": round(i * slot, 3),
             "end": round((i + 1) * slot, 3)} for i in range(n)]


def bench_one(src, fmt="9:16", with_captions=True):
    """Render one clip once. Returns (seconds, video_seconds, out_bytes)."""
    dur = ff.get_video_duration(src) or 0
    style = st.normalize({"font_size": 90, "max_chars_per_line": 16, "max_lines": 2,
                          "stroke_on": True, "stroke_outer": {"width": 8, "color": [0, 0, 0, 255]},
                          "karaoke": {"enabled": True}})
    words = fake_words(dur) if with_captions else []
    work = tempfile.mkdtemp(prefix="cs_bench_")
    out = os.path.join(work, "out.mp4")
    t0 = time.time()
    compose.caption_clip(src, words, style, fmt, out, work)
    el = time.time() - t0
    size = os.path.getsize(out) if os.path.exists(out) else 0
    return el, dur, size


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+", help="video files and/or folders with real creatives")
    ap.add_argument("--fmt", default="9:16", choices=["9:16", "1:1", "16:9"])
    ap.add_argument("--repeat", type=int, default=1, help="runs per file; median is reported")
    ap.add_argument("--parallel", default="", help="concurrency sweep, e.g. 1,2,4")
    ap.add_argument("--out", default="", help="CSV path (default: ./benchmark_<host>_<ts>.csv)")
    a = ap.parse_args()

    files = collect(a.paths)
    if not files:
        print("No video files found.")
        return 1

    host = socket.gethostname().split(".")[0]
    machine = f"{platform.system()} {platform.machine()} · {os.cpu_count()} cores · encoder={ff.video_encoder()}"
    ts = time.strftime("%Y%m%d_%H%M")
    csv_path = a.out or f"benchmark_{host}_{ts}.csv"

    print(f"\nAssembler render benchmark\n{machine}\nfiles: {len(files)}  format: {a.fmt}  repeat: {a.repeat}\n")
    rows, seq_rt = [], []

    print(f"{'file':<34}{'video s':>9}{'render s':>10}{'realtime':>10}{'MB out':>9}")
    print("-" * 72)
    for f in files:
        times = []
        dur = size = 0
        for _ in range(max(1, a.repeat)):
            el, dur, size = bench_one(f, a.fmt)
            times.append(el)
        el = statistics.median(times)
        rt = el / dur if dur else 0
        seq_rt.append(rt)
        rows.append({"machine": machine, "mode": "sequential", "file": os.path.basename(f),
                     "format": a.fmt, "video_seconds": round(dur, 2), "render_seconds": round(el, 2),
                     "realtime_factor": round(rt, 3), "out_mb": round(size / 1e6, 2), "concurrency": 1})
        print(f"{os.path.basename(f)[:33]:<34}{dur:>9.1f}{el:>10.1f}{rt:>9.2f}x{size/1e6:>9.1f}")

    if seq_rt:
        print("-" * 72)
        print(f"median realtime factor (1 job at a time): {statistics.median(seq_rt):.2f}x  "
              f"→ ~{1/statistics.median(seq_rt):.1f}s of video per second of compute")

    # concurrency sweep: the number that tells you how many workers a machine is worth
    if a.parallel:
        print("\nParallel scaling (same files, N at once)")
        print(f"{'N':>3}{'wall s':>10}{'clips/min':>12}{'speedup':>10}")
        print("-" * 36)
        base = None
        for n in [int(x) for x in a.parallel.split(",") if x.strip().isdigit()]:
            t0 = time.time()
            with cf.ThreadPoolExecutor(max_workers=n) as ex:   # ffmpeg is a subprocess → threads are fine
                list(ex.map(lambda f: bench_one(f, a.fmt), files))
            wall = time.time() - t0
            cpm = len(files) / (wall / 60)
            base = base or wall
            print(f"{n:>3}{wall:>10.1f}{cpm:>12.1f}{base/wall:>9.2f}x")
            rows.append({"machine": machine, "mode": "parallel", "file": f"{len(files)} files",
                         "format": a.fmt, "video_seconds": "", "render_seconds": round(wall, 2),
                         "realtime_factor": "", "out_mb": "", "concurrency": n})

    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["machine", "mode", "file", "format", "video_seconds",
                                           "render_seconds", "realtime_factor", "out_mb", "concurrency"])
        w.writeheader()
        w.writerows(rows)
    print(f"\nCSV: {os.path.abspath(csv_path)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
