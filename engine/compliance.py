"""Compliance scan for network edits (AppLovin profile).

The point of this module: a compliance problem is a problem of the SPOKEN line, not of the
subtitle. Rewriting a caption to say "lose extra weight" while the narrator clearly says
"lose 45 pounds" does not make a creative compliant — it just hides the issue from whoever
reviews the captions. So the scan runs over the TRANSCRIPT and returns every hit with the
word range and the TIMECODE it occupies, plus what that hit needs:

    replace  — safe to swap in the captions (and re-voice the same span)
    revoice  — the words are spoken; a caption edit alone is not enough (cut or re-dub)
    review   — a human decides (red claims, half-TPC)

Rules live in compliance_rules.json so the team extends them without touching code.
"""
import os
import re
import json

_CACHE = {"path": None, "mtime": 0, "rules": None}


def rules_path():
    return os.environ.get("CS_COMPLIANCE_RULES") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "compliance_rules.json")


def load_rules(path=None):
    p = path or rules_path()
    try:
        m = os.path.getmtime(p)
    except OSError:
        return {"groups": [], "profiles": {}}
    if _CACHE["path"] == p and _CACHE["mtime"] == m and _CACHE["rules"]:
        return _CACHE["rules"]
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:
        data = {"groups": [], "profiles": {}}
    _CACHE.update({"path": p, "mtime": m, "rules": data})
    return data


def _word_text(w):
    return str((w.get("text") if isinstance(w, dict) else w) or
               (w.get("word") if isinstance(w, dict) else "") or "")


def flatten(words):
    """Transcript as one string + a char→word-index map, so a regex hit maps back to timings."""
    parts, spans = [], []
    pos = 0
    for i, w in enumerate(words or []):
        t = _word_text(w)
        if not t:
            continue
        if parts:
            pos += 1          # the space we join with
        parts.append(t)
        spans.append((pos, pos + len(t), i))
        pos += len(t)
    return " ".join(parts), spans


def _words_for_span(spans, a, b):
    """Word indices whose characters overlap [a,b)."""
    hits = [i for (s, e, i) in spans if s < b and e > a]
    return (min(hits), max(hits)) if hits else (None, None)


def profiles(rules=None):
    """[{id, title, max_seconds, ...}] — the networks this install knows how to check."""
    rules = rules or load_rules()
    out = []
    for pid, p in (rules.get("profiles") or {}).items():
        out.append({"id": pid, "title": p.get("title") or pid,
                    "max_seconds": p.get("max_seconds"),
                    "groups": len(p.get("groups") or []),
                    "disclaimer_text": p.get("disclaimer_text", "")})
    return out


# ---- numeric evaluators -------------------------------------------------------------------
# The guide does not ban numbers; it bans a RATE. "Lose 24 pounds in 8 weeks" is 3 lbs/week,
# which the guide calls healthy, while "32 pounds in 8 weeks" is 4 and an overpromise. A regex
# cannot tell those apart, so a pattern here only proposes a CANDIDATE and one of these
# functions decides. Returns (is_hit, detail_string) — the detail goes into the UI so the
# reviewer sees the arithmetic instead of trusting a verdict.

_WORD_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
_LB_PER = {"pound": 1.0, "pounds": 1.0, "lb": 1.0, "lbs": 1.0, "lb.": 1.0, "lbs.": 1.0,
           "kilo": 2.20462, "kilos": 2.20462, "kilogram": 2.20462, "kilograms": 2.20462,
           "kg": 2.20462, "kgs": 2.20462}
_WEEKS_PER = {"day": 1 / 7.0, "days": 1 / 7.0, "week": 1.0, "weeks": 1.0,
              "month": 4.34524, "months": 4.34524, "year": 52.1429, "years": 52.1429}
# Clothing sizes as an ordered scale, so "XL to M" is measurable as 2 steps down.
_SIZE_SCALE = ["xxxl", "3xl", "xxl", "2xl", "xl", "l", "m", "s", "xs", "xxs"]


def _num(tok):
    t = str(tok or "").strip().lower().replace(",", ".")
    if t in _WORD_NUM:
        return float(_WORD_NUM[t])
    try:
        return float(t)
    except ValueError:
        return None


def _weeks(amount, unit):
    n = _num(amount)
    per = _WEEKS_PER.get(str(unit or "").strip().lower())
    return (n * per) if (n is not None and per) else None


