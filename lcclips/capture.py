"""Video capture + replay buffer, driven by a single long-running FFmpeg.

Pipeline (one process, all heavy lifting on the GPU):

    ddagrab (Desktop Duplication, GPU texture)
      -> [optional GPU scale]
      -> hardware H.264 encoder (NVENC / AMF / QSV)
    + mixed audio PCM on stdin -> AAC
      -> segment muxer: 1-second .ts files in a wrap-around ring

Saving a clip = concatenating the last N completed segments with stream copy
(no re-encode), so it takes well under a second and costs almost nothing.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import audio
from .config import APP_DIR, Config

log = logging.getLogger(__name__)

SEGMENT_SECONDS = 1
BUFFER_DIR = APP_DIR / "buffer"
FFMPEG_LOG = APP_DIR / "ffmpeg.log"

IS_WINDOWS = sys.platform == "win32"
_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0
_BELOW_NORMAL = 0x00004000 if IS_WINDOWS else 0


def _popen_flags(low_priority: bool = False) -> int:
    return _NO_WINDOW | (_BELOW_NORMAL if low_priority else 0)


# --------------------------------------------------------------------------- ffmpeg

def find_ffmpeg(configured: str = "") -> str | None:
    candidates = []
    if configured:
        candidates.append(Path(configured))
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    exe = "ffmpeg.exe" if IS_WINDOWS else "ffmpeg"
    candidates += [APP_DIR / "bin" / exe, base / "bin" / exe, base / exe, Path(sys.executable).parent / "bin" / exe]
    for c in candidates:
        if c.is_file():
            return str(c)
    return shutil.which("ffmpeg")


def _run(cmd: list[str], timeout: float = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        creationflags=_popen_flags(),
    )


def detect_monitors(ffmpeg: str, max_outputs: int = 6) -> list[tuple[int, int, int]]:
    """Return [(output_idx, width, height)] as Desktop Duplication sees them."""
    found = []
    for idx in range(max_outputs):
        cmd = [ffmpeg, "-hide_banner", "-f", "lavfi", "-i",
               f"ddagrab=output_idx={idx}:framerate=5", "-frames:v", "1", "-f", "null", "-"]
        try:
            r = _run(cmd, timeout=10)
        except subprocess.TimeoutExpired:
            break
        m = re.search(r"Stream #0:0.*?Video:.*?(\d{3,5})x(\d{3,5})", r.stderr)
        if r.returncode != 0 or not m:
            break
        found.append((idx, int(m.group(1)), int(m.group(2))))
    return found


# --------------------------------------------------------------------------- video graph

def _encoder_args(encoder: str, fps: int, mbps: int) -> list[str]:
    rate = [f"-b:v", f"{mbps}M", "-maxrate", f"{mbps}M", "-bufsize", f"{mbps * 2}M"]
    gop = ["-g", str(fps * SEGMENT_SECONDS), "-bf", "0"]
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p2", "-tune", "ll", "-rc", "cbr", *rate, *gop]
    if encoder == "h264_amf":
        return ["-c:v", "h264_amf", "-usage", "lowlatency", "-quality", "speed", "-rc", "cbr", *rate, *gop]
    if encoder == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", "veryfast", "-look_ahead", "0", *rate, *gop]
    # CPU fallback - works everywhere, but costs real CPU time in-game.
    return ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-pix_fmt", "yuv420p", *rate, *gop]


def _graph_candidates(encoder: str, source: str, size: tuple[int, int] | None) -> list[str]:
    """Filter graphs to try, cheapest (zero-copy GPU) first."""
    c: list[str] = []
    if size is None:
        if encoder in ("h264_nvenc", "h264_amf"):
            c.append(source)  # D3D11 texture straight into the encoder
        elif encoder == "h264_qsv":
            c.append(f"{source},hwmap=derive_device=qsv,format=qsv")
        c.append(f"{source},hwdownload,format=bgra,format=nv12")
    else:
        w, h = size
        if encoder in ("h264_nvenc", "h264_amf"):
            c.append(f"{source},scale_d3d11=width={w}:height={h}")  # FFmpeg 8+
        if encoder == "h264_nvenc":
            c.append(f"{source},hwmap=derive_device=cuda,scale_cuda={w}:{h}:format=nv12")
        if encoder == "h264_qsv":
            c.append(f"{source},hwmap=derive_device=qsv,format=qsv,scale_qsv=w={w}:h={h}")
        c.append(f"{source},hwdownload,format=bgra,scale={w}:{h}:flags=bilinear,format=nv12")
    return c


@dataclass
class VideoPlan:
    encoder: str
    graph: str
    args: list[str]
    width: int
    height: int
    gpu_path: bool


def _scaled_size(cfg: Config, mon_w: int, mon_h: int) -> tuple[int, int] | None:
    th = cfg.target_height()
    if th is None or th >= mon_h:
        return None
    tw = int(round(mon_w * th / mon_h / 2)) * 2
    return tw, th - (th % 2)


def plan_video(ffmpeg: str, cfg: Config, mon_w: int, mon_h: int, source: str | None = None) -> VideoPlan:
    """Probe encoder/filter combinations and return the first that works."""
    if source is None:
        source = f"ddagrab=output_idx={cfg.monitor}:framerate={cfg.fps}:draw_mouse={int(cfg.draw_mouse)}"
    size = _scaled_size(cfg, mon_w, mon_h)
    encoders = ["h264_nvenc", "h264_amf", "h264_qsv", "libx264"] if cfg.encoder == "auto" else [cfg.encoder]
    errors = []
    for enc in encoders:
        args = _encoder_args(enc, cfg.fps, cfg.bitrate_mbps)
        for graph in _graph_candidates(enc, source, size):
            cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", graph,
                   "-frames:v", "5", *args, "-f", "null", "-"]
            try:
                r = _run(cmd, timeout=20)
                ok = r.returncode == 0
                err = r.stderr.strip().splitlines()[-1:] if not ok else []
            except subprocess.TimeoutExpired:
                ok, err = False, ["timeout"]
            if ok:
                w, h = size or (mon_w, mon_h)
                plan = VideoPlan(enc, graph, args, w, h, gpu_path="hwdownload" not in graph)
                log.info("Video plan: %s | %s", enc, graph)
                return plan
            errors.append(f"{enc} [{graph.split(',', 1)[-1]}]: {' '.join(err)}")
            log.info("Probe failed: %s", errors[-1])
    raise RuntimeError("No working video encoder found:\n" + "\n".join(errors[-6:]))


def build_record_cmd(ffmpeg: str, plan: VideoPlan, cfg: Config, seg_dir: Path) -> list[str]:
    wrap = cfg.clip_seconds // SEGMENT_SECONDS + 6
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-y",
           "-f", "lavfi", "-i", plan.graph]
    if cfg.audio_offset_ms:
        cmd += ["-itsoffset", f"{cfg.audio_offset_ms / 1000:.3f}"]
    cmd += ["-thread_queue_size", "1024",
            "-f", "f32le", "-ar", str(audio.SAMPLE_RATE), "-ac", str(audio.CHANNELS), "-i", "pipe:0",
            "-map", "0:v", "-map", "1:a",
            *plan.args,
            "-c:a", "aac", "-b:a", "160k",
            "-f", "segment", "-segment_time", str(SEGMENT_SECONDS), "-segment_wrap", str(wrap),
            "-segment_format", "mpegts", "-reset_timestamps", "1",
            str(seg_dir / "seg_%04d.ts")]
    return cmd


# --------------------------------------------------------------------------- clip saving

def _foreground_title() -> str:
    if not IS_WINDOWS:
        return ""
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value
    except Exception:
        return ""


def _safe_name(s: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s).strip(" .")
    return s[:60] or "Clip"


def completed_segments(seg_dir: Path) -> list[Path]:
    """Finished segments, oldest first (the newest file is still being written)."""
    segs = sorted(seg_dir.glob("seg_*.ts"), key=lambda p: p.stat().st_mtime)
    return [p for p in segs[:-1] if p.stat().st_size > 0]


def concat_segments(ffmpeg: str, segs: list[Path], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in segs), encoding="utf-8")
    try:
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
               "-i", str(lst), "-map", "0", "-c", "copy", "-movflags", "+faststart", str(out)]
        r = _run(cmd, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip()[-500:])
    finally:
        lst.unlink(missing_ok=True)


# --------------------------------------------------------------------------- recorder

class Recorder:
    def __init__(self, cfg: Config, ffmpeg: str, plan: VideoPlan):
        self.cfg = cfg
        self.ffmpeg = ffmpeg
        self.plan = plan
        self.proc: subprocess.Popen | None = None
        self.mixer: audio.AudioMixer | None = None
        self.started_at = 0.0
        self._save_lock = threading.Lock()
        self._log = None

    def start(self) -> None:
        shutil.rmtree(BUFFER_DIR, ignore_errors=True)
        BUFFER_DIR.mkdir(parents=True, exist_ok=True)
        cmd = build_record_cmd(self.ffmpeg, self.plan, self.cfg, BUFFER_DIR)
        log.info("Recorder cmd: %s", subprocess.list2cmdline(cmd))
        self._log = open(FFMPEG_LOG, "w", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self._log,
            bufsize=0, creationflags=_popen_flags(low_priority=True),
        )
        sources = audio.build_sources(self.cfg.game_audio_device, self.cfg.game_volume,
                                      self.cfg.mic_device, self.cfg.mic_volume)
        self.mixer = audio.AudioMixer(sources, self.proc.stdin.write)
        self.mixer.start()
        self.started_at = time.monotonic()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def buffered_seconds(self) -> float:
        if not self.alive():
            return 0.0
        return min(self.cfg.clip_seconds, time.monotonic() - self.started_at)

    def stop(self) -> None:
        if self.mixer:
            self.mixer.stop()
        if self.proc:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
        if self._log:
            self._log.close()
        self.proc = self.mixer = None

    def save_clip(self, seconds: int | None = None) -> Path:
        """Save the last `seconds` of gameplay. Blocks ~1 s; call off the UI thread."""
        if not self._save_lock.acquire(blocking=False):
            raise RuntimeError("A clip is already being saved")
        try:
            if not self.alive():
                raise RuntimeError("Recorder is not running")
            seconds = seconds or self.cfg.clip_seconds
            title = _safe_name(_foreground_title())
            # Let the segment that contains "now" finish so the clip includes
            # the moment the hotkey was pressed.
            time.sleep(SEGMENT_SECONDS + 0.3)
            segs = completed_segments(BUFFER_DIR)
            count = max(1, seconds // SEGMENT_SECONDS)
            segs = segs[-count:]
            if not segs:
                raise RuntimeError("Buffer is empty - wait a moment after starting")
            stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            out = Path(self.cfg.output_dir) / f"{title}_{stamp}.mp4"
            concat_segments(self.ffmpeg, segs, out)
            log.info("Saved clip %s (%d segments)", out, len(segs))
            return out
        finally:
            self._save_lock.release()


def tail_ffmpeg_log(lines: int = 8) -> str:
    try:
        return "\n".join(FFMPEG_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def open_folder(path: str) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:
        os.startfile(path)  # type: ignore[attr-defined]
