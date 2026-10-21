"""Jira bridge: paste a list of tasks, get the videos into a session.

Why Jira and not ADAM for this: every Creative task already carries its asset as ready-made
fields — the clean `_nSub` master, the final file, the thumbnail, the language(s), the size,
the family id and, most valuable of all, the FULL VOICE-OVER TEXT. One request per task, no
filename parsing, nothing to guess. (Localized versions are the exception: they have no Jira
tasks at all — ADAM indexes them by name — so those will come from the ADAM API.)

Auth is the designer's OWN Jira API token, stored per machine in ~/.assembler/config.json.
That is deliberate: tasks carry a security level ("Agencies and Welltech"), and a shared
service token would show a freelancer tasks they cannot see in Jira itself.
"""
import os
import re
import json
import base64
import difflib

DEFAULT_SITE = "welltech.atlassian.net"

# Field ids observed on live Creative tasks (YCP/WCP/MCP, Sept 2026). Overridable from
# config.json ("jira_fields": {...}) because custom-field ids are exactly what an admin
# might change — better a config edit than a code release.
FIELDS = {
    "nsub": "customfield_18500",      # clean master WITHOUT burned-in captions
    "final": "customfield_10648",     # final file (captions burned in)
    "thumb": "customfield_10650",
    "preview": "customfield_10649",
    "asset": "customfield_10646",
    "cid": "customfield_10628",       # creative id (= ADAM folder)
    "baseid": "customfield_10629",    # family / concept id shared by Var, Res, New
    "brand": "customfield_12509",
    "langs": "customfield_12057",     # MULTI-value: one task covers every language
    "localized": "customfield_16873",
    "idea": "customfield_10635",      # New / Var / Res / Aff
    "size": "customfield_12510",
    "script": "customfield_16769",    # full voice-over text
    "script2": "customfield_10652",   # the same text, kept in a second field
    "parent": "customfield_12124",
}

# Files are downloaded only from here. A Jira field is user-editable text, so without this
# anyone who can edit a task could make every designer's machine fetch an arbitrary URL.
ALLOWED_HOSTS = {"creatives-assets-storage.adam-admin.asqq.io"}

_KEY = re.compile(r"^[A-Z][A-Z0-9]{1,9}-\d+$")
_KEY_IN_URL = re.compile(r"(?:/browse/|selectedIssue=)([A-Z][A-Z0-9]{1,9}-\d+)")
# BRAND_creativeId_baseId_..._WxH  (e.g. YG_104973_99257_WO_EN_tch_YDV_Vid_Var_1080x1920)
_ASSET = re.compile(r"^([A-Z]{2,4})_(\d+)_(\d+)_[A-Za-z0-9_]+?_(\d{3,4}x\d{3,4})"
                    r"(?:apl)?(?:_n?Sub|_tmb)?(?:\.(?:mp4|mov|png))?$")


class JiraError(Exception):
    pass


# ---------------------------------------------------------------- config

def _cfg_path():
    return os.path.join(os.path.expanduser("~"), ".assembler", "config.json")


def load_creds():
    """(site, email, token) — env first (tests / CI), then the per-machine config."""
    site = os.environ.get("JIRA_SITE", "")
    email = os.environ.get("JIRA_EMAIL", "")
    token = os.environ.get("JIRA_API_TOKEN", "")
    if not (email and token):
        try:
            with open(_cfg_path(), encoding="utf-8") as f:
                c = json.load(f) or {}
            site = site or c.get("jira_site", "")
            email = email or c.get("jira_email", "")
            token = token or c.get("jira_api_token", "")
        except Exception:
            pass
    return (site or DEFAULT_SITE).replace("https://", "").replace("http://", "").strip("/"), email, token


def fields_map():
    m = dict(FIELDS)
    try:
        with open(_cfg_path(), encoding="utf-8") as f:
            m.update((json.load(f) or {}).get("jira_fields") or {})
    except Exception:
        pass
    return m


def configured():
    _, e, t = load_creds()
    return bool(e and t)


# ---------------------------------------------------------------- input parsing

