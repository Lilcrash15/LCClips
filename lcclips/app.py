"""LCClips main window -- LC-line look (see gui/theme.py): toolbar strip,
status row, accent-bordered notebook with Clips / Settings / Themes tabs."""
from __future__ import annotations

import ctypes
import logging
import queue
import shutil
import sys
import threading
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__, audio, capture, ffmpeg_setup, hotkeys, library, share, updater, winapp
from .config import APP_DIR, APP_NAME, DEVICE_DEFAULT, DEVICE_NONE, ENCODERS, RESOLUTIONS, Config
from .editor import Editor
from .gui import theme
from .gui.icon import make_icon
from .gui.overlay import POSITIONS, Overlay

log = logging.getLogger(APP_NAME)

POLL_MS = 100
RECENT_COUNT = 8  # upper bound; the strip shows as many as fit
CARD_W = 172
PREVIEW_W = 300
REPO_URL = "https://github.com/Lilcrash15/LCClips"


SOUNDS_ON = True


def _beep(ok: bool) -> None:
    if not SOUNDS_ON:
        return
    try:
        import winsound

        winsound.MessageBeep(winsound.MB_OK if ok else winsound.MB_ICONHAND)
    except Exception:
        pass


def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def fmt_duration(s: float) -> str:
    if not s:
        return "–"
    m, sec = divmod(int(round(s)), 60)
    return f"{m}:{sec:02d}"


def fmt_ago(dt: datetime) -> str:
    secs = (datetime.now() - dt).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        return f"{int(secs // 3600)} h ago"
    if secs < 7 * 86400:
        return f"{int(secs // 86400)} d ago"
    return dt.strftime("%b %d, %Y")


