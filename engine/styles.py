"""Style schema, loading and defaults.

A *style* is the JSON object the design editor produces (font, colors, stroke,
shadow, plate, karaoke...). It is the single source of truth shared by the
browser preview and the Pillow/ffmpeg renderer, so what you see is what you export.
"""
import os
import json

STYLES_DIR = os.environ.get("CS_STYLES_DIR", "styles")
FONTS_DIR = os.environ.get("CS_FONTS_DIR", "fonts")

DEFAULT_STYLE = {
    "font_name": "Poppins-Bold.ttf",
    "font_size": 80,
    "text_color": [255, 255, 255, 255],
    "text_case": "uppercase",          # uppercase | lowercase | ""
    "wrap_mode": "chars",              # "chars" (guarantees width fit) | "words"
    "max_chars_per_line": 15,          # used when wrap_mode == "chars"
    "words_per_line": 3,               # used when wrap_mode == "words"
    "max_lines": 2,                    # 1 or 2 lines on screen
    "line_spacing": 10,
    "margin_bottom": 320,
    "box_offset_y": 0,                 # nudge plate/highlight box up(+)/down(-), text stays
    "force_single_line": False,
    "punctuation": False,              # False = clean look (abbreviation dots kept); True = keep punctuation as transcribed
    "stroke_on": True,                 # master on/off for the whole outline (inner + outer = double stroke)
    "stroke": {"width": 0, "color": [0, 0, 0, 255]},
    "stroke_outer": {"width": 0, "color": [0, 0, 0, 255]},
    "shadow": {"enabled": False, "offset_x": 0, "offset_y": 8, "glow_blur": 0, "color": [0, 0, 0, 255]},   # shadow on the WORDS (works even under a plate)
    "plate_shadow": {"enabled": False, "offset_x": 0, "offset_y": 8, "glow_blur": 0, "color": [0, 0, 0, 255]},  # drop shadow of the plate / word-plate
    "plate": {"enabled": False, "per_line": False, "color": [0, 0, 0, 160], "pad_x": 30, "pad_y": 15, "border_radius": 15},
    "scrim": {"enabled": False, "color": [0, 0, 0, 150], "pad": 40, "feather": 70},  # soft dark band behind text for light footage
    "gradient": {"colors": [[255, 90, 44, 255], [255, 212, 0, 255]], "direction": "vertical",
                 "on_text": False, "on_active": False, "on_plate": False},
    "karaoke": {"enabled": True, "active_color": [255, 212, 0, 255]},
    "karaoke_plate": {"enabled": False, "color": [255, 92, 57, 255], "pad_x": 14, "pad_y": 6, "border_radius": 10},
}

# Per-format scale factors (matches the original engine behaviour).
DEFAULT_SCALE_FACTORS = {"9:16": 1.0, "1:1": 0.65, "16:9": 0.50}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def normalize(style: dict) -> dict:
    """Fill any missing keys with defaults so the renderer never KeyErrors."""
    return _deep_merge(DEFAULT_STYLE, style or {})


def list_styles(styles_dir: str = None) -> list:
    d = styles_dir or STYLES_DIR
    if not os.path.isdir(d):
        return []
    return sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json"))


def load_style(name: str, styles_dir: str = None) -> dict:
    d = styles_dir or STYLES_DIR
    path = os.path.join(d, name if name.endswith(".json") else name + ".json")
    with open(path, "r", encoding="utf-8") as f:
        return normalize(json.load(f))


def save_style(name: str, style: dict, styles_dir: str = None) -> str:
    d = styles_dir or STYLES_DIR
    os.makedirs(d, exist_ok=True)
    safe = "".join(c for c in name if c.isalnum() or c in (" ", "_", "-")).strip() or "style"
    path = os.path.join(d, safe + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(style, f, ensure_ascii=False, indent=2)
    return path


# Where to look for fonts beyond the bundled fonts/ dir. macOS keeps Arial Black,
# Helvetica, etc. here, so a style that names "Arial Black.ttf" just works on a Mac
# without the user copying anything in.
SYSTEM_FONT_DIRS = [
    "/System/Library/Fonts",
    "/System/Library/Fonts/Supplemental",
    "/Library/Fonts",
    os.path.expanduser("~/Library/Fonts"),
    "/usr/share/fonts",
    "/usr/share/fonts/truetype",
]


def resolve_font(font_name: str, fonts_dir: str = None):
    """Find a usable font file for `font_name`.

    Search order: explicit path -> bundled fonts/ -> system font dirs (recursively).
    Returns a path, or None to let the renderer use its scaled default fallback.
    """
    d = fonts_dir or FONTS_DIR
    if not font_name:
        return None
    if os.path.isabs(font_name) and os.path.exists(font_name):
        return font_name

    base = os.path.basename(font_name)
    # 1) bundled fonts dir
    local = os.path.join(d, base)
    if os.path.exists(local):
        return local
    # 2) system font dirs (exact filename match, recursive)
    for root_dir in SYSTEM_FONT_DIRS:
        if not os.path.isdir(root_dir):
            continue
        for dirpath, _, files in os.walk(root_dir):
            if base in files:
                return os.path.join(dirpath, base)
    return None
