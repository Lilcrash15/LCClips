"""LCClips - lightweight replay-buffer clipper (tkinter UI + global hotkey + tray)."""
from __future__ import annotations

import logging
import queue
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import audio, capture, ffmpeg_setup, share
from .config import APP_DIR, APP_NAME, DEVICE_DEFAULT, DEVICE_NONE, ENCODERS, RESOLUTIONS, Config

log = logging.getLogger(APP_NAME)


def _beep(ok: bool) -> None:
    try:
        import winsound

        winsound.MessageBeep(winsound.MB_OK if ok else winsound.MB_ICONHAND)
    except Exception:
        pass


class App:
    def __init__(self) -> None:
        self.cfg = Config.load()
        self.ffmpeg = capture.find_ffmpeg(self.cfg.ffmpeg_path)
        self.recorder: capture.Recorder | None = None
        self.monitors: list[tuple[int, int, int]] = []
        self.ui_q: queue.Queue = queue.Queue()
        self._hotkey_handle = None
        self._busy = False
        self._sharing = False
        self.last_clip: Path | None = None
        self.tray = None

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._build_ui()
        self._setup_tray()
        self.root.after(100, self._pump)
        self.root.after(1000, self._watchdog)

        if not self.ffmpeg:
            self.set_status("FFmpeg not found. Click 'Download' next to FFmpeg path, or browse to ffmpeg.exe.", error=True)
            self.root.after(400, self._offer_ffmpeg_download)
        else:
            self.restart()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        f = ttk.Frame(self.root, padding=12)
        f.grid(sticky="nsew")
        self.vars: dict[str, tk.Variable] = {}
        row = 0

        def add(label: str, widget: tk.Widget, extra: tk.Widget | None = None):
            nonlocal row
            ttk.Label(f, text=label).grid(row=row, column=0, sticky="w", pady=3, padx=(0, 10))
            widget.grid(row=row, column=1, sticky="ew", pady=3)
            if extra:
                extra.grid(row=row, column=2, sticky="w", padx=(6, 0))
            row += 1

        def var(name: str, kind=tk.StringVar):
            v = kind(value=getattr(self.cfg, name))
            self.vars[name] = v
            return v

        ttk.Label(f, text="Capture", font=("Segoe UI", 10, "bold")).grid(row=row, column=0, sticky="w"); row += 1
        hk = ttk.Entry(f, textvariable=var("hotkey"), width=34)
        add("Save-clip hotkey", hk, ttk.Button(f, text="Record…", command=self.record_hotkey))
        add("Clip length (seconds)", ttk.Spinbox(f, from_=5, to=600, increment=5, textvariable=var("clip_seconds", tk.IntVar), width=8))
        self.mon_combo = ttk.Combobox(f, state="readonly", width=32)
        add("Monitor", self.mon_combo, ttk.Button(f, text="Detect", command=self.detect_monitors))
        add("Resolution", ttk.Combobox(f, values=RESOLUTIONS, state="readonly", textvariable=var("resolution"), width=12))
        add("Frame rate", ttk.Combobox(f, values=[30, 60, 120, 144], textvariable=var("fps", tk.IntVar), width=12))
        add("Bitrate (Mbps)", ttk.Spinbox(f, from_=2, to=150, increment=1, textvariable=var("bitrate_mbps", tk.IntVar), width=8))
        add("Encoder", ttk.Combobox(f, values=ENCODERS, state="readonly", textvariable=var("encoder"), width=12))
        add("Capture cursor", ttk.Checkbutton(f, variable=var("draw_mouse", tk.BooleanVar)))

        ttk.Separator(f).grid(row=row, column=0, columnspan=3, sticky="ew", pady=8); row += 1
        ttk.Label(f, text="Audio", font=("Segoe UI", 10, "bold")).grid(row=row, column=0, sticky="w"); row += 1
        self.out_combo = ttk.Combobox(f, textvariable=var("game_audio_device"), state="readonly", width=34)
        add("Game audio (output device)", self.out_combo, ttk.Button(f, text="Refresh", command=self.refresh_devices))
        add("Game volume", ttk.Scale(f, from_=0, to=2, variable=var("game_volume", tk.DoubleVar)))
        self.mic_combo = ttk.Combobox(f, textvariable=var("mic_device"), state="readonly", width=34)
        add("Microphone", self.mic_combo)
        add("Mic volume", ttk.Scale(f, from_=0, to=2, variable=var("mic_volume", tk.DoubleVar)))
        add("Audio sync offset (ms)", ttk.Spinbox(f, from_=-1000, to=1000, increment=10, textvariable=var("audio_offset_ms", tk.IntVar), width=8))

        ttk.Separator(f).grid(row=row, column=0, columnspan=3, sticky="ew", pady=8); row += 1
        add("Clips folder", ttk.Entry(f, textvariable=var("output_dir"), width=34),
            ttk.Button(f, text="Browse…", command=lambda: self._browse_dir("output_dir")))
        ff_btns = ttk.Frame(f)
        ttk.Button(ff_btns, text="Browse…", command=self._browse_ffmpeg).pack(side="left")
        self.dl_btn = ttk.Button(ff_btns, text="Download", command=self.download_ffmpeg)
        self.dl_btn.pack(side="left", padx=(4, 0))
        add("FFmpeg path (optional)", ttk.Entry(f, textvariable=var("ffmpeg_path"), width=34), ff_btns)
        add("Close button hides to tray", ttk.Checkbutton(f, variable=var("close_to_tray", tk.BooleanVar)))

        ttk.Separator(f).grid(row=row, column=0, columnspan=3, sticky="ew", pady=8); row += 1
        ttk.Label(f, text="Share links", font=("Segoe UI", 10, "bold")).grid(row=row, column=0, sticky="w"); row += 1
        add("Server URL", ttk.Entry(f, textvariable=var("share_server_url"), width=34),
            ttk.Button(f, text="Test", command=self.test_share))
        add("Upload key", ttk.Entry(f, textvariable=var("share_token"), width=34, show="•"))
        add("Auto-upload new clips", ttk.Checkbutton(f, variable=var("auto_upload", tk.BooleanVar)))
        add("Upload bitrate (Mbps, 0 = original)",
            ttk.Spinbox(f, from_=0, to=50, increment=1, textvariable=var("upload_bitrate_mbps", tk.IntVar), width=8))

        btns = ttk.Frame(f)
        btns.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(12, 4)); row += 1
        ttk.Button(btns, text="Apply & restart capture", command=self.apply).pack(side="left")
        ttk.Button(btns, text="Save clip now", command=self.save_clip).pack(side="left", padx=6)
        ttk.Button(btns, text="Open clips folder", command=lambda: capture.open_folder(self.cfg.output_dir)).pack(side="left")
        btns2 = ttk.Frame(f)
        btns2.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(0, 4)); row += 1
        ttk.Button(btns2, text="Share last clip", command=self.share_last).pack(side="left")
        ttk.Button(btns2, text="Share a file…", command=self.share_pick).pack(side="left", padx=6)
        ttk.Button(btns2, text="My uploads…", command=self.open_uploads).pack(side="left")

        self.status = tk.StringVar(value="Starting…")
        self.status_lbl = ttk.Label(f, textvariable=self.status, wraplength=520, justify="left")
        self.status_lbl.grid(row=row, column=0, columnspan=3, sticky="w", pady=(6, 0))

        self.refresh_devices()
        self._fill_monitor_combo()

    def _fill_monitor_combo(self) -> None:
        if self.monitors:
            labels = [f"{i}: {w}x{h}" for i, w, h in self.monitors]
        else:
            labels = [f"{self.cfg.monitor}: (click Detect)"]
        self.mon_combo["values"] = labels
        sel = next((l for l in labels if l.startswith(f"{self.cfg.monitor}:")), labels[0])
        self.mon_combo.set(sel)

    def detect_monitors(self) -> None:
        if not self.ffmpeg:
            self.set_status("FFmpeg not found - can't detect monitors.", error=True)
            return
        self.set_status("Detecting monitors…")
        ffmpeg = self.ffmpeg

        def worker():
            mons = capture.detect_monitors(ffmpeg)

            def done():
                self.monitors = mons
                self._fill_monitor_combo()
                self.set_status(f"Found {len(mons)} monitor(s)." if mons else
                                "No monitors found by Desktop Duplication (check ffmpeg.log).", error=not mons)
            self.ui_q.put(done)

        threading.Thread(target=worker, daemon=True).start()

    def _offer_ffmpeg_download(self) -> None:
        if messagebox.askyesno(
            "FFmpeg needed",
            "LCClips uses FFmpeg to capture and encode video.\n\n"
            "Download it now? (about 150 MB, one time - it's saved to %APPDATA%\\LCClips\\bin)",
            parent=self.root,
        ):
            self.download_ffmpeg()

    def download_ffmpeg(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.dl_btn.state(["disabled"])
        self.set_status("Downloading FFmpeg…")

        def progress(frac: float):
            self.ui_q.put(lambda: self.set_status(f"Downloading FFmpeg… {frac:.0%}"))

        def worker():
            try:
                exe = ffmpeg_setup.install(progress)

                def done():
                    self.vars["ffmpeg_path"].set("")  # auto-detected from %APPDATA%\LCClips\bin
                    self.ffmpeg = exe
                    self.cfg.ffmpeg_path = ""
                    self.cfg.save()
                    self.set_status("FFmpeg installed. Starting capture…")
                    self._busy = False
                    self.restart()
                self.ui_q.put(done)
            except Exception as e:
                log.exception("FFmpeg download failed")
                err = str(e)
                self.ui_q.put(lambda: self.set_status(
                    f"FFmpeg download failed: {err}. You can download it manually (see README) and Browse to ffmpeg.exe.",
                    error=True))
            finally:
                self._busy = False
                self.ui_q.put(lambda: self.dl_btn.state(["!disabled"]))

        threading.Thread(target=worker, daemon=True).start()

    def refresh_devices(self) -> None:
        self.out_combo["values"] = [DEVICE_DEFAULT, DEVICE_NONE, *audio.list_output_devices()]
        self.mic_combo["values"] = [DEVICE_DEFAULT, DEVICE_NONE, *audio.list_input_devices()]

    def _browse_dir(self, key: str) -> None:
        d = filedialog.askdirectory(initialdir=self.vars[key].get())
        if d:
            self.vars[key].set(d)

    def _browse_ffmpeg(self) -> None:
        p = filedialog.askopenfilename(filetypes=[("ffmpeg", "ffmpeg.exe"), ("All", "*.*")])
        if p:
            self.vars["ffmpeg_path"].set(p)

    def set_status(self, text: str, error: bool = False) -> None:
        self.status.set(text)
        self.status_lbl.configure(foreground="#c0392b" if error else "")

    # ------------------------------------------------------------------ hotkeys
    def bind_hotkey(self) -> None:
        try:
            import keyboard
        except Exception as e:
            self.set_status(f"Hotkeys unavailable: {e}", error=True)
            return
        if self._hotkey_handle is not None:
            try:
                keyboard.remove_hotkey(self._hotkey_handle)
            except (KeyError, ValueError):
                pass
            self._hotkey_handle = None
        try:
            self._hotkey_handle = keyboard.add_hotkey(self.cfg.hotkey, self.save_clip, suppress=False)
        except Exception as e:
            self.set_status(f"Invalid hotkey '{self.cfg.hotkey}': {e}", error=True)

    def record_hotkey(self) -> None:
        import keyboard

        self.vars["hotkey"].set("Press your hotkey…")

        def worker():
            hk = keyboard.read_hotkey(suppress=False)
            self.ui_q.put(lambda: self.vars["hotkey"].set(hk))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------ capture control
    def _collect(self) -> Config:
        cfg = Config(**{**self.cfg.__dict__})
        for k, v in self.vars.items():
            try:
                setattr(cfg, k, v.get())
            except tk.TclError:
                pass
        sel = self.mon_combo.get()
        if sel and sel.split(":")[0].isdigit():
            cfg.monitor = int(sel.split(":")[0])
        cfg.clip_seconds = max(5, min(600, int(cfg.clip_seconds)))
        cfg.fps = max(10, min(240, int(cfg.fps)))
        return cfg

    def apply(self) -> None:
        self.cfg = self._collect()
        self.cfg.save()
        self.ffmpeg = capture.find_ffmpeg(self.cfg.ffmpeg_path)
        if not self.ffmpeg:
            self.set_status("FFmpeg not found.", error=True)
            return
        self.restart()

    def restart(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.bind_hotkey()
        self.set_status("Starting capture (probing GPU encoder)…")
        cfg, ffmpeg, old = self.cfg, self.ffmpeg, self.recorder
        self.recorder = None

        def worker():
            try:
                if old:
                    old.stop()
                if not self.monitors:
                    self.monitors = capture.detect_monitors(ffmpeg)
                    self.ui_q.put(self._fill_monitor_combo)
                mon = next((m for m in self.monitors if m[0] == cfg.monitor), None)
                if mon is None:
                    raise RuntimeError(f"Monitor {cfg.monitor} not found by Desktop Duplication "
                                       f"(found: {[m[0] for m in self.monitors]})")
                plan = capture.plan_video(ffmpeg, cfg, mon[1], mon[2])
                rec = capture.Recorder(cfg, ffmpeg, plan)
                rec.start()
                self.recorder = rec
                path = "GPU zero-copy" if plan.gpu_path else "CPU scaling path"
                msg = (f"● Recording buffer: {plan.width}x{plan.height}@{cfg.fps} · {plan.encoder} ({path}) · "
                       f"last {cfg.clip_seconds}s · press {cfg.hotkey.upper()} to clip")
                self.ui_q.put(lambda: self.set_status(msg))
            except Exception as e:
                log.exception("Start failed")
                err = str(e)
                self.ui_q.put(lambda: self.set_status(f"Capture failed: {err}", error=True))
            finally:
                self._busy = False

        threading.Thread(target=worker, daemon=True).start()

    def save_clip(self) -> None:
        rec = self.recorder
        if rec is None:
            _beep(False)
            return

        def worker():
            try:
                out = rec.save_clip()
                self.last_clip = out
                _beep(True)
                self.ui_q.put(lambda: self.set_status(f"✔ Saved {out.name}"))
                if self.cfg.auto_upload and self.cfg.share_server_url:
                    self.ui_q.put(lambda: self.share_file(out))
                if self.tray:
                    try:
                        self.tray.notify(out.name, "Clip saved")
                    except Exception:
                        pass
            except Exception as e:
                _beep(False)
                err = str(e)
                self.ui_q.put(lambda: self.set_status(f"Save failed: {err}", error=True))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------ share links
    def _copy(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()

    def _notify(self, msg: str, title: str) -> None:
        if self.tray:
            try:
                self.tray.notify(msg, title)
            except Exception:
                pass

    def share_last(self) -> None:
        if self.last_clip and self.last_clip.exists():
            self.share_file(self.last_clip)
        else:
            self.share_pick()

    def share_pick(self) -> None:
        p = filedialog.askopenfilename(initialdir=self.cfg.output_dir, filetypes=[("Video", "*.mp4"), ("All", "*.*")])
        if p:
            self.share_file(Path(p))

    def share_file(self, clip: Path) -> None:
        if self._sharing:
            self.set_status("An upload is already running…")
            return
        cfg = self._collect()
        if not cfg.share_server_url or not cfg.share_token:
            self.set_status("Set the share server URL and upload key first (run server\\setup.bat).", error=True)
            return
        self._sharing = True
        encoder = self.recorder.plan.encoder if self.recorder else "h264_nvenc"

        def progress(stage: str, frac: float):
            self.ui_q.put(lambda: self.set_status(f"⇪ {stage} {clip.name}… {frac:.0%}"))

        def worker():
            try:
                item = share.share_clip(self.ffmpeg, cfg.share_server_url, cfg.share_token, clip,
                                        encoder=encoder, upload_mbps=cfg.upload_bitrate_mbps, progress=progress)

                def done():
                    self._copy(item.url)
                    self.set_status(f"🔗 Link copied: {item.url}")
                self.ui_q.put(done)
                _beep(True)
                self._notify(item.url, "Clip link copied")
            except Exception as e:
                log.exception("Share failed")
                _beep(False)
                err = str(e)
                self.ui_q.put(lambda: self.set_status(f"Upload failed: {err}", error=True))
            finally:
                self._sharing = False

        threading.Thread(target=worker, daemon=True).start()

    def test_share(self) -> None:
        cfg = self._collect()

        def worker():
            try:
                share.ShareClient(cfg.share_server_url, cfg.share_token).ping()
                self.ui_q.put(lambda: self.set_status("✔ Share server reachable and upload key accepted."))
            except Exception as e:
                err = str(e)
                self.ui_q.put(lambda: self.set_status(f"Share test failed: {err}", error=True))

        threading.Thread(target=worker, daemon=True).start()

    def open_uploads(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("My uploads")
        win.geometry("620x320")
        lb = tk.Listbox(win, activestyle="none")
        lb.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        items: list[share.Upload] = []

        def reload():
            items[:] = share.load_history()
            lb.delete(0, "end")
            for u in items:
                lb.insert("end", f"{u.created.replace('T', ' ')}   {u.title}   {u.url}")

        def sel() -> share.Upload | None:
            i = lb.curselection()
            return items[i[0]] if i else None

        def copy():
            if (u := sel()):
                self._copy(u.url)
                self.set_status(f"🔗 Link copied: {u.url}")

        def open_():
            if (u := sel()):
                webbrowser.open(u.url)

        def delete():
            u = sel()
            if not u or not messagebox.askyesno("Delete clip", f"Delete '{u.title}' from the share server?\nThe link will stop working.", parent=win):
                return
            cfg = self._collect()
            try:
                share.delete_upload(share.ShareClient(cfg.share_server_url, cfg.share_token), u)
            except Exception as e:
                messagebox.showerror("Delete failed", str(e), parent=win)
            reload()

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(bar, text="Copy link", command=copy).pack(side="left")
        ttk.Button(bar, text="Open", command=open_).pack(side="left", padx=6)
        ttk.Button(bar, text="Delete from server", command=delete).pack(side="left")
        lb.bind("<Double-Button-1>", lambda _e: open_())
        reload()

    # ------------------------------------------------------------------ loops
    def _pump(self) -> None:
        while True:
            try:
                fn = self.ui_q.get_nowait()
            except queue.Empty:
                break
            fn()
        self.root.after(100, self._pump)

    def _watchdog(self) -> None:
        rec = self.recorder
        if rec is not None and not rec.alive() and not self._busy:
            tail = capture.tail_ffmpeg_log()
            self.set_status(f"FFmpeg stopped unexpectedly. Log tail:\n{tail}", error=True)
            self.recorder = None
        elif rec is not None and rec.mixer and rec.mixer.errors():
            self.set_status("Audio warning: " + "; ".join(rec.mixer.errors()), error=True)
        self.root.after(2000, self._watchdog)

    # ------------------------------------------------------------------ tray / lifecycle
    def _setup_tray(self) -> None:
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception as e:
            log.warning("Tray icon unavailable (%s); closing the window will exit", e)
            return
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((6, 6, 58, 58), fill=(220, 40, 50, 255))
        d.ellipse((22, 22, 42, 42), fill=(255, 255, 255, 255))
        menu = pystray.Menu(
            pystray.MenuItem("Settings", lambda: self.ui_q.put(self.show), default=True),
            pystray.MenuItem("Save clip", lambda: self.save_clip()),
            pystray.MenuItem("Share last clip", lambda: self.ui_q.put(self.share_last)),
            pystray.MenuItem("Open clips folder", lambda: capture.open_folder(self.cfg.output_dir)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Close LCClips", lambda: self.ui_q.put(self.quit)),
        )
        self.tray = pystray.Icon(APP_NAME, img, APP_NAME, menu)
        self.tray.run_detached()

    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()

    def on_close(self) -> None:
        close_to_tray = bool(self.vars["close_to_tray"].get()) if "close_to_tray" in self.vars else self.cfg.close_to_tray
        if not (self.tray and close_to_tray):
            self.quit()
            return
        self.root.withdraw()  # keep recording in the background
        if not self.cfg.tray_notice_shown:
            self._notify("LCClips is still running. Right-click the tray icon and choose 'Close LCClips' to exit.",
                         "Still running in the tray")
            self.cfg.tray_notice_shown = True
            self.cfg.save()

    def quit(self) -> None:
        if self.recorder:
            self.recorder.stop()
        if self.tray:
            self.tray.stop()
        self.root.destroy()

    def run(self) -> None:
        if "--minimized" in sys.argv and self.tray:
            self.root.withdraw()
        self.root.mainloop()


def main() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(APP_DIR / "lcclips.log", encoding="utf-8"), logging.StreamHandler()],
    )
    if sys.platform != "win32":
        print("Note: LCClips's capture path (ddagrab/WASAPI) is Windows-only.")
    App().run()
