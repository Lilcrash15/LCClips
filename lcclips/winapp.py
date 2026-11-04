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
_SAVE_EVENT = "Local\\LCClips.SaveClip"  # `LCClips.exe --save-clip` (Stream Deck "Open" action)

_mutex_handle = None


# --------------------------------------------------------------------------- single instance

def signal_running(action: str) -> bool:
    """Tells an already-running LCClips to 'show' its window or 'save' a clip.
    Returns False if no LCClips is running. Stdlib-only so launcher.py can call
    it before importing the (slow-to-load) app."""
    if not IS_WINDOWS:
        return False
    k32 = ctypes.windll.kernel32
    k32.OpenEventW.restype = ctypes.c_void_p
    ev = k32.OpenEventW(0x0002, False, _SAVE_EVENT if action == "save" else _SHOW_EVENT)  # EVENT_MODIFY_STATE
    if not ev:
        return False
    k32.SetEvent(ctypes.c_void_p(ev))
    k32.CloseHandle(ctypes.c_void_p(ev))
    return True


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
    _listen(_SHOW_EVENT, callback)


def listen_for_save(callback: Callable[[], None]) -> None:
    """Calls `callback` whenever `LCClips.exe --save-clip` is run."""
    _listen(_SAVE_EVENT, callback)


def _listen(name: str, callback: Callable[[], None]) -> None:
    if not IS_WINDOWS:
        return
    k32 = ctypes.windll.kernel32
    k32.CreateEventW.restype = ctypes.c_void_p
    ev = k32.CreateEventW(None, False, False, name)  # auto-reset
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

def _toast_test() -> str:
    """Opens a maximized, borderless, always-on-top, focused 'game' window in another
    process (like a borderless-windowed game), shows the clip toast and checks
    with the real window z-order that the toast is above it."""
    import time
    import tkinter as tk

    from .gui.overlay import Overlay

    user32 = ctypes.windll.user32
    ps = ("Add-Type -AssemblyName System.Windows.Forms;"
          "$f=New-Object Windows.Forms.Form;$f.Text='LCClipsFakeGame';$f.FormBorderStyle='None';"
          "$f.WindowState='Maximized';$f.TopMost=$true;$f.BackColor='DarkRed';"
          "$f.Add_Shown({$f.Activate()});[void]$f.ShowDialog()")
    game_proc = subprocess.Popen(["powershell", "-NoProfile", "-Command", ps], creationflags=0x08000000)
    try:
        user32.FindWindowW.restype = ctypes.c_void_p
        game = None
        for _ in range(100):
            game = user32.FindWindowW(None, "LCClipsFakeGame")
            if game:
                break
            time.sleep(0.1)
        if not game:
            raise RuntimeError("fake game window didn't open")
        time.sleep(0.5)
        root = tk.Tk()
        root.withdraw()  # like LCClips hidden in the tray
        ov = Overlay(root)
        user32.GetAncestor.restype = ctypes.c_void_p
        root_frame = user32.GetAncestor(ctypes.c_void_p(root.winfo_id()), 2)
        if not ov.hwnd or ov.hwnd == root_frame:
            raise RuntimeError("toast handle points at the main window, not the toast")
        GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = -20, 0x80, 0x40000
        ex = user32.GetWindowLongW(ctypes.c_void_p(ov.hwnd), GWL_EXSTYLE)
        if not ex & WS_EX_TOOLWINDOW or ex & WS_EX_APPWINDOW:
            raise RuntimeError("toast would get a taskbar button")
        if user32.IsWindowVisible(ctypes.c_void_p(ov.hwnd)):
            raise RuntimeError("toast visible before anything was saved")

        def pump(sec: float) -> None:
            end = time.monotonic() + sec
            while time.monotonic() < end:
                root.update()
                time.sleep(0.02)

        def pixel(x: int, y: int):
            from PIL import ImageGrab

            return ImageGrab.grab(bbox=(x, y, x + 1, y + 1), include_layered_windows=True).getpixel((0, 0))[:3]

        def pixel_at_toast():
            x, y = ov._xy("Top right")
            return pixel(x + 150, y + 30)

        game_px = pixel(300, 300)
        if not (game_px[0] > 80 and game_px[1] < 40):
            raise RuntimeError(f"screenshots don't work on this machine (game pixel {game_px}) - can't verify")
        before = pixel_at_toast()

        ov.show("Clip saved", "Self-test", "ok", "Top right", ms=1500)
        pump(0.6)
        visible = user32.IsWindowVisible(ctypes.c_void_p(ov.hwnd))
        during = pixel_at_toast()
        user32.GetWindow.restype = ctypes.c_void_p
        h, above = ov.hwnd, False
        for _ in range(2000):  # walk down the z-order from the toast
            h = user32.GetWindow(ctypes.c_void_p(h), 2)  # GW_HWNDNEXT
            if not h:
                break
            if h == game:
                above = True
                break
        fg = user32.GetForegroundWindow()
        pump(2.0)  # past its timeout + fade
        gone = not user32.IsWindowVisible(ctypes.c_void_p(ov.hwnd))
        after = pixel_at_toast()
        root.destroy()
        if not visible:
            raise RuntimeError("toast window not visible")
        if not above:
            raise RuntimeError("toast is BELOW the game window")
        if during == before:
            raise RuntimeError(f"toast not drawn on screen (pixel stayed {during}, game is {before})")
        if not gone or after != before:
            raise RuntimeError(f"toast didn't disappear after its timeout (pixel {after})")
        return (f"drawn above the game (pixel {during} over red game {before}), no taskbar button, "
                f"game kept focus: {fg == game}, gone after timeout")
    finally:
        game_proc.kill()


