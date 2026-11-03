@echo off
REM Builds a standalone LCClips.exe in dist\LCClips. Put ffmpeg.exe in .\bin first.
cd /d "%~dp0"
if not exist .venv ( py -3 -m venv .venv )
.venv\Scripts\pip install -r requirements.txt pyinstaller
.venv\Scripts\pyinstaller --noconfirm --windowed --name LCClips --add-binary "bin\ffmpeg.exe;bin" launcher.py
