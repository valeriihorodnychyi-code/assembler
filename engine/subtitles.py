"""Karaoke event building + per-event subtitle PNG rendering.

Ported and de-duplicated from the original magic_en.py / magic_loc.py, keeping
the hardened fixes from magic_loc (empty-line guard, negative-duration guard,
timestamp normalization).
"""
import os
import re
import json
from PIL import Image, ImageDraw, ImageFont, ImageFilter


def _word_text(w):
    return w.get("text", w.get("word", "")) if isinstance(w, dict) else getattr(w, "word", "")


SENTENCE_ENDERS = ".!?…"

# English month / weekday names used by the automatic date glue. (Localized
# month names for es/fr/... can be added later; numbers + units glue already
# works language-agnostically.)
_MONTHS = {"january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december",
           "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec"}
_WEEKDAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
             "mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri", "sat", "sun"}
_SYMBOLS = {"%", "$", "€", "£", "+", "&", "/", "x"}

_RULES_CACHE = {}


def _rules_path():
    return os.environ.get("CS_CAPTION_RULES",
                          os.path.join(os.path.dirname(os.path.dirname(__file__)), "caption_rules.json"))


def load_rules(path=None):
    """Load (and cache by mtime) the shared caption rules JSON.

    Returns a safe default dict if the file is missing or malformed so captioning
    never breaks just because rules are absent.
    """
    p = path or _rules_path()
    default = {"keep_together": [], "glue": {}, "units": [], "no_line_end": {}, "min_last_line_words": 0}
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        return default
    cached = _RULES_CACHE.get(p)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return default
    _RULES_CACHE[p] = (mtime, data)
    return data


def _is_number(t):
    return bool(re.match(r"^[+\-]?[\d.,]*\d", t or ""))


def _phrase_token_lists(rules):
    out = []
    for phrase in rules.get("keep_together", []) or []:
        toks = [t for t in re.split(r"\s+", str(phrase).strip().lower()) if t]
        if len(toks) >= 2:
            out.append(toks)
    return out


def build_groups(words, rules, lang="en", pause_gap=0.5):
    """Split a phrase's word list into atomic groups that must stay together.

    A "group" is a run of consecutive words that the wrapper is never allowed to
    break across (line or caption). Rules applied: keep_together dictionary,
    number+unit, currency/symbols, dates & weekdays, and no_line_end stopwords
    (a stopword is glued to the FOLLOWING word so it can never end a line).
    """
    n = len(words)
    if n <= 1:
        return [list(words)] if words else []
    glue = rules.get("glue", {}) or {}
    units = set(u.lower() for u in (rules.get("units", []) or []))
    stop = set(s.lower() for s in (rules.get("no_line_end", {}) or {}).get(lang, []))
    lc = [(_word_text(w) if not isinstance(w, dict) else w.get("text", "")).lower() for w in words]

    bond = [False] * (n - 1)   # bond[i] => words[i] and words[i+1] cannot be split

    def gap_ok(i):
        try:
            return (float(words[i + 1]["start"]) - float(words[i]["end"])) <= pause_gap
        except (KeyError, TypeError, ValueError):
            return True

    for i in range(n - 1):
        if words[i].get("ends_sentence"):   # never glue across a sentence end
            continue
        if not gap_ok(i):                    # never glue across a long speech pause
            continue
        a, b = lc[i], lc[i + 1]
        # no_line_end stopword -> glue to the next word
        if stop and a in stop:
            bond[i] = True
        # number + unit ("5 kg", "30 days", "20 %") — only glue to a known unit/symbol,
        # not to any following word (otherwise "26 today" would glue).
        if glue.get("number_unit", True) and _is_number(a) and (b in units or b in _SYMBOLS):
            bond[i] = True
        if glue.get("currency", True) and (a in _SYMBOLS or b in _SYMBOLS):
            bond[i] = True
        # dates: "June 26", "26 June", weekday next to a neighbour
        if glue.get("dates", True):
            if (a in _MONTHS and _is_number(b)) or (_is_number(a) and b in _MONTHS):
                bond[i] = True
            if a in _WEEKDAYS or b in _WEEKDAYS:
                bond[i] = True

    # keep_together dictionary phrases (override: bond every internal pair)
    phrases = _phrase_token_lists(rules)
    if phrases:
        for start in range(n):
            for toks in phrases:
                k = len(toks)
                if start + k <= n and lc[start:start + k] == toks:
                    for j in range(start, start + k - 1):
                        if not words[j].get("ends_sentence"):
                            bond[j] = True

    groups, cur = [], [words[0]]
    for i in range(n - 1):
        if bond[i]:
            cur.append(words[i + 1])
        else:
            groups.append(cur)
            cur = [words[i + 1]]
    groups.append(cur)
    return groups


