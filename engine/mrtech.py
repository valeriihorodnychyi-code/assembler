"""Assembler caption style → MRTech caption JSON (their flat MoviePy-like format), plus the
fields their format doesn't have yet (case, words per line, lines, karaoke…) under the same
flat keys, so they can adopt them one by one. Unknown keys are safe to ignore on their side.

Their sample:
  {"font": "Figtree-Bold", "align": "center", "color": "white", "bg_color": "black",
   "fontsize": 40, "position": "bottom", "interline": 0, "margin_top": 285,
   "stroke_color": null, "stroke_width": 0, "corner_radius": 8, "margin_bottom": 426,
   "max_width_ratio": 0.82}
"""
import os

from engine import styles as st, subtitles as S

W, H = 1080, 1920          # Assembler px are absolute on a 1080×1920 frame (9:16)


def _hex(c):
    c = list(c or [0, 0, 0, 255])
    return "#{:02x}{:02x}{:02x}".format(*[max(0, min(255, int(x))) for x in c[:3]])


def _alpha(c):
    c = list(c or [0, 0, 0, 255])
    return round((c[3] if len(c) > 3 else 255) / 255.0, 2)


def to_mrtech(style, name=""):
    s = st.normalize(style or {})
    font_file = s.get("font_name") or "Poppins-Bold.ttf"
    fs = int(s.get("font_size", 80))
    lines = 1 if s.get("force_single_line") else int(s.get("max_lines", 2))
    spacing = int(s.get("line_spacing", 10))
    try:
        f = S.load_font(st.resolve_font(font_file), fs)
        asc, desc = f.getmetrics()
        lh = asc + desc
    except Exception:  # noqa: BLE001
        lh = int(fs * 1.2)
    block = lines * lh + (lines - 1) * spacing
    # Assembler: margin_bottom = from the frame bottom to the CENTRE of the caption block.
    # MRTech (bottom position): from the frame bottom to the block's BOTTOM edge.
    mb_edge = int(round(int(s.get("margin_bottom", 300)) - block / 2))
    pl = s.get("plate") or {}
    stroke_on = bool(s.get("stroke_on", True))
    sw = int((s.get("stroke") or {}).get("width", 0)) if stroke_on else 0
    so = int((s.get("stroke_outer") or {}).get("width", 0)) if stroke_on else 0
    words_mode = s.get("wrap_mode") == "words"
    ka, kp = s.get("karaoke") or {}, s.get("karaoke_plate") or {}
    sh = s.get("shadow") or {}
    out = {
        # ---- their format ----
        "font": os.path.splitext(os.path.basename(font_file))[0],
        "align": "center",
        "color": _hex(s.get("text_color")),
        "bg_color": _hex(pl.get("color")) if pl.get("enabled") else None,
        "fontsize": fs,
        "position": "bottom",
        "interline": spacing,
        "margin_top": 285,
        "stroke_color": _hex((s.get("stroke") or {}).get("color")) if sw > 0 else None,
        "stroke_width": sw,
        "corner_radius": int(pl.get("border_radius", 0)) if pl.get("enabled") else 0,
        "margin_bottom": max(0, mb_edge),
        "max_width_ratio": 0.82,
        # ---- extras: what our captions also use (add one by one) ----
        "name": name,
        "frame": [W, H],                                      # px above are for this frame size
        "text_case": s.get("text_case") or "none",            # uppercase · lowercase · none
        "max_lines": lines,                                   # lines on screen at once
        "max_words_per_line": int(s.get("words_per_line", 3)) if words_mode else None,
        "max_chars_per_line": None if words_mode else int(s.get("max_chars_per_line", 15)),
        "punctuation": {True: "all", "off": "off"}.get(s.get("punctuation"), "question_exclamation_only"),
        "bg_opacity": _alpha(pl.get("color")) if pl.get("enabled") else None,
        "bg_mode": (("full_width" if pl.get("full_width") else "per_line" if pl.get("per_line") else "block")
                    if pl.get("enabled") else None),
        "bg_padding": [int(pl.get("pad_x", 0)), int(pl.get("pad_y", 0))] if pl.get("enabled") else None,
        "stroke_outer": {"color": _hex((s.get("stroke_outer") or {}).get("color")), "width": so} if so > 0 else None,
        "shadow": ({"color": _hex(sh.get("color")), "offset": [int(sh.get("offset_x", 0)), int(sh.get("offset_y", 0))],
                    "blur": int(sh.get("glow_blur", 0))} if sh.get("enabled") else None),
        # karaoke = the word being SPOKEN right now is highlighted (needs word timestamps)
        "karaoke": ({"enabled": True, "active_color": _hex(ka.get("active_color")),
                     "active_bg_color": _hex(kp.get("color")) if kp.get("enabled") else None,
                     "active_bg_padding": [int(kp.get("pad_x", 0)), int(kp.get("pad_y", 0))] if kp.get("enabled") else None,
                     "active_bg_radius": int(kp.get("border_radius", 0)) if kp.get("enabled") else None}
                    if ka.get("enabled") else {"enabled": False}),
    }
    return out
