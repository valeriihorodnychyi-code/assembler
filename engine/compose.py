"""Render orchestration: subtitles over the hook, optional body concat, multi-format.

This is the de-duplicated successor to magic_en.py / magic_loc.py. The browser only
designs; the actual pixels are produced here by Pillow + ffmpeg, keeping the fast
hardware-encoded path on Mac (see ffmpeg_utils.video_encoder).
"""
import os
import shutil
import tempfile
from PIL import Image

from . import ffmpeg_utils as ff
from . import styles as st
from .subtitles import build_events, render_subtitle_png, generate_shadow_asset


def _region_for(t, regions):
    for r in regions:
        end = r.get("end")
        if t >= r["start"] and (end is None or t < end):
            return r
    return regions[-1]


def build_timeline(words, regions, lang="en", cuts=None):
    """Return events sorted by time, each tagged with the style that applies to it.

    `regions` is a list of {"start", "end"(nullable), "style"}. A single-style render
    is just one region covering the whole clip. `lang` selects the language-specific
    auto-caption rules (no_line_end stopwords).
    """
    # group words by region (by word start time) so chunks never cross a style boundary
    groups = {i: [] for i in range(len(regions))}
    for w in words:
        ws = float(w["start"])
        ri = next((i for i, r in enumerate(regions)
                   if ws >= r["start"] and (r.get("end") is None or ws < r["end"])), len(regions) - 1)
        groups[ri].append(w)

    tagged = []
    for i, r in enumerate(regions):
        style = st.normalize(r["style"])
        _wm = style.get("wrap_mode", "chars")
        _lim = style.get("words_per_line", 3) if _wm == "words" else style.get("max_chars_per_line", 15)
        evs = build_events(
            groups[i],
            _lim,
            style.get("text_case", "uppercase"),
            r.get("replacements", {}),
            wrap_mode=_wm,
            pause_gap=style.get("sentence_pause", 0.5),
            max_lines=style.get("max_lines", 1 if style.get("force_single_line") else 2),
            lang=lang,
            cuts=cuts,
            punctuation=style.get("punctuation", False),
        )
        for e in evs:
            tagged.append((e, style))
    tagged.sort(key=lambda es: es[0]["start"])
    return tagged


