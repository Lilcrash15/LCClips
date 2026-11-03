"""Small in-game toast ("Saving clip…" -> "Clip saved").

Built so it can never pull you out of a game: on Windows the window is a
click-through, no-activate tool window (WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
| WS_EX_TOOLWINDOW), shown once at startup and afterwards only moved and
faded with SetWindowPos/alpha - it is never re-shown, so it never takes focus.
It appears over windowed and borderless-fullscreen games (true exclusive
fullscreen draws over everything, including this)."""
from __future__ import annotations

import ctypes
import logging
import sys
import tkinter as tk

from . import theme

IS_WINDOWS = sys.platform == "win32"
log = logging.getLogger(__name__)
WIDTH, HEIGHT, MARGIN = 300, 64, 24
POSITIONS = ["Top right", "Top left", "Bottom right", "Bottom left"]
_OFFSCREEN = (-32000, -32000)


class Overlay:
    def __init__(self, root: tk.Tk):
        self.root = root
        self._hide_job = None
        self._fade_job = None
        self.alpha = 0.0
        w = self.win = tk.Toplevel(root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.geometry(f"{WIDTH}x{HEIGHT}+{_OFFSCREEN[0]}+{_OFFSCREEN[1]}")
        self.frame = tk.Frame(w, bg=theme.PANEL_BG, highlightthickness=1, highlightbackground=theme.ACCENT_DIM)
        self.frame.pack(fill="both", expand=True)
        self.bar = tk.Frame(self.frame, width=5, bg=theme.ACCENT)
        self.bar.pack(side="left", fill="y")
        body = tk.Frame(self.frame, bg=theme.PANEL_BG)
        body.pack(side="left", fill="both", expand=True, padx=12, pady=8)
        self.title = tk.Label(body, text="", bg=theme.PANEL_BG, fg=theme.FG, font=(theme.FONT, 11, "bold"), anchor="w")
        self.title.pack(fill="x")
        self.sub = tk.Label(body, text="", bg=theme.PANEL_BG, fg=theme.MUTED_FG, font=(theme.FONT, 9), anchor="w")
        self.sub.pack(fill="x")
        self._body = body
        w.update_idletasks()
        self.hwnd = 0
        if IS_WINDOWS:
            try:
                self.hwnd = ctypes.windll.user32.GetParent(w.winfo_id())
                GWL_EXSTYLE = -20
                ex = ctypes.windll.user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
                ex |= 0x80000 | 0x20 | 0x80 | 0x8000000 | 0x8  # LAYERED|TRANSPARENT|TOOLWINDOW|NOACTIVATE|TOPMOST
                ctypes.windll.user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, ex)
                # No owner window: otherwise hiding LCClips to the tray hides the toast too.
                set_long = getattr(ctypes.windll.user32, "SetWindowLongPtrW", ctypes.windll.user32.SetWindowLongW)
                set_long.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
                set_long(self.hwnd, -8, None)  # GWLP_HWNDPARENT
            except Exception:
                self.hwnd = 0
        self._set_alpha(0.0)

    # ------------------------------------------------------------------ public
    def show(self, title: str, sub: str = "", kind: str = "info", position: str = "Top right",
             ms: int = 2600) -> None:
        color = {"ok": theme.SUCCESS, "error": theme.REC_RED}.get(kind, theme.ACCENT)
        for wdg in (self.frame, self._body, self.title, self.sub):
            wdg.configure(bg=theme.PANEL_BG)
        self.frame.configure(highlightbackground=theme.ACCENT_DIM)
        self.bar.configure(bg=color)
        self.title.configure(text=title, fg=theme.FG)
        self.sub.configure(text=sub, fg=theme.MUTED_FG)
        x, y = self._xy(position)
        self._move(x, y, show=True)
        self._fade_to(0.95)
        log.info("toast '%s' at %d,%d hwnd=%s", title, x, y, self.hwnd)
        if self._hide_job:
            self.root.after_cancel(self._hide_job)
        self._hide_job = self.root.after(ms, self.hide) if ms else None

    def hide(self) -> None:
        self._hide_job = None
        self._fade_to(0.0, then=lambda: self._move(*_OFFSCREEN))

    # ------------------------------------------------------------------ internals
    def _xy(self, position: str) -> tuple[int, int]:
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x = MARGIN if "left" in position.lower() else sw - WIDTH - MARGIN
        y = sh - HEIGHT - MARGIN - 40 if "bottom" in position.lower() else MARGIN
        return x, y

    def _move(self, x: int, y: int, show: bool = False) -> None:
        if self.hwnd:
            u = ctypes.windll.user32
            HWND_TOPMOST, SWP_NOSIZE, SWP_NOACTIVATE, SWP_SHOWWINDOW = -1, 0x1, 0x10, 0x40
            if show:
                u.ShowWindow(self.hwnd, 4)  # SW_SHOWNOACTIVATE: visible even if Tk hid it with the main window
            u.SetWindowPos(self.hwnd, HWND_TOPMOST, x, y, 0, 0,
                           SWP_NOSIZE | SWP_NOACTIVATE | (SWP_SHOWWINDOW if show else 0))
        else:
            self.win.geometry(f"+{x}+{y}")
            self.win.lift()

    def _set_alpha(self, a: float) -> None:
        self.alpha = a
        try:
            self.win.attributes("-alpha", a)
        except tk.TclError:
            pass

    def _fade_to(self, target: float, then=None, step: float = 0.16) -> None:
        if self._fade_job:
            self.root.after_cancel(self._fade_job)
            self._fade_job = None

        def tick():
            d = target - self.alpha
            if abs(d) <= step:
                self._set_alpha(target)
                self._fade_job = None
                if then:
                    then()
                return
            self._set_alpha(self.alpha + (step if d > 0 else -step))
            self._fade_job = self.root.after(16, tick)

        tick()
