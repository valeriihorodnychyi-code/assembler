"""Jira bridge — offline tests (no network, no keys).

Run from the repo root:   python3 tests/test_jira.py
(or with pytest, if installed:   pytest tests/)

Covers both shapes a real Jira REST v3 can return for the same field: a plain string AND an
ADF document. Testing only strings is exactly how 'dict' object has no attribute 'strip'
reached production in 1.9.62 — keep both.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.update(JIRA_EMAIL="t@welltech.com", JIRA_API_TOKEN="tok", JIRA_SITE="welltech.atlassian.net")
from engine import jira as J  # noqa: E402

ADAM = "https://creatives-assets-storage.adam-admin.asqq.io/Creatives"


# ---------------------------------------------------------------- fixtures
def _p(*nodes):
    return {"type": "paragraph", "content": list(nodes)}


def _t(text, **k):
    return dict({"type": "text", "text": text}, **k)


def _doc(*paras):
    return {"type": "doc", "version": 1, "content": list(paras)}


def make_issue(key, brand, cid, base, size="1080x1920", nsub=True, langs=("EN",), adf=False,
               idea="Var", script="I start the Tai Chi challenge this Monday, September 28th."):
    """A Creative task shaped like the live ones (YCP-8482, WCP-…). adf=True returns every
    rich-text field as an ADF document — how REST v3 actually sends them."""
    name = f"{brand}_{cid}_{base}_WO_EN_tch_YDV_Vid_{idea}_{size}"
    url = f"{ADAM}/{brand}/{cid}/{name}"
    rich = (lambda s: _doc(_p(_t(s)))) if adf else (lambda s: s)
    return {"key": key, "fields": {
        "summary": name, "status": {"name": "In Progress"},
        "customfield_18500": rich(url + "_nSub.mp4") if nsub else None,
        "customfield_10648": rich(url + ".mp4"),
        "customfield_10650": url + "_tmb.png",
        "customfield_10646": rich(name),
        "customfield_10628": float(cid), "customfield_10629": float(base),
        "customfield_12509": [{"value": brand}],
        "customfield_12057": [{"value": lang} for lang in langs],
        "customfield_16873": {"value": "Localized" if len(langs) > 1 else "Not Localized"},
        "customfield_10635": {"value": idea},
        "customfield_12510": [{"value": size}],
        "customfield_16769": rich(script),
    }}


class _R:
    def __init__(self, code, js=None):
        self.status_code, self._js = code, js

    def json(self):
        return self._js


class FakeJira:
    """Answers like Jira: GET /issue/{key}, POST /search/jql by creative id."""
    def __init__(self, issues):
        self.db = {i["key"]: i for i in issues}
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None, **k):
        self.calls.append(("GET", url))
        key = url.rsplit("/", 1)[-1]
        return _R(200, self.db[key]) if key in self.db else _R(404)

    def post(self, url, data=None, headers=None, timeout=None, **k):
        import json
        self.calls.append(("POST", url))
        if not url.endswith("/search/jql"):
            return _R(404)
        cid = int(json.loads(data)["jql"].split("=")[1])
        return _R(200, {"issues": [i for i in self.db.values()
                                   if int(i["fields"]["customfield_10628"]) == cid]})


# ---------------------------------------------------------------- tests
def test_parse_items_accepts_keys_links_and_asset_names():
    items, bad = J.parse_items("YCP-8482, ycp-8483\nhttps://welltech.atlassian.net/browse/WCP-1\n"
                               "YG_101949_99257_WO_EN_tch_VDN_Vid_Res_1080x1080_nSub.mp4  hello")
    got = [(i["kind"], i["value"]) for i in items]
    assert ("key", "YCP-8482") in got and ("key", "YCP-8483") in got and ("key", "WCP-1") in got
    assert ("asset", "YG_101949_99257_WO_EN_tch_VDN_Vid_Res_1080x1080") in got
    assert bad == ["hello"]


def test_resolve_plain_string_fields():
    fake = FakeJira([make_issue("YCP-8482", "YG", 104973, 99257)])
    r = J.resolve("YCP-8482", requests=fake)["items"][0]
    assert r["video"].endswith("_nSub.mp4") and r["has_nsub"] and not r["burned_in"]
    assert r["lang"] == "en" and r["cid"] == 104973 and r["baseid"] == 99257
    assert "September 28th" in r["script"]


def test_resolve_adf_fields__the_1_9_62_bug():
    """Asset name in → search → issue whose rich-text fields are ADF. Used to crash."""
    iss = make_issue("WCP-13001", "WF", 113366, 111780, adf=True,
                     script="I lost the weight before my wedding.")
    res = J.resolve("WF_113366_111780_WO_EN_tch_YDV_Vid_Var_1080x1920", requests=FakeJira([iss]))
    assert not res["errors"], res["errors"]
    r = res["items"][0]
    assert r["script"] == "I lost the weight before my wedding."
    assert r["video"].endswith("_nSub.mp4") and isinstance(r["asset"], str)


def test_adf_text_and_url_extraction():
    d = _doc(_p(_t("Day one,"), {"type": "hardBreak"}, _t("day two.")), _p(_t("Next.")))
    assert J._text(d) == "Day one,\nday two.\nNext."
    assert J._text(None) == "" and J._text({"value": "EN"}) == "EN" and J._text(42) == "42"
    card = _doc(_p({"type": "inlineCard", "attrs": {"url": ADAM + "/YG/1/a.mp4"}}))
    assert J._url(card) == ADAM + "/YG/1/a.mp4"
    assert J._url("no link here") is None and J._url(None) is None


def test_asset_name_picks_exact_size_when_id_is_shared():
    # Var 1080x1920, Res 1080x1080 and Res 1920x1080 share creative id 101949
    issues = [make_issue("YCP-7307", "YG", 101949, 99257, "1080x1920"),
              make_issue("YCP-7308", "YG", 101949, 99257, "1080x1080", idea="Res"),
              make_issue("YCP-7309", "YG", 101949, 99257, "1920x1080", idea="Res")]
    res = J.resolve("YG_101949_99257_WO_EN_tch_YDV_Vid_Res_1080x1080", requests=FakeJira(issues))
    assert [r["key"] for r in res["items"]] == ["YCP-7308"]


def test_key_and_summary_of_same_task_collapse_without_extra_request():
    iss = make_issue("YCP-8482", "YG", 104973, 99257)
    fake = FakeJira([iss])
    res = J.resolve("YCP-8482\t" + iss["fields"]["summary"], requests=fake)
    assert len(res["items"]) == 1
    assert not any(c[0] == "POST" for c in fake.calls), "summary of an already-resolved task re-searched"


def test_old_creative_without_nsub_is_flagged_burned_in():
    r = J.normalize(make_issue("YCP-7649", "YG", 102821, 99257, nsub=False, langs=("EN", "ES")))
    assert r["burned_in"] and r["video"].endswith("1080x1920.mp4")
    assert "вшиті титри" in r["warn"] and "ADAM" in r["warn"]


def test_one_bad_task_does_not_sink_the_batch():
    res = J.resolve("YCP-8482 YCP-9999", requests=FakeJira([make_issue("YCP-8482", "YG", 104973, 99257)]))
    assert len(res["items"]) == 1 and res["errors"][0]["input"] == "YCP-9999"


def test_download_only_from_adam():
    J.check_url(ADAM + "/YG/1/a.mp4")
    for bad in ("https://evil.example.com/a.mp4", "http://creatives-assets-storage.adam-admin.asqq.io/a.mp4",
                "file:///etc/passwd"):
        try:
            J.check_url(bad)
        except J.JiraError:
            continue
        raise AssertionError("allowed: " + bad)


def test_align_to_script_fixes_mishearing_only():
    heard = [{"word": w, "start": i * .4, "end": i * .4 + .3} for i, w in
             enumerate("Its 28 days away and I start the Tie Chi challenge this Monday".split())]
    script = "It's 28 days away, and I start the Tai Chi challenge this Monday."
    fixed, ch = J.align_to_script(heard, script)
    assert len(fixed) == len(heard)
    assert all(a["start"] == b["start"] and a["end"] == b["end"] for a, b in zip(heard, fixed))
    assert [c["to"] for c in ch] == ["It's", "Tai"]
    # a re-dubbed date must never be reverted by a stale task text
    redub = [{"word": w, "start": i * .4, "end": i * .4 + .3} for i, w in
             enumerate("Its 28 days away and I start the Tai Chi challenge this week".split())]
    _, ch2 = J.align_to_script(redub, script + " September 28th.")
    assert not any(c["to"].startswith(("Monday", "September")) for c in ch2)
    # a localized clip against the EN text: untouched
    es = [{"word": w, "start": i * .4, "end": i * .4 + .3} for i, w in
          enumerate("Faltan 28 días y empiezo el reto de Tai Chi este lunes".split())]
    assert J.align_to_script(es, script)[1] == []


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("  ✓", name)
        except Exception as e:  # noqa: BLE001 — report every failure, not just the first
            failed += 1
            print("  ✗", name, "—", type(e).__name__, e)
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
