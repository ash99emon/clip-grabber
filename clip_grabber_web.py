"""
Clip Grabber - download just a section of a video, from a website-style app
that runs on your own computer.

Run:   python clip_grabber_web.py      (your browser opens automatically)
Stop:  click "Quit" in the app, or close the terminal window.

Needs Python 3.8+. Missing pieces (yt-dlp, a JavaScript helper and ffmpeg)
are installed automatically the first time.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import sysconfig
import threading
import time
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlparse

# A windowed .exe has no console; give print/logging somewhere harmless to go.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = sys.stdout

APP_NAME = "Clip Grabber"
APP_ID = "clip-grabber"
DEFAULT_PORT = 8765
FROZEN = getattr(sys, "frozen", False)
OUT_DIR = os.path.join(os.path.expanduser("~"), "Downloads", "Clip Grabber")
EXE = ".exe" if os.name == "nt" else ""
SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".clip_grabber.json")
COOKIE_PATH = os.path.join(os.path.expanduser("~"), ".clip_grabber_cookies.txt")
BROWSERS = ("firefox", "chrome", "edge", "brave", "opera", "vivaldi", "chromium", "safari")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


# ----------------------------------------------------------------- helpers

def parse_time(text):
    """'90', '1:30', '01:02:03', '1:30.5' -> seconds."""
    text = str(text).strip()
    if not re.fullmatch(r"\d+(:\d{1,2}){0,2}(\.\d+)?", text):
        raise ValueError(f"'{text}' is not a valid time. Use ss, mm:ss or hh:mm:ss.")
    total = 0.0
    for part in text.split(":"):
        total = total * 60 + float(part)
    return total


def fmt_tag(seconds):
    """Filename-safe tag: 90 -> 1m30s, 3723 -> 1h02m03s."""
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"


def pip_install(*packages):
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "-q", "-U",
         "--disable-pip-version-check", *packages])


# ------------------------------------------------------- engine (yt-dlp etc.)

ENGINE = {"ready": False, "message": "Starting up...", "error": None}
_engine_lock = threading.Lock()
_paths = {}


def _find_bundled(name):
    base = getattr(sys, "_MEIPASS", None)
    if base:
        p = os.path.join(base, name + EXE)
        if os.path.isfile(p):
            return p
    p = shutil.which(name)
    if p:
        return p
    p = os.path.join(sysconfig.get_path("scripts"), name + EXE)
    return p if os.path.isfile(p) else None


def prepare_engine():
    """Make sure yt-dlp, ffmpeg and a JS runtime are available. Safe to call often."""
    with _engine_lock:
        if ENGINE["ready"]:
            return
        try:
            try:
                import yt_dlp  # noqa: F401
                import yt_dlp_ejs  # noqa: F401
            except ImportError:
                if FROZEN:
                    raise
                ENGINE["message"] = "Installing the download engine (one time)..."
                pip_install("yt-dlp[default,deno]")
                import yt_dlp  # noqa: F401

            if shutil.which("ffmpeg"):
                _paths["ffmpeg"] = None  # use the system one
            else:
                try:
                    import imageio_ffmpeg
                except ImportError:
                    if FROZEN:
                        raise
                    ENGINE["message"] = "Fetching ffmpeg (one time)..."
                    pip_install("imageio-ffmpeg")
                    import imageio_ffmpeg
                _paths["ffmpeg"] = imageio_ffmpeg.get_ffmpeg_exe()

            deno = _find_bundled("deno")
            if not deno and not FROZEN:
                ENGINE["message"] = "Fetching the JavaScript helper (one time)..."
                try:
                    pip_install("deno")
                    deno = _find_bundled("deno")
                except Exception:
                    deno = None
            _paths["deno"] = deno

            ENGINE.update(ready=True, error=None, message="Ready")
        except Exception as e:
            ENGINE.update(ready=False, error=str(e),
                          message="Could not set up the download engine: " + str(e))
            raise


# ---- "YouTube sign-in" settings (borrowing the browser's login cookies)

def load_settings():
    s = {"mode": "none", "browser": "firefox"}
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("mode") in ("none", "browser", "file"):
            s["mode"] = data["mode"]
        if data.get("browser") in BROWSERS:
            s["browser"] = data["browser"]
    except Exception:
        pass
    return s


def save_settings(s):
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f)


def public_settings():
    s = load_settings()
    s["has_file"] = os.path.isfile(COOKIE_PATH)
    return s


def save_cookie_file(text):
    text = str(text or "")
    if len(text) > 5_000_000:
        raise ValueError("That file is too big to be a cookies.txt file.")
    lines = [l for l in text.splitlines() if l.strip() and not l.startswith("#")]
    looks_ok = text.lstrip().startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File")) or \
        any(len(l.split("\t")) >= 7 for l in lines)
    if not looks_ok:
        raise ValueError("That doesn't look like a cookies.txt file. Export it again with the "
                         "browser extension and pick the downloaded .txt file.")
    if "youtube.com" not in text:
        raise ValueError("This cookies file has no YouTube cookies. Open youtube.com while "
                         "signed in, then export again.")
    with open(COOKIE_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(text if text.endswith("\n") else text + "\n")
    try:
        os.chmod(COOKIE_PATH, 0o600)
    except OSError:
        pass


def base_opts(logger):
    opts = {"noplaylist": True, "logger": logger, "quiet": True, "no_warnings": False}
    if _paths.get("ffmpeg"):
        opts["ffmpeg_location"] = _paths["ffmpeg"]
    if _paths.get("deno"):
        opts["js_runtimes"] = {"deno": {"path": _paths["deno"]}}
    s = load_settings()
    if s["mode"] == "file" and os.path.isfile(COOKIE_PATH):
        opts["cookiefile"] = COOKIE_PATH
    elif s["mode"] == "browser":
        opts["cookiesfrombrowser"] = (s["browser"],)
    return opts


class ListLogger:
    def __init__(self, sink=None):
        self.lines = sink if sink is not None else []

    def _add(self, msg):
        self.lines.append(ANSI.sub("", str(msg)))
        del self.lines[:-200]

    def debug(self, msg):
        if not msg.startswith("[debug] "):
            self._add(msg)

    def info(self, msg):
        self._add(msg)

    def warning(self, msg):
        self._add("Warning: " + msg)

    def error(self, msg):
        self._add(msg)


BOT_HINT = ("YouTube wants to check you're not a bot. Click \"YouTube sign-in\" at the top, "
            "connect your browser's YouTube login, then try again.")


def clean_error(text):
    text = ANSI.sub("", str(text)).strip()
    low = text.lower()
    if "not a bot" in low or "sign in to confirm" in low:
        return BOT_HINT
    if "dpapi" in low or "cookie database" in low or "could not find" in low and "cookies" in low \
            or "failed to decrypt" in low or "app-bound" in low:
        return ("Couldn't read the login from that browser. Close it completely and try again, "
                "or use the cookies.txt option in \"YouTube sign-in\". It works with any browser.")
    text = re.sub(r"^(ERROR:\s*)?(\[[^\]]+\]\s*)?([\w-]{6,}:\s*)?", "", text)
    return text or "Something went wrong."


def get_info(url):
    prepare_engine()
    import yt_dlp
    log = ListLogger()
    opts = base_opts(log)
    opts["skip_download"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("_type") == "playlist":
        raise ValueError("That looks like a playlist. Paste the link of a single video.")
    heights = sorted({f.get("height") for f in info.get("formats") or []
                      if f.get("height") and f.get("vcodec") not in (None, "none")}, reverse=True)
    chapters = [{"title": c.get("title") or f"Chapter {i + 1}",
                 "start": c.get("start_time") or 0, "end": c.get("end_time")}
                for i, c in enumerate(info.get("chapters") or [])]
    return {
        "title": info.get("title") or "Untitled",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "id": info.get("id"),
        "site": info.get("extractor_key") or "",
        "is_live": bool(info.get("is_live")),
        "heights": heights,
        "chapters": chapters,
    }


# ------------------------------------------------------------------ jobs

JOBS = {}
JOBS_LOCK = threading.Lock()

STEP_NAMES = {
    "Merger": "Joining video and audio",
    "FFmpegMerger": "Joining video and audio",
    "ExtractAudio": "Converting to MP3",
    "FFmpegExtractAudio": "Converting to MP3",
}


def public_job(job):
    out = {k: job[k] for k in ("id", "title", "start", "end", "mode", "quality", "status",
                                "percent", "message", "created")}
    fp = job.get("filepath")
    out["filename"] = os.path.basename(fp) if fp else None
    out["size"] = os.path.getsize(fp) if fp and os.path.exists(fp) else None
    out["log"] = job["log"][-40:]
    return out


def run_job(job, url, opts_in):
    try:
        job["message"] = "Getting ready..."
        prepare_engine()
        import yt_dlp
        from yt_dlp.utils import download_range_func

        os.makedirs(OUT_DIR, exist_ok=True)
        start, end = job["start"], job["end"]
        tag = f"{fmt_tag(start)}-{fmt_tag(end)}"
        if job["mode"] == "audio":
            tag += " audio"
        tmp_dir = os.path.join(OUT_DIR, ".working", job["id"])

        def progress(d):
            if d["status"] == "downloading":
                job["status"] = "downloading"
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                done = d.get("downloaded_bytes")
                if total and done:
                    job["percent"] = max(1.0, min(97.0, done * 100.0 / total))
                speed = d.get("_speed_str", "").strip()
                job["message"] = "Downloading" + (f" ({speed})" if speed and "Unknown" not in speed else "")
            elif d["status"] == "finished":
                job["status"] = "processing"
                job["message"] = "Finishing up"

        def pp_hook(d):
            if d["status"] == "started":
                job["status"] = "processing"
                job["message"] = STEP_NAMES.get(d.get("postprocessor"), "Finishing up")

        def done_hook(path):
            job["filepath"] = path

        opts = base_opts(ListLogger(job["log"]))
        opts.update({
            "outtmpl": f"%(title).120B [{tag}].%(ext)s",
            "paths": {"home": OUT_DIR, "temp": tmp_dir},
            "download_ranges": download_range_func(None, [(start, end)]),
            "force_keyframes_at_cuts": bool(opts_in.get("precise")),
            "windowsfilenames": True,
            "overwrites": True,
            "progress_hooks": [progress],
            "postprocessor_hooks": [pp_hook],
            "post_hooks": [done_hook],
        })

        if job["mode"] == "audio":
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio",
                                       "preferredcodec": "mp3", "preferredquality": "192"}]
        else:
            h = job["quality"]
            cap = f"[height<={h}]" if isinstance(h, int) else ""
            opts["format"] = (f"bv*{cap}[ext=mp4]+ba[ext=m4a]/b{cap}[ext=mp4]/"
                              f"bv*{cap}+ba/b{cap}/bv*+ba/b")
            opts["merge_output_format"] = "mp4"

        job["status"] = "downloading"
        job["message"] = "Starting download"
        with yt_dlp.YoutubeDL(opts) as ydl:
            code = ydl.download([url])

        fp = job.get("filepath")
        if code or not fp or not os.path.exists(fp):
            errors = [l for l in job["log"] if "ERROR" in l[:12]]
            raise RuntimeError(errors[-1] if errors else "The download did not finish.")
        if os.path.getsize(fp) < 2048:
            os.remove(fp)
            job["filepath"] = None
            raise RuntimeError("The clip came out empty. Check that your start and end "
                               "times are inside the video.")
        job.update(status="done", percent=100.0, message="Saved")
    except Exception as e:
        job.update(status="error", message=clean_error(e))
    finally:
        shutil.rmtree(os.path.join(OUT_DIR, ".working", job["id"]), ignore_errors=True)
        try:
            os.rmdir(os.path.join(OUT_DIR, ".working"))
        except OSError:
            pass


def create_job(data):
    url = str(data.get("url", "")).strip()
    if not url:
        raise ValueError("Paste a video link first.")
    start = parse_time(data.get("start", "0"))
    end = parse_time(data.get("end", "0"))
    if end <= start:
        raise ValueError("The end time must be after the start time.")
    mode = "audio" if data.get("mode") == "audio" else "video"
    q = data.get("quality", "best")
    quality = int(q) if str(q).isdigit() else "best"
    job = {
        "id": uuid.uuid4().hex[:10], "url": url,
        "title": str(data.get("title") or "Clip")[:200],
        "start": start, "end": end, "mode": mode, "quality": quality,
        "status": "queued", "percent": None, "message": "Queued",
        "created": time.time(), "log": [], "filepath": None,
    }
    with JOBS_LOCK:
        JOBS[job["id"]] = job
    threading.Thread(target=run_job, args=(job, url, data), daemon=True).start()
    return job


# --------------------------------------------------------- open the folder

def reveal(path=None):
    os.makedirs(OUT_DIR, exist_ok=True)
    target = path if path and os.path.exists(path) else OUT_DIR
    is_file = os.path.isfile(target)
    if sys.platform == "win32":
        if is_file:
            subprocess.Popen(f'explorer /select,"{target}"')
        else:
            os.startfile(target)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", target] if is_file else ["open", target])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(target) if is_file else target])


def update_engine():
    if FROZEN:
        return ("This is the packaged app. To update the engine, run "
                "'Build EXE (Windows).bat' again to make a fresh copy.")
    pip_install("yt-dlp[default,deno]")
    return "Engine updated. Quit Clip Grabber and start it again to use the new version."


# ------------------------------------------------------------------ server

SERVER = None


class Handler(BaseHTTPRequestHandler):
    server_version = "ClipGrabber/1.0"

    def log_message(self, *args):  # keep the console quiet
        pass

    # Only answer requests addressed to this machine (blocks DNS-rebinding tricks).
    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost", "::1")

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self):
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            raise PermissionError("JSON only")
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send(200, INDEX_HTML, "text/html; charset=utf-8")
        if path == "/api/ping":
            return self._send(200, {"app": APP_ID, "engine": ENGINE, "folder": OUT_DIR,
                                    "packaged": FROZEN})
        if path == "/api/settings":
            return self._send(200, public_settings())
        if path == "/api/jobs":
            with JOBS_LOCK:
                jobs = sorted(JOBS.values(), key=lambda j: j["created"], reverse=True)
                return self._send(200, [public_job(j) for j in jobs])
        m = re.fullmatch(r"/api/file/([0-9a-f]{10})", path)
        if m:
            job = JOBS.get(m.group(1))
            fp = job and job.get("filepath")
            if not fp or not os.path.exists(fp):
                return self._send(404, {"error": "File not found"})
            name = os.path.basename(fp)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(os.path.getsize(fp)))
            self.send_header("Content-Disposition",
                             f"attachment; filename*=UTF-8''{quote(name)}")
            self.end_headers()
            with open(fp, "rb") as f:
                shutil.copyfileobj(f, self.wfile)
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        path = urlparse(self.path).path
        try:
            data = self._json_body()
        except PermissionError:
            return self._send(415, {"error": "JSON only"})
        except Exception:
            return self._send(400, {"error": "Bad request"})
        try:
            if path == "/api/info":
                url = str(data.get("url", "")).strip()
                if not url:
                    raise ValueError("Paste a video link first.")
                return self._send(200, get_info(url))
            if path == "/api/download":
                return self._send(200, public_job(create_job(data)))
            if path == "/api/reveal":
                job = JOBS.get(str(data.get("id", "")))
                reveal(job.get("filepath") if job else None)
                return self._send(200, {"ok": True})
            if path == "/api/settings":
                s = load_settings()
                if data.get("mode") in ("none", "browser", "file"):
                    s["mode"] = data["mode"]
                if data.get("browser") in BROWSERS:
                    s["browser"] = data["browser"]
                if s["mode"] == "file" and not os.path.isfile(COOKIE_PATH):
                    raise ValueError("Choose your cookies.txt file first.")
                save_settings(s)
                return self._send(200, public_settings())
            if path == "/api/cookies":
                save_cookie_file(data.get("text"))
                s = load_settings()
                s["mode"] = "file"
                save_settings(s)
                return self._send(200, public_settings())
            if path == "/api/cookies/remove":
                if os.path.isfile(COOKIE_PATH):
                    os.remove(COOKIE_PATH)
                s = load_settings()
                if s["mode"] == "file":
                    s["mode"] = "none"
                save_settings(s)
                return self._send(200, public_settings())
            if path == "/api/clear":
                with JOBS_LOCK:
                    for k in [k for k, j in JOBS.items() if j["status"] in ("done", "error")]:
                        JOBS.pop(k)
                return self._send(200, {"ok": True})
            if path == "/api/update":
                return self._send(200, {"message": update_engine()})
            if path == "/api/quit":
                self._send(200, {"ok": True})
                threading.Thread(target=SERVER.shutdown, daemon=True).start()
                return
        except Exception as e:
            return self._send(400, {"error": clean_error(e)})
        self._send(404, {"error": "not found"})


def already_running(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as r:
            return json.load(r).get("app") == APP_ID
    except Exception:
        return False


def main():
    global SERVER
    no_browser = "--no-browser" in sys.argv
    port = DEFAULT_PORT
    if already_running(port):
        print(f"{APP_NAME} is already running - opening it.")
        if not no_browser:
            webbrowser.open(f"http://127.0.0.1:{port}/")
        return
    try:
        SERVER = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        SERVER = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = SERVER.server_address[1]
    SERVER.daemon_threads = True
    url = f"http://127.0.0.1:{port}/"
    print(f"{APP_NAME} is running at {url}")
    print(f"Clips are saved to: {OUT_DIR}")
    print("Keep this window open while you use it. Close it (or click Quit) to stop.")
    threading.Thread(target=lambda: _safe(prepare_engine), daemon=True).start()
    if not no_browser:
        threading.Timer(0.8, webbrowser.open, [url]).start()
    try:
        SERVER.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        SERVER.server_close()


def _safe(fn):
    try:
        fn()
    except Exception:
        pass


# --------------------------------------------------------------- the page

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clip Grabber</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%23ff6a3d'/%3E%3Cpath d='M9 10v12M23 10v12' stroke='%23fff' stroke-width='3' stroke-linecap='round'/%3E%3Cpath d='M13 16h6' stroke='%23fff' stroke-width='3' stroke-linecap='round'/%3E%3C/svg%3E">
<style>
:root{
  --bg:#141312; --panel:#1d1b19; --panel2:#252220; --line:#34302c; --text:#f2ede6;
  --muted:#a39a8f; --accent:#ff6a3d; --accent-ink:#1a0d07; --accent-soft:rgba(255,106,61,.16);
  --ok:#4cc38a; --err:#ff5d5d; --track:#2c2926; --sel:rgba(255,106,61,.35);
  --mono:ui-monospace,"SF Mono","Cascadia Mono","JetBrains Mono",Consolas,monospace;
  --sans:"Segoe UI Variable","Segoe UI",system-ui,-apple-system,"Helvetica Neue",Arial,sans-serif;
  --r:14px;
}
@media (prefers-color-scheme: light){
  :root{--bg:#f6f2ec;--panel:#fffdf9;--panel2:#f1ece4;--line:#e3dcd1;--text:#1d1a17;--muted:#6f665c;
  --accent:#e8501f;--accent-ink:#fff;--accent-soft:rgba(232,80,31,.12);--track:#e9e2d8;--sel:rgba(232,80,31,.28)}
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 var(--sans)}
button,input,select{font:inherit;color:inherit}
.wrap{max-width:920px;margin:0 auto;padding:20px 16px 60px}
header{display:flex;align-items:center;gap:12px;margin-bottom:22px}
.logo{width:34px;height:34px;border-radius:9px;background:var(--accent);display:grid;place-items:center;flex:none}
h1{font-size:19px;margin:0;letter-spacing:-.01em;white-space:nowrap}
@media (max-width:520px){header{flex-wrap:wrap;row-gap:10px}header .spacer{flex-basis:100%;height:0}}
.pill{font-size:12px;padding:3px 10px;border-radius:99px;background:var(--panel2);color:var(--muted);white-space:nowrap;display:inline-flex;gap:6px;align-items:center}
.pill i{width:7px;height:7px;border-radius:50%;background:var(--muted)}
.pill.ok i{background:var(--ok)} .pill.bad i{background:var(--err)} .pill.wait i{background:var(--accent);animation:blink 1s infinite}
@keyframes blink{50%{opacity:.25}}
.spacer{flex:1}
.ghost{background:none;border:1px solid var(--line);border-radius:10px;padding:7px 12px;cursor:pointer;color:var(--muted)}
.ghost:hover{color:var(--text);border-color:var(--muted)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:18px;margin-bottom:16px}
.urlrow{display:flex;gap:10px}
.urlrow input{flex:1;min-width:0;background:var(--panel2);border:1px solid var(--line);border-radius:11px;padding:13px 14px;outline:none}
.urlrow input:focus,.tc:focus,select:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.btn{background:var(--accent);color:var(--accent-ink);border:0;border-radius:11px;padding:12px 18px;font-weight:650;cursor:pointer;white-space:nowrap}
.btn:disabled{opacity:.5;cursor:default}
.btn.big{width:100%;padding:15px;font-size:16px}
.hint{color:var(--muted);font-size:13px;margin-top:8px}
.err{color:var(--err);font-size:14px;margin-top:10px}
.hidden{display:none!important}

.media{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(0,1fr);gap:18px}
@media (max-width:760px){.media{grid-template-columns:1fr}}
.player{aspect-ratio:16/9;background:#000;border-radius:11px;overflow:hidden;position:relative}
.player iframe,.player img{position:absolute;inset:0;width:100%;height:100%;border:0;object-fit:cover}
.meta h2{font-size:17px;margin:0 0 4px;line-height:1.3}
.meta .sub{color:var(--muted);font-size:13px}
.label{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:14px 0 6px}

.timeline{position:relative;height:46px;margin:18px 6px 6px;touch-action:none;user-select:none}
.track{position:absolute;left:0;right:0;top:17px;height:12px;border-radius:6px;background:var(--track);cursor:pointer}
.range{position:absolute;top:17px;height:12px;background:var(--sel);border-top:2px solid var(--accent);border-bottom:2px solid var(--accent)}
.chap{position:absolute;top:13px;width:2px;height:20px;background:var(--line)}
.head{position:absolute;top:8px;width:2px;height:30px;background:var(--text);opacity:.7;pointer-events:none;transform:translateX(-1px)}
.handle{position:absolute;top:6px;width:16px;height:34px;margin-left:-8px;border-radius:6px;background:var(--accent);cursor:ew-resize;border:2px solid var(--panel);outline:none}
.handle:focus-visible{box-shadow:0 0 0 3px var(--accent-soft),0 0 0 5px var(--accent)}
.handle::after{content:"";position:absolute;left:5px;top:9px;width:2px;height:12px;border-left:2px solid var(--accent-ink);opacity:.5}
.scale{display:flex;justify-content:space-between;font:12px var(--mono);color:var(--muted);margin:0 6px}

.times{display:grid;grid-template-columns:1fr 1fr auto;gap:14px;align-items:end;margin-top:16px}
@media (max-width:620px){.times{grid-template-columns:1fr}.len{text-align:left}}
.tbox label{display:block;font-size:12px;color:var(--muted);margin-bottom:5px}
.tline{display:flex;gap:6px}
.tc{width:100%;min-width:0;background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:10px 12px;font:16px var(--mono);outline:none}
.small{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:0 10px;cursor:pointer;color:var(--muted);font-size:13px;white-space:nowrap}
.small:hover{color:var(--text)}
.len{text-align:right}
.len b{display:block;font:22px var(--mono);color:var(--accent)}
.len span{font-size:12px;color:var(--muted)}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{background:var(--panel2);border:1px solid var(--line);border-radius:99px;padding:5px 11px;font-size:13px;cursor:pointer}
.chip:hover{border-color:var(--accent)}
.opts{display:flex;flex-wrap:wrap;gap:14px;align-items:center;margin:16px 0}
.seg{display:inline-flex;background:var(--panel2);border:1px solid var(--line);border-radius:11px;padding:3px}
.seg button{background:none;border:0;border-radius:8px;padding:8px 14px;cursor:pointer;color:var(--muted)}
.seg button[aria-pressed=true]{background:var(--panel);color:var(--text);box-shadow:0 1px 3px rgba(0,0,0,.25)}
select{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:9px 10px;outline:none}
.toggle{display:inline-flex;gap:8px;align-items:center;cursor:pointer;color:var(--muted);font-size:14px}
.toggle input{accent-color:var(--accent);width:17px;height:17px}

.jobs h3{margin:0;font-size:15px}
.jobhead{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.job{border-top:1px solid var(--line);padding:14px 0 4px}
.job:first-of-type{border-top:0}
.jt{display:flex;gap:10px;align-items:baseline}
.jt b{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0}
.jt code{font:12px var(--mono);color:var(--muted);white-space:nowrap}
.bar{height:6px;border-radius:3px;background:var(--track);overflow:hidden;margin:9px 0 7px}
.bar>i{display:block;height:100%;background:var(--accent);transition:width .4s}
.bar.indet>i{width:35%!important;animation:slide 1.2s infinite ease-in-out}
@keyframes slide{from{transform:translateX(-100%)}to{transform:translateX(290%)}}
.bar.ok>i{background:var(--ok)} .bar.bad>i{background:var(--err);width:100%!important}
.jm{display:flex;gap:10px;align-items:center;font-size:13px;color:var(--muted);flex-wrap:wrap}
.jm .msg{flex:1;min-width:150px}
.jm.bad .msg{color:var(--err)}
.link{background:none;border:0;color:var(--accent);cursor:pointer;padding:2px 0;font-size:13px;text-decoration:none}
.link:hover{text-decoration:underline}
.chip:disabled,.small:disabled{opacity:.4;cursor:default;border-color:var(--line)}
.noprev{position:absolute;inset:0;display:grid;place-items:center;color:#8a8178;font-size:14px}
details{margin-top:6px} summary{cursor:pointer;color:var(--muted);font-size:12px}
pre{font:11.5px/1.5 var(--mono);color:var(--muted);background:var(--panel2);border-radius:8px;padding:10px;max-height:180px;overflow:auto;white-space:pre-wrap;word-break:break-word}
footer{color:var(--muted);font-size:12.5px;text-align:center;margin-top:24px}
.signin{border-color:var(--accent)}
.signin p{margin:0 0 4px}
.ckpane{margin-top:12px}
.steps{margin:8px 0 10px;padding-left:20px;line-height:1.7}
.steps b{color:var(--text);font-weight:600}
.filepick{display:inline-flex;align-items:center;gap:8px;background:var(--panel2);border:1px dashed var(--muted);border-radius:10px;padding:10px 14px;cursor:pointer}
.filepick input{display:none}
.okmsg{color:var(--ok)}
.ckrow{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:16px}
footer button{font-size:12.5px}
.empty{color:var(--muted);font-size:14px;padding:6px 0}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo" aria-hidden="true">
      <svg width="20" height="20" viewBox="0 0 20 20"><path d="M5 4v12M15 4v12M8 10h4" stroke="#fff" stroke-width="2.4" stroke-linecap="round"/></svg>
    </div>
    <h1>Clip Grabber</h1>
    <span id="engine" class="pill wait"><i></i><span>Starting...</span></span>
    <div class="spacer"></div>
    <button class="ghost" id="openSignin" title="Fix 'confirm you're not a bot' errors">YouTube sign-in</button>
    <button class="ghost" id="openFolder" title="Open the folder where clips are saved">Open folder</button>
    <button class="ghost" id="quit" title="Stop Clip Grabber">Quit</button>
  </header>

  <section class="card">
    <form class="urlrow" id="urlForm">
      <input id="url" type="url" placeholder="Paste a video link..." autocomplete="off" spellcheck="false" required>
      <button class="btn" id="loadBtn" type="submit">Load</button>
    </form>
    <div class="hint">Paste a link, pick the part you want, and download only that part.</div>
    <div class="err hidden" id="loadErr"></div>
  </section>

  <section class="card signin hidden" id="signin">
    <div class="jobhead"><h3>YouTube sign-in</h3><div class="spacer"></div><button class="link" id="closeSignin">Close</button></div>
    <p class="hint" style="margin-top:0">Sometimes YouTube says <b>"Sign in to confirm you're not a bot"</b>, often on mobile data or shared internet. Clip Grabber can borrow the YouTube login from your browser (its cookies) so YouTube trusts it. Your login stays on this computer and is only sent to YouTube.</p>
    <div class="label">How to connect</div>
    <div class="seg" role="group" aria-label="Sign-in method">
      <button type="button" data-ck="none">Off</button>
      <button type="button" data-ck="browser">From a browser</button>
      <button type="button" data-ck="file">cookies.txt file</button>
    </div>
    <div class="ckpane hidden" id="ckBrowser">
      <select id="ckBrowserSel" aria-label="Browser">
        <option value="firefox">Firefox</option><option value="chrome">Chrome</option><option value="edge">Edge</option>
        <option value="brave">Brave</option><option value="opera">Opera</option><option value="vivaldi">Vivaldi</option>
        <option value="chromium">Chromium</option><option value="safari">Safari (Mac)</option>
      </select>
      <div class="hint">Make sure you're signed in to YouTube in that browser. <b>Firefox works best.</b> On Windows, Chrome and Edge lock their logins, so if that fails, use the cookies.txt option instead.</div>
    </div>
    <div class="ckpane hidden" id="ckFile">
      <ol class="hint steps">
        <li>In the browser where you're signed in to YouTube, install the free extension <b>Get cookies.txt LOCALLY</b>.</li>
        <li>Open <b>youtube.com</b>, click the extension's icon, and click <b>Export</b>.</li>
        <li>Choose the downloaded file here:</li>
      </ol>
      <label class="filepick"><input type="file" id="ckFileInput" accept=".txt,text/plain">&#128196; <span>Choose cookies.txt</span></label>
      <button class="link" id="ckRemove" style="margin-left:10px">Remove saved file</button>
      <div class="hint" id="ckFileStatus"></div>
    </div>
    <div class="ckrow">
      <button class="btn" id="ckSave">Save and try again</button>
      <span class="hint" id="ckMsg" style="margin:0"></span>
    </div>
    <div class="hint">Tip: if you can, sign in with a spare Google account. YouTube occasionally flags accounts used this way.</div>
  </section>

  <section class="card hidden" id="editor">
    <div class="media">
      <div class="player" id="player"></div>
      <div class="meta">
        <h2 id="vTitle"></h2>
        <div class="sub" id="vSub"></div>
        <div id="chapWrap" class="hidden">
          <div class="label">Chapters</div>
          <div class="chips" id="chips"></div>
        </div>
        <div class="label">Preview</div>
        <div class="chips">
          <button class="chip" id="playClip">&#9654; Play the clip</button>
          <button class="chip" id="jumpEnd">Jump to end</button>
        </div>
        <div class="hint" id="previewNote"></div>
      </div>
    </div>

    <div class="timeline" id="timeline">
      <div class="track" id="track"></div>
      <div id="chapMarks"></div>
      <div class="range" id="range"></div>
      <div class="head hidden" id="head"></div>
      <div class="handle" id="hStart" tabindex="0" role="slider" aria-label="Clip start"></div>
      <div class="handle" id="hEnd" tabindex="0" role="slider" aria-label="Clip end"></div>
    </div>
    <div class="scale"><span>0:00</span><span id="durLabel"></span></div>

    <div class="times">
      <div class="tbox">
        <label for="tStart">Start</label>
        <div class="tline"><input class="tc" id="tStart" value="0:00"><button class="small" id="setStart" title="Use the player's current time">Use now</button></div>
      </div>
      <div class="tbox">
        <label for="tEnd">End</label>
        <div class="tline"><input class="tc" id="tEnd" value="0:30"><button class="small" id="setEnd" title="Use the player's current time">Use now</button></div>
      </div>
      <div class="len"><b id="clipLen">0:30</b><span>clip length</span></div>
    </div>
    <div class="hint">Drag the handles, type times (like 1:30 or 1:02:03), or play the video and press "Use now". Arrow keys nudge a selected handle by 1s (Shift = 5s).</div>

    <div class="opts">
      <div class="seg" role="group" aria-label="Format">
        <button type="button" id="mVideo" aria-pressed="true">Video MP4</button>
        <button type="button" id="mAudio" aria-pressed="false">Audio MP3</button>
      </div>
      <select id="quality" aria-label="Quality"></select>
      <label class="toggle"><input type="checkbox" id="precise" checked> Exact cut <span style="opacity:.7">(slower)</span></label>
    </div>
    <button class="btn big" id="dlBtn">Download clip</button>
    <div class="err hidden" id="dlErr"></div>
  </section>

  <section class="card jobs">
    <div class="jobhead"><h3>Your clips</h3><div class="spacer"></div><button class="link" id="clear">Clear finished</button></div>
    <div id="jobs"><div class="empty">Clips you download will show up here.</div></div>
  </section>

  <footer>
    Saved to <span id="folder"></span> &middot;
    <button class="link" id="update">Update download engine</button><br>
    Only download videos you have the right to use.
  </footer>
</div>

<script>
const $ = id => document.getElementById(id);
const api = async (path, body) => {
  const r = await fetch(path, body === undefined ? {} : {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
  const j = await r.json().catch(() => ({error:"No response from the app. Is it still running?"}));
  if (!r.ok || j.error) throw new Error(j.error || "Request failed");
  return j;
};

/* ---------- time helpers ---------- */
function fmt(t){
  t = Math.max(0, Math.round(t*10)/10);
  const h = Math.floor(t/3600), m = Math.floor(t%3600/60), s = t%60;
  const ss = (s<10?"0":"") + (Number.isInteger(s) ? s : s.toFixed(1));
  return h ? `${h}:${String(m).padStart(2,"0")}:${ss}` : `${m}:${ss}`;
}
function parse(txt){
  txt = String(txt).trim();
  if (!/^\d+(:\d{1,2}){0,2}(\.\d+)?$/.test(txt)) return null;
  return txt.split(":").reduce((a,p) => a*60 + parseFloat(p), 0);
}

/* ---------- state ---------- */
const S = {info:null, url:"", dur:0, start:0, end:30, mode:"video", yt:null, ytReady:false, stopAt:null};

/* ---------- engine status ---------- */
async function ping(){
  try{
    const p = await api("/api/ping");
    $("folder").textContent = p.folder;
    const el = $("engine"), e = p.engine;
    el.className = "pill " + (e.ready ? "ok" : e.error ? "bad" : "wait");
    el.lastChild.textContent = e.ready ? "Ready" : e.error ? "Setup problem" : e.message;
    el.title = e.error || "";
    if (!e.ready && !e.error) setTimeout(ping, 1500);
  }catch(_){ setTimeout(ping, 3000); }
}
ping();

/* ---------- load a video ---------- */
$("urlForm").addEventListener("submit", async ev => {
  ev.preventDefault();
  const url = $("url").value.trim();
  if (!url) return;
  const btn = $("loadBtn"); btn.disabled = true; btn.textContent = "Loading...";
  $("loadErr").classList.add("hidden");
  try{
    const info = await api("/api/info", {url});
    S.info = info; S.url = url; S.dur = info.duration || 0;
    if (info.is_live) throw new Error("Live streams aren't supported. Try again once the stream has ended.");
    S.start = 0; S.end = S.dur ? Math.min(30, S.dur) : 30;
    showEditor();
  }catch(e){
    $("loadErr").textContent = e.message; $("loadErr").classList.remove("hidden");
    if (/not a bot|YouTube sign-in/.test(e.message)) openSignin();
  }finally{ btn.disabled = false; btn.textContent = "Load"; }
});

function showEditor(){
  const i = S.info;
  $("vTitle").textContent = i.title;
  $("vSub").textContent = [i.uploader, S.dur ? fmt(S.dur) : "length unknown", i.site].filter(Boolean).join(" · ");
  $("durLabel").textContent = fmt(S.dur);
  $("timeline").classList.toggle("hidden", !S.dur);
  document.querySelector(".scale").classList.toggle("hidden", !S.dur);
  // quality list
  const q = $("quality"); q.innerHTML = "";
  const add = (v,t) => { const o = document.createElement("option"); o.value = v; o.textContent = t; q.appendChild(o); };
  add("best","Best quality");
  [...new Set(i.heights.filter(h => h >= 144))].forEach(h => add(h, h + "p"));
  // chapters
  const chips = $("chips"); chips.innerHTML = ""; $("chapMarks").innerHTML = "";
  $("chapWrap").classList.toggle("hidden", !i.chapters.length);
  i.chapters.forEach(c => {
    const b = document.createElement("button"); b.className = "chip"; b.textContent = c.title;
    b.title = fmt(c.start) + " - " + fmt(c.end ?? S.dur);
    b.onclick = () => { setRange(c.start, c.end ?? S.dur); seek(c.start); };
    chips.appendChild(b);
    if (c.start > 0){ const m = document.createElement("div"); m.className = "chap"; m.style.left = pct(c.start); $("chapMarks").appendChild(m); }
  });
  buildPlayer();
  setRange(S.start, S.end);
  $("editor").classList.remove("hidden");
  $("editor").scrollIntoView({behavior:"smooth", block:"start"});
}

/* ---------- player (embedded preview where available) ---------- */
function buildPlayer(){
  const box = $("player"); box.innerHTML = ""; S.yt = null; S.ytReady = false;
  $("head").classList.add("hidden");
  const isYT = /youtube/i.test(S.info.site) && S.info.id;
  const thumb = () => {
    box.innerHTML = "";
    if (S.info.thumbnail){ const im = new Image(); im.src = S.info.thumbnail; im.alt = ""; box.appendChild(im); }
    else box.innerHTML = '<div class="noprev">No preview</div>';
    ["playClip","jumpEnd","setStart","setEnd"].forEach(id => $(id).disabled = true);
  };
  if (!isYT){ thumb(); $("previewNote").textContent = "Preview isn't available for this site, but you can still pick times and download."; return; }
  ["playClip","jumpEnd","setStart","setEnd"].forEach(id => $(id).disabled = false);
  $("previewNote").textContent = "";
  const div = document.createElement("div"); div.id = "ytp"; box.appendChild(div);
  const make = () => {
    S.yt = new YT.Player("ytp", {
      videoId: S.info.id,
      playerVars: {rel:0, playsinline:1, origin: location.origin},
      events: {
        onReady: () => { S.ytReady = true; $("head").classList.remove("hidden"); },
        onError: () => { thumb(); $("previewNote").textContent = "This video can't be previewed here, but it can still be downloaded."; }
      }
    });
  };
  if (window.YT && YT.Player) make();
  else {
    window.onYouTubeIframeAPIReady = make;
    if (!document.querySelector("script[data-yt]")){
      const s = document.createElement("script"); s.src = "https://www.youtube.com/iframe_api"; s.dataset.yt = 1;
      s.onerror = () => { thumb(); $("previewNote").textContent = "Couldn't load the preview player."; };
      document.head.appendChild(s);
    }
  }
}
const now = () => (S.ytReady && S.yt.getCurrentTime) ? S.yt.getCurrentTime() : null;
function seek(t){ if (S.ytReady) S.yt.seekTo(t, true); }
setInterval(() => {
  const t = now(); if (t == null) return;
  $("head").style.left = pct(t);
  if (S.stopAt != null && t >= S.stopAt){ S.yt.pauseVideo(); S.stopAt = null; }
}, 200);
$("playClip").onclick = () => { if (!S.ytReady) return; seek(S.start); S.yt.playVideo(); S.stopAt = S.end; };
$("jumpEnd").onclick = () => { if (!S.ytReady) return; S.stopAt = S.end; seek(Math.max(S.start, S.end - 3)); S.yt.playVideo(); };
$("setStart").onclick = () => { const t = now(); if (t != null) setRange(t, Math.max(S.end, t + 1)); };
$("setEnd").onclick = () => { const t = now(); if (t != null) setRange(Math.min(S.start, t - 1), t); };

/* ---------- range selection ---------- */
const pct = t => (S.dur ? (t / S.dur * 100) : 0) + "%";
function setRange(a, b){
  const max = S.dur || Infinity;
  a = Math.max(0, Math.min(a, max)); b = Math.max(0, Math.min(b, max));
  if (b - a < 0.5){ if (a + 0.5 <= max) b = a + 0.5; else a = b - 0.5; }
  S.start = Math.round(a*10)/10; S.end = Math.round(b*10)/10;
  $("hStart").style.left = pct(S.start); $("hEnd").style.left = pct(S.end);
  $("range").style.left = pct(S.start); $("range").style.width = (S.dur ? (S.end-S.start)/S.dur*100 : 0) + "%";
  if (document.activeElement !== $("tStart")) $("tStart").value = fmt(S.start);
  if (document.activeElement !== $("tEnd")) $("tEnd").value = fmt(S.end);
  $("clipLen").textContent = fmt(S.end - S.start);
  $("hStart").setAttribute("aria-valuetext", fmt(S.start)); $("hEnd").setAttribute("aria-valuetext", fmt(S.end));
}
function timeAt(clientX){
  const r = $("track").getBoundingClientRect();
  return Math.max(0, Math.min(1, (clientX - r.left) / r.width)) * S.dur;
}
function drag(handle, which){
  handle.addEventListener("pointerdown", e => {
    e.preventDefault(); handle.setPointerCapture(e.pointerId); handle.focus();
    const move = ev => { const t = timeAt(ev.clientX); which === "s" ? setRange(Math.min(t, S.end - .5), S.end) : setRange(S.start, Math.max(t, S.start + .5)); };
    const up = () => { handle.removeEventListener("pointermove", move); handle.removeEventListener("pointerup", up); seek(which === "s" ? S.start : S.end); };
    handle.addEventListener("pointermove", move); handle.addEventListener("pointerup", up);
  });
  handle.addEventListener("keydown", e => {
    const step = e.shiftKey ? 5 : 1;
    const d = e.key === "ArrowLeft" || e.key === "ArrowDown" ? -step : e.key === "ArrowRight" || e.key === "ArrowUp" ? step : 0;
    if (!d) return; e.preventDefault();
    which === "s" ? setRange(Math.min(S.start + d, S.end - .5), S.end) : setRange(S.start, Math.max(S.end + d, S.start + .5));
  });
}
drag($("hStart"), "s"); drag($("hEnd"), "e");
$("track").addEventListener("pointerdown", e => {
  const t = timeAt(e.clientX);
  Math.abs(t - S.start) < Math.abs(t - S.end) ? setRange(t, S.end) : setRange(S.start, t);
  seek(t);
});
[["tStart","s"],["tEnd","e"]].forEach(([id, w]) => {
  const el = $(id);
  el.addEventListener("change", () => {
    const t = parse(el.value);
    if (t == null){ el.value = fmt(w === "s" ? S.start : S.end); return; }
    w === "s" ? setRange(t, Math.max(S.end, t + .5)) : setRange(Math.min(S.start, t - .5), t);
    el.value = fmt(w === "s" ? S.start : S.end);
  });
  el.addEventListener("keydown", e => { if (e.key === "Enter") el.blur(); });
});

/* ---------- options ---------- */
function setMode(m){
  S.mode = m;
  $("mVideo").setAttribute("aria-pressed", m === "video"); $("mAudio").setAttribute("aria-pressed", m === "audio");
  $("quality").classList.toggle("hidden", m === "audio");
}
$("mVideo").onclick = () => setMode("video"); $("mAudio").onclick = () => setMode("audio");

/* ---------- download + jobs ---------- */
$("dlBtn").onclick = async () => {
  const btn = $("dlBtn"); btn.disabled = true; $("dlErr").classList.add("hidden");
  try{
    await api("/api/download", {url:S.url, title:S.info.title, start:String(S.start), end:String(S.end),
      mode:S.mode, quality:$("quality").value, precise:$("precise").checked});
    refreshJobs();
    $("jobs").scrollIntoView({behavior:"smooth", block:"nearest"});
  }catch(e){ $("dlErr").textContent = e.message; $("dlErr").classList.remove("hidden"); }
  finally{ setTimeout(() => btn.disabled = false, 600); }
};

let pollTimer = null;
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const size = b => b == null ? "" : b > 1e6 ? (b/1e6).toFixed(1) + " MB" : Math.max(1, Math.round(b/1e3)) + " KB";
async function refreshJobs(){
  clearTimeout(pollTimer);
  let jobs = [];
  try{ jobs = await api("/api/jobs"); }catch(_){}
  const box = $("jobs");
  if (!jobs.length) box.innerHTML = '<div class="empty">Clips you download will show up here.</div>';
  else box.innerHTML = jobs.map(j => {
    const active = !["done","error"].includes(j.status);
    const barCls = j.status === "done" ? "ok" : j.status === "error" ? "bad" : (j.percent == null ? "indet" : "");
    const w = j.status === "done" ? 100 : (j.percent || 0);
    const acts = j.status === "done"
      ? `<button class="link" data-reveal="${j.id}">Show in folder</button><a class="link" href="/api/file/${j.id}" download>Save a copy</a>`
      : (j.status === "error" && /YouTube sign-in/.test(j.message) ? `<button class="link" data-signin="1">Fix this</button>` : "");
    return `<div class="job">
      <div class="jt"><b title="${esc(j.title)}">${esc(j.title)}</b><code>${fmt(j.start)} – ${fmt(j.end)} · ${j.mode === "audio" ? "MP3" : "MP4" + (j.quality !== "best" ? " " + j.quality + "p" : "")}</code></div>
      <div class="bar ${barCls}"><i style="width:${w}%"></i></div>
      <div class="jm ${j.status === "error" ? "bad" : ""}"><span class="msg">${esc(j.status === "done" ? (j.filename + " · " + size(j.size)) : j.message)}${active && j.percent != null ? " · " + Math.round(j.percent) + "%" : ""}</span>${acts}</div>
      ${j.log.length ? `<details><summary>Details</summary><pre>${esc(j.log.join("\n"))}</pre></details>` : ""}
    </div>`;
  }).join("");
  // keep open <details> state simple: re-render is fine at this size
  const busy = jobs.some(j => !["done","error"].includes(j.status));
  pollTimer = setTimeout(refreshJobs, busy ? 700 : 4000);
}
$("jobs").addEventListener("click", e => {
  const id = e.target.dataset && e.target.dataset.reveal;
  if (id) api("/api/reveal", {id}).catch(err => alert(err.message));
  if (e.target.dataset && e.target.dataset.signin) openSignin();
});

/* ---------- YouTube sign-in (cookies) ---------- */
const CK = {mode:"none", browser:"firefox", has_file:false};
function renderCk(){
  document.querySelectorAll("[data-ck]").forEach(b => b.setAttribute("aria-pressed", b.dataset.ck === CK.mode));
  $("ckBrowser").classList.toggle("hidden", CK.mode !== "browser");
  $("ckFile").classList.toggle("hidden", CK.mode !== "file");
  $("ckBrowserSel").value = CK.browser;
  $("ckFileStatus").innerHTML = CK.has_file ? '<span class="okmsg">&#10003; A cookies file is saved.</span> Choose a new one any time to replace it.' : "No file saved yet.";
  $("ckRemove").classList.toggle("hidden", !CK.has_file);
  const on = CK.mode === "browser" ? "On (" + $("ckBrowserSel").selectedOptions[0].textContent + ")" : CK.mode === "file" && CK.has_file ? "On (cookies file)" : "";
  $("openSignin").textContent = on ? "YouTube sign-in \u2713" : "YouTube sign-in";
}
async function loadCk(){ try{ Object.assign(CK, await api("/api/settings")); renderCk(); }catch(_){} }
function openSignin(){ $("signin").classList.remove("hidden"); $("ckMsg").textContent = ""; $("signin").scrollIntoView({behavior:"smooth", block:"start"}); }
$("openSignin").onclick = () => $("signin").classList.contains("hidden") ? openSignin() : $("signin").classList.add("hidden");
$("closeSignin").onclick = () => $("signin").classList.add("hidden");
document.querySelectorAll("[data-ck]").forEach(b => b.onclick = () => { CK.mode = b.dataset.ck; renderCk(); });
$("ckBrowserSel").onchange = () => { CK.browser = $("ckBrowserSel").value; };
$("ckFileInput").onchange = async e => {
  const f = e.target.files[0]; e.target.value = "";
  if (!f) return;
  $("ckFileStatus").textContent = "Saving...";
  try{ Object.assign(CK, await api("/api/cookies", {text: await f.text()})); renderCk(); $("ckMsg").textContent = "Cookies saved. Click \"Save and try again\"."; }
  catch(err){ $("ckFileStatus").innerHTML = '<span style="color:var(--err)">' + esc(err.message) + "</span>"; }
};
$("ckRemove").onclick = async () => { try{ Object.assign(CK, await api("/api/cookies/remove", {})); renderCk(); }catch(err){ alert(err.message); } };
$("ckSave").onclick = async () => {
  $("ckMsg").textContent = "";
  try{
    Object.assign(CK, await api("/api/settings", {mode: CK.mode, browser: $("ckBrowserSel").value}));
    renderCk();
    $("ckMsg").innerHTML = '<span class="okmsg">Saved.</span>';
    if ($("url").value.trim()){ $("signin").classList.add("hidden"); $("urlForm").requestSubmit(); }
  }catch(err){ $("ckMsg").innerHTML = '<span style="color:var(--err)">' + esc(err.message) + "</span>"; }
};
loadCk();
$("clear").onclick = () => api("/api/clear", {}).then(refreshJobs);
$("openFolder").onclick = () => api("/api/reveal", {}).catch(err => alert(err.message));
$("update").onclick = async () => {
  const b = $("update"); b.disabled = true; b.textContent = "Updating...";
  try{ alert((await api("/api/update", {})).message); }catch(e){ alert(e.message); }
  finally{ b.disabled = false; b.textContent = "Update download engine"; }
};
$("quit").onclick = async () => {
  if (!confirm("Stop Clip Grabber? Downloads in progress will stop.")) return;
  try{ await api("/api/quit", {}); }catch(_){}
  document.body.innerHTML = '<div class="wrap"><div class="card" style="margin-top:40px;text-align:center"><h2 style="margin:0 0 6px">Clip Grabber has stopped</h2><div class="hint">You can close this tab. Start the app again whenever you need it.</div></div></div>';
};
refreshJobs();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