class App:
    def __init__(self) -> None:
        self.cfg = Config.load()
        self.ffmpeg = capture.find_ffmpeg(self.cfg.ffmpeg_path)
        self.recorder: capture.Recorder | None = None
        self.monitors: list[tuple[int, int, int]] = []
        self.ui_q: queue.Queue = queue.Queue()
        self._busy = False
        self._sharing: set[str] = set()
        self.last_clip: Path | None = None
        self.tray = None
        self.clips: list[library.Clip] = []
        self.selected: str | None = None
        self._images: dict[tuple[str, int], object] = {}
        self._refresh_pending = False
        self.library = library.Library(lambda: self.ffmpeg, lambda key: self.ui_q.put(self._schedule_refresh))
        # LC Editor runs in-process (local HTTP server + Edge app window), sharing our FFmpeg.
        self.editor = Editor(APP_DIR / "editor", lambda: self.ffmpeg, accent=self._editor_accent,
                             on_export_done=lambda path: self.ui_q.put(lambda: self._on_editor_export(path)))

        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} -- v{__version__}")
        self.root.geometry("1200x800")
        self.root.minsize(1060, 700)
        theme.apply_theme(self.root, theme.resolve_colors(self.cfg.theme))
        self._apply_titlebar(self.root)
        self._apply_icon()
        # Readonly comboboxes keep a stuck accent "selected" fill after a pick (same fix as LCBot).
        self.root.bind_class("TCombobox", "<<ComboboxSelected>>", lambda e: e.widget.selection_clear(), add="+")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        global SOUNDS_ON
        SOUNDS_ON = self.cfg.sounds
        self.hotkey = hotkeys.GlobalHotkey(self.save_clip)
        self._hotkey_warning: str | None = None
        self._saving = False
        self.overlay = Overlay(self.root)

        self._build_toolbar()
        self._build_ui()
        self._setup_tray()
        self.root.after(POLL_MS, self._pump)
        self.root.after(2000, self._watchdog)
        self.root.after(60_000, self._tick_minute)
        self.refresh_clips()
        self._update_info: updater.UpdateInfo | None = None
        self.root.after(3000, self._auto_update_check)
        self.root.after(5000, self._expire_shares)
        if "--updated" in sys.argv:
            self.root.after(800, lambda: self.set_status(f"✔ Updated to v{__version__}", "ok"))

        if not self.ffmpeg:
            self.set_status("FFmpeg not found. Click Download next to FFmpeg path in Settings.", "error")
            self.root.after(400, self._offer_ffmpeg_download)
        else:
            self.restart()

    # ================================================================== chrome
    def _apply_titlebar(self, window: tk.Misc) -> None:
        if sys.platform != "win32":
            return
        try:
            window.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
            val = ctypes.c_int(1 if theme.current_is_dark() else 0)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(val), ctypes.sizeof(val)) != 0:
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(val), ctypes.sizeof(val))
        except Exception:
            pass

    def _icon_image(self, size: int = 64):
        return make_icon(size, theme.ACCENT)

    def _apply_icon(self) -> None:
        try:
            icon = self._icon_image(256)
            if sys.platform == "win32":
                ico = APP_DIR / "icon.ico"
                APP_DIR.mkdir(parents=True, exist_ok=True)
                icon.save(ico, sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
                self.root.iconbitmap(str(ico))
                self.root.iconbitmap(default=str(ico))
            else:
                from PIL import ImageTk

                self._icon_photo = ImageTk.PhotoImage(icon.resize((64, 64)))
                self.root.iconphoto(True, self._icon_photo)
        except Exception as e:
            log.info("App icon not applied: %s", e)

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self.root, style="Toolbar.TFrame")
        bar.pack(side="top", fill="x")
        self.file_menu = tk.Menu(self.root, tearoff=0, **theme.popup_menu_kwargs())
        self.file_menu.add_command(label="Save clip now", command=self.save_clip)
        self.file_menu.add_command(label="Share a file…", command=self.share_pick)
        self.file_menu.add_command(label="Open LC Editor…", command=lambda: self.open_editor(None))
        self.file_menu.add_command(label="Open clips folder", command=lambda: capture.open_folder(self.cfg.output_dir))
        self.file_menu.add_separator()
        self.file_menu.add_command(label="Close LCClips", command=self.quit)
        ttk.Menubutton(bar, text="File", menu=self.file_menu, style="Toolbar.TMenubutton").pack(side="left", padx=(4, 0), pady=2)
        self.help_menu = tk.Menu(self.root, tearoff=0, **theme.popup_menu_kwargs())
        self.help_menu.add_command(label="Check for updates", command=lambda: self.check_for_updates(manual=True))
        self.help_menu.add_command(label="About", command=self._show_about)
        self.help_menu.add_command(label="Set up share links…", command=self._open_server_folder)
        self.help_menu.add_command(label="Open logs folder", command=lambda: capture.open_folder(str(APP_DIR)))
        self.help_menu.add_command(label="LCClips on GitHub", command=lambda: webbrowser.open(REPO_URL))
        ttk.Menubutton(bar, text="Help", menu=self.help_menu, style="Toolbar.TMenubutton").pack(side="left", padx=(2, 0), pady=2)

    def _open_server_folder(self) -> None:
        base = Path(sys.executable).parent if winapp.IS_FROZEN else Path(__file__).resolve().parent.parent
        folder = base / "server"
        if not folder.is_dir():
            webbrowser.open(REPO_URL + "/tree/main/server")
            return
        messagebox.showinfo("Set up share links",
                            "In the folder that opens, read README.md, then double-click setup.bat.\n"
                            "It deploys your free Cloudflare share server and fills in LCClips' settings.",
                            parent=self.root)
        capture.open_folder(str(folder))

    def _show_about(self) -> None:
        messagebox.showinfo(f"About {APP_NAME}",
                            f"{APP_NAME} v{__version__}\n\nLightweight GPU game clipping.\n{REPO_URL}", parent=self.root)

    # ================================================================== layout
    def _build_ui(self) -> None:
        top = ttk.Frame(self.root, padding=(10, 8))
        top.pack(fill="x")
        self.rec_label = ttk.Label(top, text="● REC", style="RecOff.TLabel")
        self.rec_label.pack(side="left")
        self.status_label = ttk.Label(top, text="Starting…", style="Muted.TLabel")
        self.status_label.pack(side="left", padx=10)
        ttk.Button(top, text="Save clip", style="Accent.TButton", command=self.save_clip).pack(side="right")
        self.hotkey_hint = ttk.Label(top, text="", style="Muted.TLabel")
        self.hotkey_hint.pack(side="right", padx=10)
        self.update_label = ttk.Label(top, text="", style="UpdateAvailable.TLabel", cursor="hand2")
        self.update_label.pack(side="right", padx=10)
        self.update_label.bind("<Button-1>", lambda e: self.show_update_dialog())

        self.border = tk.Frame(self.root, bg=theme.ACCENT)
        self.border.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.inner = tk.Frame(self.border, bg=theme.BG)
        self.inner.pack(fill="both", expand=True, padx=2, pady=2)
        self.notebook = ttk.Notebook(self.inner)
        self.notebook.pack(fill="both", expand=True)
        self.clips_tab = ttk.Frame(self.notebook)
        self.settings_tab = ttk.Frame(self.notebook)
        self.themes_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.clips_tab, text="Clips")
        self.notebook.add(self.settings_tab, text="Settings")
        self.notebook.add(self.themes_tab, text="Themes")
        self._build_clips_tab()
        self._build_settings_tab()
        self._build_themes_tab()

    # ------------------------------------------------------------------ Clips tab
    def _build_clips_tab(self) -> None:
        wrap = ttk.Frame(self.clips_tab, padding=(14, 12))
        wrap.pack(fill="both", expand=True)
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        f = ttk.Frame(wrap)
        f.grid(row=0, column=0, sticky="nsew")
        self._build_details(wrap)

        head = ttk.Frame(f)
        head.pack(fill="x")
        ttk.Label(head, text="Recent clips", style="Heading.TLabel").pack(side="left")
        ttk.Button(head, text="↻", style="Icon.TButton", width=3, command=self.refresh_clips).pack(side="right")
        ttk.Button(head, text="Open clips folder", command=lambda: capture.open_folder(self.cfg.output_dir)).pack(side="right", padx=6)
        self.recent_row = ttk.Frame(f)
        self.recent_row.pack(fill="x", pady=(8, 14))
        self._recent_fit = RECENT_COUNT
        self.recent_row.bind("<Configure>", self._on_recent_resize)

        head2 = ttk.Frame(f)
        head2.pack(fill="x")
        ttk.Label(head2, text="All clips", style="Heading.TLabel").pack(side="left")
        self.count_label = ttk.Label(head2, text="", style="Muted.TLabel")
        self.count_label.pack(side="left", padx=8)
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._fill_tree())
        search = ttk.Entry(head2, textvariable=self.search_var, width=28)
        search.pack(side="right")
        ttk.Label(head2, text="Search", style="Muted.TLabel").pack(side="right", padx=6)

        body = ttk.Frame(f)
        body.pack(fill="both", expand=True, pady=(8, 0))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        cols = ("game", "date", "length", "res", "size", "link")
        self.tree = ttk.Treeview(body, columns=cols, show="headings", selectmode="browse")
        for col, text, w, anchor in [("game", "Clip", 220, "w"), ("date", "Saved", 150, "w"),
                                     ("length", "Length", 70, "center"), ("res", "Resolution", 100, "center"),
                                     ("size", "Size", 80, "e"), ("link", "Shared", 70, "center")]:
            self.tree.heading(col, text=text, anchor=anchor)
            self.tree.column(col, width=w, anchor=anchor, stretch=(col == "game"))
        sb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Double-1>", lambda e: self.play_selected())
        self.tree.bind("<Delete>", lambda e: self.delete_selected())

    def _build_details(self, parent: ttk.Frame) -> None:
        d = ttk.Frame(parent, style="Card.TFrame", padding=12)
        d.grid(row=0, column=1, sticky="nsew", padx=(14, 0))
        ttk.Label(d, text="Selected clip", style="CardMuted.TLabel").pack(anchor="w", pady=(0, 6))
        self.d_thumb = ttk.Label(d, text="Select a clip", style="Thumb.TLabel")
        self.d_thumb.pack(fill="x")
        self.d_title = ttk.Label(d, text="", style="CardTitle.TLabel", wraplength=PREVIEW_W)
        self.d_title.pack(anchor="w", pady=(8, 0))
        self.d_meta = ttk.Label(d, text="", style="CardMuted.TLabel", wraplength=PREVIEW_W, justify="left")
        self.d_meta.pack(anchor="w")
        linkrow = ttk.Frame(d, style="Card.TFrame")
        linkrow.pack(fill="x", pady=(4, 0))
        self.d_link = ttk.Label(linkrow, text="", style="CardLink.TLabel", cursor="hand2")
        self.d_link.pack(side="left")
        self.d_link.bind("<Button-1>", lambda e: self._open_link())
        self.d_unlink = ttk.Label(linkrow, text="remove", style="CardMuted.TLabel", cursor="hand2")
        self.d_unlink.bind("<Button-1>", lambda e: self.unshare_selected())

        btns = ttk.Frame(d, style="Card.TFrame")
        btns.pack(fill="x", pady=(10, 0))
        for c in range(3):
            btns.columnconfigure(c, weight=1, uniform="b")
        self.b_share = ttk.Button(btns, text="↗  Share", style="Accent.TButton", command=self.share_selected)
        self.b_copy = ttk.Button(btns, text="Copy link", command=self.copy_selected_link)
        self.b_download = ttk.Button(btns, text="↓ Download", command=self.download_selected)
        self.b_folder = ttk.Button(btns, text="Open folder", command=self.reveal_selected)
        self.b_play = ttk.Button(btns, text="▶ Play", command=self.play_selected)
        self.b_delete = ttk.Button(btns, text="Delete", command=self.delete_selected)
        layout = [(self.b_share, 0, 0), (self.b_copy, 0, 1), (self.b_download, 0, 2),
                  (self.b_play, 1, 0), (self.b_folder, 1, 1), (self.b_delete, 1, 2)]
        for b, r, c in layout:
            b.configure(padding=(6, 7))
            b.grid(row=r, column=c, sticky="ew", padx=2, pady=2)
        self.b_edit = ttk.Button(btns, text="✂  Edit in LC Editor", command=self.edit_selected, padding=(6, 7))
        self.b_edit.grid(row=2, column=0, columnspan=3, sticky="ew", padx=2, pady=(6, 2))
        self._detail_buttons = [b for b, *_ in layout] + [self.b_edit]
        self._update_details()

    # ------------------------------------------------------------------ clip data
    def _schedule_refresh(self) -> None:
        if not self._refresh_pending:
            self._refresh_pending = True
            self.root.after(300, self.refresh_clips)

    def refresh_clips(self) -> None:
        self._refresh_pending = False
        self.clips = self.library.scan(self.cfg.output_dir)
        self._uploads = {u.file: u for u in share.active_uploads(self.cfg.share_expire_days)}
        if self.selected and not any(c.key == self.selected for c in self.clips):
            self.selected = None
        self._build_recent()
        self._fill_tree()
        self._update_details()

    def _clip(self, key: str | None) -> library.Clip | None:
        return next((c for c in self.clips if c.key == key), None) if key else None

    def _upload_for(self, clip: library.Clip) -> share.Upload | None:
        return self._uploads.get(str(clip.path))

    def _image(self, clip: library.Clip, width: int):
        """Thumbnail scaled to `width` (16:9). Cached per file + size."""
        p = self.library.thumb_path(clip)
        k = (str(p), width)
        if k in self._images:
            return self._images[k]
        if not p.exists():
            return None
        try:
            from PIL import Image, ImageTk

            with Image.open(p) as im:
                img = ImageTk.PhotoImage(im.convert("RGB").resize((width, width * 9 // 16), Image.LANCZOS))
        except Exception:
            try:  # Pillow missing: Tk can read PNG itself, integer downscale only
                img = tk.PhotoImage(file=str(p))
                factor = max(1, library.THUMB_W // width)
                if factor > 1:
                    img = img.subsample(factor, factor)
            except tk.TclError:
                return None
        self._images[k] = img
        return img

    def _build_recent(self) -> None:
        for w in self.recent_row.winfo_children():
            w.destroy()
        recent = self.clips[:self._recent_fit]
        if not recent:
            ttk.Label(self.recent_row, text=f"No clips yet. Press {self.cfg.hotkey.upper()} in game to save your first one.",
                      style="Muted.TLabel").pack(anchor="w", pady=20)
            return
        for clip in recent:
            sel = clip.key == self.selected
            outer = ttk.Frame(self.recent_row, style="SelectedCard.TFrame" if sel else "Card.TFrame", padding=2)
            outer.pack(side="left", padx=(0, 10))
            card = ttk.Frame(outer, style="Card.TFrame", padding=6)
            card.pack()
            img = self._image(clip, CARD_W)
            thumb = ttk.Label(card, style="Thumb.TLabel")
            if img:
                thumb.configure(image=img)
            else:
                thumb.configure(text="…", width=27)
                thumb.configure(padding=(0, 40))
            thumb.pack()
            shared = " · shared" if self._upload_for(clip) else ""
            title = ttk.Label(card, text=clip.title[:26], style="Card.TLabel", font=(theme.FONT, 9, "bold"))
            title.pack(anchor="w", pady=(6, 0))
            meta = ttk.Label(card, text=f"{fmt_ago(clip.created)} · {fmt_duration(clip.duration)}{shared}",
                             style="CardMuted.TLabel")
            meta.pack(anchor="w")
            for w in (outer, card, thumb, title, meta):
                w.bind("<Button-1>", lambda e, k=clip.key: self.select_clip(k))
                w.bind("<Double-1>", lambda e, k=clip.key: (self.select_clip(k), self.play_selected()))
                w.configure(cursor="hand2")

    def _on_recent_resize(self, e) -> None:
        fit = max(1, min(8, (e.width + 10) // (CARD_W + 26)))
        if fit != self._recent_fit:
            self._recent_fit = fit
            self.root.after_idle(self._build_recent)

    def _fill_tree(self) -> None:
        q = self.search_var.get().strip().lower() if hasattr(self, "search_var") else ""
        self.tree.delete(*self.tree.get_children())
        shown = 0
        for c in self.clips:
            if q and q not in c.title.lower() and q not in c.path.name.lower():
                continue
            res = f"{c.width}×{c.height}" if c.width else "–"
            self.tree.insert("", "end", iid=c.key, values=(
                c.title, c.created.strftime("%Y-%m-%d  %H:%M"), fmt_duration(c.duration), res,
                fmt_size(c.size), "✔" if self._upload_for(c) else ""))
            shown += 1
        total = len(self.clips)
        self.count_label.configure(text=f"{total} clip{'s' if total != 1 else ''}" +
                                   (f" · {shown} shown" if q else ""))
        if self.selected and self.tree.exists(self.selected):
            self.tree.selection_set(self.selected)
            self.tree.see(self.selected)

    def select_clip(self, key: str) -> None:
        self.selected = key
        if self.tree.exists(key):
            self.tree.selection_set(key)
            self.tree.see(key)
        self._build_recent()
        self._update_details()

    def _on_tree_select(self, _e=None) -> None:
        sel = self.tree.selection()
        if sel and sel[0] != self.selected:
            self.selected = sel[0]
            self._build_recent()
            self._update_details()

    def _update_details(self) -> None:
        clip = self._clip(self.selected)
        if not clip:
            self.d_thumb.configure(image="", text="Select a clip to preview it", padding=(0, 76), style="Thumb.TLabel")
            self.d_title.configure(text="")
            self.d_meta.configure(text="")
            self.d_link.configure(text="")
            self.d_unlink.pack_forget()
            for b in self._detail_buttons:
                b.state(["disabled"])
            return
        img = self._image(clip, PREVIEW_W)
        if img:
            self.d_thumb.configure(image=img, text="", padding=0, style="Preview.TLabel")
        else:
            self.d_thumb.configure(image="", text="Generating preview…", padding=(0, 76), style="Thumb.TLabel")
        self.d_title.configure(text=clip.title)
        res = f"{clip.width}×{clip.height}" if clip.width else "…"
        self.d_meta.configure(text=f"{clip.created.strftime('%a, %b %d %Y · %H:%M')}  ·  {fmt_duration(clip.duration)}\n"
                                   f"{res} · {fmt_size(clip.size)}")
        up = self._upload_for(clip)
        sharing = clip.key in self._sharing
        self.d_link.configure(text=up.url.split("://", 1)[-1] if up else ("Uploading…" if sharing else "Not shared yet"),
                              style="CardLink.TLabel" if up else "CardMuted.TLabel")
        if up:
            self.d_unlink.pack(side="left", padx=(8, 0))
        else:
            self.d_unlink.pack_forget()
        for b in self._detail_buttons:
            b.state(["!disabled"])
        self.b_share.configure(text="↗ Shared" if up else ("Uploading…" if sharing else "↗ Share"))
        if sharing:
            self.b_share.state(["disabled"])
        if not up:
            self.b_copy.state(["disabled"])

    # ------------------------------------------------------------------ shared-clip auto-delete
    def _expire_shares(self) -> None:
        cfg = self.cfg

        def worker():
            try:
                deleted, _failed = share.expire_old_uploads(cfg.share_server_url, cfg.share_token, cfg.share_expire_days)
                if deleted:
                    self.ui_q.put(lambda: (self.set_status(
                        f"Removed {deleted} shared clip(s) older than {cfg.share_expire_days} days from your share server."),
                        self.refresh_clips()))
            except Exception:
                log.exception("Auto-delete failed")

        threading.Thread(target=worker, daemon=True).start()
        self.root.after(3_600_000, self._expire_shares)  # hourly while running

    # ------------------------------------------------------------------ updates
    def _auto_update_check(self) -> None:
        if self.cfg.check_updates:
            self.check_for_updates(manual=False)
        self.root.after(6 * 3_600_000, self._auto_update_check)  # every 6 h while in the tray

    def check_for_updates(self, manual: bool) -> None:
        if manual:
            self.set_status("Checking for updates…")

        def worker():
            try:
                info = updater.check()
            except updater.UpdateError as e:
                err = str(e)
                if manual:
                    self.ui_q.put(lambda: self.set_status(f"Update check failed: {err}", "error"))
                return

            def done():
                self._update_info = info
                if info:
                    self.update_label.configure(text=f"⬆ Update available: {info.version}")
                    if manual:
                        self.show_update_dialog()
                    else:
                        self._notify(f"LCClips {info.version} is available. Open LCClips to update.", "Update available")
                else:
                    self.update_label.configure(text="")
                    if manual:
                        self.set_status(f"✔ You're up to date (v{__version__}).", "ok")
            self.ui_q.put(done)

        threading.Thread(target=worker, daemon=True).start()

    def show_update_dialog(self) -> None:
        info = self._update_info
        if not info:
            return
        self.show()
        win = tk.Toplevel(self.root)
        win.title("Update LCClips")
        win.configure(bg=theme.BG)
        win.transient(self.root)
        win.resizable(False, False)
        self._apply_titlebar(win)
        f = ttk.Frame(win, padding=18)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text=f"LCClips {info.version} is available", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(f, text=f"You have v{__version__}  ·  download {info.size / 1e6:.0f} MB",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 10))
        notes = tk.Text(f, width=70, height=14, wrap="word", relief="flat", bg=theme.PANEL_BG, fg=theme.FG,
                        highlightthickness=1, highlightbackground=theme.ACCENT_DIM, padx=10, pady=8,
                        font=(theme.FONT, 9))
        self._render_notes(notes, info.notes or "No release notes.")
        notes.configure(state="disabled")
        notes.pack(fill="both")
        bar = ttk.Progressbar(f, mode="determinate", maximum=1.0)
        msg = ttk.Label(f, text="", style="Muted.TLabel")
        msg.pack(anchor="w", pady=(10, 0))
        btns = ttk.Frame(f)
        btns.pack(fill="x", pady=(10, 0))
        go = ttk.Button(btns, text="Update now", style="Accent.TButton")
        go.pack(side="left")
        ttk.Button(btns, text="View on GitHub", command=lambda: webbrowser.open(info.page_url)).pack(side="left", padx=6)
        later = ttk.Button(btns, text="Later", command=win.destroy)
        later.pack(side="right")

        if not updater.can_self_update():
            go.state(["disabled"])
            msg.configure(text="You're running LCClips from source - update with git pull instead.")

        def start():
            go.state(["disabled"])
            later.state(["disabled"])
            bar.pack(fill="x", pady=(10, 0), before=btns)
            msg.configure(text="Downloading…", style="Muted.TLabel")

            def progress(frac: float):
                self.ui_q.put(lambda: (bar.configure(value=frac), msg.configure(text=f"Downloading… {frac:.0%}")))

            def worker():
                try:
                    path = updater.download(info, progress)
                except updater.UpdateError as e:
                    err = str(e)

                    def failed():
                        msg.configure(text=err, style="Error.TLabel")
                        go.state(["!disabled"])
                        later.state(["!disabled"])
                    self.ui_q.put(failed)
                    return

                def install():
                    msg.configure(text="Installing - LCClips will close and reopen in a few seconds…")
                    try:
                        updater.launch_installer(path)
                    except OSError as e:
                        msg.configure(text=f"Couldn't start the installer: {e}", style="Error.TLabel")
                        return
                    self.root.after(500, self.quit)
                self.ui_q.put(install)

            threading.Thread(target=worker, daemon=True).start()

        go.configure(command=start)

    @staticmethod
    def _render_notes(widget: tk.Text, md: str) -> None:
        """Just enough Markdown for release notes: headings, bullets, **bold**, `code`."""
        import re

        widget.tag_configure("h", foreground=theme.ACCENT, font=(theme.FONT, 10, "bold"), spacing1=6, spacing3=2)
        widget.tag_configure("b", font=(theme.FONT, 9, "bold"))
        for line in md.replace("\r", "").splitlines():
            m = re.match(r"^\s*#{1,6}\s*(.*)", line)
            if m:
                widget.insert("end", m.group(1) + "\n", "h")
                continue
            line = re.sub(r"^(\s*)[-*]\s+", r"\1•  ", line)
            for i, part in enumerate(re.split(r"\*\*(.+?)\*\*", line)):
                widget.insert("end", part.replace("`", ""), "b" if i % 2 else ())
            widget.insert("end", "\n")

    def _tick_minute(self) -> None:
        self._build_recent()  # keeps "x min ago" fresh
        self.root.after(60_000, self._tick_minute)

    # ------------------------------------------------------------------ clip actions
    def _selected_clip(self) -> library.Clip | None:
        clip = self._clip(self.selected)
        if clip and not clip.path.exists():
            self.set_status("That clip file no longer exists.", "error")
            self.refresh_clips()
            return None
        return clip

    # ------------------------------------------------------------------ LC Editor
    def _editor_accent(self) -> tuple[str, str]:
        a = theme.ACCENT.lstrip("#")
        r, g, b = (int(a[i:i + 2], 16) for i in (0, 2, 4))
        lighter = "#%02x%02x%02x" % tuple(int(c + (255 - c) * 0.3) for c in (r, g, b))
        return theme.ACCENT, lighter

    def open_editor(self, path: Path | None) -> None:
        if not self.ffmpeg:
            self.set_status("LC Editor needs FFmpeg - download it in Settings first.", "error")
            return
        try:
            self.editor.open(str(path) if path else None)
            self.set_status(f"Opened {path.name} in LC Editor." if path else "Opened LC Editor.")
        except Exception as e:
            log.exception("Couldn't open LC Editor")
            self.set_status(f"Couldn't open LC Editor: {e}", "error")

    def edit_selected(self) -> None:
        if (c := self._selected_clip()):
            self.open_editor(c.path)

    def _on_editor_export(self, path: str) -> None:
        self.refresh_clips()
        if Path(path).parent.resolve() == Path(self.cfg.output_dir).resolve():
            self.selected = str(Path(path))
            self.refresh_clips()
        self.set_status(f"✔ LC Editor saved {Path(path).name}", "ok")

    def play_selected(self) -> None:
        if (c := self._selected_clip()):
            library.play(c.path)

    def reveal_selected(self) -> None:
        if (c := self._selected_clip()):
            library.reveal_in_explorer(c.path)

    def share_selected(self) -> None:
        if (c := self._selected_clip()):
            self.share_file(c.path)

    def copy_selected_link(self) -> None:
        c = self._clip(self.selected)
        up = c and self._upload_for(c)
        if up:
            self._copy(up.url)
            self.set_status(f"Link copied: {up.url}", "ok")

    def _open_link(self) -> None:
        c = self._clip(self.selected)
        up = c and self._upload_for(c)
        if up:
            webbrowser.open(up.url)

    def download_selected(self) -> None:
        c = self._selected_clip()
        if not c:
            return
        downloads = Path.home() / "Downloads"
        dest = filedialog.asksaveasfilename(
            parent=self.root, title="Save a copy of this clip",
            initialdir=str(downloads if downloads.is_dir() else Path.home()),
            initialfile=c.path.name, defaultextension=".mp4", filetypes=[("MP4 video", "*.mp4")])
        if not dest:
            return
        self.set_status(f"Saving a copy to {dest}…")

        def worker():
            try:
                shutil.copy2(c.path, dest)
                self.ui_q.put(lambda: self.set_status(f"✔ Saved a copy to {dest}", "ok"))
            except OSError as e:
                err = str(e)
                self.ui_q.put(lambda: self.set_status(f"Couldn't save copy: {err}", "error"))

        threading.Thread(target=worker, daemon=True).start()

    def delete_selected(self) -> None:
        c = self._selected_clip()
        if not c:
            return
        extra = "\n\nIts share link will keep working until you remove it." if self._upload_for(c) else ""
        if not messagebox.askyesno("Delete clip", f"Move '{c.path.name}' to the Recycle Bin?{extra}", parent=self.root):
            return
        try:
            recycled = library.send_to_recycle_bin(c.path)
            self.library.forget(c)
            self.selected = None
            self.set_status(f"Moved {c.path.name} to the Recycle Bin." if recycled else f"Deleted {c.path.name}.", "ok")
        except OSError as e:
            self.set_status(f"Couldn't delete: {e}", "error")
        self.refresh_clips()

    def unshare_selected(self) -> None:
        c = self._clip(self.selected)
        up = c and self._upload_for(c)
        if not up or not messagebox.askyesno(
                "Remove share link", f"Delete '{c.title}' from your share server?\nThe link will stop working "
                                     "(your local file is kept).", parent=self.root):
            return
        cfg = self._collect()

        def worker():
            try:
                share.delete_upload(share.ShareClient(cfg.share_server_url, cfg.share_token), up)
                self.ui_q.put(lambda: (self.set_status("Share link removed.", "ok"), self.refresh_clips()))
            except Exception as e:
                err = str(e)
                self.ui_q.put(lambda: self.set_status(f"Couldn't remove link: {err}", "error"))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------ Settings tab
    def _build_settings_tab(self) -> None:
        outer = ttk.Frame(self.settings_tab, padding=(14, 12))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1, uniform="col")
        outer.columnconfigure(1, weight=1, uniform="col")
        self.vars: dict[str, tk.Variable] = {}

        def var(name: str, kind=tk.StringVar):
            v = kind(value=getattr(self.cfg, name))
            self.vars[name] = v
            return v

        def section(title: str, r: int, c: int, rowspan: int = 1) -> ttk.LabelFrame:
            box = ttk.LabelFrame(outer, text=title, padding=12)
            box.grid(row=r, column=c, rowspan=rowspan, sticky="nsew", padx=6, pady=6)
            box.columnconfigure(1, weight=1)
            box._row = 0  # type: ignore[attr-defined]
            return box

        def add(box, label: str, widget: tk.Widget, extra: tk.Widget | None = None):
            r = box._row
            ttk.Label(box, text=label).grid(row=r, column=0, sticky="w", pady=4, padx=(0, 12))
            widget.grid(row=r, column=1, sticky="ew", pady=4)
            if extra:
                extra.grid(row=r, column=2, sticky="w", padx=(6, 0))
            box._row += 1

        cap = section("Capture", 0, 0)
        add(cap, "Save-clip hotkey", ttk.Entry(cap, textvariable=var("hotkey")),
            ttk.Button(cap, text="Record…", command=self.record_hotkey))
        sd = ttk.Frame(cap)
        ttk.Label(sd, text="Stream Deck: a Hotkey action with this combo works, or use",
                  style="Muted.TLabel").pack(side="left")
        ttk.Button(sd, text="Copy Open command", command=self._copy_streamdeck_cmd,
                   padding=(6, 2)).pack(side="left", padx=(6, 0))
        sd.grid(row=cap._row, column=0, columnspan=3, sticky="w", pady=(0, 4))
        cap._row += 1
        add(cap, "Clip length (seconds)", ttk.Spinbox(cap, from_=5, to=600, increment=5,
                                                      textvariable=var("clip_seconds", tk.IntVar), width=8))
        self.mon_combo = ttk.Combobox(cap, state="readonly")
        add(cap, "Monitor", self.mon_combo, ttk.Button(cap, text="Detect", command=self.detect_monitors))
        add(cap, "Resolution", ttk.Combobox(cap, values=RESOLUTIONS, state="readonly", textvariable=var("resolution")))
        add(cap, "Frame rate", ttk.Combobox(cap, values=[30, 60, 120, 144], textvariable=var("fps", tk.IntVar)))
        add(cap, "Bitrate (Mbps)", ttk.Spinbox(cap, from_=2, to=150, increment=1,
                                               textvariable=var("bitrate_mbps", tk.IntVar), width=8))
        add(cap, "Encoder", ttk.Combobox(cap, values=ENCODERS, state="readonly", textvariable=var("encoder")))
        add(cap, "Capture cursor", ttk.Checkbutton(cap, variable=var("draw_mouse", tk.BooleanVar)))

        files = section("Files & behavior", 1, 0)
        add(files, "Clips folder", ttk.Entry(files, textvariable=var("output_dir")),
            ttk.Button(files, text="Browse…", command=lambda: self._browse_dir("output_dir")))
        ff_btns = ttk.Frame(files)
        ttk.Button(ff_btns, text="Browse…", command=self._browse_ffmpeg).pack(side="left")
        self.dl_btn = ttk.Button(ff_btns, text="Download", command=self.download_ffmpeg)
        self.dl_btn.pack(side="left", padx=(4, 0))
        add(files, "FFmpeg path (optional)", ttk.Entry(files, textvariable=var("ffmpeg_path")), ff_btns)
        add(files, "Close button hides to tray", ttk.Checkbutton(files, variable=var("close_to_tray", tk.BooleanVar)))

        add(files, "Check for updates automatically", ttk.Checkbutton(files, variable=var("check_updates", tk.BooleanVar)))
        self.startup_var = tk.BooleanVar(value=winapp.startup_enabled())
        if winapp.IS_WINDOWS:
            add(files, "Start with Windows (in tray)", ttk.Checkbutton(files, variable=self.startup_var))

        aud = section("Audio & alerts", 0, 1)
        self.out_combo = ttk.Combobox(aud, textvariable=var("game_audio_device"), state="readonly")
        add(aud, "Game audio (output)", self.out_combo, ttk.Button(aud, text="Refresh", command=self.refresh_devices))
        add(aud, "Game volume", ttk.Scale(aud, from_=0, to=2, variable=var("game_volume", tk.DoubleVar)))
        self.mic_combo = ttk.Combobox(aud, textvariable=var("mic_device"), state="readonly")
        add(aud, "Microphone", self.mic_combo)
        add(aud, "Mic volume", ttk.Scale(aud, from_=0, to=2, variable=var("mic_volume", tk.DoubleVar)))
        add(aud, "Audio sync offset (ms)", ttk.Spinbox(aud, from_=-1000, to=1000, increment=10,
                                                       textvariable=var("audio_offset_ms", tk.IntVar), width=8))
        alert = ttk.Frame(aud)
        ttk.Checkbutton(alert, variable=var("overlay_enabled", tk.BooleanVar)).pack(side="left")
        ttk.Combobox(alert, values=POSITIONS, state="readonly", textvariable=var("overlay_position"),
                     width=13).pack(side="left", padx=(6, 0))
        ttk.Button(alert, text="Test", command=self._test_overlay, padding=(8, 2)).pack(side="left", padx=(6, 0))
        add(aud, "On-screen clip alert", alert)
        add(aud, "Clip sounds", ttk.Checkbutton(aud, variable=var("sounds", tk.BooleanVar)))

        sh = section("Share links", 1, 1)
        add(sh, "Server URL", ttk.Entry(sh, textvariable=var("share_server_url")),
            ttk.Button(sh, text="Test", command=self.test_share))
        add(sh, "Upload key", ttk.Entry(sh, textvariable=var("share_token"), show="•"))
        add(sh, "Auto-upload new clips", ttk.Checkbutton(sh, variable=var("auto_upload", tk.BooleanVar)))
        add(sh, "Upload bitrate (0 = original)", ttk.Spinbox(sh, from_=0, to=50, increment=1,
                                                             textvariable=var("upload_bitrate_mbps", tk.IntVar), width=8))
        add(sh, "Delete shared clips after (days)", ttk.Spinbox(sh, from_=0, to=365, increment=1,
                                                                textvariable=var("share_expire_days", tk.IntVar), width=8))
        ttk.Label(sh, text="Removes them from your share server to stay under the free 10 GB. 0 = keep forever. "
                           "Clips on your PC are never deleted.", style="Muted.TLabel", wraplength=420,
                  justify="left").grid(row=sh._row, column=0, columnspan=3, sticky="w", pady=(2, 0))
        sh._row += 1

        bar = ttk.Frame(outer)
        bar.grid(row=2, column=0, columnspan=2, sticky="ew", padx=6, pady=(10, 0))
        ttk.Button(bar, text="Save & restart capture", style="Accent.TButton", command=self.apply).pack(side="left")
        self.saved_label = ttk.Label(bar, text="", style="Success.TLabel")
        self.saved_label.pack(side="left", padx=12)

        self.refresh_devices()
        self._fill_monitor_combo()

    def _flash_saved(self) -> None:
        self.saved_label.configure(text="Saved!")
        self.root.after(2500, lambda: self.saved_label.configure(text=""))

    # ------------------------------------------------------------------ Themes tab
    def _build_themes_tab(self) -> None:
        f = ttk.Frame(self.themes_tab, padding=(20, 16))
        f.pack(fill="both", expand=True)
        box = ttk.LabelFrame(f, text="Theme", padding=14)
        box.pack(anchor="nw")
        labels = [theme.THEME_LABELS[k] for k in theme.THEME_ORDER]
        self.theme_var = tk.StringVar(value=theme.THEME_LABELS.get(self.cfg.theme, labels[0]))
        ttk.Label(box, text="Color theme").grid(row=0, column=0, sticky="w", padx=(0, 12))
        ttk.Combobox(box, values=labels, state="readonly", textvariable=self.theme_var, width=26).grid(row=0, column=1)
        ttk.Button(box, text="Apply theme", style="Accent.TButton", command=self.apply_theme).grid(row=0, column=2, padx=(10, 0))
        ttk.Label(box, text="The same themes as the rest of the LC apps (LCBot, LCAI).",
                  style="Muted.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(10, 0))

    def apply_theme(self) -> None:
        name = next((k for k, v in theme.THEME_LABELS.items() if v == self.theme_var.get()), "classic")
        theme.apply_theme(self.root, theme.resolve_colors(name))
        self.border.configure(bg=theme.ACCENT)
        self.inner.configure(bg=theme.BG)
        for m in (self.file_menu, self.help_menu):
            m.configure(**theme.popup_menu_kwargs())
        self._apply_titlebar(self.root)
        self.cfg.theme = name
        self.cfg.save()
        self.set_status(f"Theme set to {theme.THEME_LABELS[name]}.", "ok")

    # ------------------------------------------------------------------ settings helpers
    def _fill_monitor_combo(self) -> None:
        labels = [f"{i}: {w}x{h}" for i, w, h in self.monitors] or [f"{self.cfg.monitor}: (click Detect)"]
        self.mon_combo["values"] = labels
        self.mon_combo.set(next((l for l in labels if l.startswith(f"{self.cfg.monitor}:")), labels[0]))

    def detect_monitors(self) -> None:
        if not self.ffmpeg:
            self.set_status("FFmpeg not found - can't detect monitors.", "error")
            return
        self.set_status("Detecting monitors…")
        ffmpeg = self.ffmpeg

        def worker():
            mons = capture.detect_monitors(ffmpeg)

            def done():
                self.monitors = mons
                self._fill_monitor_combo()
                self.set_status(f"Found {len(mons)} monitor(s)." if mons else
                                "No monitors found by Desktop Duplication (check ffmpeg.log).",
                                "ok" if mons else "error")
            self.ui_q.put(done)

        threading.Thread(target=worker, daemon=True).start()

    def refresh_devices(self) -> None:
        self.out_combo["values"] = [DEVICE_DEFAULT, DEVICE_NONE, *audio.list_output_devices()]
        self.mic_combo["values"] = [DEVICE_DEFAULT, DEVICE_NONE, *audio.list_input_devices()]

    def _browse_dir(self, key: str) -> None:
        d = filedialog.askdirectory(initialdir=self.vars[key].get(), parent=self.root)
        if d:
            self.vars[key].set(d)

    def _browse_ffmpeg(self) -> None:
        p = filedialog.askopenfilename(filetypes=[("ffmpeg", "ffmpeg.exe"), ("All", "*.*")], parent=self.root)
        if p:
            self.vars["ffmpeg_path"].set(p)

    def set_status(self, text: str, kind: str = "info") -> None:
        style = {"error": "Error.TLabel", "ok": "Success.TLabel"}.get(kind, "Muted.TLabel")
        self.status_label.configure(text=text, style=style)

    def _set_recording(self, on: bool) -> None:
        self.rec_label.configure(text="● REC" if on else "● OFF", style="Rec.TLabel" if on else "RecOff.TLabel")
        self.hotkey_hint.configure(text=f"Clip: {self.cfg.hotkey.upper()}  ·  last {self.cfg.clip_seconds}s" if on else "")

    # ------------------------------------------------------------------ FFmpeg download
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
                    self.vars["ffmpeg_path"].set("")
                    self.ffmpeg = exe
                    self.cfg.ffmpeg_path = ""
                    self.cfg.save()
                    self.set_status("FFmpeg installed. Starting capture…", "ok")
                    self._busy = False
                    self.restart()
                    self.refresh_clips()
                self.ui_q.put(done)
            except Exception as e:
                log.exception("FFmpeg download failed")
                err = str(e)
                self.ui_q.put(lambda: self.set_status(
                    f"FFmpeg download failed: {err}. Download it manually (see README) and Browse to ffmpeg.exe.", "error"))
            finally:
                self._busy = False
                self.ui_q.put(lambda: self.dl_btn.state(["!disabled"]))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------ hotkeys
    def bind_hotkey(self) -> None:
        self._hotkey_warning = self.hotkey.set(self.cfg.hotkey)
        if self._hotkey_warning:
            log.warning(self._hotkey_warning)
            self.set_status(self._hotkey_warning, "error")

    def record_hotkey(self) -> None:
        try:
            import keyboard
        except Exception as e:
            self.set_status(f"Hotkeys unavailable: {e}", "error")
            return
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
        global SOUNDS_ON
        SOUNDS_ON = self.cfg.sounds
        try:
            if self.startup_var.get() != winapp.startup_enabled():
                winapp.set_startup(self.startup_var.get())
        except OSError as e:
            log.warning("Couldn't change Start with Windows: %s", e)
        self._flash_saved()
        self.ffmpeg = capture.find_ffmpeg(self.cfg.ffmpeg_path)
        self.refresh_clips()
        if not self.ffmpeg:
            self.set_status("FFmpeg not found.", "error")
            return
        self.restart()

    def restart(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.bind_hotkey()
        self._set_recording(False)
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
                path = "GPU zero-copy" if plan.gpu_path else "CPU scaling"
                msg = f"Recording  {plan.width}x{plan.height} @ {cfg.fps} fps  ·  {plan.encoder} ({path})"
                warn = self._hotkey_warning
                self.ui_q.put(lambda: (self.set_status(warn or msg, "error" if warn else "info"),
                                       self._set_recording(True)))
            except Exception as e:
                log.exception("Start failed")
                err = str(e)
                self.ui_q.put(lambda: self.set_status(f"Capture failed: {err}", "error"))
            finally:
                self._busy = False

        threading.Thread(target=worker, daemon=True).start()

    def _alert(self, title: str, sub: str = "", kind: str = "info", ms: int = 2600) -> None:
        """Thread-safe on-screen toast (respects the setting)."""
        if self.cfg.overlay_enabled:
            self.ui_q.put(lambda: self.overlay.show(title, sub, kind, self.cfg.overlay_position, ms))

    def _test_overlay(self) -> None:
        pos = self.vars["overlay_position"].get()
        self.overlay.show("Clip saved", f"Test alert · {pos}", "ok", pos)

    def _copy_streamdeck_cmd(self) -> None:
        cmd = (f'"{sys.executable}" --save-clip' if winapp.IS_FROZEN
               else f'"{sys.executable}" -m lcclips --save-clip')
        self._copy(cmd)
        self.set_status("Copied. In Stream Deck add System → Open and paste it as the App / File.", "ok")

    def save_clip(self) -> None:
        """Hotkey / Stream Deck / button. Gives instant feedback, then saves in the background."""
        rec = self.recorder
        if rec is None:
            _beep(False)
            self._alert("Not recording", "LCClips isn't capturing right now", "error")
            return
        if self._saving:
            return  # a second press while saving does nothing
        self._saving = True
        _beep(True)  # immediate "got it" tone
        self._alert("Saving clip…", f"Last {self.cfg.clip_seconds}s", "info", ms=0)
        self.ui_q.put(lambda: self.set_status("Saving clip…"))

        def worker():
            try:
                out = rec.save_clip()
                self.last_clip = out

                def done():
                    self.set_status(f"✔ Saved {out.name}", "ok")
                    title = share.title_from_filename(out)
                    self._alert("Clip saved", f"{title} · {self.cfg.clip_seconds}s"
                                + (" · uploading…" if self.cfg.auto_upload and self.cfg.share_server_url else ""), "ok")
                    self.selected = str(out)
                    self.refresh_clips()
                    if self.cfg.auto_upload and self.cfg.share_server_url:
                        self.share_file(out)
                self.ui_q.put(done)
            except Exception as e:
                _beep(False)
                err = str(e)
                self.ui_q.put(lambda: self.set_status(f"Save failed: {err}", "error"))
                self._alert("Clip not saved", err[:60], "error", ms=4000)
            finally:
                self._saving = False

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------ sharing
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
        clip = self.last_clip or (self.clips[0].path if self.clips else None)
        if clip and clip.exists():
            self.share_file(clip)
        else:
            self.share_pick()

    def share_pick(self) -> None:
        p = filedialog.askopenfilename(initialdir=self.cfg.output_dir, filetypes=[("Video", "*.mp4"), ("All", "*.*")],
                                       parent=self.root)
        if p:
            self.share_file(Path(p))

    def share_file(self, clip: Path) -> None:
        key = str(clip)
        if key in self._sharing:
            return
        cfg = self._collect()
        if not cfg.share_server_url or not cfg.share_token:
            self.notebook.select(self.settings_tab)
            self.set_status("Set the share server URL and upload key first (run server\\setup.bat).", "error")
            return
        self._sharing.add(key)
        self._update_details()
        encoder = self.recorder.plan.encoder if self.recorder else "h264_nvenc"

        def progress(stage: str, frac: float):
            self.ui_q.put(lambda: self.set_status(f"{stage} {clip.name}… {frac:.0%}"))

        def worker():
            try:
                item = share.share_clip(self.ffmpeg, cfg.share_server_url, cfg.share_token, clip,
                                        encoder=encoder, upload_mbps=cfg.upload_bitrate_mbps, progress=progress,
                                        expire_days=cfg.share_expire_days)

                def done():
                    self._sharing.discard(key)
                    self._copy(item.url)
                    self.set_status(f"Link copied: {item.url}", "ok")
                    self.refresh_clips()
                self.ui_q.put(done)
                _beep(True)
                self._notify(item.url, "Clip link copied")
                self._alert("Link copied", item.url.split("://", 1)[-1], "ok")
            except Exception as e:
                log.exception("Share failed")
                _beep(False)
                err = str(e)

                def failed():
                    self._sharing.discard(key)
                    self.set_status(f"Upload failed: {err}", "error")
                    self._update_details()
                self.ui_q.put(failed)

        threading.Thread(target=worker, daemon=True).start()

    def test_share(self) -> None:
        cfg = self._collect()

        def worker():
            try:
                share.ShareClient(cfg.share_server_url, cfg.share_token).ping()
                self.ui_q.put(lambda: self.set_status("✔ Share server reachable and upload key accepted.", "ok"))
            except Exception as e:
                err = str(e)
                self.ui_q.put(lambda: self.set_status(f"Share test failed: {err}", "error"))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------ loops
    def _pump(self) -> None:
        while True:
            try:
                fn = self.ui_q.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                log.exception("UI callback failed")
        self.root.after(POLL_MS, self._pump)

    def _watchdog(self) -> None:
        rec = self.recorder
        if rec is not None and not rec.alive() and not self._busy:
            tail = capture.tail_ffmpeg_log(3)
            self.set_status(f"FFmpeg stopped unexpectedly: {tail}", "error")
            self._set_recording(False)
            self.recorder = None
        elif rec is not None and rec.mixer and rec.mixer.errors():
            self.set_status("Audio warning: " + "; ".join(rec.mixer.errors()), "error")
        self.root.after(2000, self._watchdog)

    # ------------------------------------------------------------------ tray / lifecycle
    def _setup_tray(self) -> None:
        try:
            import pystray
        except Exception as e:
            log.warning("Tray icon unavailable (%s); closing the window will exit", e)
            return
        try:
            img = self._icon_image(64)
        except Exception:
            return
        menu = pystray.Menu(
            pystray.MenuItem("Open LCClips", lambda: self.ui_q.put(self.show), default=True),
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
        self.root.focus_force()
        self.refresh_clips()

    def on_close(self) -> None:
        close_to_tray = bool(self.vars["close_to_tray"].get())
        if not (self.tray and close_to_tray):
            self.quit()
            return
        self.root.withdraw()
        if not self.cfg.tray_notice_shown:
            self._notify("LCClips is still running. Right-click the tray icon and choose 'Close LCClips' to exit.",
                         "Still running in the tray")
            self.cfg.tray_notice_shown = True
            self.cfg.save()

    def quit(self) -> None:
        self.hotkey.stop()
        if self.recorder:
            self.recorder.stop()
        try:
            self.editor.shutdown()
        except Exception:
            log.exception("Editor shutdown failed")
        if self.tray:
            self.tray.stop()
        self.root.destroy()

    def run(self) -> None:
        if "--minimized" in sys.argv and self.tray:
            self.root.withdraw()
        self.root.mainloop()


def main() -> None:
    if "--selftest" in sys.argv:
        i = sys.argv.index("--selftest")
        report = Path(sys.argv[i + 1]) if len(sys.argv) > i + 1 else APP_DIR / "selftest.txt"
        sys.exit(winapp.selftest(report))
    if "--save-clip" in sys.argv and winapp.signal_running("save"):
        return
    if not winapp.acquire_single_instance():
        return  # the running copy was asked to show its window
    APP_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(APP_DIR / "lcclips.log", encoding="utf-8"), logging.StreamHandler()],
    )
    if sys.platform != "win32":
        print("Note: LCClips' capture path (ddagrab/WASAPI) is Windows-only.")
    app = App()
    winapp.listen_for_show(lambda: app.ui_q.put(app.show))
    winapp.listen_for_save(app.save_clip)  # LCClips.exe --save-clip
    app.run()