def parse_items(text):
    """Whatever the designer pasted → [{"kind": "key"|"asset", "value": ...}, ...].

    Accepts task keys (YCP-8482), task links, and asset names (= task summaries, which ARE
    the ADAM filenames) — mixed, one per line or separated by commas/spaces/tabs. Copying a
    row from a Jira list often brings BOTH the key and the summary of the same task; those
    collapse to one item later, once both resolve to the same issue.
    """
    out, seen, bad = [], set(), []
    for tok in re.split(r"[\s,;]+", str(text or "")):
        tok = tok.strip().strip("()[]<>\"'")
        if not tok:
            continue
        m = _KEY_IN_URL.search(tok)
        if m:
            item = ("key", m.group(1))
        elif _KEY.match(tok.upper()) and "_" not in tok:
            item = ("key", tok.upper())
        else:
            base = os.path.basename(tok.split("?")[0])
            am = _ASSET.match(base)
            if not am:
                if len(tok) > 2:
                    bad.append(tok)
                continue
            name = re.sub(r"(?:_n?Sub|_tmb)?(?:\.(?:mp4|mov|png))?$", "", base)
            item = ("asset", name)
        if item not in seen:
            seen.add(item)
            out.append({"kind": item[0], "value": item[1]})
    return out, bad


# ---------------------------------------------------------------- HTTP

def _headers(email, token):
    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    return {"Authorization": f"Basic {auth}", "Accept": "application/json",
            "Content-Type": "application/json"}


def _get_issue(key, site, email, token, fields, requests):
    r = requests.get(f"https://{site}/rest/api/3/issue/{key}",
                     params={"fields": ",".join(fields)},
                     headers=_headers(email, token), timeout=20)
    if r.status_code == 401:
        raise JiraError("Jira не прийняла токен (401) — перевір email і API token у ⚙ Settings")
    if r.status_code == 404:
        # Jira answers 404 both for "no such task" and "you can't see it" — say both
        raise JiraError(f"{key}: таску не знайдено або немає доступу")
    if r.status_code != 200:
        raise JiraError(f"{key}: Jira {r.status_code}")
    return r.json()


def _search(jql, site, email, token, fields, requests, limit=20):
    body = {"jql": jql, "fields": list(fields), "maxResults": limit}
    # /search/jql is the current endpoint; the old /search is being retired by Atlassian,
    # so fall back to it only if this site doesn't have the new one yet.
    for path in ("/rest/api/3/search/jql", "/rest/api/3/search"):
        r = requests.post(f"https://{site}{path}", data=json.dumps(body),
                          headers=_headers(email, token), timeout=20)
        if r.status_code in (404, 410):
            continue
        if r.status_code == 401:
            raise JiraError("Jira не прийняла токен (401) — перевір email і API token у ⚙ Settings")
        if r.status_code != 200:
            raise JiraError(f"пошук у Jira: {r.status_code}")
        return r.json().get("issues") or []
    raise JiraError("пошук у Jira недоступний")


# ---------------------------------------------------------------- normalization

def _val(v):
    """Jira option fields come as {"value": x}, lists of them, or plain scalars."""
    if v is None:
        return None
    if isinstance(v, dict):
        return v.get("value", v.get("name"))
    return v


def _vals(v):
    if v is None:
        return []
    if isinstance(v, list):
        return [x for x in (_val(i) for i in v) if x]
    x = _val(v)
    return [x] if x else []


def _num(v):
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return None


# Jira REST v3 returns every RICH-TEXT field (multi-line text, description) as an ADF document
# — {"type":"doc","content":[{"type":"paragraph","content":[{"type":"text","text":"…"}]}]} —
# not as a string. Plain-text and URL fields stay strings. Which one a custom field is depends
# on how an admin configured it, so every text-ish field goes through _text(): it accepts a
# string, an ADF document, an option object or a list, and always returns a plain string.
_ADF_BLOCKS = {"paragraph", "heading", "blockquote", "codeBlock", "listItem", "tableRow",
               "rule", "panel", "mediaSingle", "bulletList", "orderedList"}


def _adf_text(node):
    if isinstance(node, list):
        return "".join(_adf_text(n) for n in node)
    if not isinstance(node, dict):
        return ""
    t = node.get("type")
    if t == "text":
        return node.get("text", "")
    if t == "hardBreak":
        return "\n"
    if t in ("mention", "emoji", "status"):
        a = node.get("attrs") or {}
        return a.get("text") or a.get("shortName") or ""
    if t in ("inlineCard", "blockCard"):
        return (node.get("attrs") or {}).get("url", "")
    inner = _adf_text(node.get("content") or [])
    return inner + ("\n" if t in _ADF_BLOCKS else "")


