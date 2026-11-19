"""✨ Seedance on a PIECE (dates): piece job + one-pass splice back — offline (fake WaveSpeed)."""
import os, subprocess, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CS_WORK_ROOT", tempfile.mkdtemp(prefix="piece_sessions_"))
os.environ["WAVESPEED_API_KEY"] = "test-key"
from fastapi.testclient import TestClient  # noqa: E402
import server.app as A  # noqa: E402
from engine import wavespeed as WS, revoice as RV  # noqa: E402

C = TestClient(A.app)
FID = "piecetest01"


def _ff(*a):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *a], check=True)


def _setup():
    d = os.path.join(A.WORK_ROOT, FID); os.makedirs(d, exist_ok=True)
    _ff("-f", "lavfi", "-i", "color=c=blue:s=360x640:r=25:d=8", "-f", "lavfi", "-i", "sine=f=440:d=8",
        "-pix_fmt", "yuv420p", "-shortest", os.path.join(d, "long.mp4"))
    gen = os.path.join(d, "gen_red.mp4")   # what "Seedance" returns: other size, other fps, 4 s
    _ff("-f", "lavfi", "-i", "color=c=red:s=720x1280:r=24:d=4", "-pix_fmt", "yuv420p", gen)
    return d, gen


def _px(path, t):
    out = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", path, "-frames:v", "1", "-vf", "scale=1:1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return tuple(out[:3])


def test_turbo_estimate():
    assert WS.estimate_turbo(4.0, "720p") == (0.54, 8.0)
    assert WS.estimate_turbo(10.0, "1080p") == (1.53, 20.0)
    assert WS.estimate_turbo(1.0, "720p")[1] == 6.0     # input padded to 2 s, output min 4 s


def test_piece_job_and_splice():
    d, gen = _setup()
    sent = {}
    WS.upload = lambda path, rq, api_key=None: "https://fake/in.mp4"
    def sub(url, prompt, rq, **k):
        sent.update(k, prompt=prompt); return "pp1"
    WS.submit_edit = sub
    WS.result = lambda pid, rq, api_key=None: {"status": "completed", "outputs": ["https://fake/out.mp4"], "error": "", "id": pid}
    import requests as R
    class G:
        status_code = 200
        content = open(gen, "rb").read()
    R.get = lambda u, *a, **k: G()
    jobs = []
    for a, b in ((1.0, 5.0), (5.5, 7.5)):
        r = C.post("/api/regen/piece/start", json={"file_id": FID, "clip": "long.mp4", "a": a, "b": b,
                                                   "prompt": "She says November 23rd."})
        assert r.status_code == 200, r.text
        jobs.append(r.json()["job"])
    done = []
    for j in jobs:
        for _ in range(100):
            s = C.get(f"/api/regen/{j}").json()
            if s["state"] in ("done", "error"):
                break
            time.sleep(0.1)
        assert s["state"] == "done", s
        done.append({"piece": s["piece"], "a": s["a"], "b": s["b"]})
    assert sent["model"] == WS.MODELS["turbo25"] and sent["keep_audio"] is True and sent["resolution"] == "1080p"
    r = C.post("/api/regen/splice", json={"file_id": FID, "clip": "long.mp4", "pieces": done})
    assert r.status_code == 200, r.text
    out = os.path.join(d, r.json()["clip"])
    assert abs(RV.duration(out) - 8.0) < 0.06
    red, blue = _px(out, 3.0), _px(out, 0.5)
    assert red[0] > 200 and red[2] < 60, red
    assert blue[2] > 200 and blue[0] < 60, blue
    assert _px(out, 5.2)[2] > 200 and _px(out, 6.5)[0] > 200 and _px(out, 7.8)[2] > 200


def test_lipsync_and_seedance_with_its_own_sound():
    d, gen = _setup()
    _ff("-y", "-f", "lavfi", "-i", "color=c=red:s=720x1280:r=24:d=4", "-f", "lavfi", "-i", "sine=f=1500:d=4",
        "-pix_fmt", "yuv420p", "-shortest", gen)
    calls = []
    WS.upload = lambda path, rq, api_key=None: "https://fake/" + os.path.basename(path)
    WS.submit_lipsync = lambda v, a, rq, **k: calls.append(("lip", v, a)) or "lp1"
    WS.submit_edit = lambda url, prompt, rq, **k: calls.append(("sd", k["keep_audio"])) or "sd1"
    WS.result = lambda pid, rq, api_key=None: {"status": "completed", "outputs": ["https://fake/out.mp4"], "error": "", "id": pid}
    import requests as R
    class G:
        status_code = 200
        content = open(gen, "rb").read()
    R.get = lambda u, *a, **k: G()
    def run(body):
        r = C.post("/api/regen/piece/start", json=dict({"file_id": FID, "clip": "long.mp4", "prompt": ""}, **body))
        assert r.status_code == 200, r.text
        j = r.json()["job"]
        for _ in range(100):
            s = C.get(f"/api/regen/{j}").json()
            if s["state"] in ("done", "error"):
                return s
            time.sleep(0.1)
    s1 = run({"a": 1.0, "b": 2.5, "model": "lipsync"})
    assert s1["state"] == "done" and calls[0][0] == "lip" and calls[0][2].endswith(".wav") and "audio" not in s1
    assert abs(s1["cost"] - 0.17) < 0.01           # 2 s × $0.084
    s2 = run({"a": 4.0, "b": 6.0, "model": "turbo25", "keep_audio": False, "prompt": "She says December 1st."})
    assert s2["state"] == "done" and calls[1] == ("sd", False) and s2.get("audio")
    r = C.post("/api/regen/splice", json={"file_id": FID, "clip": "long.mp4", "pieces": [
        {"piece": s1["piece"], "a": s1["a"], "b": s1["b"]}, {"piece": s2["piece"], "a": s2["a"], "b": s2["b"], "audio": s2["audio"]}]})
    assert r.status_code == 200, r.text
    out = os.path.join(d, r.json()["clip"])
    assert abs(RV.duration(out) - 8.0) < 0.08


def test_piece_limits():
    _setup()
    r = C.post("/api/regen/piece/start", json={"file_id": FID, "clip": "long.mp4", "a": 0, "b": 0.5, "prompt": "x"})
    assert r.status_code == 400


if __name__ == "__main__":
    n = 0
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f(); n += 1; print("✓", k)
    print(f"{n}/{n} passed")
