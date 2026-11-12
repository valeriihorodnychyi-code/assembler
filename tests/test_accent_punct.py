"""Punctuation modes (Off / ?! only / All) and ✦ accent words + 🎨 phrase regions."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine import compose, styles as st, subtitles as S


def test_punct_modes():
    c = S._clean_display
    words = ["really?!", "Hello.", "wait...", "U.S.", "go,", "yes!"]
    assert [c(w, False) for w in words] == ["really?!", "Hello", "wait", "U.S.", "go", "yes!"]   # default: ? ! stay
    assert [c(w, "qe") for w in words] == [c(w, False) for w in words]
    assert [c(w, "off") for w in words] == ["really", "Hello", "wait", "U.S.", "go", "yes"]
    assert [c(w, True) for w in words] == words
    assert [c(w, "all") for w in words] == words


def _ws():
    ws = [{"word": w, "start": i * 0.4, "end": i * 0.4 + 0.35}
          for i, w in enumerate("He does military walking every day".split())]
    ws[2]["accent"] = ws[3]["accent"] = True
    return ws


def test_phrase_region_is_its_own_caption():
    base, big = st.normalize({}), st.normalize({"font_size": 120})
    t = compose.build_timeline(_ws(), [{"start": 0, "end": 0.8, "style": base},
                                       {"start": 0.8, "end": 1.6, "style": big},
                                       {"start": 1.6, "end": None, "style": base}])
    caps = {}
    for e, s in t:
        caps[" ".join(w["text"] for l in e["lines"] for w in l)] = s["font_size"]
    assert caps.get("MILITARY WALKING") == 120
    assert all(v == 80 or k == "MILITARY WALKING" for k, v in caps.items())


def test_accent_word_colour():
    style = st.normalize({"karaoke": {"enabled": False}, "accent": {"color": [0, 255, 0, 255]}})
    tagged = compose.build_timeline(_ws(), [{"start": 0, "end": None, "style": style}])
    ev = next(e for e, _ in tagged if any(w["text"] == "MILITARY" for l in e["lines"] for w in l))
    flat = [w for l in ev["lines"] for w in l]
    assert {w["text"]: w["accent"] for w in flat}["MILITARY"] is True
    assert not all(w["accent"] for w in flat), "the caption must also hold a plain word for this test"
    from PIL import Image
    p = tempfile.mktemp(suffix=".png")
    S.render_subtitle_png(ev, p, 1080, 1920, st.resolve_font(style.get("font_name")), style, 1.0)
    cols = {px[:3] for px in Image.open(p).convert("RGBA").getdata() if px[3] == 255}
    assert (0, 255, 0) in cols and (255, 255, 255) in cols


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f(); print("✓", n)