def render_format(video_path, tagged_events, fmt, output_path, work_dir,
                  body_path=None, scale_factors=None, bitrates=None, smart_trim=False,
                  hold_last=False):
    """Render a single aspect ratio to output_path."""
    scale_factors = scale_factors or st.DEFAULT_SCALE_FACTORS
    bitrates = bitrates or {"temp_hook": "35M", "final_export": "8M"}
    TARGET_W, TARGET_H = ff.DIMS[fmt]
    scale = 1.0   # NO per-format scaling — font px is absolute, identical caption in every format (9:16 look)

    input_w, input_h = ff.get_video_size(video_path)
    is_wide = input_w > input_h
    duration = ff.get_video_duration(video_path)

    # Smart trim (originals only): cut just after the last spoken word.
    render_duration = duration
    if smart_trim and tagged_events:
        last_end = max(e["end"] for e, _ in tagged_events)
        render_duration = min(duration, last_end + 0.1)
        tagged_events = [(e, s) for e, s in tagged_events if e["start"] < render_duration]
        if tagged_events:
            e, s = tagged_events[-1]
            e["end"] = render_duration

    # Optional legacy behaviour (off by default): keep the last caption until the clip ends.
    # Default is OFF so the last caption respects its capped end from build_events and never
    # freezes over trailing b-roll / the packshot.
    if hold_last and tagged_events:
        tagged_events[-1][0]["end"] = render_duration

    # Robust subtitle tiling. The subtitle track below is a SEQUENTIAL concat of PNGs
    # (each with a duration). If any event overlaps the next — which the +0.1s min-duration
    # guard can cause for densely-timed words (fast speech / Scribe) — the concat timeline
    # drifts and later captions get pushed past the clip end and DROPPED on render (while the
    # time-based preview still shows them). Enforce a strict gap-or-touch timeline so every
    # caption lands exactly on its timecode and nothing falls off.
    if tagged_events:
        tagged_events.sort(key=lambda es: float(es[0]["start"]))
        _clean = []
        for _i, (e, s) in enumerate(tagged_events):
            st_ = max(0.0, float(e["start"]))
            en_ = min(float(e["end"]), render_duration)
            if _i + 1 < len(tagged_events):
                en_ = min(en_, float(tagged_events[_i + 1][0]["start"]))
            if en_ - st_ < 0.02:      # swallowed by an overlap → skip (a 0/neg duration corrupts the concat)
                continue
            e["start"], e["end"] = st_, en_
            _clean.append((e, s))
        tagged_events = _clean

    temp_hook = os.path.join(work_dir, "temp_hook.mp4")
    temp_shadow = os.path.join(work_dir, "temp_shadow.png")
    sub_dir = tempfile.mkdtemp(dir=work_dir)

    cmd = ["ffmpeg", "-t", f"{render_duration}", "-i", video_path]

    # Background composition (letterbox + blurred bg + drop shadow for vertical-in-wide).
    if fmt in ("16:9", "1:1") and not is_wide:
        vid_w = int(TARGET_H * 9 / 16)
        x_off = int((TARGET_W - vid_w) / 2)
        generate_shadow_asset(vid_w, TARGET_H, TARGET_W, TARGET_H, temp_shadow)
        cmd += ["-i", temp_shadow]
        fc = (f"[0:v]fps=30,scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,"
              f"crop={TARGET_W}:{TARGET_H},boxblur=40,colorchannelmixer=rr=0.4:gg=0.4:bb=0.4[bgd]; "
              f"[bgd][1:v]overlay=0:0[bgs]; [0:v]fps=30,scale={vid_w}:{TARGET_H}[fg]; "
              f"[bgs][fg]overlay={x_off}:0[bg]; ")
        sub_idx = 2
    else:
        fc = (f"[0:v]fps=30,scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,"
              f"crop={TARGET_W}:{TARGET_H},setsar=1[bg]; ")
        sub_idx = 1

    # #12 Continuous scrim: when scrim is on, render it as ONE faded band spanning the whole
    # caption window instead of a hard per-caption band. It fades IN before the first caption
    # and OUT after the last, so it no longer "pops" with the first title and never blinks
    # between captions. The text PNGs then skip their own scrim (draw_scrim=False).
    base_label = "[bg]"
    scrim_cfg = (tagged_events[0][1].get("scrim") if tagged_events else None) or {}
    use_scrim = bool(scrim_cfg.get("enabled")) and bool(tagged_events)
    if use_scrim:
        win_start = min(float(e["start"]) for e, _ in tagged_events)
        win_end = min(render_duration, max(float(e["end"]) for e, _ in tagged_events))
        fade = max(0.0, float(scrim_cfg.get("fade", 0.25)))
        scrim_start = max(0.0, win_start - fade)          # fully faded in by the time the first caption appears
        tallest = max((e for e, _ in tagged_events), key=lambda e: len(e.get("lines", [])))
        scrim_png = os.path.join(sub_dir, "scrim.png")
        render_subtitle_png(dict(tallest), scrim_png, TARGET_W, TARGET_H,
                            st.resolve_font(tagged_events[0][1].get("font_name")),
                            tagged_events[0][1], scale, scrim_only=True)
        cmd += ["-loop", "1", "-t", f"{render_duration}", "-i", scrim_png]
        _fin = max(0.001, fade)
        fc += (f"[{sub_idx}:v]format=yuva420p,"
               f"fade=t=in:st={scrim_start:.3f}:d={_fin:.3f}:alpha=1,"
               f"fade=t=out:st={win_end:.3f}:d={_fin:.3f}:alpha=1[scr]; "
               f"[bg][scr]overlay=0:0[bgsc]; ")
        base_label = "[bgsc]"
        sub_idx += 1

    # Subtitle stream via concat demuxer (one overlay for the whole track).
    if not tagged_events:
        last_v = base_label
    else:
        blank = os.path.join(sub_dir, "blank.png")
        Image.new("RGBA", (TARGET_W, TARGET_H), (0, 0, 0, 0)).save(blank)
        subs_txt = os.path.join(sub_dir, "subs.txt")
        with open(subs_txt, "w") as f:
            current = 0.0
            for i, (event, style) in enumerate(tagged_events):
                if event["start"] > current:
                    f.write("file 'blank.png'\n")
                    f.write(f"duration {event['start'] - current:.3f}\n")
                png = f"sub_{i}.png"
                font = st.resolve_font(style.get("font_name"))
                render_subtitle_png(event, os.path.join(sub_dir, png),
                                    TARGET_W, TARGET_H, font, style, scale,
                                    draw_scrim=not use_scrim)
                f.write(f"file '{png}'\n")
                f.write(f"duration {event['end'] - event['start']:.3f}\n")
                current = event["end"]
            if render_duration > current:
                f.write("file 'blank.png'\n")
                f.write(f"duration {render_duration - current:.3f}\n")
            f.write("file 'blank.png'\n")  # ffmpeg requires the last entry duplicated
        cmd += ["-f", "concat", "-safe", "0", "-i", subs_txt]
        fc += f"{base_label}[{sub_idx}:v]overlay=0:0:format=rgb[final_v]"
        last_v = "[final_v]"

    has_body = bool(body_path and os.path.exists(body_path))
    hook_bitrate = bitrates["temp_hook"] if has_body else bitrates["final_export"]

    cmd += ["-filter_complex", fc.strip().strip(";"), "-map", last_v, "-map", "0:a?"]
    cmd += ff.encoder_quality_args(hook_bitrate)
    cmd += [temp_hook, "-y"]
    ff.run(cmd)
    if os.path.exists(temp_shadow):
        os.remove(temp_shadow)

    if has_body:
        _concat_body(temp_hook, body_path, fmt, output_path, work_dir, bitrates)
    else:
        shutil.copy(temp_hook, output_path)

    shutil.rmtree(sub_dir, ignore_errors=True)
    if os.path.exists(temp_hook):
        os.remove(temp_hook)
    return output_path