def _eval_weight_rate(groups, g):
    """Pounds-per-week from (amount, weight-unit, period, period-unit) in either order."""
    vals = [v for v in groups if v]
    amount = unit = per_n = per_u = None
    for i, v in enumerate(vals):
        lv = str(v).strip().lower()
        if lv in _LB_PER and i > 0:
            amount, unit = vals[i - 1], lv
        if lv in _WEEKS_PER and i > 0:
            per_n, per_u = vals[i - 1], lv
    lbs = _num(amount)
    if lbs is None or unit is None:
        return False, ""
    lbs *= _LB_PER[unit]
    wk = _weeks(per_n, per_u)
    if not wk or wk <= 0:
        return False, ""
    rate = lbs / wk
    limit = float(g.get("max_lbs_per_week") or 3.0)
    return (rate > limit + 1e-6,
            f"{lbs:.0f} lbs / {wk:.1f} тижн. = {rate:.1f} lbs/тиждень (межа {limit:g})")


def _eval_size_rate(groups, g):
    """A size change inside a short period. Steps on the size scale + the period, both shown."""
    vals = [str(v).strip().lower() for v in groups if v]
    sizes = [v for v in vals if v in _SIZE_SCALE or v.startswith("size")]
    per_n = per_u = None
    for i, v in enumerate(vals):
        if v in _WEEKS_PER and i > 0:
            per_n, per_u = vals[i - 1], v
    wk = _weeks(per_n, per_u)
    if not wk:
        return False, ""
    limit = float(g.get("max_weeks") or 12)
    steps = ""
    if len(sizes) >= 2 and sizes[0] in _SIZE_SCALE and sizes[1] in _SIZE_SCALE:
        d = abs(_SIZE_SCALE.index(sizes[0]) - _SIZE_SCALE.index(sizes[1]))
        steps = f", {d} розмір(и)"
    return (wk <= limit + 1e-6, f"{wk:.1f} тижн.{steps} (межа {limit:g} тижн.)")


_EVALS = {"weight_rate": _eval_weight_rate, "size_rate": _eval_size_rate}


def scan(words, rules=None, profile="applovin"):
    """Find every compliance hit for ONE network profile.

    Returns {"hits": [...], "profile": {...}, "counts": {...}, "text": str}. A profile is always
    explicit: there is no "default rules for every creative" mode, by design.
    """
    rules = rules or load_rules()
    prof = (rules.get("profiles") or {}).get(profile) or {}
    # Groups belong to the PROFILE, so one network's rules can never leak into another's scan.
    # (A top-level "groups" is still honoured — older rule files had no profiles.)
    groups = prof.get("groups") or rules.get("groups") or []
    text, spans = flatten(words)
    low = text.lower()
    hits = []
    seen = set()
    for g in groups:
        action = g.get("action", "review")
        for pat in (g.get("patterns") or []):
            try:
                rx = re.compile(pat, re.I)
            except re.error:
                continue
            ev = _EVALS.get(g.get("eval") or "")
            for m in rx.finditer(low):
                a, b = m.span()
                detail = ""
                if ev:                   # the pattern only proposed it; the arithmetic decides
                    ok, detail = ev(m.groups(), g)
                    if not ok:
                        continue
                wi, wj = _words_for_span(spans, a, b)
                if wi is None:
                    continue
                key = (wi, wj)
                if key in seen:          # a longer, more specific rule already claimed this span
                    continue
                seen.add(key)
                w0 = words[wi] if wi < len(words) else {}
                w1 = words[wj] if wj < len(words) else {}
                hits.append({
                    "group": g.get("id", ""),
                    "title": g.get("title", ""),
                    "action": action,
                    "source": g.get("source", ""),
                    "detail": detail,
                    "why": g.get("why", ""),
                    "suggest": g.get("suggest", []),
                    "match": text[a:b],
                    "word_from": wi, "word_to": wj,
                    "start": round(float(w0.get("start", 0) or 0), 3),
                    "end": round(float(w1.get("end", 0) or 0), 3),
                })
    # A broad rule often matches INSIDE a more specific one ("April 13th" inside "on Monday
    # April 13th"). Keep the widest hit per span — one line per real problem, not three.
    hits.sort(key=lambda h: (h["word_from"], -(h["word_to"] - h["word_from"])))
    kept = []
    for h in hits:
        if any(k["word_from"] <= h["word_from"] and h["word_to"] <= k["word_to"] for k in kept):
            continue
        kept.append(h)
    hits = kept
    hits.sort(key=lambda h: (h["start"], h["word_from"]))
    counts = {"cut": 0, "text": 0, "replace": 0, "revoice": 0, "review": 0}
    for h in hits:
        counts[h["action"]] = counts.get(h["action"], 0) + 1
    return {"hits": hits, "counts": counts, "profile": prof, "text": text}


