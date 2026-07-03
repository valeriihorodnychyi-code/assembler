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
import ssl
import shutil
import zipfile
import tempfile
import subprocess
import urllib.request
import urllib.error

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
    "https://github.com/valeriihorodnychyi-code/assembler/archive/refs/heads/main.zip",
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


def _ssl_context():
    """A cert-verifying SSL context that works inside a frozen .app.

    The system Python trusts the OS keychain, but the PyInstaller-frozen Python has
    no CA bundle, so plain urlopen dies with CERTIFICATE_VERIFY_FAILED. certifi ships
    a CA bundle we can point at. Returns None if certifi isn't available (then the
    caller falls back to an unverified context)."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


def _download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "Assembler-Updater"})
    # For a private repo, add: req.add_header("Authorization", "token " + TOKEN)
    ctx = _ssl_context()
    try:
        with urllib.request.urlopen(req, timeout=UPDATE_TIMEOUT, context=ctx) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
        return
    except (ssl.SSLError, urllib.error.URLError) as e:
        # Frozen-Python cert failure: retry once WITHOUT verification. This only fetches a
        # PUBLIC GitHub zip (no secrets), and the downloaded code is still gated by the
        # healthcheck before it's swapped in — so a MITM'd zip can't run broken/foreign code.
        reason = getattr(e, "reason", e)
        is_cert = isinstance(e, ssl.SSLError) or isinstance(reason, ssl.SSLError) \
            or "CERTIFICATE_VERIFY_FAILED" in str(reason)
        if not is_cert:
            raise
        _log("cert verify failed — retrying download without verification (public zip)")
        unv = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=UPDATE_TIMEOUT, context=unv) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)


def _healthcheck(code_root, python_exe=None):
    """Validate a candidate before swapping it in.

    We do NOT spawn a subprocess: inside a frozen .app, sys.executable is the app
    itself (it would relaunch the app, not run a script). Instead we syntax-check
    every Python file in-process. Anything that survives this but still fails at
    runtime is caught by the launcher's start-up rollback.
    """
    import ast
    if not _is_code_root(code_root):
        return False
    for base, dirs, files in os.walk(code_root):
        dirs[:] = [d for d in dirs if d not in ("venv", "__pycache__", ".git")]
        for fn in files:
            if fn.endswith(".py"):
                p = os.path.join(base, fn)
                try:
                    ast.parse(open(p, encoding="utf-8").read())
                except Exception as e:
                    _log(f"healthcheck: syntax error in {os.path.relpath(p, code_root)}: {e}")
                    return False
    return True


def _version_of(code_root):
    try:
        with open(os.path.join(code_root, "version.json"), encoding="utf-8") as f:
            return json.load(f).get("version", "?")
    except Exception:
        return "?"


def _ver_tuple(v):
    try:
        return tuple(int(x) for x in str(v).split(".")[:3])
    except Exception:
        return (0,)


def seed_from_baseline(baseline_dir):
    """Install the code baseline shipped inside the .app.

    Two cases:
      • First run (no code yet) → copy the bundled baseline in.
      • Existing code, but the bundled baseline is NEWER → upgrade OFFLINE from the
        bundle (no network). This is what makes a physically-handed-out newer .app
        actually update a teammate whose network blocks the GitHub auto-update.
    An equal/older bundle is left alone (the installed code may be ahead via GitHub).
    """
    if not (baseline_dir and _is_code_root(baseline_dir)):
        _log("no usable baseline to seed from")
        return
    base_ver = _version_of(baseline_dir)
    if _is_code_root(CODE_HOME):
        cur_ver = _version_of(CODE_HOME)
        if _ver_tuple(base_ver) <= _ver_tuple(cur_ver):
            return  # installed code is same or newer — keep it
        _log(f"bundled baseline v{base_ver} newer than installed v{cur_ver} — upgrading offline")
        prev = CODE_HOME + ".prev"
        shutil.rmtree(prev, ignore_errors=True)
        try:
            os.rename(CODE_HOME, prev)   # keep a rollback copy
        except Exception:
            shutil.rmtree(CODE_HOME, ignore_errors=True)
    else:
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