def _concat_body(hook, body, fmt, output_path, work_dir, bitrates):
    """Append a body clip, normalizing fps/SAR. Source loudness is preserved (no loudnorm)."""
    TARGET_W, TARGET_H = ff.DIMS[fmt]
    body_w, body_h = ff.get_video_size(body)
    inputs = ["-i", hook, "-i", body]
    temp_shadow = os.path.join(work_dir, "temp_shadow_body.png")

    if fmt in ("16:9", "1:1") and not (body_w >= body_h):
        vid_w = int(TARGET_H * 9 / 16)
        x_off = int((TARGET_W - vid_w) / 2)
        generate_shadow_asset(vid_w, TARGET_H, TARGET_W, TARGET_H, temp_shadow)
        inputs += ["-i", temp_shadow]
        fc = (f"[0:v]setsar=1,fps=30[v0]; "
              f"[1:v]scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,"
              f"crop={TARGET_W}:{TARGET_H},boxblur=40,colorchannelmixer=rr=0.4:gg=0.4:bb=0.4[bbg]; "
              f"[2:v]scale={TARGET_W}:{TARGET_H}[bsh]; [bbg][bsh]overlay=x=0:y=0[bbs]; "
              f"[1:v]scale={vid_w}:{TARGET_H}[bfg]; [bbs][bfg]overlay={x_off}:0,setsar=1,fps=30[v1]; "
              f"[v0][0:a][v1][1:a]concat=n=2:v=1:a=1[outv][outa]")  # keep source loudness — no loudnorm (it boosted audio the user had already cleaned/quieted)
    else:
        fc = (f"[0:v]setsar=1,fps=30[v0]; "
              f"[1:v]scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,"
              f"crop={TARGET_W}:{TARGET_H},setsar=1,fps=30[v1]; "
              f"[v0][0:a][v1][1:a]concat=n=2:v=1:a=1[outv][outa]")  # keep source loudness — no loudnorm (it boosted audio the user had already cleaned/quieted)

    cmd = ["ffmpeg", *inputs, "-filter_complex", fc, "-map", "[outv]", "-map", "[outa]"]
    cmd += ff.encoder_quality_args(bitrates["final_export"])
    cmd += ["-c:a", "aac", "-b:a", "192k", output_path, "-y"]
    ff.run(cmd)
    if os.path.exists(temp_shadow):
        os.remove(temp_shadow)


