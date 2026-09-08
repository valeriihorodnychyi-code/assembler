"""Caption polish pass (Gemini) — fixes misheard words and decides line breaks.

WHY IT'S SHAPED THIS WAY
The single most important invariant in this app is that caption layout is computed in ONE
place (compose/subtitles.build_timeline), which is what makes the preview equal the render.
So this pass must NOT lay out captions. It returns DATA:

    fix          -> per-word spelling/casing corrections, addressed BY INDEX
    break_after  -> indices after which the caption should break
    rules        -> suggestions for caption_rules.json (so the fix becomes free next time)

`break_after` is applied as the same `brk="soft"` marker the user's own Enter produces, so
the model's decisions ride the existing rails and appear identically in preview and render.

Word count is fixed BY CONSTRUCTION: a correction may only replace one token with one token
(no spaces), and any out-of-range or malformed entry is dropped. Word timings come from
Whisper/Scribe and are never touched — that's what keeps captions in sync.

Deterministic rules (caption_rules.json / textrules.py) run BEFORE this and stay the primary
mechanism; the model is only for what rules can't know: misheard brands, homophones, and the
aesthetic call on where a line should break.

Key: env GEMINI_API_KEY (or config.json "gemini_api_key"). Never hardcoded, never committed.
"""
import os
import json

API_BASE = os.environ.get("CS_GEMINI_BASE", "https://generativelanguage.googleapis.com/v1beta")
MODEL = os.environ.get("CS_GEMINI_MODEL", "gemini-3.7-flash")
FALLBACK_MODELS = ("gemini-2.5-flash",)   # if the configured model isn't available on this key

MAX_REPL_LEN = 40

_SCHEMA = {
    "type": "object",
    "properties": {
        "fix": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer"},
                    "text": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["i", "text"],
            },
        },
        "break_after": {"type": "array", "items": {"type": "integer"}},
        "rules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string"},
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["kind"],
            },
        },
    },
    "required": ["fix", "break_after"],
}

_INSTRUCTIONS = """You are a subtitle editor for short-form video ads. You receive the exact
word list of a transcript, one word per line as "index<TAB>word".

Do THREE things:

1. fix — correct only words that are clearly MISHEARD or wrongly cased (brand names, product
   names, homophones). One word in, ONE word out: the replacement must contain no spaces.
   Never add, delete, reorder, merge or split words. If nothing is wrong, return an empty list.

2. break_after — the indices after which the caption should break to a new caption/line.
   Break on natural sense units. Never split: a number from its unit, a preposition or article
   from the word it belongs to, a brand name, or a negation from its verb. Never leave a single
   short word alone on a line. Respect the limits given below.

3. rules — if a fix is a SYSTEMATIC problem that will repeat (a brand always misheard, a unit
   always spelled out), suggest a reusable rule so it can be handled without a model next time.

Do not rewrite the copy, do not translate, do not "improve" the wording, do not touch
punctuation. Return only the JSON."""


def _key(api_key=None):
    api_key = api_key or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Gemini API key missing (add it in ⚙ Settings, or GEMINI_API_KEY).")
    return api_key


def _word_of(w):
    return str(w.get("text") if w.get("text") is not None else w.get("word", "") or "")


def _set_word(w, val):
    if "text" in w:
        w["text"] = val
    if "word" in w:
        w["word"] = val
    if "text" not in w and "word" not in w:
        w["text"] = val


def _prompt(words, lang, max_chars, max_lines, rules):
    lines = "\n".join(f"{i}\t{_word_of(w)}" for i, w in enumerate(words))
    keep = (rules or {}).get("keep_together") or []
    ctx = [
        f"Language: {lang or 'en'}",
        f"Max characters per caption line: {max_chars}",
        f"Max lines per caption: {max_lines}",
    ]
    if keep:
        ctx.append("Phrases that must never be split: " + ", ".join(str(k) for k in keep[:40]))
    return f"{_INSTRUCTIONS}\n\n{chr(10).join(ctx)}\n\nWORDS:\n{lines}"


