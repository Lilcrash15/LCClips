"""Windows plumbing for LC Editor (port of platform_windows.go): native Win32
file dialogs (callable from any thread, owned by the foreground window), the
chromeless Edge/Chrome/Brave --app window, Explorer reveal and shell open.

Test/dev hook (all platforms): LCE_OPEN / LCE_SAVE environment variables
answer the Open / Save dialogs without showing them."""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

VIDEO_PATTERNS = ("*.mp4;*.mkv;*.mov;*.m4v;*.webm;*.avi;*.wmv;*.flv;*.ts;*.mts;*.m2ts;*.mpg;*.mpeg;*.3gp;"
                  "*.ogv;*.mxf;*.mp3;*.m4a;*.wav;*.flac;*.ogg;*.opus;*.aac")

if IS_WINDOWS:
    from ctypes import wintypes

    class OPENFILENAMEW(ctypes.Structure):
        _fields_ = [
            ("lStructSize", wintypes.DWORD), ("hwndOwner", wintypes.HWND), ("hInstance", wintypes.HINSTANCE),
            ("lpstrFilter", wintypes.LPCWSTR), ("lpstrCustomFilter", wintypes.LPWSTR),
            ("nMaxCustFilter", wintypes.DWORD), ("nFilterIndex", wintypes.DWORD), ("lpstrFile", wintypes.LPWSTR),
            ("nMaxFile", wintypes.DWORD), ("lpstrFileTitle", wintypes.LPWSTR), ("nMaxFileTitle", wintypes.DWORD),
            ("lpstrInitialDir", wintypes.LPCWSTR), ("lpstrTitle", wintypes.LPCWSTR), ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD), ("nFileExtension", wintypes.WORD), ("lpstrDefExt", wintypes.LPCWSTR),
            ("lCustData", wintypes.LPARAM), ("lpfnHook", ctypes.c_void_p), ("lpTemplateName", wintypes.LPCWSTR),
            ("pvReserved", ctypes.c_void_p), ("dwReserved", wintypes.DWORD), ("FlagsEx", wintypes.DWORD),
        ]

    _OFN_OVERWRITEPROMPT, _OFN_NOCHANGEDIR, _OFN_PATHMUSTEXIST = 0x2, 0x8, 0x800
    _OFN_FILEMUSTEXIST, _OFN_NOREADONLYRETURN, _OFN_EXPLORER, _OFN_ENABLESIZING = 0x1000, 0x8000, 0x80000, 0x800000


def _filter(*pairs: str) -> str:
    return "\0".join(pairs) + "\0\0"


def _dialog(save: bool, title: str, initial_dir: str, def_name: str, def_ext: str, filt: str) -> str:
    """Returns the chosen path, or "" if cancelled."""
    ole32, user32, comdlg32 = ctypes.windll.ole32, ctypes.windll.user32, ctypes.windll.comdlg32
    ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED (this is a fresh request thread)
    try:
        owner = user32.GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(def_name, 32768)
        ofn = OPENFILENAMEW()
        ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
        ofn.hwndOwner = owner
        ofn.lpstrFilter = filt
        ofn.nFilterIndex = 1
        ofn.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
        ofn.nMaxFile = 32768
        ofn.lpstrInitialDir = initial_dir or None
        ofn.lpstrTitle = title
        ofn.lpstrDefExt = def_ext.lstrip(".") or None
        if save:
            ofn.Flags = (_OFN_OVERWRITEPROMPT | _OFN_NOCHANGEDIR | _OFN_PATHMUSTEXIST | _OFN_NOREADONLYRETURN
                         | _OFN_EXPLORER | _OFN_ENABLESIZING)
            ok = comdlg32.GetSaveFileNameW(ctypes.byref(ofn))
        else:
            ofn.Flags = _OFN_FILEMUSTEXIST | _OFN_NOCHANGEDIR | _OFN_PATHMUSTEXIST | _OFN_EXPLORER | _OFN_ENABLESIZING
            ok = comdlg32.GetOpenFileNameW(ctypes.byref(ofn))
        if owner:
            user32.SetForegroundWindow(owner)
        if not ok:
            code = comdlg32.CommDlgExtendedError()
            if code:
                raise OSError(f"file dialog failed (code 0x{code:x})")
            return ""
        return buf.value
    finally:
        ole32.CoUninitialize()


def open_file_dialog(initial_dir: str) -> str:
    if "LCE_OPEN" in os.environ or not IS_WINDOWS:
        return os.environ.get("LCE_OPEN", "")
    return _dialog(False, "Open video", initial_dir, "", "",
                   _filter("Video files", VIDEO_PATTERNS, "All files (*.*)", "*.*"))


def save_file_dialog(initial_dir: str, def_name: str, def_ext: str) -> str:
    if "LCE_SAVE" in os.environ or not IS_WINDOWS:
        return os.environ.get("LCE_SAVE") or os.path.join(initial_dir, def_name)
    ext = def_ext.lower()
    pairs = [f"Same as source (*{ext})", "*" + ext]
    for e in (".mp4", ".mkv", ".mov"):
        if e != ext:
            pairs += [f"{e[1:].upper()} video (*{e})", "*" + e]
    pairs += ["All files (*.*)", "*.*"]
    return _dialog(True, "Save cut video as", initial_dir, def_name, ext, _filter(*pairs))


def shell_open(target: str) -> None:
    if IS_WINDOWS:
        os.startfile(target)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", target])


def reveal_file(path: str) -> None:
    if IS_WINDOWS:
        subprocess.Popen(f'explorer.exe /select,"{path}"')
    else:
        shell_open(str(Path(path).parent))


def _browser_candidates() -> list[str]:
    roots = [os.environ.get(k, "") for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    rel = [r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe",
           r"BraveSoftware\Brave-Browser\Application\brave.exe"]
    return [str(Path(root) / r) for r in rel for root in roots if root and (Path(root) / r).is_file()]


def open_window(url: str, profile_dir: Path) -> None:
    """Chromeless app window; falls back to the default browser."""
    if os.environ.get("LCE_NO_WINDOW"):
        return
    if IS_WINDOWS:
        for b in _browser_candidates():
            try:
                subprocess.Popen([
                    b, f"--app={url}", f"--user-data-dir={profile_dir}", "--no-first-run",
                    "--no-default-browser-check", "--disable-sync", "--disable-extensions",
                    "--hide-crash-restore-bubble", "--autoplay-policy=no-user-gesture-required",
                    "--disable-features=Translate,msEdgeTranslate,msImplicitSignin,msEdgeSidebarV2,"
                    "EdgeCollections,msShoppingExp",
                    "--window-size=1360,880",
                ])
                return
            except OSError:
                continue
    import webbrowser

    webbrowser.open(url)


def focus_window(title_suffix: str) -> bool:
    """Bring an existing editor window (title ends with `title_suffix`) to the front."""
    if not IS_WINDOWS:
        return False
    user32 = ctypes.windll.user32
    found: list[int] = []
    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if n and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value.endswith(title_suffix):
                found.append(hwnd)
                return False
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    if not found:
        return False
    SW_RESTORE = 9
    if user32.IsIconic(found[0]):
        user32.ShowWindow(found[0], SW_RESTORE)
    user32.SetForegroundWindow(found[0])
    return True
