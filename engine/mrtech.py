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


def to_mrtech(style, name="", clean=False):
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
        "font": os.path.splitext(os.path.basename(font_file))[0],
        "align": "center",
        "color": _hex(s.get("text_color")),
        "bg_color": _hex(pl.get("color")) if pl.get("enabled") else None,
        "fontsize": fs,
        "position": "bottom",
        "interline": spacing,
        "margin_top": 285,
        # one outline in their format: the OUTER one if there are two (it's what you see from afar)
        "stroke_color": (_hex((s.get("stroke_outer") or {}).get("color")) if so > 0
                         else _hex((s.get("stroke") or {}).get("color")) if sw > 0 else None),
        "stroke_width": so if so > 0 else sw,
        "corner_radius": int(pl.get("border_radius", 0)) if pl.get("enabled") else 0,
        "margin_bottom": max(0, mb_edge),
        "max_width_ratio": 0.82,
    }
    if clean:
        return out
    sh2 = s.get("shadow2") or {}
    shadows = []
    if sh.get("enabled"):
        if sh2.get("enabled"):     # drawn first (underneath)
            shadows.append({"color": _hex(sh2.get("color")), "opacity": _alpha(sh2.get("color")),
                            "offset": [int(sh2.get("offset_x", 0)), int(sh2.get("offset_y", 0))], "blur": int(sh2.get("glow_blur", 0))})
        shadows.append({"color": _hex(sh.get("color")), "opacity": _alpha(sh.get("color")),
                        "offset": [int(sh.get("offset_x", 0)), int(sh.get("offset_y", 0))], "blur": int(sh.get("glow_blur", 0))})
    sc = s.get("scrim") or {}
    ps = s.get("plate_shadow") or {}
    out["x_assembler"] = {          # everything our captions use that the 13 keys can't say — see CAPTION_SPEC.md
        "spec": "assembler-captions/1",
        "name": name,
        "frame": [W, H],
        "margin_bottom_center": int(s.get("margin_bottom", 300)),
        "text_case": s.get("text_case") or "none",
        "punctuation": {True: "all", "off": "off"}.get(s.get("punctuation"), "question_exclamation_only"),
        "layout": {"max_lines": lines,
                   "max_chars_per_line": None if words_mode else int(s.get("max_chars_per_line", 15)),
                   "max_words_per_line": int(s.get("words_per_line", 3)) if words_mode else None,
                   "new_caption_after_pause_s": float(s.get("sentence_pause", 0.5)),
                   "min_last_line_words": 2, "shrink_to_fit_width_ratio": 0.92},
        "plate": ({"mode": "full_width" if pl.get("full_width") else "per_line" if pl.get("per_line") else "block",
                   "color": _hex(pl.get("color")), "opacity": _alpha(pl.get("color")),
                   "padding": [int(pl.get("pad_x", 0)), int(pl.get("pad_y", 0))],
                   "radius": int(pl.get("border_radius", 0)), "offset_y": int(s.get("box_offset_y", 0)),
                   "shadow": ({"color": _hex(ps.get("color")), "opacity": _alpha(ps.get("color")),
                               "offset": [int(ps.get("offset_x", 0)), int(ps.get("offset_y", 0))], "blur": int(ps.get("glow_blur", 0))}
                              if ps.get("enabled") else None)}
                  if pl.get("enabled") else None),
        "stroke": {"inner": {"color": _hex((s.get("stroke") or {}).get("color")), "width": sw} if sw > 0 else None,
                   "outer": {"color": _hex((s.get("stroke_outer") or {}).get("color")), "width": so} if so > 0 else None},
        "shadows": shadows,
        "scrim": ({"color": _hex(sc.get("color")), "opacity": _alpha(sc.get("color")), "padding": int(sc.get("pad", 40)),
                   "feather": int(sc.get("feather", 70)), "fade_s": float(sc.get("fade", 0.25))} if sc.get("enabled") else None),
        "karaoke": ({"active_color": _hex(ka.get("active_color")),
                     "active_plate": ({"color": _hex(kp.get("color")), "opacity": _alpha(kp.get("color")),
                                       "padding": [int(kp.get("pad_x", 0)), int(kp.get("pad_y", 0))],
                                       "radius": int(kp.get("border_radius", 0))} if kp.get("enabled") else None)}
                    if ka.get("enabled") else None),
    }
    return out
