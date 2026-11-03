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
        self.notice: str | None = None  # informational (not an error) message for the UI

    def set(self, combo: str) -> str | None:
        """(Re)binds the hotkey. Returns a warning for the user, or None if all is well."""
        self.stop()
        self.notice = None
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
            # Another app (often the NVIDIA overlay) owns this combo. A low-level hook
            # matching virtual-key codes sees the key before that app does, from the
            # keyboard and from Stream Deck alike.
            hook_ok, hook_err = self._start_llhook(mods, vk)
            if hook_ok:
                self.backend = "hook"
                self.notice = (f"{combo.upper()} is also a shortcut in another app (often the NVIDIA overlay). "
                               f"LCClips still catches it from your keyboard and Stream Deck.")
                log.info("Hotkey %s taken by another app (%s); using low-level hook", combo, err)
                return None
            return f"Couldn't set up {combo.upper()}: {err}; {hook_err}"
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

    # ------------------------------------------------------------------ low-level hook (virtual-key based)
    def _start_llhook(self, mods: int, vk: int) -> tuple[bool, str]:
        from ctypes import wintypes

        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        result: dict = {}
        ready = threading.Event()

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]

        HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        mod_vks = {MOD_CONTROL: (0x11, 0xA2, 0xA3), MOD_ALT: (0x12, 0xA4, 0xA5),
                   MOD_SHIFT: (0x10, 0xA0, 0xA1), MOD_WIN: (0x5B, 0x5C)}
        all_mod_vks = {v for vs in mod_vks.values() for v in vs}
        down: set[int] = set()

        def held(flag: int) -> bool:
            return any(v in down or user32.GetAsyncKeyState(v) & 0x8000 for v in mod_vks[flag])

        def proc(code, wparam, lparam):
            if code == 0:
                k = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents.vkCode
                if wparam in (0x100, 0x104):  # WM_KEYDOWN / WM_SYSKEYDOWN
                    repeat = k in down
                    down.add(k)
                    if k == vk and not repeat and all(held(f) == bool(mods & f) for f in mod_vks):
                        # Never block the hook (Windows drops slow hooks): run the callback elsewhere.
                        threading.Thread(target=self._safe_callback, daemon=True).start()
                elif wparam in (0x101, 0x105):  # WM_KEYUP / WM_SYSKEYUP
                    down.discard(k)
                    if k in all_mod_vks:
                        for vs in mod_vks.values():
                            if k in vs:
                                down.difference_update(vs)
            return user32.CallNextHookEx(None, code, wparam, lparam)

        cb = HOOKPROC(proc)

        def loop():
            self._tid = kernel32.GetCurrentThreadId()
            hook = user32.SetWindowsHookExW(13, cb, kernel32.GetModuleHandleW(None), 0)  # WH_KEYBOARD_LL
            result["ok"] = bool(hook)
            result["err"] = "" if hook else ctypes.FormatError(kernel32.GetLastError()).strip()
            ready.set()
            if not hook:
                return
            msg = wintypes.MSG()
            try:
                while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                    pass
            finally:
                user32.UnhookWindowsHookEx(ctypes.c_void_p(hook))

        self._hook_proc = cb  # keep the callback alive
        self._thread = threading.Thread(target=loop, daemon=True, name="hotkey-hook")
        self._thread.start()
        ready.wait(3)
        return result.get("ok", False), result.get("err", "timed out")

    def _safe_callback(self) -> None:
        try:
            self.callback()
        except Exception:
            log.exception("hotkey callback failed")

    # ------------------------------------------------------------------ keyboard-library fallback (non-Windows)
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