def assemble_segments(clips, fmt, output_path, bitrates=None):
    """Concat N clips (hook+hook+body+packshot…) into one creative, single pass.

    Each clip is scaled/cropped to the target format, normalized to 30fps/SAR 1, then
    concatenated; clips without an audio track get silent audio so concat stays aligned.
    Source loudness is preserved (no loudnorm) so pre-cleaned audio isn't boosted back up.
    """
    if not clips:
        raise ValueError("no clips to assemble")
    bitrates = bitrates or {"final_export": "8M"}
    TW, TH = ff.DIMS[fmt]

    inputs, silent_inputs, vfilters, concat_inputs = [], [], [], []
    for c in clips:
        inputs += ["-i", c]
    sidx = len(clips)  # silent anullsrc inputs are appended after the clip inputs
    for i, c in enumerate(clips):
        vfilters.append(
            f"[{i}:v]scale={TW}:{TH}:force_original_aspect_ratio=increase,"
            f"crop={TW}:{TH},setsar=1,fps=30[v{i}]"
        )
        if ff.has_audio_stream(c):
            concat_inputs.append(f"[v{i}][{i}:a]")
        else:
            dur = ff.get_video_duration(c)
            silent_inputs += ["-f", "lavfi", "-t", f"{dur}", "-i", "anullsrc=r=44100:cl=stereo"]
            concat_inputs.append(f"[v{i}][{sidx}:a]")
            sidx += 1

    n = len(clips)
    fc = ("; ".join(vfilters) + "; " + "".join(concat_inputs) +
          f"concat=n={n}:v=1:a=1[outv][outa]")   # keep source loudness — no loudnorm (was boosting pre-cleaned audio)
    cmd = ["ffmpeg", *inputs, *silent_inputs, "-filter_complex", fc,
           "-map", "[outv]", "-map", "[outa]"]
    cmd += ff.encoder_quality_args(bitrates["final_export"])
    cmd += ["-c:a", "aac", "-b:a", "192k", output_path, "-y"]
    ff.run(cmd)
    return output_path


def trim_clip(clip_path, start, end, output_path):
    """Cut [start, end] out of a clip (frame-accurate, re-encoded)."""
    dur = max(0.1, float(end) - float(start))
    cmd = ["ffmpeg", "-ss", f"{float(start)}", "-i", clip_path, "-t", f"{dur}"]
    cmd += ff.encoder_quality_args("12M")
    cmd += ["-c:a", "aac", "-b:a", "192k", output_path, "-y"]
    ff.run(cmd)
    return output_path


def shift_words(words, start, end):
    """Re-base word timecodes to a trimmed clip: drop words outside [start,end],
    shift the rest so the clip starts at 0."""
    out = []
    for w in words or []:
        ws, we = float(w["start"]), float(w["end"])
        if we <= start or ws >= end:
            continue
        out.append({"word": w.get("word", w.get("text", "")),
                    "start": max(0.0, ws - start), "end": max(0.05, we - start)})
    return out


def clip_caption_window(tagged, cap_in=None, cap_out=None):
    """Keep captions only inside [cap_in, cap_out] (seconds). Events fully outside are
    dropped; partial ones are clamped. Used to cut titles between scenes / long pauses."""
    if cap_in is None and cap_out is None:
        return tagged
    lo = float(cap_in) if cap_in is not None else -1e9
    hi = float(cap_out) if cap_out is not None else 1e9
    out = []
    for e, s in tagged:
        if e["end"] <= lo or e["start"] >= hi:
            continue
        e = dict(e)
        e["start"] = max(e["start"], lo)
        e["end"] = min(e["end"], hi)
        if e["end"] > e["start"]:
            out.append((e, s))
    return out


