# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Assembler.app (thin bundle, Apple Silicon / arm64).
#
# What gets frozen: ONLY packaging/launcher.py + the runtime (Python, the pip
# deps, ffmpeg, a code baseline). The app code is reloaded from ~/.assembler/code
# at launch, so the engine/server imports are listed as hiddenimports below
# (PyInstaller can't discover them statically — they're imported at runtime).
import os
from PyInstaller.utils.hooks import collect_all

REPO = os.path.dirname(os.path.abspath(SPECPATH))           # captions_studio/
PKG = os.path.join(REPO, "packaging")
STAGED_CODE = os.path.join(PKG, "build", "code")            # snapshot made by build_app.sh
VENDOR_BIN = os.path.join(PKG, "vendor", "bin")             # ffmpeg + ffprobe (arm64)
PRECONFIG = os.path.join(PKG, "preconfig.json")             # shared keys (git-ignored)

# ---- data shipped inside the .app (Contents/Resources) ----------------------
datas = [
    (os.path.join(PKG, "update.py"), "."),
    (STAGED_CODE, "code"),
    (VENDOR_BIN, "bin"),
]
if os.path.exists(PRECONFIG):
    datas.append((PRECONFIG, "."))

# ---- libraries the runtime needs (imported by the code loaded at runtime) ---
hiddenimports = [
    "server.app",
    "fastapi", "starlette", "pydantic", "pydantic_core",
    "uvicorn", "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
    "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan", "uvicorn.lifespan.on",
    "anyio", "sniffio", "h11", "click",
    "multipart", "requests",
    "PIL", "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont",
    "faster_whisper", "ctranslate2", "tokenizers", "huggingface_hub",
]

binaries = []
for pkg in ("faster_whisper", "ctranslate2", "tokenizers", "huggingface_hub", "uvicorn"):
    try:
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hiddenimports += h
    except Exception:
        pass

block_cipher = None

a = Analysis(
    [os.path.join(PKG, "launcher.py")],
    pathex=[REPO],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy.tests"],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name="Assembler", debug=False, strip=False, upx=False,
    console=False, target_arch="arm64",
)
coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False, name="Assembler",
)
app = BUNDLE(
    coll,
    name="Assembler.app",
    icon=os.path.join(PKG, "icon.icns") if os.path.exists(os.path.join(PKG, "icon.icns")) else None,
    bundle_identifier="com.welltech.assembler",
    info_plist={
        "CFBundleName": "Assembler",
        "CFBundleDisplayName": "Assembler",
        "CFBundleShortVersionString": "1.0.0",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        # No camera/mic/etc. needed. Network is used for localization + updates.
    },
)
