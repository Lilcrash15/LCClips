"""LC Editor engine: Python port of LCEditor's ffmpeg.go + jobs.go.

Same ffmpeg recipes, probe regexes, progress parsing, job model and atomic
output as the Go original, so the original index.html works unchanged."""
from __future__ import annotations

import concurrent.futures
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger("lcclips.editor")
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class Canceled(Exception):
    pass


def ff_time(t: float) -> str:
    return f"{max(0.0, t):.3f}"


def random_token(n: int = 16) -> str:
    return secrets.token_hex(n)


# --------------------------------------------------------------------------- ffmpeg

class FFmpeg:
    """Wraps the ffmpeg LCClips already bundles; detects GPU encoders in the background."""

    def __init__(self, path_getter: Callable[[], str | None]):
        self._path_getter = path_getter
        self.ready = threading.Event()
        self.err: str | None = None
        self.encoders: list[str] = []
        self.path: str | None = None

    def prepare(self) -> None:
        try:
            self.path = self._path_getter()
            if not self.path:
                self.err = "ffmpeg was not found"
                return
            self.encoders = ["x264", *self._detect_hardware()]
            log.info("editor: encoders %s", self.encoders)
        finally:
            self.ready.set()

    def wait(self) -> str | None:
        self.ready.wait()
        return self.err

    def _detect_hardware(self) -> list[str]:
        cands = [("nvenc", "h264_nvenc"), ("amf", "h264_amf"), ("qsv", "h264_qsv")]

        def test(codec: str) -> bool:
            try:
                return subprocess.run(
                    [self.path, "-hide_banner", "-nostdin", "-loglevel", "error", "-f", "lavfi",
                     "-i", "color=c=black:s=320x240:r=30:d=0.5", "-pix_fmt", "yuv420p", "-c:v", codec, "-f", "null", "-"],
                    stdin=subprocess.DEVNULL, capture_output=True, timeout=15, creationflags=_NO_WINDOW,
                ).returncode == 0
            except (subprocess.TimeoutExpired, OSError):
                return False

        with concurrent.futures.ThreadPoolExecutor(3) as ex:
            ok = dict(zip([n for n, _ in cands], ex.map(test, [c for _, c in cands])))
        return [n for n, _ in cands if ok[n]]

    def _popen(self, args: list[str], **kw) -> subprocess.Popen:
        return subprocess.Popen([self.path, *args], stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, **kw)

    # ------------------------------------------------------------------ probe
    _re_duration = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
    _re_input = re.compile(r"Input #0, ([^ ]+), from")
    _re_video = re.compile(r"Stream #0:\d+.*?: Video: ([A-Za-z0-9_]+)")
    _re_audio = re.compile(r"Stream #0:\d+.*?: Audio: ([A-Za-z0-9_]+)")
    _re_res = re.compile(r", (\d{2,5})x(\d{2,5})")
    _re_fps = re.compile(r", ([\d.]+) fps")
    _re_tbr = re.compile(r", ([\d.]+) tbr")

    def probe(self, path: str) -> dict:
        p = Path(path)
        try:
            st = p.stat()
        except OSError as e:
            raise ValueError(f"can't read file: {e}") from None
        if p.is_dir():
            raise ValueError("that's a folder, not a video file")
        try:
            r = subprocess.run([self.path, "-hide_banner", "-nostdin", "-i", str(p)], stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, errors="replace", timeout=30, creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise ValueError("ffmpeg took too long to read this file") from None
        text = r.stderr
        info = {"path": str(p), "name": p.name, "ext": p.suffix.lower(), "size": st.st_size, "duration": 0.0,
                "width": 0, "height": 0, "fps": 0.0, "videoCodec": "", "audioCodecs": [], "format": ""}
        m = self._re_input.search(text)
        if not m:
            raise ValueError("ffmpeg couldn't read this file — it may not be a video")
        info["format"] = m.group(1).rstrip(",")
        if (m := self._re_duration.search(text)):
            info["duration"] = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
        for line in text.splitlines():
            if (m := self._re_video.search(line)):
                if info["videoCodec"] or "attached pic" in line:
                    continue
                info["videoCodec"] = m[1]
                if (r2 := self._re_res.search(line)):
                    info["width"], info["height"] = int(r2[1]), int(r2[2])
                if (r2 := self._re_fps.search(line)) or (r2 := self._re_tbr.search(line)):
                    info["fps"] = float(r2[1])
            elif (m := self._re_audio.search(line)):
                info["audioCodecs"].append(m[1])
        if not info["videoCodec"] and not info["audioCodecs"]:
            raise ValueError("no video or audio streams found in this file")
        return info

    def thumb(self, path: str, t: float, height: int) -> bytes:
        r = subprocess.run([self.path, "-hide_banner", "-nostdin", "-loglevel", "error", "-ss", ff_time(t), "-i", path,
                            "-frames:v", "1", "-vf", f"scale=-2:{height}", "-f", "image2pipe", "-c:v", "mjpeg",
                            "-q:v", "6", "pipe:1"], stdin=subprocess.DEVNULL, capture_output=True, timeout=20,
                           creationflags=_NO_WINDOW)
        if r.returncode != 0 or not r.stdout:
            raise ValueError("no frame")
        return r.stdout

    # ------------------------------------------------------------------ run with progress
    def run(self, cancel: threading.Event, args: list[str], on_time: Callable[[float], None] | None) -> None:
        full = ["-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats", *args]
        log.info("editor ffmpeg %s", " ".join(full))
        proc = self._popen(full, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        tail = _Tail(40)
        t = threading.Thread(target=lambda: [tail.write(l) for l in iter(proc.stderr.readline, b"")], daemon=True)
        t.start()
        def watch():
            while proc.poll() is None:
                if cancel.wait(0.2):
                    proc.kill()
                    return

        threading.Thread(target=watch, daemon=True).start()
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("out_time_us=") and on_time:
                try:
                    us = float(line.split("=", 1)[1])
                    if us >= 0:
                        on_time(us / 1e6)
                except ValueError:
                    pass
        rc = proc.wait()
        t.join(2)
        if cancel.is_set():
            raise Canceled()
        if rc != 0:
            raise RuntimeError(tail.last_meaningful() or f"ffmpeg exited with code {rc}")


_re_log_prefix = re.compile(r"^\[[^\]]+ @ 0x[0-9a-f]+\]\s*")
_ERR_WORDS = ("error", "invalid", "not supported", "could not", "unable", "incompatible", "no such", "permission")


class _Tail:
    def __init__(self, n: int):
        self.n, self.lines, self.lock = n, [], threading.Lock()

    def write(self, raw: bytes) -> None:
        for l in raw.decode("utf-8", "replace").replace("\r", "\n").split("\n"):
            if l.strip():
                with self.lock:
                    self.lines = (self.lines + [l])[-self.n:]

    def last_meaningful(self) -> str:
        with self.lock:
            lines = list(self.lines)
        picked = [l.strip() for l in lines if any(w in l.lower() for w in _ERR_WORDS)] or lines[-3:]
        return "\n".join(_re_log_prefix.sub("", l) for l in picked[:2])


# --------------------------------------------------------------------------- jobs

@dataclass
class Job:
    id: str
    kind: str  # export | proxy
    status: str = "running"
    progress: float = 0.0
    stage: str = ""
    message: str = ""
    output: str = ""
    started: float = field(default_factory=time.monotonic)
    elapsed: float = 0.0
    cancel_ev: threading.Event = field(default_factory=threading.Event, repr=False)
    done_ev: threading.Event = field(default_factory=threading.Event, repr=False)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict:
        with self.lock:
            el = time.monotonic() - self.started if self.status == "running" else self.elapsed
            return {"id": self.id, "kind": self.kind, "status": self.status, "progress": self.progress,
                    "stage": self.stage, "message": self.message, "output": self.output, "elapsed": el}

    def running(self) -> bool:
        with self.lock:
            return self.status == "running"

    def set(self, progress: float, stage: str) -> None:
        with self.lock:
            progress = min(progress, 1.0)
            if progress > self.progress:
                self.progress = progress
            self.stage = stage

    def finish(self, err: BaseException | None, output: str) -> None:
        with self.lock:
            self.elapsed = time.monotonic() - self.started
            if err is None:
                self.status, self.progress, self.output = "done", 1.0, output
            elif isinstance(err, Canceled):
                self.status = "canceled"
            else:
                self.status, self.message = "error", str(err)
        self.done_ev.set()

    def cancel(self) -> None:
        self.cancel_ev.set()


def new_job(kind: str) -> Job:
    return Job(id=random_token()[:8], kind=kind)


# --------------------------------------------------------------------------- export recipes

def video_encoder_args(enc: str, ext: str) -> list[str]:
    if ext == ".webm":
        return ["-c:v", "libvpx-vp9", "-crf", "31", "-b:v", "0", "-deadline", "good", "-cpu-used", "4", "-row-mt", "1"]
    return {
        "nvenc": ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "19", "-b:v", "0", "-pix_fmt", "yuv420p"],
        "amf": ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp", "-qp_i", "18", "-qp_p", "20", "-qp_b", "22",
                "-pix_fmt", "yuv420p"],
        "qsv": ["-c:v", "h264_qsv", "-global_quality", "20", "-pix_fmt", "nv12"],
    }.get(enc, ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"])


def audio_encoder_args(ext: str) -> list[str]:
    return ["-c:a", "libopus", "-b:a", "160k"] if ext == ".webm" else ["-c:a", "aac", "-b:a", "192k"]


def container_args(ext: str) -> list[str]:
    return ["-movflags", "+faststart"] if ext in (".mp4", ".mov", ".m4v") else []


def segment_args(src: dict, seg: dict, opt: dict, ext: str, out: str) -> list[str]:
    dur = seg["end"] - seg["start"]
    args = ["-ss", ff_time(seg["start"]), "-i", src["path"], "-t", ff_time(dur), "-map", "0:v:0?", "-map", "0:a?"]
    if opt["mode"] == "precise":
        args += video_encoder_args(opt.get("encoder", "x264"), ext)
        if not opt.get("mute"):
            args += audio_encoder_args(ext)
    else:
        args += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
    if opt.get("mute"):
        args += ["-an"]
    args += ["-map_metadata", "0", "-map_chapters", "-1", *container_args(ext), out]
    return args


def validate_segments(segs: list, dur: float) -> list[dict]:
    if not segs:
        raise ValueError("Nothing selected to export.")
    if len(segs) > 200:
        raise ValueError("Too many clips.")
    out = []
    for i, s in enumerate(segs):
        try:
            a, b = float(s["start"]), float(s["end"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("bad request") from None
        a = max(0.0, a)
        if dur > 0:
            b = min(b, dur)
        if b - a < 0.05:
            raise ValueError(f"Clip {i + 1} is too short.")
        out.append({"start": a, "end": b})
    return out


def export(ff: FFmpeg, job: Job, src: dict, opt: dict, out: str, tmp_root: Path) -> None:
    segs = opt["segments"]
    total = sum(s["end"] - s["start"] for s in segs) or 1.0
    ext = Path(out).suffix.lower()
    partial = str(Path(out).parent / f".lceditor-{job.id}{ext}")
    try:
        if len(segs) == 1:
            ff.run(job.cancel_ev, segment_args(src, segs[0], opt, ext, partial), lambda t: job.set(t / total, "Cutting"))
        else:
            tmp = tmp_root / job.id
            tmp.mkdir(parents=True, exist_ok=True)
            try:
                lines, done = [], 0.0
                for i, s in enumerate(segs):
                    part = str(tmp / f"part{i:03d}{ext}")
                    stage = f"Cutting clip {i + 1} of {len(segs)}"
                    job.set(done / total * 0.97, stage)
                    base = done
                    ff.run(job.cancel_ev, segment_args(src, s, opt, ext, part),
                           lambda t, b=base, st=stage: job.set((b + t) / total * 0.97, st))
                    done += s["end"] - s["start"]
                    lines.append("file '" + Path(part).as_posix().replace("'", "'\\''") + "'\n")
                lst = tmp / "list.txt"
                lst.write_text("".join(lines), encoding="utf-8")
                job.set(0.97, "Joining clips")
                ff.run(job.cancel_ev, ["-f", "concat", "-safe", "0", "-i", str(lst), "-map", "0", "-c", "copy",
                                       *container_args(ext), partial], None)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        if not os.path.isfile(partial):
            raise RuntimeError("ffmpeg finished but no output file was written")
        if os.path.exists(out):
            os.remove(out)  # the save dialog already confirmed overwriting
        os.replace(partial, out)
    finally:
        if os.path.exists(partial):
            os.remove(partial)


def build_proxy(ff: FFmpeg, job: Job, src: dict, out: str) -> None:
    args = ["-i", src["path"], "-map", "0:v:0?", "-map", "0:a:0?",
            "-vf", "scale=-2:'trunc(min(720,ih)/2)*2'",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "27", "-g", "15", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-movflags", "+faststart", out]
    dur = src.get("duration") or 0
    try:
        ff.run(job.cancel_ev, args, (lambda t: job.set(t / dur, "Building preview")) if dur > 0 else None)
    except BaseException:
        if os.path.exists(out):
            os.remove(out)
        raise


def unique_name(folder: str, base: str, ext: str) -> str:
    name = base + ext
    i = 2
    while os.path.exists(os.path.join(folder, name)) and i < 1000:
        name = f"{base}{i}{ext}"
        i += 1
    return name


def same_path(a: str, b: str) -> bool:
    pa, pb = os.path.abspath(a), os.path.abspath(b)
    return pa.lower() == pb.lower() if sys.platform == "win32" else pa == pb
