"""Global hotkey via Win32 RegisterHotKey.

Why not a keyboard hook: hooks (the `keyboard` library) identify keys by
hardware scan code, and macro pads like the Elgato Stream Deck inject key
presses that often carry no scan code, so the hook never matches. Windows'
own hotkey system matches on virtual-key codes, works for real keyboards and
injected input alike, and fires on key-down with no polling delay.

Falls back to the `keyboard` library on other platforms, or when another app
(e.g. the NVIDIA overlay, whose default "save replay" is also Alt+F10) has
already registered the same combination."""
from __future__ import annotations

import ctypes
import logging
import sys
import threading
from typing import Callable

log = logging.getLogger(__name__)
IS_WINDOWS = sys.platform == "win32"

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012

_MODS = {"alt": MOD_ALT, "left alt": MOD_ALT, "right alt": MOD_ALT, "alt gr": MOD_ALT | MOD_CONTROL,
         "ctrl": MOD_CONTROL, "control": MOD_CONTROL, "left ctrl": MOD_CONTROL, "right ctrl": MOD_CONTROL,
         "shift": MOD_SHIFT, "left shift": MOD_SHIFT, "right shift": MOD_SHIFT,
         "win": MOD_WIN, "windows": MOD_WIN, "left windows": MOD_WIN, "right windows": MOD_WIN, "cmd": MOD_WIN}

_KEYS: dict[str, int] = {
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
    **{chr(c): c - 32 for c in range(ord("a"), ord("z") + 1)},
    **{str(d): 0x30 + d for d in range(10)},
    **{f"num {d}": 0x60 + d for d in range(10)}, **{f"numpad {d}": 0x60 + d for d in range(10)},
    **{f"num{d}": 0x60 + d for d in range(10)}, **{f"numpad{d}": 0x60 + d for d in range(10)},
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "insert": 0x2D, "delete": 0x2E, "del": 0x2E, "home": 0x24, "end": 0x23,
    "page up": 0x21, "pageup": 0x21, "page down": 0x22, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "print screen": 0x2C, "printscreen": 0x2C, "prtsc": 0x2C, "pause": 0x13, "scroll lock": 0x91,
    "num *": 0x6A, "num +": 0x6B, "num -": 0x6D, "num .": 0x6E, "num /": 0x6F,
    "multiply": 0x6A, "add": 0x6B, "subtract": 0x6D, "decimal": 0x6E, "divide": 0x6F,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0,
    "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
    "media play/pause": 0xB3, "play/pause media": 0xB3, "media next": 0xB0, "media previous": 0xB1,
}


def parse(combo: str) -> tuple[int, int]:
    """'alt+f10' -> (MOD_ALT, VK_F10). Raises ValueError for anything unusable."""
    mods, vk = 0, None
    for part in [p.strip().lower() for p in combo.replace(" + ", "+").split("+")]:
        if not part:
            continue
        if part in _MODS:
            mods |= _MODS[part]
        elif part in _KEYS and vk is None:
            vk = _KEYS[part]
        else:
            raise ValueError(f"unknown key '{part}'")
    if vk is None:
        raise ValueError("needs a key besides Ctrl/Alt/Shift/Win")
    return mods, vk


class GlobalHotkey:
    def __init__(self, callback: Callable[[], None]):
        self.callback = callback
        self._thread: threading.Thread | None = None
        self._tid = 0
        self._kb_handle = None
        self.backend = ""

    def set(self, combo: str) -> str | None:
        """(Re)binds the hotkey. Returns a warning for the user, or None if all is well."""
        self.stop()
        if IS_WINDOWS:
            try:
                mods, vk = parse(combo)
            except ValueError as e:
                return f"Invalid hotkey '{combo}': {e}"
            ok, err = self._start_win32(mods, vk)
            if ok:
                self.backend = "windows"
                log.info("Hotkey %s registered with Windows", combo)
                return None
            # Someone else owns it - a keyboard hook still works for physical keys.
            fallback = self._start_hook(combo)
            return (f"{combo.upper()} is already used by another app ({err}). It will still work from your keyboard, "
                    f"but not from Stream Deck - pick a different hotkey in Settings."
                    if fallback is None else f"Couldn't register {combo.upper()}: {err}")
        return self._start_hook(combo)

    # ------------------------------------------------------------------ Windows
    def _start_win32(self, mods: int, vk: int) -> tuple[bool, str]:
        from ctypes import wintypes

        result: dict = {}
        ready = threading.Event()
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32

        def loop():
            self._tid = kernel32.GetCurrentThreadId()
            ok = user32.RegisterHotKey(None, 1, mods | MOD_NOREPEAT, vk)
            code = 0 if ok else kernel32.GetLastError()
            result["ok"] = bool(ok)
            result["err"] = "" if ok else ctypes.FormatError(code).strip().rstrip(".")
            ready.set()
            if not ok:
                return
            msg = wintypes.MSG()
            try:
                while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                    if msg.message == WM_HOTKEY:
                        try:
                            self.callback()
                        except Exception:
                            log.exception("hotkey callback failed")
            finally:
                user32.UnregisterHotKey(None, 1)

        self._thread = threading.Thread(target=loop, daemon=True, name="hotkey")
        self._thread.start()
        ready.wait(3)
        return result.get("ok", False), result.get("err", "timed out")

    # ------------------------------------------------------------------ keyboard-hook fallback
    def _start_hook(self, combo: str) -> str | None:
        try:
            import keyboard

            self._kb_handle = keyboard.add_hotkey(combo, self.callback, suppress=False)
            self.backend = "hook"
            return None
        except Exception as e:
            return f"Hotkeys unavailable: {e}"

    def stop(self) -> None:
        if self._thread and self._thread.is_alive() and self._tid:
            ctypes.windll.user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            self._thread.join(2)
        self._thread, self._tid = None, 0
        if self._kb_handle is not None:
            try:
                import keyboard

                keyboard.remove_hotkey(self._kb_handle)
            except Exception:
                pass
            self._kb_handle = None
        self.backend = ""
