"""Upload clips to your LCClips share server (server/) and get a link back.

Standard library only. Flow for one clip:
  1. optional smaller upload copy (GPU encoder, below-normal priority)
  2. thumbnail frame
  3. POST /api/upload -> PUT parts (50 MB each) -> PUT thumb -> POST complete
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import __version__
from .capture import _encoder_args, _popen_flags
from .config import APP_DIR

log = logging.getLogger(__name__)

PART_SIZE = 50 * 1024 * 1024  # Workers' request limit is 100 MB; R2 parts must be >= 5 MB
HISTORY_PATH = APP_DIR / "uploads.json"

Progress = Callable[[str, float], None]  # (stage, 0..1)


class ShareError(RuntimeError):
    pass


# --------------------------------------------------------------------------- media prep

def _ff(ffmpeg: str, args: list[str], timeout: float = 600) -> subprocess.CompletedProcess:
    return subprocess.run([ffmpeg, "-hide_banner", *args], stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, errors="replace", timeout=timeout,
                          creationflags=_popen_flags(low_priority=True))


def probe(ffmpeg: str, path: Path) -> tuple[tuple[int, int], float]:
    r = _ff(ffmpeg, ["-i", str(path)], timeout=30)
    d = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
    s = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", r.stderr)
    dur = int(d[1]) * 3600 + int(d[2]) * 60 + float(d[3]) if d else 0.0
    size = (int(s[1]), int(s[2])) if s else (0, 0)
    return size, dur


def make_thumbnail(ffmpeg: str, src: Path, out: Path, duration: float) -> bool:
    t = f"{min(1.0, duration / 2):.2f}"
    r = _ff(ffmpeg, ["-y", "-ss", t, "-i", str(src), "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "4", str(out)], 60)
    return r.returncode == 0 and out.exists()


def make_upload_copy(ffmpeg: str, src: Path, out: Path, encoder: str, mbps: int, duration: float) -> Path:
    """Re-encode to ~mbps for faster uploads. Returns src unchanged if it's already small enough."""
    if mbps <= 0 or duration <= 0:
        return src
    current_mbps = src.stat().st_size * 8 / duration / 1e6
    if current_mbps <= mbps * 1.25:
        return src
    for enc in dict.fromkeys([encoder, "h264_nvenc", "h264_amf", "h264_qsv", "libx264"]):
        args = ["-y", "-i", str(src), "-map", "0", *_encoder_args(enc, 60, mbps),
                "-c:a", "copy", "-movflags", "+faststart", str(out)]
        r = _ff(ffmpeg, args)
        if r.returncode == 0 and out.exists() and out.stat().st_size > 0:
            log.info("Upload copy via %s: %.1f MB -> %.1f MB", enc, src.stat().st_size / 1e6, out.stat().st_size / 1e6)
            return out
    log.warning("Could not make upload copy; uploading original")
    return src


# --------------------------------------------------------------------------- HTTP client