def caption_clip(clip_path, words, style, fmt, output_path, work_dir, cap_in=None, cap_out=None, cuts=None, regions=None):
    """Bake captions onto a single clip (no body). Used by the non-destructive
    assemble so a hook's captions are burned only at final assembly time.

    `regions` (optional) lets individual phrases carry their own style (#10 per-phrase style):
    a list of {"start","end"(nullable),"style"} that partitions the timeline. When omitted,
    the whole clip uses the single `style`."""
    if regions:
        regions = [{"start": float(r.get("start", 0)), "end": r.get("end"),
                    "style": st.normalize(r.get("style") or style)} for r in regions]
    else:
        regions = [{"start": 0, "end": None, "style": st.normalize(style)}]
    tagged = build_timeline(words or [], regions, cuts=cuts)
    # NOTE: do NOT hold the last caption to the clip end — build_events already gives it a short
    # readable tail (and trims at a scene cut). Forcing end=duration made the last caption hang
    # over trailing footage / the packshot (the #8 bug); the same fix now applies to this path.
    tagged = clip_caption_window(tagged, cap_in, cap_out)
    render_format(clip_path, [(dict(e), s) for e, s in tagged], fmt, output_path,
                  work_dir, body_path=None)
    return output_path


def mix_tracks(video_path, tracks, output_path):
    """Mix multiple music tracks under a creative. Each track:
        {path, at(sec into creative), in(sec into track), out(sec), volume(0..1), duck(bool)}
    A track is trimmed [in,out], delayed to start at `at`, set to its volume; all tracks are
    summed; ducking (sidechain under the voice) applies to the whole music sum if any track asks."""
    tracks = [t for t in (tracks or []) if t.get("path") and os.path.exists(t["path"])]
    if not tracks:
        shutil.copyfile(video_path, output_path)
        return output_path
    inputs = ["-i", video_path]
    parts, labels = [], []
    for i, t in enumerate(tracks):
        idx = i + 1
        inputs += ["-i", t["path"]]
        a_in = max(0.0, float(t.get("in", 0)))
        a_out = float(t.get("out", 1e6))
        at = int(round(max(0.0, float(t.get("at", 0))) * 1000))
        vol = max(0.0, float(t.get("volume", 0.25)))
        trim = f"atrim={a_in}:{a_out}," if a_out < 1e5 else f"atrim=start={a_in},"
        parts.append(f"[{idx}:a]aformat=sample_rates=44100:channel_layouts=stereo,{trim}"
                     f"asetpts=PTS-STARTPTS,adelay={at}|{at},volume={vol:.3f}[m{i}]")
        labels.append(f"[m{i}]")
    if len(labels) > 1:
        parts.append("".join(labels) + f"amix=inputs={len(labels)}:duration=longest:normalize=0[music]")
    else:
        parts.append(f"{labels[0]}anull[music]")
    if any(t.get("duck", True) for t in tracks):
        parts.append("[music][0:a]sidechaincompress=threshold=0.05:ratio=8:attack=20:release=400[mz]")
        parts.append("[0:a][mz]amix=inputs=2:duration=first:normalize=0[aout]")
    else:
        parts.append("[0:a][music]amix=inputs=2:duration=first:normalize=0[aout]")
    cmd = ["ffmpeg", *inputs, "-filter_complex", "; ".join(parts),
           "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
           "-shortest", output_path, "-y"]
    ff.run(cmd)
    return output_path


