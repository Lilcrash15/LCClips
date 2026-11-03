"""Windows app plumbing: single instance, "Start with Windows", self-test.
Everything here is a safe no-op on other platforms."""
from __future__ import annotations

import ctypes
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

IS_WINDOWS = sys.platform == "win32"
IS_FROZEN = bool(getattr(sys, "frozen", False))
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "LCClips"  # same value name the installer writes
_MUTEX_NAME = "Local\\LCClips.SingleInstance"
_SHOW_EVENT = "Local\\LCClips.ShowWindow"

_mutex_handle = None


# --------------------------------------------------------------------------- single instance

def acquire_single_instance() -> bool:
    """True if we're the first instance. Otherwise asks the running one to show its window."""
    global _mutex_handle
    if not IS_WINDOWS:
        return True
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW.restype = ctypes.c_void_p
    _mutex_handle = k32.CreateMutexW(None, False, _MUTEX_NAME)
    ERROR_ALREADY_EXISTS = 183
    if k32.GetLastError() != ERROR_ALREADY_EXISTS:
        return True
    EVENT_MODIFY_STATE = 0x0002
    k32.OpenEventW.restype = ctypes.c_void_p
    ev = k32.OpenEventW(EVENT_MODIFY_STATE, False, _SHOW_EVENT)
    if ev:
        k32.SetEvent(ctypes.c_void_p(ev))
        k32.CloseHandle(ctypes.c_void_p(ev))
    return False


def listen_for_show(callback: Callable[[], None]) -> None:
    """Calls `callback` (from a worker thread) whenever a second launch asks us to show."""
    if not IS_WINDOWS:
        return
    k32 = ctypes.windll.kernel32
    k32.CreateEventW.restype = ctypes.c_void_p
    ev = k32.CreateEventW(None, False, False, _SHOW_EVENT)  # auto-reset
    if not ev:
        return

    def loop():
        INFINITE = 0xFFFFFFFF
        while k32.WaitForSingleObject(ctypes.c_void_p(ev), INFINITE) == 0:
            callback()

    threading.Thread(target=loop, daemon=True, name="show-listener").start()


# --------------------------------------------------------------------------- start with Windows

def launch_command() -> str:
    if IS_FROZEN:
        return f'"{sys.executable}" --minimized'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pyw if pyw.exists() else sys.executable}" -m lcclips --minimized'


def startup_enabled() -> bool:
    if not IS_WINDOWS:
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_VALUE)
            return True
    except OSError:
        return False


def set_startup(enabled: bool) -> None:
    if not IS_WINDOWS:
        return
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(k, RUN_VALUE)
            except FileNotFoundError:
                pass


# --------------------------------------------------------------------------- self-test (CI)

def selftest(report: Path) -> int:
    """Used by the build pipeline on the packaged .exe: checks every dependency
    loads and the bundled FFmpeg has what LCClips needs. Writes a report file
    (a --windowed exe has no console)."""
    lines, ok = [], True

    def check(name: str, fn):
        nonlocal ok
        try:
            lines.append(f"PASS {name}: {fn()}")
        except Exception as e:
            ok = False
            lines.append(f"FAIL {name}: {type(e).__name__}: {e}")

    from . import __version__

    lines.append(f"LCClips {__version__} frozen={IS_FROZEN}")
    check("numpy", lambda: __import__("numpy").__version__)
    check("PIL", lambda: __import__("PIL").__version__)
    check("tkinter", lambda: __import__("tkinter").TkVersion)
    check("keyboard", lambda: bool(__import__("keyboard")))
    check("pystray", lambda: __import__("pystray").Icon.__module__)
    check("soundcard", lambda: __import__("soundcard").__name__)
    check("app modules", lambda: [__import__(f"lcclips.{m}") for m in
                                  ("app", "audio", "capture", "share", "library", "ffmpeg_setup")] and "ok")

    from . import capture

    ff = capture.find_ffmpeg()
    flags = 0x08000000 if IS_WINDOWS else 0

    def ff_run(*args):
        return subprocess.run([ff, "-hide_banner", *args], capture_output=True, text=True,
                              errors="replace", timeout=60, creationflags=flags).stdout

    def bundled():
        if not ff:
            raise RuntimeError("no ffmpeg found")
        if IS_FROZEN and not Path(ff).resolve().is_relative_to(Path(sys._MEIPASS).resolve()):  # type: ignore[attr-defined]
            raise RuntimeError(f"using {ff}, not the bundled copy")
        return ff

    check("ffmpeg bundled", bundled)
    check("ffmpeg version", lambda: ff_run("-version").splitlines()[0])
    for filt in ("ddagrab",):
        check(f"filter {filt}", lambda f=filt: f in ff_run("-filters") or (_ for _ in ()).throw(RuntimeError("missing")))
    for enc in ("h264_nvenc", "h264_amf", "h264_qsv", "libx264", "aac"):
        check(f"encoder {enc}", lambda e=enc: e in ff_run("-encoders") or (_ for _ in ()).throw(RuntimeError("missing")))

    lines.append("RESULT " + ("PASS" if ok else "FAIL"))
    report.write_text("\n".join(lines), encoding="utf-8")
    return 0 if ok else 1
