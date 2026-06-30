"""Entry point baked into Assembler.app.

This is the ONLY Python that PyInstaller freezes. Everything it does on launch:

  1. Find the bundled resources (ffmpeg/ffprobe, code baseline, preconfigured keys).
  2. Put bundled ffmpeg/ffprobe first on PATH so the engine finds them.
  3. Write ~/.assembler/config.json with the shared keys (once, if missing).
  4. Seed / auto-update the swappable CODE into ~/.assembler/code (healthcheck-gated).
  5. Point the library at a persistent folder OUTSIDE the code dir (survives updates).
  6. Start the local server (which opens the browser) from the live code.
  7. In the background, make sure Whisper 'small' + 'medium' are downloaded once.

A colleague's Mac needs NO Python, NO ffmpeg, NO brew — all of it is in the .app.
"""
import os
import sys
import json
import threading
import datetime

_LOCK_FH = None  # held for the process lifetime to keep the single-instance lock

ASSEMBLER_HOME = os.path.join(os.path.expanduser("~"), ".assembler")
CONFIG_PATH = os.path.join(ASSEMBLER_HOME, "config.json")
LOG_PATH = os.path.join(ASSEMBLER_HOME, "launch.log")
MODELS_MARK = os.path.join(ASSEMBLER_HOME, "models_ready")
DEFAULT_LIBRARY = os.path.join(os.path.expanduser("~"), "Documents", "Assembler", "library")
# The exact folder name to create on the shared Google Drive. Auto-detection
# looks for this name; keep it in sync with what the team is told to create.
LIBRARY_FOLDER_NAME = "Assembler Library"
WHISPER_MODELS = ("small", "medium")


# ------------------------------------------------------------- bundle paths ---
def resources_dir():
    """Where data files live: PyInstaller (_MEIPASS), py2app (Resources), or dev."""
    if getattr(sys, "frozen", False):
        mei = getattr(sys, "_MEIPASS", None)
        if mei:
            return mei  # PyInstaller
        # py2app: .../Assembler.app/Contents/Resources
        return os.path.normpath(os.path.join(os.path.dirname(sys.executable), "..", "Resources"))
    # Dev mode: running from the repo (this file is packaging/launcher.py)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


RES = resources_dir()
BUNDLED_BIN = os.path.join(RES, "bin")             # ffmpeg + ffprobe live here
BASELINE_CODE = os.path.join(RES, "code")          # shipped code snapshot
PRECONFIG = os.path.join(RES, "preconfig.json")    # shared keys baked at build time


class _Tee:
    def __init__(self, *streams):
        self.streams = streams
    def write(self, s):
        for st in self.streams:
            try:
                st.write(s); st.flush()
            except Exception:
                pass
    def flush(self):
        for st in self.streams:
            try:
                st.flush()
            except Exception:
                pass


def _setup_logging():
    os.makedirs(ASSEMBLER_HOME, exist_ok=True)
    try:
        f = open(LOG_PATH, "a", encoding="utf-8")
        f.write(f"\n==== launch {datetime.datetime.now().isoformat()} ====\n")
        sys.stdout = _Tee(sys.__stdout__, f)
        sys.stderr = _Tee(sys.__stderr__, f)
    except Exception:
        pass


def log(msg):
    print(f"[launch] {msg}", flush=True)


# ------------------------------------------------------------------ steps ---
def inject_ffmpeg_path():
    if os.path.isdir(BUNDLED_BIN):
        os.environ["PATH"] = BUNDLED_BIN + os.pathsep + os.environ.get("PATH", "")
        ff = os.path.join(BUNDLED_BIN, "ffmpeg")
        fp = os.path.join(BUNDLED_BIN, "ffprobe")
        if os.path.exists(ff):
            os.environ["CS_FFMPEG"] = ff
        if os.path.exists(fp):
            os.environ["CS_FFPROBE"] = fp
        log(f"ffmpeg from bundle: {BUNDLED_BIN}")
    else:
        log("no bundled bin/ — relying on system ffmpeg (dev mode)")


def preconfig_keys():
    """Write shared keys to ~/.assembler/config.json once, if the user has none yet."""
    os.makedirs(ASSEMBLER_HOME, exist_ok=True)
    if os.path.exists(CONFIG_PATH):
        return  # never clobber a config the user already has
    if not os.path.exists(PRECONFIG):
        log("no preconfig.json in bundle — user will paste keys in Settings")
        return
    try:
        with open(PRECONFIG, encoding="utf-8") as f:
            data = json.load(f)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        log("wrote shared keys to ~/.assembler/config.json")
    except Exception as e:
        log(f"preconfig failed: {e}")