def build_events(words, limit, text_case="uppercase", replacements=None,
                 pause_gap=0.5, max_lines=2, wrap_mode="chars", lang="en", rules=None, cuts=None):
    """Group a flat list of {text,start,end} words into karaoke events.

    Each event = a visible 1-2 line chunk + the index of the currently active word.

    Chunks respect natural boundaries so a caption never mixes the tail of one
    sentence with the head of the next: a new phrase starts after sentence-ending
    punctuation (. ! ?) OR after a speech pause longer than `pause_gap` seconds.
    Within a phrase, words wrap to <= max_chars lines, grouped into <= 2-line chunks.
    """
    replacements = replacements or {}
    if rules is None:
        rules = load_rules()
    clean = []
    for w in words:
        raw = _word_text(w).strip()
        ends_sentence = raw[-1:] in SENTENCE_ENDERS if raw else False
        text = raw.rstrip(".,!?…")
        for old, new in replacements.items():
            text = re.sub(r"\b" + re.escape(old) + r"\b", new, text, flags=re.IGNORECASE)
        if text_case == "uppercase":
            text = text.upper()
        elif text_case == "lowercase":
            text = text.lower()
        if text:
            clean.append({"text": text, "start": float(w["start"]), "end": float(w["end"]),
                          "ends_sentence": ends_sentence, "brk": w.get("brk")})

    if not clean:
        return []

    # 0) Merge hyphenated fragments into one word (e.g. PT "segunda-feira") so a line
    #    break never splits inside a hyphenated compound.
    merged = []
    for w in clean:
        if merged and (merged[-1]["text"].endswith("-") or w["text"].startswith("-") or w["text"] == "-"):
            merged[-1]["text"] += w["text"]
            merged[-1]["end"] = w["end"]
            merged[-1]["ends_sentence"] = w["ends_sentence"]
            merged[-1]["brk"] = w.get("brk")
        else:
            merged.append(w)
    clean = merged

    # 1) MANUAL layout (the user's own line/chunk breaks) takes priority when present;
    #    otherwise fall back to automatic phrase + max-chars wrapping.
    chunks = []
    if any(w.get("brk") for w in clean):
        lines, line = [], []
        for w in clean:
            line.append(w)
            if w.get("brk") == "line":
                lines.append(line); line = []
            elif w.get("brk") == "chunk":
                lines.append(line); line = []; chunks.append(lines); lines = []
        if line:
            lines.append(line)
        if lines:
            chunks.append(lines)
    else:
        cut_list = sorted(float(c) for c in (cuts or []))

        def _cut_between(a_start, b_start):   # a scene cut falls between two words -> new caption
            return any(a_start < c <= b_start for c in cut_list)

        phrases, cur = [], []
        for w in clean:
            if cur:
                prev = cur[-1]
                if (prev["ends_sentence"] or (w["start"] - prev["end"]) > pause_gap
                        or _cut_between(prev["start"], w["start"])):
                    phrases.append(cur)
                    cur = []
            cur.append(w)
        if cur:
            phrases.append(cur)
        lim = max(1, int(limit))
        for phrase in phrases:
            # Build atomic groups first (keep_together / numbers / dates / stopwords),
            # then wrap by GROUPS so a glued unit never breaks across a line or caption.
            groups = build_groups(phrase, rules, lang=lang, pause_gap=pause_gap)
            glines = []   # lines as lists OF groups
            if wrap_mode == "words":          # wrap by WORD count per line
                line, count = [], 0
                for g in groups:
                    gw = len(g)
                    if line and count + gw > lim:
                        glines.append(line)
                        line, count = [], 0
                    line.append(g)
                    count += gw
                if line:
                    glines.append(line)
            else:                             # wrap by CHARS (default — guarantees width fit)
                line, line_chars = [], 0
                for g in groups:
                    glen = sum(len(w["text"]) for w in g) + (len(g) - 1)
                    space_len = 1 if line else 0
                    if line and line_chars + space_len + glen > lim:
                        glines.append(line)
                        line, line_chars = [], 0
                        space_len = 0
                    line.append(g)
                    line_chars += space_len + glen
                if line:
                    glines.append(line)
            # widow control: avoid a final line with fewer than N words
            minw = int((rules or {}).get("min_last_line_words", 0) or 0)
            while (minw > 1 and len(glines) > 1
                   and sum(len(g) for g in glines[-1]) < minw and len(glines[-2]) > 1):
                glines[-1].insert(0, glines[-2].pop())
            lines = [[w for g in line for w in g] for line in glines]   # flatten groups -> words
            step = max(1, int(max_lines))
            for j in range(0, len(lines), step):
                chunks.append(lines[j:j + step])

    events = []
    for chunk in chunks:
        all_words = [w for line in chunk for w in line]
        if not all_words:
            continue
        chunk_end = all_words[-1]["end"]
        for i, w in enumerate(all_words):
            start_time = w["start"]
            end_time = all_words[i + 1]["start"] if i + 1 < len(all_words) else chunk_end
            if end_time <= start_time:  # guard: Whisper hallucinated negative duration
                end_time = start_time + 0.1
            events.append({"start": start_time, "end": end_time,
                           "lines": chunk, "active_word_index": i})

    # Hard timestamp normalization so overlays never overlap or invert.
    for i in range(len(events) - 1):
        events[i]["end"] = events[i + 1]["start"]
        if events[i]["end"] <= events[i]["start"]:
            events[i]["end"] = events[i]["start"] + 0.1

    # Clamp a caption's end to the next scene cut so a frozen caption never lingers
    # over the next shot (the post-cut words already start a fresh caption above).
    if cuts:
        cl = sorted(float(c) for c in cuts)
        for e in events:
            for c in cl:
                if e["start"] < c < e["end"]:
                    e["end"] = c
                    break
    return events