class ShareClient:
    def __init__(self, server_url: str, token: str):
        if not server_url or not token:
            raise ShareError("Share server URL / upload key not set (run server\\setup.bat)")
        self.base = server_url.rstrip("/")
        self.token = token

    def _req(self, method: str, path: str, body: bytes | dict | None = None, timeout: float = 300) -> dict:
        headers = {"Authorization": f"Bearer {self.token}", "User-Agent": f"LCClips/{__version__}"}
        data = None
        if isinstance(body, dict):
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        elif body is not None:
            data = body
            headers["Content-Type"] = "application/octet-stream"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error", "")
            except Exception:
                msg = ""
            raise ShareError(f"Server said {e.code}: {msg or e.reason}") from None
        except urllib.error.URLError as e:
            raise ShareError(f"Can't reach share server: {e.reason}") from None

    def ping(self) -> None:
        self._req("GET", "/api/ping", timeout=15)

    def upload(self, video: Path, thumb: Path | None, meta: dict, progress: Progress | None = None) -> dict:
        size = video.stat().st_size
        start = self._req("POST", "/api/upload", {**meta, "size": size})
        clip_id, upload_id = start["id"], start["uploadId"]
        parts, sent = [], 0
        with open(video, "rb") as f:
            n = 1
            while chunk := f.read(PART_SIZE):
                for attempt in range(3):
                    try:
                        p = self._req("PUT", f"/api/upload/{clip_id}/part/{n}?uploadId={urllib.parse.quote(upload_id)}", chunk)
                        break
                    except ShareError:
                        if attempt == 2:
                            raise
                        time.sleep(2 * (attempt + 1))
                parts.append({"partNumber": p["partNumber"], "etag": p["etag"]})
                sent += len(chunk)
                n += 1
                if progress:
                    progress("Uploading", sent / size)
        if thumb and thumb.exists():
            self._req("PUT", f"/api/upload/{clip_id}/thumb", thumb.read_bytes())
        return self._req("POST", f"/api/upload/{clip_id}/complete", {"uploadId": upload_id, "parts": parts})

    def delete(self, clip_id: str) -> None:
        self._req("DELETE", f"/api/clips/{clip_id}")



# --------------------------------------------------------------------------- history

@dataclass
class Upload:
    id: str
    url: str
    title: str
    file: str
    created: str


def load_history() -> list[Upload]:
    try:
        return [Upload(**u) for u in json.loads(HISTORY_PATH.read_text(encoding="utf-8"))]
    except (OSError, ValueError, TypeError):
        return []


def _save_history(items: list[Upload]) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps([asdict(u) for u in items], indent=2), encoding="utf-8")


def is_expired(item: Upload, expire_days: int) -> bool:
    if expire_days <= 0:
        return False
    try:
        return (datetime.now() - datetime.fromisoformat(item.created)).days >= expire_days
    except ValueError:
        return False


def active_uploads(expire_days: int) -> list[Upload]:
    """Upload history minus links the server's auto-delete rule has already removed."""
    items = load_history()
    live = [u for u in items if not is_expired(u, expire_days)]
    if len(live) != len(items):
        _save_history(live)
    return live


def find_upload(file: Path, expire_days: int = 0) -> Upload | None:
    return next((u for u in active_uploads(expire_days) if Path(u.file) == file), None)


def delete_upload(client: ShareClient, item: Upload) -> None:
    client.delete(item.id)
    _save_history([u for u in load_history() if u.id != item.id])


# --------------------------------------------------------------------------- main entry

def title_from_filename(path: Path) -> str:
    # "Valorant_2026-11-03_21-14-05.mp4" -> "Valorant"
    return re.sub(r"_\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d$", "", path.stem) or "Clip"


def share_clip(ffmpeg: str, server_url: str, token: str, clip: Path, encoder: str = "h264_nvenc",
               upload_mbps: int = 8, progress: Progress | None = None, expire_days: int = 0) -> Upload:
    """Upload `clip` (or return the existing link if it was uploaded before and hasn't expired)."""
    existing = find_upload(clip, expire_days)
    if existing:
        return existing
    client = ShareClient(server_url, token)
    (w, h), dur = probe(ffmpeg, clip)
    with tempfile.TemporaryDirectory(prefix="lcclips-") as td:
        tmp = Path(td)
        if progress:
            progress("Preparing", 0.0)
        video = make_upload_copy(ffmpeg, clip, tmp / "upload.mp4", encoder, upload_mbps, dur)
        thumb = tmp / "thumb.jpg"
        has_thumb = make_thumbnail(ffmpeg, video, thumb, dur)
        meta = {"title": title_from_filename(clip), "width": w, "height": h, "duration": dur}
        res = client.upload(video, thumb if has_thumb else None, meta, progress)
    item = Upload(id=res["id"], url=res["url"], title=meta["title"], file=str(clip),
                  created=datetime.now().isoformat(timespec="seconds"))
    _save_history([item, *load_history()])
    return item