def detect_drive_library():
    """Find the shared library folder synced by Google Drive for Desktop.

    Looks for a folder literally named 'Assembler Library' under any synced
    Google Drive (Shared drives or My Drive). Returns the first match, else None.
    """
    import glob
    home = os.path.expanduser("~")
    # Drive/Dropbox sync roots (current + legacy mount points).
    roots = (glob.glob(os.path.join(home, "Library", "CloudStorage", "GoogleDrive-*"))
             + glob.glob(os.path.join(home, "Library", "CloudStorage", "Dropbox*"))
             + glob.glob(os.path.join(home, "Google Drive*"))
             + glob.glob(os.path.join(home, "Dropbox*"))
             + ["/Volumes/GoogleDrive"])
    # Look a few levels under Shared drives / My Drive (handles nested folders), then
    # also directly under the sync root, for a folder literally named LIBRARY_FOLDER_NAME.
    mids = ["Shared drives/*", "Shared drives/*/*", "Shared drives/*/*/*",
            "My Drive", "My Drive/*", "My Drive/*/*", "My Drive/*/*/*",
            "", "*", "*/*"]
    for root in roots:
        if not os.path.isdir(root):
            continue
        for mid in mids:
            parts = [p for p in mid.split("/") if p]
            pat = os.path.join(root, *parts, LIBRARY_FOLDER_NAME) if parts else os.path.join(root, LIBRARY_FOLDER_NAME)
            hits = sorted(glob.glob(pat))
            if hits:
                return hits[0]
    return None


def library_dir():
    """Library location, in priority order, always OUTSIDE CODE_HOME so updates
    never wipe it: (1) explicit config, (2) auto-detected Google Drive folder,
    (3) local Documents fallback."""
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, encoding="utf-8") as f:
                ld = json.load(f).get("library_dir")
                if ld:
                    log(f"library: from config -> {ld}")
                    return os.path.expanduser(ld)
    except Exception:
        pass
    drive = detect_drive_library()
    if drive:
        log(f"library: auto-detected Google Drive -> {drive}")
        return drive
    log("library: no config / no Drive folder — using local Documents fallback")
    return DEFAULT_LIBRARY


def prefetch_whisper():
    """Download Whisper 'small' + 'medium' once, quietly, in the background."""
    if os.path.exists(MODELS_MARK):
        return
    try:
        from faster_whisper import WhisperModel
        for size in WHISPER_MODELS:
            log(f"ensuring Whisper model '{size}' (first-run download if needed)…")
            WhisperModel(size, device="cpu", compute_type="int8")
        with open(MODELS_MARK, "w") as f:
            f.write("ok")
        log("Whisper models ready")
    except Exception as e:
        log(f"model prefetch deferred ({type(e).__name__}: {e}) — will retry next launch")


def acquire_single_instance():
    """Hard cap: only ONE Assembler may run. If the lock is already held (another
    instance, or a stray relaunch), we don't start a second server — we just open
    the browser to the existing one and exit. This makes a relaunch-storm impossible.
    """
    global _LOCK_FH
    import fcntl
    try:
        os.makedirs(ASSEMBLER_HOME, exist_ok=True)
        _LOCK_FH = open(os.path.join(ASSEMBLER_HOME, "app.lock"), "w")
        fcntl.flock(_LOCK_FH, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def run_server(code_home):
    if code_home not in sys.path:
        sys.path.insert(0, code_home)
    os.chdir(code_home)
    import server.app as appmod
    appmod.main()


# ------------------------------------------------------------------- main ---
def main():
    _setup_logging()
    if not acquire_single_instance():
        log("another Assembler instance is already running — opening browser and exiting")
        try:
            import webbrowser
            port = int(os.environ.get("CS_PORT", "8765"))
            webbrowser.open(f"http://127.0.0.1:{port}")
        except Exception:
            pass
        return
    log(f"resources: {RES}")
    inject_ffmpeg_path()
    preconfig_keys()

    os.environ["CS_LIBRARY_DIR"] = library_dir()
    os.makedirs(os.environ["CS_LIBRARY_DIR"], exist_ok=True)
    log(f"library: {os.environ['CS_LIBRARY_DIR']}")

    # Make the updater importable (it sits next to this file in the bundle).
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, RES)
    try:
        import update as updater
    except Exception:
        from packaging import update as updater  # dev-mode fallback

    code_home = updater.ensure_code(BASELINE_CODE)
    log(f"code home: {code_home}")

    # Background: download models without blocking the UI.
    threading.Thread(target=prefetch_whisper, daemon=True).start()

    try:
        run_server(code_home)
    except Exception as e:
        log(f"server failed to start ({type(e).__name__}: {e}); attempting rollback")
        if updater.rollback():
            run_server(updater.CODE_HOME)
        else:
            raise


if __name__ == "__main__":
    # Must be the very first thing: in a frozen app, multiprocessing 'spawn'
    # re-execs this binary. freeze_support() makes those children behave as
    # workers instead of re-running main() (which would relaunch the whole app).
    import multiprocessing
    multiprocessing.freeze_support()
    main()
