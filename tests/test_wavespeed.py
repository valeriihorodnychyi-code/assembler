"""✨ WaveSpeed / Seedance 2.5 — offline (fake HTTP, no credits).   python3 tests/test_wavespeed.py"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine import wavespeed as WS  # noqa: E402

os.environ["WAVESPEED_API_KEY"] = "test-key"


class R:
    def __init__(self, code, js=None):
        self.status_code, self._js, self.text = code, js or {}, ""

    def json(self):
        return self._js


class Fake:
    def __init__(self):
        self.calls = []

    def post(self, url, headers=None, json=None, files=None, timeout=None):
        self.calls.append(("POST", url, json))
        assert headers["Authorization"] == "Bearer test-key"
        if url.endswith("/media/uploads"):
            return R(200, {"data": {"download_url": "https://cdn/x.mp4", "upload": {"url": "https://put/x", "headers": {"Content-Type": "video/mp4"}}}})
        if "seedance" in url:
            return R(200, {"data": {"id": "p1"}})
        return R(404)

    def put(self, url, headers=None, data=None, timeout=None):
        self.calls.append(("PUT", url, None))
        assert "Authorization" not in (headers or {}), "the key must never go to the storage PUT"
        return R(200)

    def get(self, url, headers=None, timeout=None):
        return R(200, {"data": {"status": "completed", "outputs": ["https://cdn/o.mp4"]}})


def test_estimate_bills_input_plus_output_within_4_to_30s():
    assert WS.estimate(2.0, "720p") == (1.76, 8.0)      # 2 s is billed as 4 s in + 4 s out
    assert WS.estimate(10.0, "1080p") == (11.0, 20.0)
    assert WS.estimate(45.0, "480p") == (6.6, 60.0)


def test_upload_then_submit_keeps_the_original_audio_by_default():
    f = Fake()
    p = os.path.join(tempfile.mkdtemp(), "c.mp4")
    open(p, "wb").write(b"0" * 2000)
    url = WS.upload(p, f)
    assert url == "https://cdn/x.mp4" and [c[0] for c in f.calls] == ["POST", "PUT"]
    assert WS.submit_edit(url, "make it night", f) == "p1"
    body = f.calls[-1][2]
    assert body["generate_audio"] is False and body["video"] == url and body["resolution"] == "720p"
    assert WS.result("p1", f)["outputs"] == ["https://cdn/o.mp4"]


def test_reference_images_go_to_seedance():
    f = Fake()
    WS.submit_edit("https://cdn/x.mp4", "Replace the woman with the person in @Image 1", f, refs=["https://cdn/r1.png"])
    assert f.calls[-1][2]["reference_images"] == ["https://cdn/r1.png"]


def test_empty_prompt_is_refused():
    try:
        WS.submit_edit("u", "  ", Fake())
    except WS.WaveSpeedError:
        return
    raise AssertionError("must fail")


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    bad = 0
    for n, fn in tests:
        try:
            fn()
            print("  ✓", n)
        except Exception as e:  # noqa: BLE001
            bad += 1
            print("  ✗", n, "—", type(e).__name__, e)
    print(f"\n{len(tests) - bad}/{len(tests)} passed")
    sys.exit(1 if bad else 0)
