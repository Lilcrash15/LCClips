@echo off
REM One-time setup for the LCClips share server. Needs Node.js (nodejs.org).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
pause