def mix_music(video_path, music_path, output_path, volume=0.25, start=0.0, duck=True):
    """Mix a music bed under a creative's audio.

    volume: 0..1 music level. start: seconds into the track (catch a good beat).
    duck:   lower music automatically while the voice plays (sidechain compress).
    Music loops to cover the whole video; output is trimmed to the video length.
    """
    vol = max(0.0, float(volume))
    inputs = ["-i", video_path, "-stream_loop", "-1", "-ss", f"{float(start)}", "-i", music_path]
    if duck:
        fc = (f"[1:a]volume={vol}[m]; "
              f"[m][0:a]sidechaincompress=threshold=0.05:ratio=8:attack=20:release=400[mz]; "
              f"[0:a][mz]amix=inputs=2:duration=first:normalize=0[aout]")
    else:
        fc = (f"[1:a]volume={vol}[m]; "
              f"[0:a][m]amix=inputs=2:duration=first:normalize=0[aout]")
    cmd = ["ffmpeg", *inputs, "-filter_complex", fc, "-map", "0:v", "-map", "[aout]",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", output_path, "-y"]
    ff.run(cmd)
    return output_path


def assemble_recipe(segments, fmt, output_path, work_dir=None, bitrates=None, music=None):
    """Non-destructive assemble. Each segment is a dict:
        {"clip": path}                              -> used as-is (already final)
        {"clip": path, "words": [...], "style": {}} -> captions baked now, then used
    All segments are concatenated in one creative. Captions stay editable as data
    right up to this call — no need to pre-render each hook.
    """
    own = work_dir is None
    work_dir = work_dir or tempfile.mkdtemp(prefix="cs_nd_")
    try:
        clips = []          # (path, fade_in) — fade_in!=None => overlay-fade onto the prior tail
        for i, seg in enumerate(segments):
            path = seg["clip"]
            words = seg.get("words")
            trim = seg.get("trim")
            if trim:  # cut the clip and re-base caption timecodes onto the cut
                tp = os.path.join(work_dir, f"trim_{i}.mp4")
                trim_clip(path, trim[0], trim[1], tp)
                path = tp
                if words:
                    words = shift_words(words, float(trim[0]), float(trim[1]))
            if words and seg.get("style"):
                seg_cuts = seg.get("cuts")
                if seg_cuts and trim:   # re-base scene cuts to the trimmed clip, like words
                    lo, hi = float(trim[0]), float(trim[1])
                    seg_cuts = [c - lo for c in seg_cuts if lo < c < hi]
                cap = os.path.join(work_dir, f"cap_{i}.mp4")
                caption_clip(path, words, seg["style"], fmt, cap, work_dir,
                             seg.get("cap_in"), seg.get("cap_out"), cuts=seg_cuts,
                             regions=seg.get("regions"))
                path = cap
            if seg.get("overlays"):  # PNG / alpha-.mov stickers on top (reaction style)
                ovp = os.path.join(work_dir, f"ov_{i}.mp4")
                apply_overlays(path, seg["overlays"], fmt, ovp)
                path = ovp
            clips.append((path, seg.get("fade_in")))

        # Split into a hard-concat head + any trailing fade-in overlays (packshots).
        first_fade = next((k for k, (_, fd) in enumerate(clips) if fd), None)
        if first_fade is None or first_fade == 0:
            assembled = os.path.join(work_dir, "assembled.mp4")
            assemble_segments([p for p, _ in clips], fmt, assembled, bitrates=bitrates)
        else:
            assembled = os.path.join(work_dir, "head.mp4")
            assemble_segments([p for p, _ in clips[:first_fade]], fmt, assembled, bitrates=bitrates)
            for j, (p, fd) in enumerate(clips[first_fade:]):
                if fd:
                    nxt = os.path.join(work_dir, f"fade_{j}.mp4")
                    append_packshot_fade(assembled, p, nxt, fmt, fade=float(fd), bitrates=bitrates)
                else:  # a non-fade clip after a packshot — just concat it on
                    nxt = os.path.join(work_dir, f"cat_{j}.mp4")
                    assemble_segments([assembled, p], fmt, nxt, bitrates=bitrates)
                assembled = nxt

        if music and music.get("tracks"):
            # segment-anchored multi-track audio: each track starts at the chosen segment's
            # start time IN THIS creative (so timing is correct even if hooks differ in length).
            starts, acc = [], 0.0
            for p, _ in clips:
                starts.append(acc)
                acc += ff.get_video_duration(p)
            tracks = []
            for t in music["tracks"]:
                if not (t.get("path") and os.path.exists(t["path"])):
                    continue
                ss = max(0, min(int(t.get("startSeg", 0)), len(starts) - 1))
                tracks.append({"path": t["path"], "at": starts[ss],
                               "in": float(t.get("in", 0)), "out": float(t.get("out", 1e6)),
                               "volume": float(t.get("volume", 0.25)), "duck": bool(t.get("duck", True))})
            mix_tracks(assembled, tracks, output_path)
        elif music and music.get("path"):
            mix_music(assembled, music["path"], output_path,
                      volume=music.get("volume", 0.25), start=music.get("start", 0.0),
                      duck=music.get("duck", True))
        else:
            shutil.copyfile(assembled, output_path)
    finally:
        if own:
            shutil.rmtree(work_dir, ignore_errors=True)
    return output_path


def append_packshot_fade(base_path, pack_path, output_path, fmt, fade=0.5, bitrates=None):
    """Overlay a packshot onto the tail of `base`, ramping the packshot's opacity
    0 -> 100% over `fade` seconds (a slight overlap onto the body), then playing the
    packshot out at full opacity. The body stays visible underneath during the fade.

    Total length = base + packshot - fade. Audio: body audio plays through, packshot
    audio fades in delayed to match the visual; both are mixed over the overlap.
    """
    bitrates = bitrates or {"final_export": "8M"}
    TW, TH = ff.DIMS[fmt]
    Tb = ff.get_video_duration(base_path)
    Tp = ff.get_video_duration(pack_path)
    f = max(0.05, min(float(fade), Tp - 0.05, Tb - 0.05))  # never exceed either clip
    t0 = max(0.0, Tb - f)            # packshot enters here (overlaps body tail)
    total = t0 + Tp
    ext = max(0.0, total - Tb)       # freeze body's last frame to cover the overlay tail
    norm = (f"scale={TW}:{TH}:force_original_aspect_ratio=increase,"
            f"crop={TW}:{TH},setsar=1,fps=30")
    vf = (f"[0:v]{norm},tpad=stop_mode=clone:stop_duration={ext:.3f}[base];"
          f"[1:v]{norm},format=yuva420p,fade=t=in:st=0:d={f:.3f}:alpha=1,"
          f"setpts=PTS-STARTPTS+{t0:.3f}/TB[pk];"
          f"[base][pk]overlay=0:0:eof_action=pass[vov];"
          f"[vov]trim=0:{total:.3f},setpts=PTS-STARTPTS[outv]")

    t0ms = int(round(t0 * 1000))
    inputs = ["-i", base_path, "-i", pack_path]
    if ff.has_audio_stream(pack_path):
        pa = f"[1:a]aformat=sample_rates=44100:channel_layouts=stereo,adelay={t0ms}|{t0ms}[pa]"
    else:  # silent packshot — synthesize silence so the mix stays aligned
        inputs += ["-f", "lavfi", "-t", f"{Tp}", "-i", "anullsrc=r=44100:cl=stereo"]
        pa = f"[2:a]adelay={t0ms}|{t0ms}[pa]"
    af = (f"[0:a]aformat=sample_rates=44100:channel_layouts=stereo,apad[ba];{pa};"
          f"[ba][pa]amix=inputs=2:duration=longest:normalize=0,atrim=0:{total:.3f}[outa]")

    cmd = ["ffmpeg", *inputs, "-filter_complex", vf + ";" + af,
           "-map", "[outv]", "-map", "[outa]"]
    cmd += ff.encoder_quality_args(bitrates["final_export"])
    cmd += ["-c:a", "aac", "-b:a", "192k", output_path, "-y"]
    ff.run(cmd)
    return output_path


def _stream_dims(path):
    """(width, height) of a media file's first video stream."""
    import subprocess
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
            stderr=subprocess.DEVNULL).decode().strip().split(",")
        return int(out[0]), int(out[1])
    except Exception:
        return 1, 1