def _hotkey_test() -> str:
    """Injects key presses the way Stream Deck does (virtual-key codes, no scan
    code) and checks LCClips receives them - both when it owns the hotkey and
    when another app already registered the same combination."""
    from ctypes import wintypes

    from . import hotkeys

    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    combo, keys = "ctrl+alt+shift+f24", (0x11, 0x12, 0x10, 0x87)  # Ctrl, Alt, Shift, F24

    def inject() -> None:
        for vk in keys:
            user32.keybd_event(vk, 0, 0, 0)
        for vk in reversed(keys):
            user32.keybd_event(vk, 0, 0x2, 0)  # KEYEVENTF_KEYUP

    def attempt() -> str:
        fired = threading.Event()
        hk = hotkeys.GlobalHotkey(fired.set)
        warn = hk.set(combo)
        if warn:
            raise RuntimeError(warn)
        backend = hk.backend
        try:
            time.sleep(0.2)
            inject()
            if not fired.wait(3):
                raise RuntimeError(f"no key press received via {backend}")
        finally:
            hk.stop()
        return backend

    import time

    own = attempt()
    # Simulate the user's case: another app (e.g. NVIDIA overlay) owns the combo.
    occupied, stop_tid = threading.Event(), [0]

    def occupier():
        stop_tid[0] = kernel32.GetCurrentThreadId()
        user32.RegisterHotKey(None, 7, 0x1 | 0x2 | 0x4, 0x87)
        occupied.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass
        user32.UnregisterHotKey(None, 7)

    t = threading.Thread(target=occupier, daemon=True)
    t.start()
    occupied.wait(3)
    try:
        taken = attempt()
    finally:
        user32.PostThreadMessageW(stop_tid[0], 0x0012, 0, 0)
        t.join(2)
    if taken != "hook":
        raise RuntimeError(f"expected hook fallback, got {taken}")
    return f"free combo -> {own}; combo owned by another app -> {taken}; injected presses received both times"


