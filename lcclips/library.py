"""Clip library: scans the clips folder, caches per-clip metadata (length,
resolution) and generates preview thumbnails in a background thread."""
from __future__ import annotations

import hashlib
import json
import logging
import queue
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import share
from .capture import _popen_flags
from .config import APP_DIR

log = logging.getLogger(__name__)

THUMB_DIR = APP_DIR / "thumbs"
CACHE_PATH = APP_DIR / "library.json"
THUMB_W, THUMB_H = 384, 216


@dataclass
class Clip:
    path: Path
    title: str
    mtime: float
    size: int
    duration: float = 0.0
    width: int = 0
    height: int = 0

    @property
    def created(self) -> datetime:
        return datetime.fromtimestamp(self.mtime)

    @property
    def key(self) -> str:
        return str(self.path)


class Library:
    def __init__(self, get_ffmpeg: Callable[[], str | None], on_update: Callable[[str], None]):
        self._get_ffmpeg = get_ffmpeg
        self._on_update = on_update
        self._lock = threading.Lock()
        try:
            self._cache: dict[str, dict] = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._cache = {}
        self._q: queue.Queue[Clip] = queue.Queue()
        self._pending: set[str] = set()
        threading.Thread(target=self._worker, daemon=True, name="library").start()

    # ------------------------------------------------------------------ public
    def scan(self, folder: str) -> list[Clip]:
        clips: list[Clip] = []
        try:
            files = list(Path(folder).glob("*.mp4"))
        except OSError:
            files = []
        for p in files:
            if p.name.startswith("."):  # LC Editor's in-progress exports (.lceditor-*.mp4)
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            c = Clip(p, share.title_from_filename(p), st.st_mtime, st.st_size)
            with self._lock:
                meta = self._cache.get(c.key)
            if meta and meta.get("mtime") == c.mtime and meta.get("size") == c.size:
                c.duration, c.width, c.height = meta["duration"], meta["width"], meta["height"]
                if not self.thumb_path(c).exists() and meta.get("thumb", True):
                    self._enqueue(c)
            else:
                self._enqueue(c)
            clips.append(c)
        clips.sort(key=lambda c: c.mtime, reverse=True)
        return clips

    def thumb_path(self, clip: Clip) -> Path:
        h = hashlib.sha1(f"{clip.path}|{clip.mtime}|{clip.size}".encode()).hexdigest()[:16]
        return THUMB_DIR / f"{h}.png"

    def forget(self, clip: Clip) -> None:
        with self._lock:
            self._cache.pop(clip.key, None)
            self._save()
        self.thumb_path(clip).unlink(missing_ok=True)

    # ------------------------------------------------------------------ worker
    def _enqueue(self, clip: Clip) -> None:
        with self._lock:
            if clip.key in self._pending:
                return
            self._pending.add(clip.key)
        self._q.put(clip)

    def _save(self) -> None:
        try:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            CACHE_PATH.write_text(json.dumps(self._cache), encoding="utf-8")
        except OSError:
            pass

    def _worker(self) -> None:
        while True:
            clip = self._q.get()
            try:
                ffmpeg = self._get_ffmpeg()
                if not ffmpeg:
                    continue
                (w, h), dur = share.probe(ffmpeg, clip.path)
                ok = self._make_thumb(ffmpeg, clip, dur)
                with self._lock:
                    self._cache[clip.key] = {"mtime": clip.mtime, "size": clip.size, "duration": dur,
                                             "width": w, "height": h, "thumb": ok}
                    self._save()
                self._on_update(clip.key)
            except Exception:
                log.exception("Library: failed to process %s", clip.path)
            finally:
                with self._lock:
                    self._pending.discard(clip.key)

    def _make_thumb(self, ffmpeg: str, clip: Clip, duration: float) -> bool:
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        out = self.thumb_path(clip)
        vf = (f"scale={THUMB_W}:{THUMB_H}:force_original_aspect_ratio=decrease,"
              f"pad={THUMB_W}:{THUMB_H}:(ow-iw)/2:(oh-ih)/2:black")
        t = f"{min(1.0, duration / 2):.2f}" if duration else "0"
        r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", t, "-i", str(clip.path),
                            "-frames:v", "1", "-vf", vf, str(out)],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=60,
                           creationflags=_popen_flags(low_priority=True))
        return r.returncode == 0 and out.exists()


# ---------------------------------------------------------------------- file actions

def reveal_in_explorer(path: Path) -> None:
    if sys.platform == "win32":
        subprocess.Popen(f'explorer /select,"{path}"')
    else:
        subprocess.Popen(["xdg-open", str(path.parent)])


def play(path: Path) -> None:
    if sys.platform == "win32":
        import os

        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(path)])


def send_to_recycle_bin(path: Path) -> bool:
    """Recycle Bin on Windows (undoable). Returns False if it had to hard-delete."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                        ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                        ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

        FO_DELETE, FOF_ALLOWUNDO, FOF_NOCONFIRMATION, FOF_SILENT, FOF_NOERRORUI = 3, 0x40, 0x10, 0x4, 0x400
        op = SHFILEOPSTRUCTW(None, FO_DELETE, str(path) + "\0", None,
                             FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI, False, None, None)
        if ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0:
            return True
    path.unlink()
    return False