def apply_overlays(video_path, overlays, fmt, output_path):
    """Composite PNG / alpha-.mov overlays on top of a clip (whole-clip duration).

    The base is first normalized to the format's render size, so overlay coords
    (x, y, w in render-space px — e.g. 1080x1920 for 9:16) land exactly where the
    user dragged them in the preview. Images loop for the whole clip; videos loop too.
    """
    import math
    overlays = [o for o in (overlays or []) if o.get("path") and os.path.exists(o["path"])]
    if not overlays:
        return video_path
    TW, TH = ff.DIMS[fmt]
    dur = ff.get_video_duration(video_path)  # hard cap so looped/held overlays always terminate
    norm = f"scale={TW}:{TH}:force_original_aspect_ratio=increase,crop={TW}:{TH},setsar=1,fps=30"
    inputs = ["-i", video_path]
    parts = [f"[0:v]{norm}[base]"]
    prev = "[base]"
    img_ext = (".png", ".webp", ".apng", ".gif", ".jpg", ".jpeg")
    for i, ov in enumerate(overlays):
        idx = i + 1
        if os.path.splitext(ov["path"])[1].lower() not in img_ext:
            inputs += ["-stream_loop", "-1", "-i", ov["path"]]  # loop a video overlay for the whole clip
        else:
            inputs += ["-i", ov["path"]]  # a still image: overlay holds the single frame (eof_action=repeat)
        w = max(2, int(round(float(ov.get("w", 240)))))
        x = float(ov.get("x", 0))
        y = float(ov.get("y", 0))
        ang = float(ov.get("angle", 0) or 0)
        if abs(ang) < 0.5:
            parts.append(f"[{idx}:v]scale={w}:-1[ov{i}]")
            X, Y = int(round(x)), int(round(y))
        else:  # rotate around the sticker's own centre, then re-anchor so the centre is unchanged
            iw, ih = _stream_dims(ov["path"])
            h = w * ih / max(1, iw)
            rad = math.radians(ang)
            ow = abs(w * math.cos(rad)) + abs(h * math.sin(rad))
            oh = abs(w * math.sin(rad)) + abs(h * math.cos(rad))
            parts.append(f"[{idx}:v]scale={w}:-1,rotate={rad:.5f}:c=none:"
                         f"ow={int(math.ceil(ow))}:oh={int(math.ceil(oh))}[ov{i}]")
            X = int(round(x + w / 2 - ow / 2))
            Y = int(round(y + h / 2 - oh / 2))
        out = f"[c{i}]"
        oi, oo = ov.get("in"), ov.get("out")          # show the sticker only within [in, out] seconds
        enable = ""
        if oi is not None or oo is not None:
            lo = float(oi) if oi is not None else 0.0
            hi = float(oo) if oo is not None else 1e9
            enable = f":enable='between(t,{lo},{hi})'"
        parts.append(f"{prev}[ov{i}]overlay={X}:{Y}:format=auto:eof_action=repeat{enable}{out}")
        prev = out
    cmd = ["ffmpeg", *inputs, "-filter_complex", "; ".join(parts),
           "-map", prev, "-map", "0:a?", "-t", f"{dur:.3f}"]
    cmd += ff.encoder_quality_args("12M")
    cmd += ["-c:a", "aac", "-b:a", "192k", output_path, "-y"]
    ff.run(cmd)
    return output_path


