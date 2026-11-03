"""Build LCClips.exe and the LCClips-Setup-<version>.exe installer (Windows only).

    py -3.12 -m pip install -r requirements.txt pyinstaller
    py -3.12 packaging\\build.py

Needs Inno Setup 6 (https://jrsoftware.org/isdl.php, or `choco install innosetup`).
GitHub Actions runs this same script (.github/workflows/build.yml).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lcclips import __version__, ffmpeg_setup  # noqa: E402
from lcclips.gui.icon import write_ico  # noqa: E402

BUILD = ROOT / "build"
DIST = ROOT / "dist"

VERSION_INFO = """VSVersionInfo(
  ffi=FixedFileInfo(filevers=({v4}), prodvers=({v4}), mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Lilcrash15'),
      StringStruct('FileDescription', 'LCClips - lightweight game clipping'),
      StringStruct('FileVersion', '{v}'),
      StringStruct('InternalName', 'LCClips'),
      StringStruct('OriginalFilename', 'LCClips.exe'),
      StringStruct('ProductName', 'LCClips'),
      StringStruct('ProductVersion', '{v}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])])
"""


def run(cmd: list[str]) -> None:
    print(">", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT)


def find_iscc() -> str:
    for c in (shutil.which("iscc"), r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
              r"C:\Program Files\Inno Setup 6\ISCC.exe"):
        if c and Path(c).exists():
            return c
    sys.exit("Inno Setup 6 not found (install it from https://jrsoftware.org/isdl.php)")


def main() -> None:
    if sys.platform != "win32":
        sys.exit("Build on Windows (or let GitHub Actions do it).")
    print(f"Building LCClips {__version__}")
    BUILD.mkdir(exist_ok=True)
    shutil.rmtree(DIST / "LCClips", ignore_errors=True)

    ico = write_ico(BUILD / "icon.ico")
    ffmpeg = BUILD / "bin" / "ffmpeg.exe"
    if not ffmpeg.exists():
        print("Downloading FFmpeg…", flush=True)
        ffmpeg_setup.install(dest_dir=BUILD / "bin")

    parts = (__version__.split(".") + ["0", "0", "0"])[:4]
    vfile = BUILD / "version_info.txt"
    vfile.write_text(VERSION_INFO.format(v=__version__, v4=", ".join(str(int(p)) for p in parts)), encoding="utf-8")

    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
         "--name", "LCClips", "--icon", str(ico), "--version-file", str(vfile),
         "--add-binary", f"{ffmpeg};bin",
         "--collect-data", "soundcard", "--hidden-import", "pystray._win32",
         "--distpath", str(DIST), "--workpath", str(BUILD / "pyi"), "--specpath", str(BUILD),
         str(ROOT / "launcher.py")])

    exe = DIST / "LCClips" / "LCClips.exe"
    report = BUILD / "selftest.txt"
    report.unlink(missing_ok=True)
    print("Self-testing the packaged app…", flush=True)
    rc = subprocess.run([str(exe), "--selftest", str(report)], timeout=300).returncode
    print(report.read_text(encoding="utf-8") if report.exists() else "(no self-test report written)")
    if rc != 0:
        sys.exit("Self-test failed - not building the installer.")

    run([find_iscc(), f"/DMyAppVersion={__version__}", f"/DRoot={ROOT}", str(ROOT / "packaging" / "installer.iss")])
    print(f"\nDone: {DIST / f'LCClips-Setup-{__version__}.exe'}")


if __name__ == "__main__":
    main()