def apply_captions(words, fixes):
    """Swap the caption text of the given word ranges.

    `fixes` = [{"word_from":i,"word_to":j,"text":"lose extra weight"}]. The replacement is
    spread over the SAME words (extra words emptied, never removed) so word timings — and
    therefore the whole caption layout — stay exactly as they were.
    """
    out = [dict(w) for w in (words or [])]
    changes = []
    for f in (fixes or []):
        try:
            i, j = int(f.get("word_from")), int(f.get("word_to"))
        except Exception:
            continue
        if not (0 <= i <= j < len(out)):
            continue
        new_words = str(f.get("text", "")).split()
        old = " ".join(_word_text(out[k]) for k in range(i, j + 1))
        span = j - i + 1
        if len(new_words) > span:        # squeeze the tail into the last slot — never add words
            new_words = new_words[:span - 1] + [" ".join(new_words[span - 1:])]
        for k in range(i, j + 1):
            val = new_words[k - i] if (k - i) < len(new_words) else ""
            if "text" in out[k] or "word" not in out[k]:
                out[k]["text"] = val
            if "word" in out[k]:
                out[k]["word"] = val
        changes.append({"word_from": i, "word_to": j, "from": old, "to": f.get("text", "")})
    return out, changes


_ORPHANS = {"i", "i've", "ive", "she", "he", "they", "we", "you", "and", "but", "so", "then",
            "that", "it", "also", "plus", "because", "cause", "cuz"}


def minimal_cut(words, hit, clip_dur=0.0, hook_end=None, profile_cfg=None):
    """The narrowest clean cut that removes one overpromise.

    The guide is explicit — "we try to cut out as little as possible so as not to harm the
    creative itself" — so this does NOT drop the sentence or the scene. It cuts exactly the
    offending words and moves each boundary into the SILENCE beside them, using the gaps
    already present in the word timings: land in the middle of a pause and neither the word
    before nor the word after gets clipped. When the words run together there is no pause to
    use, so the cut sits on the word edge and the seam is reported as tight.

    Also applies the guide's exception: "if the hook is short and the problematic part makes up
    basically the entire hook, leave it as is" — cutting nearly all of a hook destroys the
    creative, so the recommendation flips to keep.

    Returns {"cut": [a, b], "seconds", "seam", "recommend", "why"}.
    """
    cfg = profile_cfg or {}
    ws = [w for w in (words or []) if str(_word_text(w)).strip()]
    i, j = int(hit.get("word_from", 0)), int(hit.get("word_to", 0))
    if not ws or not (0 <= i <= j < len(ws)):
        return {"cut": None, "seconds": 0.0, "seam": "", "recommend": "review",
                "why": "не збігаються індекси слів — перескануй"}
    # Absorb a function word that would be left dangling. Cutting only the matched words turns
    # "I lost 32 pounds in 8 weeks and now I feel amazing" into "... years I and now I feel
    # amazing" — a stray "I" is exactly the kind of damage the guide warns about. So a pronoun /
    # conjunction glued to the front of the hit (no real pause between them) goes with it.
    while i > 0:
        prev = str(_word_text(ws[i - 1])).strip().lower().strip(".,!?")
        if prev not in _ORPHANS:
            break
        if float(ws[i]["start"]) - float(ws[i - 1]["end"]) > 0.12:
            break                      # a pause between them: it belongs to the sentence before
        i -= 1

    a_word, b_word = float(ws[i]["start"]), float(ws[j]["end"])

    # gap BEFORE the first offending word / AFTER the last one
    prev_end = float(ws[i - 1]["end"]) if i > 0 else 0.0
    next_start = float(ws[j + 1]["start"]) if j + 1 < len(ws) else (clip_dur or b_word)
    gap_l = max(0.0, a_word - prev_end)
    gap_r = max(0.0, next_start - b_word)
    # meet in the middle of each pause; keep a 40ms guard so no consonant is shaved off
    a = a_word - min(gap_l / 2.0, max(0.0, gap_l - 0.04))
    b = b_word + min(gap_r / 2.0, max(0.0, gap_r - 0.04))
    a, b = max(0.0, a), (min(b, clip_dur) if clip_dur else b)

    tight = [s for s, gp in (("початок", gap_l), ("кінець", gap_r)) if gp < 0.12]
    seam = ("щільний стик: " + " і ".join(tight) + " — послухай, чи не зрізає слово") if tight else "стик у паузі"

    recommend, why = "cut", "вирізати цей відрізок"
    if hook_end and a < float(hook_end):
        share = (min(b, float(hook_end)) - a) / max(0.01, float(hook_end))
        thr = float(cfg.get("keep_if_hit_covers_hook") or 0.7)
        if share >= thr:
            recommend = "keep"
            why = (f"знахідка займає {share*100:.0f}% хука — гайд каже такий залишати як є "
                   f"(різати майже весь хук = вбити креатив)")
    return {"cut": [round(a, 3), round(b, 3)], "seconds": round(b - a, 3),
            "seam": seam, "recommend": recommend, "why": why}
