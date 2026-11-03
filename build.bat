@echo off
REM Developer build: produces dist\LCClips-Setup-<version>.exe (needs Python 3.12 + Inno Setup 6).
cd /d "%~dp0"
py -3.12 -m pip install -r requirements.txt pyinstaller || exit /b 1
py -3.12 packaging\build.py