def _editor_test(ffmpeg: str) -> str:
    """Runs LC Editor's real HTTP API: open a clip, export Fast and Precise (2 clips joined)."""
    import json
    import os
    import tempfile
    import time
    import urllib.request

    from .editor import Editor
    from .editor.server import UI_DIR

    if not (UI_DIR / "index.html").is_file():
        raise RuntimeError(f"editor UI missing at {UI_DIR}")
    flags = 0x08000000 if IS_WINDOWS else 0
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "test.mp4")
        subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
                        "-f", "lavfi", "-i", "sine=440", "-t", "6", "-c:v", "libx264", "-preset", "ultrafast",
                        "-g", "30", "-c:a", "aac", src], check=True, capture_output=True, timeout=60, creationflags=flags)
        os.environ["LCE_NO_WINDOW"] = "1"
        ed = Editor(Path(td) / "data", lambda: ffmpeg)
        ed.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def call(path, body=None):
            r = urllib.request.Request(ed.base_url + path, data=None if body is None else json.dumps(body).encode(),
                                       headers={"X-Token": ed.token, "Content-Type": "application/json"},
                                       method="GET" if body is None else "POST")
            with opener.open(r, timeout=60) as resp:
                return json.loads(resp.read())

        try:
            if b"LC Editor" not in opener.open(f"{ed.url}", timeout=10).read():
                raise RuntimeError("editor page didn't render")
            info = call("/api/open", {"path": src})
            results = []
            for mode, segs in (("fast", [{"start": 1, "end": 4}]),
                               ("precise", [{"start": 0.5, "end": 2}, {"start": 3, "end": 5}])):
                out = os.path.join(td, f"cut_{mode}.mp4")
                os.environ["LCE_SAVE"] = out
                job = call("/api/export", {"segments": segs, "mode": mode, "encoder": "x264"})
                t0 = time.monotonic()
                while time.monotonic() - t0 < 90:
                    j = call("/api/state")["job"]
                    if j and j["id"] == job["id"] and j["status"] != "running":
                        break
                    time.sleep(0.2)
                if j["status"] != "done" or not os.path.isfile(out):
                    raise RuntimeError(f"{mode} export {j['status']}: {j.get('message')}")
                results.append(f"{mode} {ed.ff.probe(out)['duration']:.1f}s")
        finally:
            os.environ.pop("LCE_SAVE", None)
            ed.shutdown()
        return f"opened {info['width']}x{info['height']} {info['duration']:.0f}s; " + ", ".join(results)


def _pipeline_test(ffmpeg: str) -> str:
    """Runs the app's real recording command (test pattern instead of the screen,
    silent PCM on stdin like the audio mixer) and saves a clip from the buffer."""
    import tempfile
    import time

    from . import audio, capture
    from .config import Config

    cfg = Config(clip_seconds=5, fps=30, encoder="libx264", bitrate_mbps=4)
    plan = capture.VideoPlan("libx264", "testsrc2=size=640x360:rate=30,format=nv12",
                             capture._encoder_args("libx264", cfg.fps, cfg.bitrate_mbps), 640, 360, False)
    with tempfile.TemporaryDirectory() as td:
        seg_dir = Path(td) / "buf"
        seg_dir.mkdir()
        cmd = capture.build_record_cmd(ffmpeg, plan, cfg, seg_dir)
        flags = 0x08000000 if IS_WINDOWS else 0
        err_path = Path(td) / "err.txt"
        with open(err_path, "w") as err:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err, creationflags=flags)
            block = bytes(audio.BLOCK * audio.CHANNELS * 4)
            t0 = time.monotonic()
            try:
                while time.monotonic() - t0 < 4 and p.poll() is None:
                    p.stdin.write(block)
                    time.sleep(audio.BLOCK / audio.SAMPLE_RATE)
            except OSError:
                pass
            alive = p.poll() is None
            try:
                p.stdin.close()
            except OSError:
                pass
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(10)
        if not alive:
            raise RuntimeError("recorder exited: " + err_path.read_text(errors="replace").strip()[-300:])
        segs = capture.completed_segments(seg_dir)
        if len(segs) < 2:
            raise RuntimeError(f"only {len(segs)} segments written")
        out = Path(td) / "clip.mp4"
        capture.concat_segments(ffmpeg, segs, out)
        (w, h), dur = __import__("lcclips.share", fromlist=["probe"]).probe(ffmpeg, out)
        if not out.exists() or dur <= 0:
            raise RuntimeError("saved clip is empty")
        return f"{len(segs)} segments -> clip {w}x{h} {dur:.1f}s"


def selftest(report: Path) -> int:
    """Used by the build pipeline on the packaged .exe: checks every dependency
    loads and the bundled FFmpeg has what LCClips needs. Writes a report file
    (a --windowed exe has no console)."""
    lines, ok = [], True

    def check(name: str, fn, required: bool = True):
        nonlocal ok
        try:
            lines.append(f"PASS {name}: {fn()}")
        except Exception as e:
            if required:
                ok = False
            lines.append(f"{'FAIL' if required else 'INFO'} {name}: {type(e).__name__}: {e}")

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

    check("recording pipeline", lambda: _pipeline_test(ff))
    check("LC Editor", lambda: _editor_test(ff))
    if IS_WINDOWS:
        check("hotkey (injected, like Stream Deck)", _hotkey_test, required=False)
        check("toast above a borderless always-on-top game", _toast_test, required=False)
    lines.append("RESULT " + ("PASS" if ok else "FAIL"))
    report.write_text("\n".join(lines), encoding="utf-8")
    return 0 if ok else 1
