#!/bin/bash
# Double-click to start Clip Grabber on macOS (also works on Linux: bash this file).
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is not installed. Get it from https://www.python.org/downloads/ and run this again."
  read -r -p "Press Enter to close..."
  exit 1
fi

echo "Checking for updates to the download engine..."
python3 -m pip install -q -U --user --disable-pip-version-check "yt-dlp[default,deno]" imageio-ffmpeg 2>/dev/null \
  || python3 -m pip install -q -U --break-system-packages --disable-pip-version-check "yt-dlp[default,deno]" imageio-ffmpeg

python3 clip_grabber_web.py || read -r -p "Press Enter to close..."
