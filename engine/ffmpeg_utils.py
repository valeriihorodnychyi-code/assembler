"""Low-level ffmpeg/ffprobe helpers and platform-aware codec selection.

The whole point of keeping renders "seconds fast" on the team's M3 MacBooks is
h264_videotoolbox (Apple hardware encoding). On non-Mac machines we transparently
fall back to libx264 so the same code still runs everywhere.
"""
import os
import platform
import subprocess
import functools

# Binary names. In a packaged .app the launcher puts the bundled ffmpeg/ffprobe
# first on PATH, so the bare names resolve to them. CS_FFMPEG / CS_FFPROBE allow
# an explicit absolute-path override if PATH can't be relied on.
FFMPEG = os.environ.get("CS_FFMPEG") or "ffmpeg"
FFPROBE = os.environ.get("CS_FFPROBE") or "ffprobe"


@functools.lru_cache(maxsize=1)
def video_encoder() -> str:
    """Return the best available H.264 encoder for this machine."""
    if platform.system() == "Darwin":
        try:
            out = subprocess.run(
                [FFMPEG, "-hide_banner", "-encoders"],
                capture_output=True, text=True, check=False,
            ).stdout
            if "h264_videotoolbox" in out:
                return "h264_videotoolbox"
        except FileNotFoundError:
            pass
    return "libx264"


def encoder_quality_args(bitrate: str) -> list:
    """Encoder-specific quality flags.

    videotoolbox uses bitrate targeting; libx264 is happier with CRF, but we keep
    a bitrate target for predictable file sizes across the team.
    """
    enc = video_encoder()
    if enc == "libx264":
        return ["-c:v", "libx264", "-preset", "veryfast", "-b:v", bitrate, "-pix_fmt", "yuv420p"]
    return ["-c:v", enc, "-b:v", bitrate, "-pix_fmt", "yuv420p"]


def get_video_size(path: str):
    cmd = [FFPROBE, "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", path]
    w, h = subprocess.check_output(cmd).decode("utf-8").strip().split("x")
    return int(w), int(h)


def get_video_duration(path: str) -> float:
    cmd = [FFPROBE, "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", path]
    return float(subprocess.check_output(cmd).decode("utf-8").strip())


def has_audio_stream(path: str) -> bool:
    cmd = [FFPROBE, "-v", "error", "-select_streams", "a",
           "-show_entries", "stream=index", "-of", "csv=p=0", path]
    return bool(subprocess.check_output(cmd).decode("utf-8").strip())


def run(cmd: list, quiet: bool = True):
    """Run an ffmpeg command, raising with captured stderr on failure."""
    kwargs = {}
    if quiet:
        kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE}
    proc = subprocess.run(cmd, **kwargs)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "ignore") if proc.stderr else ""
        raise RuntimeError(f"ffmpeg failed (code {proc.returncode}):\n{err[-2000:]}")
    return proc


def detect_scene_cuts(path: str, threshold: float = 0.4) -> list:
    """Return scene-cut timestamps (seconds) via ffmpeg's scene-score filter.

    One decode-only pass (no encode) — cheap. `threshold` 0..1: lower = more
    sensitive (catches softer transitions), higher = only hard cuts.
    """
    import re
    cmd = [FFMPEG, "-i", path, "-filter:v",
           f"select='gt(scene,{float(threshold)})',metadata=print:file=-",
           "-an", "-f", "null", "-"]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    blob = (proc.stdout or b"").decode("utf-8", "ignore") + (proc.stderr or b"").decode("utf-8", "ignore")
    cuts = sorted({round(float(m), 3) for m in re.findall(r"pts_time:([0-9.]+)", blob)})
    return [c for c in cuts if c > 0.05]   # ignore a "cut" at the very first frame


# Target dimensions per aspect ratio
DIMS = {
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "16:9": (1920, 1080),
}