def assemble(hook_path, body_path, fmt, output_path, work_dir=None, bitrates=None):
    """Concat a captioned hook with a (pre-localized) body clip -> final creative.

    Reuses the same fps/SAR normalization + loudness matching as the localization
    concat, so a fresh hook snaps onto a reused body in one fast ffmpeg pass.
    """
    own = work_dir is None
    work_dir = work_dir or tempfile.mkdtemp(prefix="cs_asm_")
    bitrates = bitrates or {"temp_hook": "35M", "final_export": "8M"}
    try:
        _concat_body(hook_path, body_path, fmt, output_path, work_dir, bitrates)
    finally:
        if own:
            shutil.rmtree(work_dir, ignore_errors=True)
    return output_path


def render(video_path, words, regions, formats, output_dir, work_dir=None,
           bodies=None, default_body=None, scale_factors=None, bitrates=None,
           smart_trim=False, out_prefix="caption", cuts=None):
    """Top-level render. Returns list of output file paths (one per format).

    `regions`: list of {"start","end","style"} OR pass a single style via
               regions=[{"start":0,"end":None,"style": {...}}].
    `bodies`:  optional {fmt: body_path}; `default_body`: fallback body for all formats.
    """
    os.makedirs(output_dir, exist_ok=True)
    own_work = work_dir is None
    work_dir = work_dir or tempfile.mkdtemp(prefix="cs_render_")
    bodies = bodies or {}
    outputs = []
    try:
        tagged = build_timeline(words, regions, cuts=cuts)
        for fmt in formats:
            body = bodies.get(fmt, default_body)
            out = os.path.join(output_dir, f"{fmt.replace(':', 'x')}_{out_prefix}.mp4")
            # build_timeline mutates events in smart_trim; copy per format
            per_fmt = [(dict(e), s) for e, s in tagged]
            render_format(video_path, per_fmt, fmt, out, work_dir,
                          body_path=body, scale_factors=scale_factors,
                          bitrates=bitrates, smart_trim=smart_trim)
            outputs.append(out)
    finally:
        if own_work:
            shutil.rmtree(work_dir, ignore_errors=True)
    return outputs
