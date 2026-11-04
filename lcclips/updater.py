"""In-app updates from GitHub Releases (same idea as LCBot's update check, plus
one-click install): find the latest release, download its LCClips-Setup-*.exe,
verify it, run it silently and let the installer relaunch LCClips."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import BUILD, __version__

log = logging.getLogger(__name__)

REPO = "Lilcrash15/LCClips"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?")
_ASSET_RE = re.compile(r"^LCClips-Setup-.*\.exe$", re.I)
_HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": f"LCClips/{__version__}"}


class UpdateError(RuntimeError):
    pass


@dataclass
class UpdateInfo:
    version: str  # tag, e.g. "v0.3.0"
    notes: str
    page_url: str
    asset_url: str
    asset_name: str
    size: int
    sha256: str | None  # GitHub's asset digest, when available
    rebuild: bool = False  # same version number, newer build (release re-published)


def parse_version(text: str) -> tuple[int, ...]:
    m = _VERSION_RE.search(text or "")
    return tuple(int(g) for g in m.groups() if g is not None) if m else (0,)


def _release_build(data: dict, timeout: float) -> str | None:
    """Build ID published next to the installer (LCClips-build.json), if any."""
    a = next((a for a in data.get("assets", []) if a.get("name") == "LCClips-build.json"), None)
    if not a:
        return None
    try:
        req = urllib.request.Request(a["browser_download_url"], headers={"User-Agent": _HEADERS["User-Agent"]})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return str(json.load(r).get("build") or "") or None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def check(current: str = __version__, timeout: float = 10.0, build: str = BUILD) -> UpdateInfo | None:
    """Newer release with an installer attached, or None if up to date.
    Raises UpdateError when the check itself fails (callers decide whether to show it)."""
    try:
        req = urllib.request.Request(LATEST_RELEASE_URL, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise UpdateError("No releases published yet" if e.code == 404 else f"GitHub returned {e.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise UpdateError(f"Couldn't reach GitHub ({getattr(e, 'reason', e)})") from None

    tag = data.get("tag_name") or ""
    rebuild = False
    if parse_version(tag) < parse_version(current):
        return None
    if parse_version(tag) == parse_version(current):
        # Same version: only an update if the release was re-published with a different build.
        remote = _release_build(data, timeout)
        if build == "dev" or not remote or remote == build:
            return None
        rebuild = True
    asset = next((a for a in data.get("assets", []) if _ASSET_RE.match(a.get("name", ""))), None)
    if not asset:
        # Release published but the build hasn't attached the installer yet.
        return None
    digest = asset.get("digest") or ""
    return UpdateInfo(
        version=tag,
        notes=(data.get("body") or "").strip(),
        page_url=data.get("html_url") or RELEASES_PAGE,
        asset_url=asset["browser_download_url"],
        asset_name=asset["name"],
        size=int(asset.get("size") or 0),
        sha256=digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else None,
        rebuild=rebuild,
    )


def download(info: UpdateInfo, progress: Callable[[float], None] | None = None) -> Path:
    dest_dir = Path(tempfile.gettempdir()) / "LCClips-update"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / info.asset_name
    part = dest.with_suffix(".part")
    h = hashlib.sha256()
    done = 0
    try:
        req = urllib.request.Request(info.asset_url, headers={"User-Agent": _HEADERS["User-Agent"]})
        with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or info.size or 0)
            while chunk := r.read(1 << 20):
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress and total:
                    progress(min(1.0, done / total))
    except (urllib.error.URLError, OSError) as e:
        part.unlink(missing_ok=True)
        raise UpdateError(f"Download failed ({getattr(e, 'reason', e)})") from None
    if info.size and done != info.size:
        part.unlink(missing_ok=True)
        raise UpdateError(f"Download incomplete ({done} of {info.size} bytes)")
    if info.sha256 and h.hexdigest() != info.sha256:
        part.unlink(missing_ok=True)
        raise UpdateError("Downloaded installer failed its integrity check")
    part.replace(dest)
    return dest


def can_self_update() -> bool:
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def launch_installer(installer: Path) -> None:
    """Starts the installer silently, detached from us. The caller should quit
    right after; the installer relaunches LCClips when it's done."""
    DETACHED_PROCESS, CREATE_NEW_PROCESS_GROUP = 0x00000008, 0x00000200
    subprocess.Popen(
        [str(installer), "/SILENT", "/SP-", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"],
        close_fds=True, creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
    )
