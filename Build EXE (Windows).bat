@echo off
title Build Clip Grabber.exe
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python is not installed. Get it from https://www.python.org/downloads/ and tick "Add Python to PATH".
  pause
  exit /b 1
)

echo [1/3] Installing the build tools and download engine...
%PY% -m pip install -U --disable-pip-version-check pyinstaller "yt-dlp[default,deno]" imageio-ffmpeg || goto :fail

for /f "delims=" %%D in ('%PY% -c "import os,sysconfig;print(os.path.join(sysconfig.get_path('scripts'),'deno.exe'))"') do set "DENO=%%D"
set "DENO_ARG="
if exist "%DENO%" (set DENO_ARG=--add-binary "%DENO%;.") else (echo Note: deno.exe not found, YouTube support may be limited.)

echo [2/3] Building Clip Grabber.exe (this takes a few minutes)...
%PY% -m PyInstaller --noconfirm --clean --onefile --noconsole --name "Clip Grabber" ^
  --collect-all imageio_ffmpeg --collect-all yt_dlp_ejs %DENO_ARG% clip_grabber_web.py || goto :fail

echo [3/3] Done!
echo Your app is "dist\Clip Grabber.exe". Copy it anywhere, e.g. your Desktop, and double-click it.
explorer dist
pause
exit /b 0

:fail
echo.
echo Something went wrong. Scroll up to see the error.
pause
exit /b 1