# Bold-ish fallbacks tried (in order) when the requested font isn't found, so we
# never silently drop to the tiny non-scalable PIL bitmap default.
_FALLBACK_FONTS = [
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def load_font(font_path, size):
    """Always return a *scalable* font of the requested size.

    The old behaviour fell back to ImageFont.load_default(), a fixed ~10px bitmap
    that ignores `size` -> microscopic captions. We instead try the requested font,
    then known system fonts, then a size-aware default.
    """
    if font_path:
        try:
            return ImageFont.truetype(font_path, size)
        except (IOError, OSError):
            pass
    for fb in _FALLBACK_FONTS:
        if os.path.exists(fb):
            try:
                return ImageFont.truetype(fb, size)
            except (IOError, OSError):
                continue
    try:  # Pillow >= 10.1 supports a sized default
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def render_subtitle_png(event, filename, width, height, font_path, style_cfg, scale_factor=1.0):
    """Render a single karaoke event to a transparent PNG (pixel-faithful to preview)."""
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    final_img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font_size = int(int(style_cfg["font_size"]) * scale_factor)
    font = load_font(font_path, font_size)

    if style_cfg.get("force_single_line", False):
        flat = [w for line in event["lines"] for w in line]
        lines = [flat]
    else:
        lines = event["lines"]

    active_idx = event["active_word_index"]
    # shrink-to-fit: never let a caption line exceed the safe frame width (keeps long
    # keep-together phrases or a single long word from spilling past the frame edge).
    safe_w = width * 0.92
    _sp = font.getlength(" ")
    _maxw = max((sum(font.getlength(w["text"]) for w in line) + (len(line) - 1) * _sp
                 for line in lines), default=0)
    if _maxw > safe_w > 0:
        font_size = max(8, int(font_size * safe_w / _maxw))
        font = load_font(font_path, font_size)
    ascent, descent = font.getmetrics()
    line_height = ascent + descent
    line_spacing = int(int(style_cfg.get("line_spacing", 10)) * scale_factor)
    space_width = font.getlength(" ")

    total_height = len(lines) * line_height + (len(lines) - 1) * line_spacing
    margin_bottom = int(int(style_cfg.get("margin_bottom", 300)) * (height / 1920.0) * scale_factor)
    start_y = (height - margin_bottom) - (total_height / 2)
    # box nudge: moves plate/highlight boxes up(+)/down(-) without moving the text
    box_off = int(int(style_cfg.get("box_offset_y", 0)) * scale_factor)

    line_widths = [sum(font.getlength(w["text"]) for w in line) + (len(line) - 1) * space_width
                   for line in lines]

    word_positions, current_y, word_counter, cx = [], start_y, 0, width / 2
    for i, line in enumerate(lines):
        current_x = cx - (line_widths[i] / 2)
        for w in line:
            word_positions.append((current_x, current_y, w["text"], word_counter == active_idx))
            current_x += font.getlength(w["text"]) + space_width
            word_counter += 1
        current_y += line_height + line_spacing

    plate_on = style_cfg.get("plate", {}).get("enabled", False)
    shadow_on = style_cfg.get("shadow", {}).get("enabled", False)
    sh = style_cfg.get("shadow", {})
    sh_x = int(int(sh.get("offset_x", 0)) * scale_factor)
    sh_y = int(int(sh.get("offset_y", 10)) * scale_factor)
    sh_c = tuple(sh.get("color", [0, 0, 0, 255]))
    sh_blur = int(int(sh.get("glow_blur", 0)) * scale_factor)

    if plate_on:
        plate = style_cfg["plate"]
        pad_x = int(int(plate.get("pad_x", 30)) * scale_factor)
        pad_y = int(int(plate.get("pad_y", 15)) * scale_factor)
        radius = int(int(plate.get("border_radius", 15)) * scale_factor)
        plate_color = tuple(plate["color"])
        per_line = plate.get("per_line", False)
        full_w = plate.get("full_width", False)

        # full-width edge-to-edge bar, one rect around the whole block, or per-line
        rects = []
        if full_w:
            radius = 0  # an edge-to-edge bar reads better with square corners
            rects.append([0, start_y - pad_y - box_off,
                          width, start_y + total_height + pad_y - box_off])
        elif per_line:
            for i, lw in enumerate(line_widths):
                ly = start_y + i * (line_height + line_spacing) - box_off
                rx0 = cx - (lw / 2) - pad_x
                rects.append([rx0, ly - pad_y, rx0 + lw + pad_x * 2, ly + line_height + pad_y])
        else:
            max_lw = max(line_widths) if line_widths else 0
            rx0 = cx - (max_lw / 2) - pad_x
            rects.append([rx0, start_y - pad_y - box_off,
                          rx0 + max_lw + pad_x * 2, start_y + total_height + pad_y - box_off])

        if shadow_on:
            shadow_img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            sd = ImageDraw.Draw(shadow_img)
            for r in rects:
                sc = [r[0] + sh_x, r[1] + sh_y, r[2] + sh_x, r[3] + sh_y]
                (sd.rounded_rectangle(sc, radius=radius, fill=sh_c) if radius > 0
                 else sd.rectangle(sc, fill=sh_c))
            if sh_blur > 0:
                shadow_img = shadow_img.filter(ImageFilter.GaussianBlur(sh_blur))
            final_img.alpha_composite(shadow_img)

        plate_img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        pd = ImageDraw.Draw(plate_img)
        for r in rects:
            (pd.rounded_rectangle(r, radius=radius, fill=plate_color) if radius > 0
             else pd.rectangle(r, fill=plate_color))
        final_img.alpha_composite(plate_img)

    elif shadow_on:
        st = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        sd = ImageDraw.Draw(st)
        so_w = int(int(style_cfg.get("stroke_outer", {}).get("width", 0)) * scale_factor)
        s_w = int(int(style_cfg.get("stroke", {}).get("width", 0)) * scale_factor)
        for x, y, text, _ in word_positions:
            if so_w > 0:
                sd.text((x + sh_x, y + sh_y), text, font=font, fill=sh_c, stroke_width=so_w, stroke_fill=sh_c)
            elif s_w > 0:
                sd.text((x + sh_x, y + sh_y), text, font=font, fill=sh_c, stroke_width=s_w, stroke_fill=sh_c)
            else:
                sd.text((x + sh_x, y + sh_y), text, font=font, fill=sh_c)
        if sh_blur > 0:
            st = st.filter(ImageFilter.GaussianBlur(sh_blur))
        final_img.alpha_composite(st)

    kp = style_cfg.get("karaoke_plate", {})
    if kp.get("enabled", False):
        kp_c = tuple(kp.get("color", [255, 128, 0, 255]))
        kp_px = int(int(kp.get("pad_x", 15)) * scale_factor)
        kp_py = int(int(kp.get("pad_y", 5)) * scale_factor)
        kp_rad = int(int(kp.get("border_radius", 10)) * scale_factor)
        kp_img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        kd = ImageDraw.Draw(kp_img)
        for x, y, text, is_active in word_positions:
            if is_active:
                bbox = draw.textbbox((x, y), text, font=font)
                c = [bbox[0] - kp_px, bbox[1] - kp_py - box_off, bbox[2] + kp_px, bbox[3] + kp_py - box_off]
                (kd.rounded_rectangle(c, radius=kp_rad, fill=kp_c) if kp_rad > 0
                 else kd.rectangle(c, fill=kp_c))
        final_img.alpha_composite(kp_img)

    text_img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    td = ImageDraw.Draw(text_img)
    text_c = tuple(style_cfg["text_color"])
    karaoke_on = style_cfg.get("karaoke", {}).get("enabled", False)
    karaoke_c = tuple(style_cfg.get("karaoke", {}).get("active_color", [255, 0, 0, 255]))
    s_w = int(int(style_cfg.get("stroke", {}).get("width", 0)) * scale_factor)
    s_c = tuple(style_cfg.get("stroke", {}).get("color", [0, 0, 0, 255]))
    so_w = int(int(style_cfg.get("stroke_outer", {}).get("width", 0)) * scale_factor)
    so_c = tuple(style_cfg.get("stroke_outer", {}).get("color", [0, 0, 0, 255]))

    if so_w > 0:
        for x, y, text, _ in word_positions:
            td.text((x, y), text, font=font, fill=so_c, stroke_width=so_w, stroke_fill=so_c)
    for x, y, text, is_active in word_positions:
        fill = karaoke_c if (karaoke_on and is_active) else text_c
        if s_w > 0:
            td.text((x, y), text, font=font, fill=fill, stroke_width=s_w, stroke_fill=s_c)
        else:
            td.text((x, y), text, font=font, fill=fill)
    final_img.alpha_composite(text_img)
    final_img.save(filename)


def generate_shadow_asset(video_w, video_h, target_w, target_h, filename):
    img = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    spread, offset_y = 40, 15
    x0 = (target_w - video_w) / 2 - spread
    y0 = (target_h - video_h) / 2 - spread + offset_y
    x1, y1 = x0 + video_w + spread * 2, y0 + video_h + spread * 2
    draw.rectangle([x0, y0, x1, y1], fill=(0, 0, 0, 200))
    img.filter(ImageFilter.GaussianBlur(35)).save(filename)
