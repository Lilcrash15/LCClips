"""LC Editor's local HTTP API (port of main.go + handlers.go), hosted inside
LCClips. Same routes and JSON shapes as the Go original (handoff section 4),
plus `openPath`/`openSeq` in the event stream so LCClips can load a clip into
an editor window that's already open."""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import shutil
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from . import core, platform

log = logging.getLogger("lcclips.editor")

APP_TITLE = "LC Editor"
VERSION = "1.0.0"
UI_DIR = Path(__file__).resolve().parent / "ui"
PRECISE_EXTS = {".mp4", ".mkv", ".mov", ".m4v"}


class Editor:
    def __init__(self, data_dir: Path, ffmpeg_path: Callable[[], str | None],
                 accent: Callable[[], tuple[str, str]] = lambda: ("#ff4f68", "#ff7a59"),
                 on_export_done: Callable[[str], None] | None = None):
        self.data_dir = data_dir
        self.token = core.random_token()
        self.ff = core.FFmpeg(ffmpeg_path)
        self.accent = accent
        self.on_export_done = on_export_done
        self.lock = threading.Lock()
        self.media: dict | None = None
        self.proxy_path = ""
        self.job: core.Job | None = None
        self.start_path = ""
        self.open_path, self.open_seq = "", 0
        self.clients = 0
        self.thumb_cache: dict[str, bytes] = {}
        self.thumb_lock = threading.Lock()
        self.thumb_sem = threading.Semaphore(4)
        self._icon: bytes | None = None
        self.httpd: ThreadingHTTPServer | None = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self.httpd:
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        cleanup_temp(self.data_dir)
        threading.Thread(target=self.ff.prepare, daemon=True, name="editor-ffmpeg").start()
        editor = self

        class Handler(_Handler):
            app = editor

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="editor-http").start()
        log.info("LC Editor server on %s", self.base_url)

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]  # type: ignore[union-attr]
        return f"http://{host}:{port}"

    @property
    def url(self) -> str:
        return f"{self.base_url}/?t={self.token}"

    def open(self, path: str | None = None) -> None:
        """Show the editor, optionally loading `path` (reuses an open window)."""
        self.start()
        with self.lock:
            has_window = self.clients > 0
            if path:
                if has_window:
                    self.open_path, self.open_seq = path, self.open_seq + 1
                else:
                    self.start_path = path
        if has_window and platform.focus_window(APP_TITLE):
            return
        if not has_window:
            platform.open_window(self.url, self.data_dir / "WindowProfile")

    def shutdown(self) -> None:
        with self.lock:
            job, proxy = self.job, self.proxy_path
        if job:
            job.cancel()
            job.done_ev.wait(5)
        if proxy and os.path.exists(proxy):
            os.remove(proxy)
        cleanup_temp(self.data_dir)
        if self.httpd:
            self.httpd.shutdown()

    # ------------------------------------------------------------------ state
    def state(self) -> dict:
        s = {"ready": self.ff.ready.is_set(), "encoders": []}
        if s["ready"]:
            if self.ff.err:
                s["ffError"] = self.ff.err
            s["encoders"] = list(self.ff.encoders)
        with self.lock:
            s["media"] = self.media
            s["proxy"] = bool(self.proxy_path) and os.path.isfile(self.proxy_path)
            s["job"] = self.job.snapshot() if self.job else None
            if self.start_path:
                s["startPath"] = self.start_path
            s["openPath"], s["openSeq"] = self.open_path, self.open_seq
        return s

    def icon_png(self) -> bytes:
        if self._icon is None:
            self._icon = _render_icon()
        return self._icon

    # ------------------------------------------------------------------ jobs
    def _run_export(self, job: core.Job, src: dict, opt: dict, out: str) -> None:
        err = None
        try:
            core.export(self.ff, job, src, opt, out, self.data_dir / "tmp")
        except BaseException as e:  # noqa: BLE001 - reported to the UI
            err = e
            if not isinstance(e, core.Canceled):
                log.warning("export failed: %s", e)
        job.finish(err, out)
        if err is None and self.on_export_done:
            try:
                self.on_export_done(out)
            except Exception:
                log.exception("on_export_done failed")

    def _run_proxy(self, job: core.Job, src: dict, out: str) -> None:
        err = None
        try:
            core.build_proxy(self.ff, job, src, out)
        except BaseException as e:  # noqa: BLE001
            err = e
        job.finish(err, out)