def _text(v):
    """Any Jira field value → plain text (never a dict, never None)."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return ", ".join(x for x in (_text(i) for i in v) if x)
    if isinstance(v, dict):
        if v.get("type") == "doc" or "content" in v:
            return re.sub(r"\n{3,}", "\n\n", _adf_text(v)).strip()
        for k in ("value", "name", "url", "href", "text"):
            if isinstance(v.get(k), (str, int, float)):
                return str(v[k])
    return ""


_URL = re.compile(r"https://[^\s<>\"')\]]+")


def _url(v):
    """A URL field, or a rich-text field that holds one → the first https URL in it, or None."""
    t = _text(v).strip()
    if not t:
        return None
    m = _URL.search(t)
    return m.group(0) if m else None


def normalize(issue, site=DEFAULT_SITE, fmap=None):
    """A raw Jira issue → the record Assembler works with."""
    fm = fmap or FIELDS
    f = issue.get("fields") or {}
    g = lambda k: f.get(fm[k])
    nsub, final = _url(g("nsub")), _url(g("final"))
    langs = [x for x in (_text(v) for v in (g("langs") if isinstance(g("langs"), list) else [g("langs")])) if x]
    script = (_text(g("script")) or _text(g("script2"))).strip()
    summary = _text(f.get("summary")).strip()
    rec = {
        "key": _text(issue.get("key")),
        "link": f"https://{site}/browse/{_text(issue.get('key'))}",
        "asset": _text(g("asset")).strip() or summary,
        "summary": summary,
        "video": nsub or final or None,
        "has_nsub": bool(nsub),
        # no clean master → the only file has captions burned in; putting ours on top would
        # print two caption tracks, so the UI must say so instead of silently using it
        "burned_in": (not nsub) and bool(final),
        "thumb": _url(g("thumb")) or _url(g("preview")),
        "langs": langs,
        "lang": (langs[0].lower() if langs else ""),
        "localized": (_text(g("localized")) == "Localized"),
        "size": (_vals(g("size")) or [""])[0],
        "cid": _num(_text(g("cid"))),
        "baseid": _num(_text(g("baseid"))),
        "brand": _text((_vals(g("brand")) or [""])[0]),
        "idea": _text(g("idea")),
        "parent": _text(g("parent")).strip(),
        "script": script,
        "status": _text(f.get("status")),
        "warn": "",
    }
    if rec["burned_in"]:
        rec["warn"] = "немає чистого _nSub — у файлі вже вшиті титри, свої зверху не клади"
    elif not rec["video"]:
        rec["warn"] = "у таски ще немає завантаженого відео"
    if len(langs) > 1:
        rec["warn"] = (rec["warn"] + "; " if rec["warn"] else "") + \
            f"локалізовано в {len(langs)} мов — локалізовані файли прийдуть з ADAM"
    return rec


# ---------------------------------------------------------------- resolve

def resolve(text, requests=None):
    """Pasted text → {"items": [record...], "errors": [...], "unrecognized": [...]}.

    Metadata only, nothing downloaded — fast enough to run on 50 tasks in seconds.
    """
    if requests is None:
        import requests  # noqa: F811 — lazy, and injectable for tests
    site, email, token = load_creds()
    if not (email and token):
        raise JiraError("Jira не налаштована: додай email і API token у ⚙ Settings")
    fm = fields_map()
    want = ["summary", "status"] + list(fm.values())
    items, unrec = parse_items(text)
    out, errors, by_key, by_name = [], [], {}, set()
    for it in items:
        try:
            # A row copied from a Jira list brings the key AND the summary of one task; once
            # the key resolved, its summary needs no second round-trip.
            if it["kind"] == "asset" and it["value"] in by_name:
                continue
            if it["kind"] == "key":
                issue = _get_issue(it["value"], site, email, token, want, requests)
            else:
                name = it["value"]
                m = _ASSET.match(name)
                cid = m.group(2) if m else None
                # The creative id is not unique on its own: Var 1080x1920, Res 1080x1080 and
                # Res 1920x1080 share it. Search by id (exact, indexed), then pick the task
                # whose summary is EXACTLY this name — text search tokenizes on "_".
                jql = (f'cf[{fm["cid"].split("_")[-1]}] = {cid}' if cid
                       else f'summary ~ "\\"{name}\\""')
                hits = _search(jql, site, email, token, want, requests)
                exact = [h for h in hits if _text((h.get("fields") or {}).get("summary")).strip() == name]
                if not exact:
                    raise JiraError(f"{name}: таски з такою назвою не знайдено")
                issue = exact[0]
            rec = normalize(issue, site, fm)
            if rec["key"] in by_key:          # key + summary of the same task pasted together
                continue
            by_key[rec["key"]] = rec
            by_name.update(n for n in (rec["summary"], rec["asset"]) if n)
            out.append(rec)
        except JiraError as e:
            errors.append({"input": it["value"], "error": str(e)})
        except Exception as e:  # network etc. — one bad line must not sink the batch
            errors.append({"input": it["value"], "error": f"{type(e).__name__}: {e}"})
    return {"items": out, "errors": errors, "unrecognized": unrec}


# ---------------------------------------------------------------- download

def safe_filename(url):
    from urllib.parse import urlparse, unquote
    name = os.path.basename(unquote(urlparse(url).path)) or "clip.mp4"
    return re.sub(r"[^\w.\-]+", "_", name)


def check_url(url):
    from urllib.parse import urlparse
    u = urlparse(url or "")
    if u.scheme != "https" or u.hostname not in ALLOWED_HOSTS:
        raise JiraError(f"відмовляюсь качати не з ADAM: {u.hostname or url}")


def download(url, dest_dir, requests=None, chunk=1 << 20):
    """Stream one ADAM file into the session. Written to a .part first, so a dropped
    connection never leaves a truncated MP4 that looks complete."""
    if requests is None:
        import requests  # noqa: F811
    check_url(url)
    name = safe_filename(url)
    dest = os.path.join(dest_dir, name)
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return name                       # same asset already in this session
    tmp = dest + ".part"
    with requests.get(url, stream=True, timeout=(10, 120)) as r:
        if r.status_code in (403, 404):
            # this bucket hides listing, so a missing key answers 403 — not 404
            raise JiraError(f"файла немає в ADAM: {name}")
        if r.status_code != 200:
            raise JiraError(f"ADAM {r.status_code}: {name}")
        with open(tmp, "wb") as out:
            for part in r.iter_content(chunk):
                if part:
                    out.write(part)
    os.replace(tmp, dest)
    return name


# ---------------------------------------------------------------- script alignment

def _norm(w):
    return re.sub(r"[^\w']+", "", str(w).lower())


def align_to_script(words, script, min_ratio=0.6, min_word_sim=0.5):
    """Fix MISHEARD words using the voice-over text from Jira. Timings are never touched.

    The transcript knows WHEN each word is said; the Jira text knows WHAT is said. Align
    the two word sequences and, where they disagree one-for-one, take the word from the
    script and keep the recogniser's timing. Deliberately conservative:

      * whole-text similarity below `min_ratio` → do nothing. That is the guard for a
        localized clip (ES audio vs EN script) and for a script that no longer matches the
        file — without it, "aligning" Spanish to English would overwrite the Spanish.
      * only 1-for-1 substitutions are applied; where the counts differ the timing of the
        new words would be a guess, so the transcript is left as it is.
      * each pair must look alike (`min_word_sim`). "Tie"→"Tai" is a mishearing; "this
        week"→"September 28th" is a CHANGED line (a re-dubbed date), and a stale Jira text
        must never revert the file's real words.

    Returns (new_words, changes) — the word count is identical to the input.
    """
    if not words or not script:
        return list(words or []), []
    key = "word" if "word" in words[0] else "text"
    heard = [_norm(w.get(key, "")) for w in words]
    told_raw = str(script).split()
    told = [_norm(t) for t in told_raw]
    sm = difflib.SequenceMatcher(a=heard, b=told, autojunk=False)
    if sm.ratio() < min_ratio:
        return list(words), []
    out = [dict(w) for w in words]
    changes = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != "replace" or (i2 - i1) != (j2 - j1):
            continue
        for k in range(i2 - i1):
            a, b = heard[i1 + k], told[j1 + k]
            if not a or not b or a == b:
                continue
            if difflib.SequenceMatcher(a=a, b=b).ratio() < min_word_sim:
                continue
            old = out[i1 + k].get(key, "")
            new = told_raw[j1 + k]
            out[i1 + k][key] = new
            changes.append({"index": i1 + k, "from": old, "to": new})
    return out, changes