def _call(model, prompt, api_key, timeout=90):
    import requests  # lazy
    url = f"{API_BASE}/models/{model}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _SCHEMA,
            "temperature": 0.2,
        },
    }
    r = requests.post(url, headers={"x-goog-api-key": api_key,
                                    "Content-Type": "application/json"},
                      json=body, timeout=timeout)
    return r


def ask(words, lang="en", max_chars=15, max_lines=2, rules=None, api_key=None, model=None):
    """Raw model answer as a dict (validated shape, not yet applied)."""
    api_key = _key(api_key)
    prompt = _prompt(words, lang, max_chars, max_lines, rules)
    tried = []
    for m in [model or MODEL] + list(FALLBACK_MODELS):
        if m in tried:
            continue
        tried.append(m)
        r = _call(m, prompt, api_key)
        if r.status_code == 404:      # model not available on this key → try the next one
            continue
        if r.status_code >= 300:
            raise RuntimeError(f"Gemini failed ({r.status_code}): {r.text[:300]}")
        data = r.json() or {}
        try:
            txt = data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:
            raise RuntimeError(f"Gemini returned no content: {json.dumps(data)[:300]}")
        try:
            out = json.loads(txt)
        except Exception:
            raise RuntimeError(f"Gemini returned non-JSON: {txt[:300]}")
        out["_model"] = m
        return out
    raise RuntimeError(f"None of these Gemini models are available on this key: {', '.join(tried)}")


def apply_answer(words, answer):
    """Apply a model answer to a COPY of `words`, dropping anything unsafe.

    Returns (new_words, changes, dropped) where `changes` is a human-readable diff the UI
    shows before the user accepts it.
    """
    out = [dict(w) for w in (words or [])]
    n = len(out)
    changes, dropped = [], []

    for f in (answer or {}).get("fix", []) or []:
        try:
            i = int(f.get("i"))
        except Exception:
            dropped.append(f"fix without a valid index: {f}")
            continue
        new = str(f.get("text", "")).strip()
        if not (0 <= i < n):
            dropped.append(f"fix index {i} out of range")
            continue
        if not new or " " in new or "\t" in new or len(new) > MAX_REPL_LEN:
            dropped.append(f"fix {i}: '{new}' is not a single token")   # would break word timings
            continue
        old = _word_of(out[i])
        if new == old:
            continue
        _set_word(out[i], new)
        changes.append({"type": "fix", "i": i, "from": old, "to": new,
                        "why": str(f.get("why", ""))[:120]})

    brk = set()
    for b in (answer or {}).get("break_after", []) or []:
        try:
            i = int(b)
        except Exception:
            continue
        if 0 <= i < n - 1:          # a break after the last word means nothing
            brk.add(i)
        else:
            dropped.append(f"break_after {b} out of range")
    for i, w in enumerate(out):
        if i in brk:
            if w.get("brk") != "soft":
                changes.append({"type": "break", "i": i, "after": _word_of(w)})
            w["brk"] = "soft"
        elif w.get("brk") == "soft":
            w.pop("brk", None)      # the pass owns the breaks it proposes
            changes.append({"type": "unbreak", "i": i, "after": _word_of(w)})

    return out, changes, dropped


def polish(words, lang="en", max_chars=15, max_lines=2, rules=None, api_key=None, model=None):
    """One call: ask + apply. Returns a dict ready for the UI."""
    answer = ask(words, lang=lang, max_chars=max_chars, max_lines=max_lines,
                 rules=rules, api_key=api_key, model=model)
    new_words, changes, dropped = apply_answer(words, answer)
    return {"words": new_words, "changes": changes, "dropped": dropped,
            "rules": (answer.get("rules") or [])[:20], "model": answer.get("_model")}
