"""Transcript clean-up rules — turn raw ASR output into caption-ready text.

Runs right after transcription (Whisper AND Scribe), so the designer sees in the transcript
exactly what will be burned in. Word timings are preserved: when several spoken words collapse
into one token ("twenty eight" -> "28"), the new token keeps the first word's start and the
last word's end.

Everything is data-driven from caption_rules.json ("text_rules"), so the team can extend it
without touching code.
"""
import re

# spelled-out numbers -> digits (source language of the creatives; other langs can be added
# to caption_rules.json under text_rules.<lang>.number_words)
_EN_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19,
}
_EN_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50,
            "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_EN_SCALE = {"hundred": 100, "thousand": 1000, "million": 1000000}

_DEFAULT_TEXT_RULES = {
    "en": {
        "numbers": True,               # twenty eight -> 28
        "min_number": 2,               # keep "one/two" as words (reads better); 2 = only >= 2-word numbers or >= 10
        "replace": {                   # literal swaps, case-insensitive, applied on single words
            "per cent": "%", "percent": "%", "percents": "%",
            "dollars": "$", "dollar": "$", "euros": "€", "euro": "€",
            "kilograms": "kg", "kilogram": "kg", "kilos": "kg", "kilo": "kg",
            "pounds": "lbs", "kilometers": "km", "kilometres": "km",
            "minutes": "min", "minute": "min", "seconds": "sec", "second": "sec",
            "okay": "OK", "versus": "vs",
        },
    }
}


def _rules_for(rules, lang):
    tr = (rules or {}).get("text_rules") or {}
    base = dict(_DEFAULT_TEXT_RULES.get(lang, _DEFAULT_TEXT_RULES["en"]))
    over = tr.get(lang) or tr.get("en") or {}
    base.update(over)
    # a team-wide replace map merges on top of the defaults instead of replacing them
    if "replace" in over:
        merged = dict(_DEFAULT_TEXT_RULES.get(lang, _DEFAULT_TEXT_RULES["en"]).get("replace", {}))
        merged.update(over["replace"] or {})
        base["replace"] = merged
    return base


def _norm(tok):
    """word -> comparable key (drop punctuation/case) + the trailing punctuation we must keep."""
    m = re.match(r"^(.*?)([^\w%$€]*)$", tok, flags=re.UNICODE)
    core, tail = (m.group(1), m.group(2)) if m else (tok, "")
    return core.lower().strip("\"'“”‘’()[]"), core, tail


def _number_run(keys, i, cfg):
    """Longest spelled-out number starting at i → (value, length) or (None, 0)."""
    units, tens, scale = _EN_UNITS, _EN_TENS, _EN_SCALE
    total = cur = 0
    n = 0
    seen = False
    j = i
    while j < len(keys):
        k = keys[j]
        if k == "and" and seen and j + 1 < len(keys) and (keys[j + 1] in units or keys[j + 1] in tens):
            j += 1
            n += 1
            continue
        if k in units:
            cur += units[k]
        elif k in tens:
            cur += tens[k]
        elif k in scale:
            mult = scale[k]
            if mult == 100:
                cur = max(cur, 1) * 100
            else:
                total += max(cur, 1) * mult
                cur = 0
        else:
            break
        seen = True
        j += 1
        n += 1
    if not seen:
        return None, 0
    val = total + cur
    # "one/two" alone usually reads better as a word; only convert longer runs or bigger values
    if n < int(cfg.get("min_number", 2)) and val < 10:
        return None, 0
    return val, n


def clean_words(words, lang="en", rules=None):
    """Return a new word list with numbers as digits and literal replacements applied.

    Timings are preserved; merged tokens span from the first word's start to the last word's end.
    """
    if not words:
        return words
    cfg = _rules_for(rules, (lang or "en").lower()[:2])
    repl = {k.lower(): v for k, v in (cfg.get("replace") or {}).items()}
    keys, cores, tails = [], [], []
    for w in words:
        k, core, tail = _norm(str(w.get("word", w.get("text", ""))))
        keys.append(k)
        cores.append(core)
        tails.append(tail)

    out = []
    i = 0
    while i < len(words):
        val, n = (_number_run(keys, i, cfg) if cfg.get("numbers", True) else (None, 0))
        if val is not None and n > 0:
            first, last = words[i], words[i + n - 1]
            token = str(val) + (tails[i + n - 1] or "")
            out.append({**first, "word": token, "start": first.get("start"), "end": last.get("end")})
            i += n
            continue
        k = keys[i]
        if k in repl:
            new = repl[k]
            # glue a symbol onto the preceding number: "50 percent" -> "50%",
            # but currency reads the other way round: "125 dollars" -> "$125"
            if new in ("%", "$", "€") and out and re.fullmatch(r"[\d.,]+", str(out[-1].get("word", "")).strip()):
                num = str(out[-1]["word"]).strip()
                token = (new + num) if new in ("$", "€") else (num + new)
                out[-1] = {**out[-1], "word": token + (tails[i] or ""), "end": words[i].get("end")}
            else:
                out.append({**words[i], "word": new + (tails[i] or "")})
            i += 1
            continue
        out.append(words[i])
        i += 1
    return out
