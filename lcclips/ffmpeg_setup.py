"""One-click FFmpeg install: downloads a static Windows build (BtbN, GPL, includes
ddagrab + NVENC/AMF/QSV) and extracts just ffmpeg.exe into %APPDATA%/LCClips/bin."""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from . import __version__
from .config import APP_DIR

log = logging.getLogger(__name__)

INSTALL_DIR = APP_DIR / "bin"
RELEASE_API = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/tags/latest"
FALLBACK_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
# e.g. ffmpeg-n8.0-latest-win64-gpl-8.0.zip (also tolerates n8.0.1 / -8.0.1)
_RELEASE_RE = re.compile(r"^ffmpeg-n(\d+)\.(\d+)(?:\.\d+)?-latest-win64-gpl-[\d.]+\.zip$")
_HEADERS = {"User-Agent": f"LCClips/{__version__}"}


def pick_download_url(assets: list[dict]) -> str:
    """Newest stable release branch (>= 7.1), else the master build."""
    best: tuple[tuple[int, int], str] | None = None
    for a in assets:
        m = _RELEASE_RE.match(a.get("name", ""))
        if m:
            ver = (int(m[1]), int(m[2]))
            if ver >= (7, 1) and (best is None or ver > best[0]):
                best = (ver, a["browser_download_url"])
    return best[1] if best else FALLBACK_URL


def resolve_url() -> str:
    try:
        req = urllib.request.Request(RELEASE_API, headers={**_HEADERS, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return pick_download_url(json.load(r).get("assets", []))
    except Exception as e:
        log.warning("GitHub API lookup failed (%s); using fallback URL", e)
        return FALLBACK_URL


def extract_ffmpeg(zip_path: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        member = next((n for n in z.namelist() if n.lower().endswith("bin/ffmpeg.exe")), None)
        if not member:
            raise RuntimeError("ffmpeg.exe not found in the downloaded archive")
        out = dest_dir / "ffmpeg.exe"
        tmp = out.with_suffix(".exe.part")
        with z.open(member) as src, open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        tmp.replace(out)
    return out


def install(progress: Callable[[float], None] | None = None, dest_dir: Path | None = None) -> str:
    """Download + extract. Returns the path to ffmpeg.exe. Blocking; run off the UI thread."""
    dest_dir = dest_dir or INSTALL_DIR
    url = resolve_url()
    log.info("Downloading FFmpeg from %s", url)
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dest_dir / "ffmpeg-download.zip"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=_HEADERS), timeout=60) as r, \
                open(zip_path, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while chunk := r.read(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done / total)
        exe = extract_ffmpeg(zip_path, dest_dir)
    finally:
        zip_path.unlink(missing_ok=True)
    flags = 0x08000000 if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    r = subprocess.run([str(exe), "-hide_banner", "-version"], capture_output=True, text=True, creationflags=flags)
    if r.returncode != 0:
        raise RuntimeError("Downloaded FFmpeg failed to run")
    log.info("FFmpeg installed: %s", r.stdout.splitlines()[0] if r.stdout else exe)
    return str(exe)