def cleanup_temp(data_dir: Path) -> None:
    shutil.rmtree(data_dir / "tmp", ignore_errors=True)
    for p in data_dir.glob("preview-*.mp4"):
        try:
            p.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- HTTP

class _Handler(BaseHTTPRequestHandler):
    app: Editor
    protocol_version = "HTTP/1.1"
    server_version = "LCEditor"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    # ------------------------------------------------------------------ helpers
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {"Cache-Control": "no-store"}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _err(self, code: int, msg: str) -> None:
        self._json({"error": msg}, code)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 1 << 20:
            return {}
        try:
            v = json.loads(self.rfile.read(n))
            return v if isinstance(v, dict) else {}
        except ValueError:
            return {}

    def _q(self) -> dict[str, str]:
        return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}

    # ------------------------------------------------------------------ dispatch
    def do_GET(self):
        self._dispatch()

    def do_HEAD(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def _dispatch(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        q = self._q()
        try:
            if path == "/":
                return self._index(q)
            if path in ("/icon.png", "/favicon.ico"):
                return self._send(200, self.app.icon_png(), "image/png", {"Cache-Control": "max-age=86400"})
            if path.startswith("/api/") or path in ("/media", "/thumb"):
                tok = self.headers.get("X-Token") or q.get("t", "")
                if tok != self.app.token:
                    return self._send(403, b"forbidden", "text/plain")
                route = {
                    "/api/events": self._events, "/api/state": self._state, "/api/open": self._open,
                    "/api/proxy": self._proxy, "/api/export": self._export, "/api/cancel": self._cancel,
                    "/api/reveal": self._reveal, "/api/openfile": self._openfile,
                    "/media": self._media, "/thumb": self._thumb,
                }.get(path)
                if route:
                    return route(q)
            self._send(404, b"not found", "text/plain")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _post_only(self) -> bool:
        if self.command != "POST":
            self._err(405, "POST only")
            return False
        return True

    # ------------------------------------------------------------------ routes
    def _index(self, q) -> None:
        if q.get("t") != self.app.token:
            return self._send(403, b"forbidden", "text/plain")
        html = (UI_DIR / "index.html").read_text(encoding="utf-8")
        html = html.replace("{{TOKEN}}", self.app.token).replace("{{VERSION}}", VERSION)
        a1, a2 = self.app.accent()
        # Match LCClips' current theme accent (the LC Editor layout is unchanged).
        r, g, b = (int(a1.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        override = (f"<style>:root{{--accent:{a1};--accent2:{a2};--accentDim:rgba({r},{g},{b},.18)}}"
                    f".btn.on{{border-color:rgba({r},{g},{b},.5)}}.sel{{box-shadow:inset 0 0 0 1px rgba({r},{g},{b},.25)}}"
                    f".empty img{{filter:drop-shadow(0 10px 30px rgba({r},{g},{b},.35))}}</style>")
        html = html.replace("</head>", override + "</head>", 1)
        self._send(200, html.encode(), "text/html; charset=utf-8")

    def _state(self, q) -> None:
        s = self.app.state()
        with self.app.lock:
            self.app.start_path = ""  # the startup file is handed over exactly once
        self._json(s)

    def _events(self, q) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        with self.app.lock:
            self.app.clients += 1
        last, last_send = "", 0.0
        try:
            while True:
                s = self.app.state()
                msg = json.dumps({k: s.get(k) for k in ("ready", "ffError", "encoders", "job", "proxy",
                                                        "openPath", "openSeq") if k in s})
                if msg != last:
                    self.wfile.write(f"data: {msg}\n\n".encode())
                    self.wfile.flush()
                    last, last_send = msg, time.monotonic()
                elif time.monotonic() - last_send > 15:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    last_send = time.monotonic()
                time.sleep(0.2)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            with self.app.lock:
                self.app.clients -= 1

    def _open(self, q) -> None:
        if not self._post_only():
            return
        app = self.app
        path = str(self._body().get("path") or "")
        if not path:
            with app.lock:
                d = os.path.dirname(app.media["path"]) if app.media else ""
            try:
                path = platform.open_file_dialog(d)
            except OSError as e:
                return self._err(500, str(e))
            if not path:
                return self._json({"canceled": True})
        err = app.ff.wait()
        if err:
            return self._err(500, "ffmpeg is unavailable: " + err)
        try:
            info = app.ff.probe(path)
        except ValueError as e:
            return self._err(400, str(e))
        log.info("editor opened %s (%.2fs %dx%d %s)", info["path"], info["duration"], info["width"],
                 info["height"], info["videoCodec"])
        with app.lock:
            if app.job and app.job.kind == "proxy" and app.job.running():
                app.job.cancel()
            if app.proxy_path and os.path.exists(app.proxy_path):
                os.remove(app.proxy_path)
            app.proxy_path = ""
            app.media = info
        self._json(info)

    def _media(self, q) -> None:
        with self.app.lock:
            media, proxy = self.app.media, self.app.proxy_path
        if not media:
            return self._send(404, b"not found", "text/plain")
        path = media["path"]
        if q.get("proxy") == "1":
            if not proxy or not os.path.isfile(proxy):
                return self._send(404, b"not found", "text/plain")
            path = proxy
        ext = Path(path).suffix.lower()
        ctype = {".mkv": "video/x-matroska", ".ts": "video/mp2t", ".mts": "video/mp2t", ".m2ts": "video/mp2t",
                 ".mov": "video/mp4", ".webm": "video/webm"}.get(ext) or mimetypes.guess_type(path)[0] or \
            "application/octet-stream"
        try:
            size = os.path.getsize(path)
            f = open(path, "rb")
        except OSError as e:
            return self._send(404, str(e).encode(), "text/plain")
        with f:
            start, end, code = 0, size - 1, 200
            m = re.match(r"bytes=(\d*)-(\d*)$", (self.headers.get("Range") or "").strip())
            if m and (m[1] or m[2]):
                if m[1]:
                    start = int(m[1])
                    end = min(int(m[2]), size - 1) if m[2] else size - 1
                else:
                    start = max(0, size - int(m[2]))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                code = 206
            length = end - start + 1
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(256 * 1024, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def _thumb(self, q) -> None:
        app = self.app
        with app.lock:
            media = app.media
        if not media or not media.get("videoCodec"):
            return self._send(404, b"not found", "text/plain")
        try:
            t = float(q.get("time", "0"))
            h = int(q.get("h", "72"))
        except ValueError:
            t, h = 0.0, 72
        if not 16 <= h <= 360:
            h = 72
        key = f"{media['path']}|{t:.2f}|{h}"
        with app.thumb_lock:
            data = app.thumb_cache.get(key)
        if data is None:
            with app.thumb_sem:
                try:
                    data = app.ff.thumb(media["path"], t, h)
                except Exception:
                    return self._send(404, b"no frame", "text/plain")
            with app.thumb_lock:
                if len(app.thumb_cache) > 600:
                    app.thumb_cache.clear()
                app.thumb_cache[key] = data
        self._send(200, data, "image/jpeg", {"Cache-Control": "max-age=3600"})

    def _proxy(self, q) -> None:
        if not self._post_only():
            return
        app = self.app
        with app.lock:
            if not app.media:
                return self._err(400, "no file open")
            if app.job and app.job.running():
                return self._err(409, "busy")
            job = core.new_job("proxy")
            out = str(app.data_dir / f"preview-{job.id}.mp4")
            if app.proxy_path and os.path.exists(app.proxy_path):
                os.remove(app.proxy_path)
            app.proxy_path, app.job, src = out, job, app.media
        threading.Thread(target=app._run_proxy, args=(job, src, out), daemon=True).start()
        self._json(job.snapshot())

    def _export(self, q) -> None:
        if not self._post_only():
            return
        app = self.app
        opt = self._body()
        with app.lock:
            src, busy = app.media, bool(app.job and app.job.running())
        if not src:
            return self._err(400, "Open a video first.")
        if busy:
            return self._err(409, "Something is already running — wait for it or cancel it.")
        try:
            opt["segments"] = core.validate_segments(opt.get("segments") or [], src.get("duration") or 0)
        except ValueError as e:
            return self._err(400, str(e))
        if opt.get("mode") != "precise":
            opt["mode"] = "fast"
        src_ext = src.get("ext") or ".mp4"
        def_ext = ".mp4" if opt["mode"] == "precise" and src_ext not in PRECISE_EXTS else src_ext
        folder = os.path.dirname(src["path"])
        base = os.path.splitext(src["name"])[0]
        def_name = core.unique_name(folder, base + "_cut", def_ext)
        try:
            out = platform.save_file_dialog(folder, def_name, def_ext)
        except OSError as e:
            return self._err(500, str(e))
        if not out:
            return self._json({"canceled": True})
        if not os.path.splitext(out)[1]:
            out += def_ext
        if core.same_path(out, src["path"]):
            return self._err(400, "Pick a different file name — LC Editor won't overwrite the video you're cutting from.")
        with app.lock:
            if app.job and app.job.running():
                return self._err(409, "busy")
            job = core.new_job("export")
            job.output = out
            app.job = job
        threading.Thread(target=app._run_export, args=(job, src, opt, out), daemon=True).start()
        self._json(job.snapshot())

    def _cancel(self, q) -> None:
        with self.app.lock:
            job = self.app.job
        if job:
            job.cancel()
        self._json({"ok": True})

    def _exported_path(self) -> str | None:
        """Only the last successful export's output may be opened or revealed."""
        req = str(self._body().get("path") or "")
        with self.app.lock:
            job = self.app.job
        if not job or job.kind != "export":
            return None
        s = job.snapshot()
        return s["output"] if s["status"] == "done" and core.same_path(s["output"], req) else None

    def _reveal(self, q) -> None:
        p = self._exported_path()
        if not p:
            return self._err(400, "unknown file")
        try:
            platform.reveal_file(p)
        except OSError as e:
            return self._err(500, str(e))
        self._json({"ok": True})

    def _openfile(self, q) -> None:
        p = self._exported_path()
        if not p:
            return self._err(400, "unknown file")
        try:
            platform.shell_open(p)
        except OSError as e:
            return self._err(500, str(e))
        self._json({"ok": True})


# --------------------------------------------------------------------------- icon (port of tools/make_icon.py)

def _render_icon() -> bytes:
    import io

    from PIL import Image, ImageChops, ImageDraw

    S = 512
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    top, bot = (255, 86, 92), (196, 32, 120)
    for y in range(S):
        t = y / (S - 1)
        gd.line([(0, y), (S, y)], fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)) + (255,))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([20, 20, S - 20, S - 20], radius=110, fill=255)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)
    tri = [(185, 140), (185, 372), (385, 256)]
    ImageDraw.Draw(img).polygon(tri, fill=(255, 255, 255, 255))
    cut = Image.new("L", (S, S), 0)
    ImageDraw.Draw(cut).line([(150, 380), (360, 115)], fill=255, width=29)
    tri_mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(tri_mask).polygon(tri, fill=255)
    img.paste(Image.new("RGBA", (S, S), (222, 58, 104, 255)), (0, 0), ImageChops.multiply(cut, tri_mask))
    buf = io.BytesIO()
    img.resize((256, 256), Image.LANCZOS).save(buf, "PNG")
    return buf.getvalue()
