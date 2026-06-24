"""Self-update for the thin .app bundle.

Design (the "thin bundle"):
  * The .app ships the heavy RUNTIME (Python + ffmpeg + libraries + Whisper) once.
  * The CODE (engine/, server/, web/, ...) lives in CODE_HOME and is swapped here.

On launch the launcher calls `ensure_code()`:
  1. If CODE_HOME is empty, seed it from the baseline bundled inside the .app.
  2. Try to fetch the latest code zip from UPDATE_URL.
  3. Extract to a temp dir, find the code root (the dir containing server/app.py).
  4. Gate it through tools/healthcheck.py. Only if HEALTH passes do we swap it in.
  5. The previous good copy is kept as <CODE_HOME>.prev for rollback.

Everything here is best-effort and network-guarded: no internet, a bad zip, or a
failing healthcheck all leave the currently-working code untouched.
"""
import os
import sys
import json
import shutil
import zipfile
import tempfile
import subprocess
import urllib.request

# ------------------------------------------------------------------ config ---
# Where the live code runs from (separate from the .app and from user data).
ASSEMBLER_HOME = os.path.join(os.path.expanduser("~"), ".assembler")
CODE_HOME = os.path.join(ASSEMBLER_HOME, "code")

# Public GitHub repo zip of the default branch. No token needed for a PUBLIC repo.
# For a PRIVATE repo, switch to a release-asset URL and add an Authorization header
# in _download() (a token baked into the bundle).
#   Public branch zip:   https://github.com/<owner>/<repo>/archive/refs/heads/main.zip
#   Release asset:       https://github.com/<owner>/<repo>/releases/latest/download/code.zip
UPDATE_URL = os.environ.get(
    "CS_UPDATE_URL",
    "https://github.com/REPLACE_OWNER/REPLACE_REPO/archive/refs/heads/main.zip",
)
UPDATE_TIMEOUT = int(os.environ.get("CS_UPDATE_TIMEOUT", "25"))


def _log(msg):
    print(f"[update] {msg}", flush=True)


def _is_code_root(d):
    return os.path.isfile(os.path.join(d, "server", "app.py"))


def _find_code_root(base):
    """The branch zip wraps everything in a single top folder; find the real root."""
    if _is_code_root(base):
        return base
    entries = [os.path.join(base, e) for e in os.listdir(base)]
    dirs = [e for e in entries if os.path.isdir(e)]
    for d in dirs:
        if _is_code_root(d):
            return d
    # one level deeper, just in case
    for d in dirs:
        for sub in os.listdir(d):
            p = os.path.join(d, sub)
            if os.path.isdir(p) and _is_code_root(p):
                return p
    return None


def _download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "Assembler-Updater"})
    # For a private repo, add: req.add_header("Authorization", "token " + TOKEN)
    with urllib.request.urlopen(req, timeout=UPDATE_TIMEOUT) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def _healthcheck(code_root, python_exe):
    hc = os.path.join(code_root, "tools", "healthcheck.py")
    if not os.path.isfile(hc):
        # No healthcheck shipped — fall back to a minimal import smoke test.
        code = ("import sys; sys.path.insert(0, r'%s');"
                "import server.app" % code_root)
        r = subprocess.run([python_exe, "-c", code], capture_output=True, text=True,
                           cwd=code_root, timeout=120)
        return r.returncode == 0
    r = subprocess.run([python_exe, hc], capture_output=True, text=True,
                       cwd=code_root, timeout=180)
    out = (r.stdout or "") + (r.stderr or "")
    return "HEALTH: ALL GOOD" in out


def _version_of(code_root):
    try:
        with open(os.path.join(code_root, "version.json"), encoding="utf-8") as f:
            return json.load(f).get("version", "?")
    except Exception:
        return "?"


def seed_from_baseline(baseline_dir):
    """First run: copy the code baseline shipped inside the .app into CODE_HOME."""
    if _is_code_root(CODE_HOME):
        return
    if not (baseline_dir and _is_code_root(baseline_dir)):
        _log("no usable baseline to seed from")
        return
    os.makedirs(ASSEMBLER_HOME, exist_ok=True)
    if os.path.exists(CODE_HOME):
        shutil.rmtree(CODE_HOME, ignore_errors=True)
    shutil.copytree(baseline_dir, CODE_HOME)
    _log(f"seeded code from baseline -> v{_version_of(CODE_HOME)}")


def try_update(python_exe=None):
    """Best-effort fetch + healthcheck-gated swap. Returns True if code was updated."""
    python_exe = python_exe or sys.executable
    if "REPLACE_OWNER" in UPDATE_URL:
        _log("UPDATE_URL not configured yet — skipping auto-update")
        return False
    tmp = tempfile.mkdtemp(prefix="assembler_upd_")
    try:
        zpath = os.path.join(tmp, "code.zip")
        _log(f"checking {UPDATE_URL}")
        _download(UPDATE_URL, zpath)
        with zipfile.ZipFile(zpath) as z:
            z.extractall(os.path.join(tmp, "x"))
        new_root = _find_code_root(os.path.join(tmp, "x"))
        if not new_root:
            _log("downloaded zip has no recognizable code root — skipping")
            return False
        new_ver = _version_of(new_root)
        cur_ver = _version_of(CODE_HOME) if _is_code_root(CODE_HOME) else "(none)"
        if not _healthcheck(new_root, python_exe):
            _log(f"candidate v{new_ver} FAILED healthcheck — keeping v{cur_ver}")
            return False
        # Swap in atomically-ish, keeping the previous good copy for rollback.
        prev = CODE_HOME + ".prev"
        staged = CODE_HOME + ".new"
        if os.path.exists(staged):
            shutil.rmtree(staged, ignore_errors=True)
        shutil.copytree(new_root, staged)
        if os.path.exists(prev):
            shutil.rmtree(prev, ignore_errors=True)
        if os.path.exists(CODE_HOME):
            os.rename(CODE_HOME, prev)
        os.rename(staged, CODE_HOME)
        _log(f"updated code v{cur_ver} -> v{new_ver}")
        return True
    except Exception as e:
        _log(f"update skipped ({type(e).__name__}: {e})")
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def rollback():
    """Restore the previous good copy if the current one won't run."""
    prev = CODE_HOME + ".prev"
    if not _is_code_root(prev):
        return False
    broken = CODE_HOME + ".broken"
    shutil.rmtree(broken, ignore_errors=True)
    if os.path.exists(CODE_HOME):
        os.rename(CODE_HOME, broken)
    os.rename(prev, CODE_HOME)
    _log("rolled back to previous good code")
    return True


def ensure_code(baseline_dir):
    """Full launch-time flow: seed if needed, then try to update."""
    seed_from_baseline(baseline_dir)
    try_update()
    return CODE_HOME
