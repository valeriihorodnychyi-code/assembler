"""Dictation: Scribe → WaveSpeed LLM → English, with the Whisper fallback (all mocked)."""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine import wavespeed as W, transcribe as T
class R:
    def __init__(s, code, data): s.status_code=code; s._d=data
    def json(s): return {"data": s._d}
class Req:
    n=0
    def post(s, url, **k):
        assert url.endswith("wavespeed-ai/any-llm"), url
        assert k["json"]["model"]=="google/gemini-2.5-flash" and "system_prompt" in k["json"]
        return R(200, {"id": "p1", "status": "created"})
    def get(s, url, **k):
        Req.n+=1
        return R(200, {"id":"p1","status":"completed" if Req.n>1 else "processing","outputs":["A woman opens the door."]})
os.environ["WAVESPEED_API_KEY"]="x"
print(W.llm("Жінка відкриває двері", Req(), system=T.DICTATE_SYSTEM, sleep=lambda s: None))
import server.app as A
T.scribe_text=lambda p: {"text":"жінка відкриває двері","language":"uk"}
W.llm=lambda *a, **k: "A woman opens the door."
os.environ["ELEVENLABS_API_KEY"]="y"
print(A._dictate("/tmp/x.wav"))
W.llm=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
T.dictate_english=lambda p, **k: {"text":"Woman opens door","language":"uk"}
print(A._dictate("/tmp/x.wav"))
