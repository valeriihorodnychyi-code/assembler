#!/usr/bin/env python3
"""Assembler health-check. Run after any change to confirm nothing is broken:
    python3 tools/healthcheck.py
Checks: Python modules compile, JS in index.html parses (if node present), and the
key API endpoints respond (incl. the caption-render path through the real engine)."""
import ast
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
ok = True


def mark(good, label, extra=""):
    global ok
    ok = ok and good
    print(("  OK   " if good else "  FAIL ") + label + ((" — " + extra) if extra else ""))


print("== compile python ==")
for f in ["engine/compose.py", "engine/subtitles.py", "engine/styles.py",
          "engine/library.py", "engine/dub.py", "engine/heygen.py",
          "engine/localize.py", "server/app.py"]:
    try:
        ast.parse(open(os.path.join(ROOT, f), encoding="utf-8").read())
        mark(True, f)
    except Exception as e:
        mark(False, f, str(e))

print("== parse frontend JS ==")
if shutil.which("node"):
    html = open(os.path.join(ROOT, "web/index.html"), encoding="utf-8").read()
    code = "\n;\n".join(re.findall(r"<script[^>]*>(.*?)</script>", html, re.S))
    tmp = "/tmp/_assembler_js_check.js"
    open(tmp, "w").write(code)
    r = subprocess.run(["node", "--check", tmp], capture_output=True, text=True)
    mark(r.returncode == 0, "web/index.html JS", r.stderr.strip()[:200])
else:
    print("  ..   node not found, skipping JS parse")

print("== API endpoints ==")
try:
    from server.app import app
    from starlette.testclient import TestClient
    c = TestClient(app)
    mark(c.get("/").status_code == 200, "GET /  (app shell)")
    info = c.get("/api/info")
    mark(info.status_code == 200, "GET /api/info")
    for ep in ["/api/styles", "/api/settings", "/api/library"]:
        mark(c.get(ep).status_code == 200, "GET " + ep)
    # caption-render path: subtitles.build_events + Pillow draw + manual line break
    style = info.json().get("default_style", {})
    pf = c.post("/api/preview_frame", json={"style": style,
        "words": [{"word": "HELLO", "start": 0, "end": 0.5},
                  {"word": "WORLD", "start": 0.5, "end": 1.0, "brk": "line"}],
        "time": 0.2, "format": "9:16", "dur": 1.0})
    mark(pf.status_code in (200, 204), "POST /api/preview_frame (caption render)", str(pf.status_code))
except Exception as e:
    mark(False, "API smoke", str(e))

print("\nHEALTH:", "ALL GOOD ✅" if ok else "ISSUES ❌")
sys.exit(0 if ok else 1)
