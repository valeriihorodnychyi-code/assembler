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
        "numbers": True,               # twenty eight / twenty-eight -> 28, four weeks -> 4 weeks
        "min_number": 1,               # 1 = every number as digits (2..9 too); "one"/"zero" only before a unit
                                       # (one week -> 1 week, but "one of my friends" stays). 2 = the old way
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


# a number right before one of these is a quantity → digits even for "one" ("one week" -> "1 week")
_EN_UNIT_WORDS = {
    "day", "days", "week", "weeks", "month", "months", "year", "years", "hour", "hours", "minute", "minutes",
    "min", "mins", "sec", "secs", "pound", "pounds", "lb", "lbs", "kilo", "kilos", "kg", "kilogram", "kilograms",
    "inch", "inches", "mile", "miles", "km", "kilometer", "kilometers", "step", "steps", "rep", "reps", "set",
    "sets", "time", "times", "glass", "glasses", "cup", "cups", "percent", "%", "dollar", "dollars", "size",
    "sizes", "workout", "workouts", "exercise", "exercises", "move", "moves", "meal", "meals", "calorie", "calories",
}
_NUMW = lambda k: k in _EN_UNITS or k in _EN_TENS or k in _EN_SCALE


def _split_hyphen(k):
    """'twenty-eight' -> (['twenty', 'eight'], '') · 'twenty-eight-day' -> (['twenty', 'eight'], 'day')
    · 'four-week' -> (['four'], 'week') · anything else -> None. (Scribe writes compounds hyphenated.)"""
    if "-" not in k:
        return None
    parts = [x for x in k.split("-") if x]
    nums = []
    for x in parts:
        if _NUMW(x):
            nums.append(x)
        else:
            break
    if not nums:
        return None
    rest = parts[len(nums):]
    return nums, "-".join(rest)


_EN_BEFORE = {"day", "week", "month", "step", "level", "part", "round", "phase", "stage", "number", "chapter",
              "episode", "lesson", "tip", "rule", "size", "age", "aged"}   # "day one" -> "day 1"


def _number_run(keys, i, cfg, tails=None):
    """Longest spelled-out number starting at i → (value, length, suffix) or (None, 0, "").
    suffix = what follows a hyphenated number inside the same token ('day' in 'twenty-eight-day')."""
    units, tens, scale = _EN_UNITS, _EN_TENS, _EN_SCALE
    total = cur = 0
    n = 0
    seen = False
    j = i
    suffix = ""
    atoms = 0

    def eat(k):
        nonlocal total, cur
        if k in units:
            cur += units[k]
        elif k in tens:
            cur += tens[k]
        else:
            mult = scale[k]
            if mult == 100:
                cur = max(cur, 1) * 100
            else:
                total += max(cur, 1) * mult
                cur = 0

    while j < len(keys):
        k = keys[j]
        if k == "and" and seen and j + 1 < len(keys) and (keys[j + 1] in units or keys[j + 1] in tens
                                                          or (_split_hyphen(keys[j + 1]) or [None])[0]):
            j += 1
            n += 1
            continue
        hy = _split_hyphen(k)
        if hy:
            for x in hy[0]:
                eat(x)
                atoms += 1
            seen = True
            j += 1
            n += 1
            if hy[1]:                      # "twenty-eight-day": the number ends inside this token
                suffix = hy[1]
                break
            if tails and tails[j - 1].strip():   # "twenty-six, one hundred" — a comma ends the number
                break
            continue
        if _NUMW(k):
            eat(k)
        else:
            break
        atoms += 1
        seen = True
        j += 1
        n += 1
        if tails and tails[j - 1].strip():
            break
    if not seen:
        return None, 0, ""
    val = total + cur
    nxt = keys[i + n] if i + n < len(keys) else ""
    mn = int(cfg.get("min_number", 1))
    if atoms < mn and val < 10:            # min_number 2: the old "tiny numbers stay words"
        return None, 0, ""
    prv = keys[i - 1] if i > 0 else ""
    if val <= 1 and atoms == 1 and not suffix and nxt not in _EN_UNIT_WORDS and prv not in _EN_BEFORE:
        return None, 0, ""                 # "one of my friends", "no one", "zero chance" stay words
    if atoms == 1 and keys[i] == "second":
        return None, 0, ""
    return val, n, suffix


def _fmt_num(v):
    return f"{v:,}" if v >= 10000 else str(v)   # 10,000 steps · but years / 5000 stay plain


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
        val, n, suf = (_number_run(keys, i, cfg, tails) if cfg.get("numbers", True) else (None, 0, ""))
        if val is not None and n > 0:
            first, last = words[i], words[i + n - 1]
            token = _fmt_num(val) + (("-" + suf) if suf else "") + (tails[i + n - 1] or "")
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
