@echo off
title Clip Grabber
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo.
  echo  Python is not installed.
  echo  Get it from https://www.python.org/downloads/
  echo  During setup, tick "Add Python to PATH". Then run this file again.
  echo.
  pause
  exit /b 1
)

echo Checking for updates to the download engine...
%PY% -m pip install -q -U --disable-pip-version-check "yt-dlp[default,deno]" imageio-ffmpeg

echo.
%PY% clip_grabber_web.py
if errorlevel 1 pause
