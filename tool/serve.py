import http.server
import socketserver
import socket
import os
import re
import json
import mimetypes
import hashlib
import time
import urllib.parse
import subprocess
import shutil
import sys
import threading
import zipfile
import io
import requests

PORT = 8888
DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(DIR, "settings.json")
CACHE_FILE = os.path.join(DIR, "cache.json")

# Auto-detect tool paths
IS_WIN = sys.platform == "win32"
YT_DLP = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe") or ""
FFMPEG = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe") or ""

# Fallback: winget paths on Windows
if IS_WIN and (not YT_DLP or not FFMPEG):
    winget_base = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
    if not YT_DLP:
        for root, dirs, files in os.walk(winget_base):
            if "yt-dlp.exe" in files and "yt-dlp.yt-dlp_" in root:
                YT_DLP = os.path.join(root, "yt-dlp.exe")
                break
    if not FFMPEG:
        for root, dirs, files in os.walk(winget_base):
            if "ffmpeg.exe" in files and "Gyan.FFmpeg_" in root:
                FFMPEG = os.path.join(root, "ffmpeg.exe")
                break

# Global WBI key cache
wbi_cache = {"img_key": "", "sub_key": "", "mix_key": "", "ts": 0}

def yt_dlp_base_args():
    """Base yt-dlp args including cookies if available."""
    args = [
        "--add-header", "User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "--add-header", "Referer:https://www.bilibili.com",
        "--add-header", "Origin:https://www.bilibili.com",
        "--add-header", "Accept-Language:zh-CN,zh;q=0.9",
    ]
    cookie_file = os.path.join(DIR, "cookies.txt")
    if os.path.exists(cookie_file):
        args += ["--cookies", cookie_file]
    return args

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_settings(data):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_cache(data):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def find_cached(bvid):
    """Return (song_entry, full_path) if bvid is cached and file exists."""
    cache = load_cache()
    entry = cache.get(bvid)
    if not entry:
        return None
    bases = [DIR]
    dl_path = load_settings().get("dl_path", "").strip()
    if dl_path and os.path.isdir(dl_path) and os.path.abspath(dl_path) != os.path.abspath(DIR):
        bases.insert(0, dl_path)
    for base in bases:
        full = os.path.join(base, entry["file"])
        if os.path.isfile(full):
            is_custom = os.path.abspath(base) != os.path.abspath(DIR)
            song = {
                "name": entry["file"].replace(".mp3", ""),
                "file": entry["file"],
                "size": os.path.getsize(full),
                "src": "/api/file?path=" + urllib.parse.quote(full, safe="") if is_custom else "/api/file?file=" + urllib.parse.quote(entry["file"], safe=""),
            }
            return (song, full)
    return None

_caching_lock = threading.Lock()
_caching = set()

def cache_in_background(bvid):
    """Download and cache a video in a background thread. Skips if already caching."""
    with _caching_lock:
        if bvid in _caching:
            log(f"Background cache already in progress for {bvid}, skipping")
            return
        _caching.add(bvid)
    try:
        settings = load_settings()
        dl_dir = settings.get("dl_path", "")
        out_dir = dl_dir if dl_dir and os.path.isdir(dl_dir) else DIR
        info = get_video_info(bvid)
        title = info.get("title", bvid)
        log(f"Background caching {bvid}: {title} -> {out_dir}")
        filename = download_and_convert(bvid, title, dl_dir)
        cache = load_cache()
        cache[bvid] = {"file": filename, "title": title, "time": int(time.time())}
        save_cache(cache)
        log(f"Background cache done: {bvid} -> {filename}")
    except Exception as e:
        log(f"Background cache failed for {bvid}: {e}")
    finally:
        with _caching_lock:
            _caching.discard(bvid)

def get_wbi_mix(session):
    global wbi_cache
    now = time.time()
    if wbi_cache["mix_key"] and (now - wbi_cache["ts"]) < 3600:
        return wbi_cache["mix_key"]

    r = session.get("https://api.bilibili.com/x/web-interface/nav")
    data = r.json()["data"]
    if not data.get("wbi_img"):
        raise Exception("Failed to get WBI keys from Bilibili nav endpoint")
    wbi = data["wbi_img"]
    img_key = wbi["img_url"].split("/")[-1].split(".")[0]
    sub_key = wbi["sub_url"].split("/")[-1].split(".")[0]
    mix = "".join(a + b for a, b in zip(img_key, sub_key)) + img_key[32:] + sub_key[32:]

    wbi_cache = {"img_key": img_key, "sub_key": sub_key, "mix_key": mix, "ts": now}
    return mix


def get_video_info(bvid):
    """Get Bilibili video info by bvid."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.bilibili.com",
    })
    url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
    r = s.get(url, timeout=10)
    data = r.json()
    if data.get("code") != 0:
        raise Exception(data.get("message", "Video not found"))
    v = data["data"]
    return {
        "bvid": v["bvid"],
        "title": v.get("title", ""),
        "author": v["owner"]["name"] if "owner" in v else "",
        "play": v.get("stat", {}).get("view", 0),
        "duration": f"{v.get('duration', 0) // 60}:{v.get('duration', 0) % 60:02d}",
    }


def search_bilibili(keyword, page=1):
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.bilibili.com",
    })

    mix = get_wbi_mix(s)
    params = {"keyword": keyword, "search_type": "video", "page": str(page), "page_size": "10"}
    wts = int(time.time())
    query = urllib.parse.urlencode(sorted(params.items()))
    w_rid = hashlib.md5((query + mix).encode()).hexdigest()

    url = f"https://api.bilibili.com/x/web-interface/wbi/search/type?{query}&w_rid={w_rid}&wts={wts}"
    r = s.get(url, timeout=10)
    data = r.json()

    results = []
    for v in data.get("data", {}).get("result", []):
        title = re.sub(r"<[^>]+>", "", v.get("title", ""))
        results.append({
            "bvid": v["bvid"],
            "title": title,
            "author": v.get("author", ""),
            "play": v.get("play", 0),
            "duration": v.get("duration", ""),
        })
    total = data.get("data", {}).get("numResults", 0)
    return results, total


def download_and_convert(bvid, title_hint="", dl_dir=None):
    url = f"https://www.bilibili.com/video/{bvid}/"
    temp = os.path.join(DIR, f"__temp_{bvid}.m4a")
    out_dir = dl_dir if dl_dir and os.path.isdir(dl_dir) else DIR

    result = subprocess.run(
        [YT_DLP, "-f", "bestaudio", url, "-o", temp] + yt_dlp_base_args(),
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise Exception(f"yt-dlp failed: {result.stderr[:300]}")

    if not os.path.exists(temp):
        raise Exception("Download failed - no output file")

    if not title_hint:
        info = subprocess.run(
            [YT_DLP, "--print", "title", url] + yt_dlp_base_args(),
            capture_output=True, text=True, timeout=30,
        )
        title_hint = info.stdout.strip() or bvid

    safe_title = re.sub(r'[\\/:*?"<>|]', "", title_hint)[:80]
    mp3_path = os.path.join(out_dir, f"{safe_title}.mp3")

    result = subprocess.run(
        [FFMPEG, "-i", temp, "-codec:a", "libmp3lame", "-q:a", "2", mp3_path, "-y"],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise Exception(f"ffmpeg failed: {result.stderr[:300]}")

    if os.path.exists(temp):
        os.remove(temp)

    return os.path.basename(mp3_path)


class MusicServer(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIR, **kwargs)

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def api_search(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        q = params.get("q", [""])[0].strip()
        page = int(params.get("page", ["1"])[0])
        if not q:
            self.send_json({"error": "Missing query"})
            return

        try:
            results, total = search_bilibili(q, page)
            cache = load_cache()
            for r in results:
                r["cached"] = r["bvid"] in cache
            self.send_json({"results": results, "total": total, "page": page})
        except Exception as e:
            log(f"SEARCH ERROR: {e}")
            self.send_json({"error": str(e)})

    def api_info(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        bvid = params.get("bvid", [""])[0].strip()
        if not bvid:
            self.send_json({"error": "Missing bvid"})
            return
        try:
            info = get_video_info(bvid)
            cache = load_cache()
            info["cached"] = bvid in cache
            self.send_json({"results": [info]})
        except Exception as e:
            log(f"INFO ERROR: {e}")
            self.send_json({"error": str(e)})

    def api_download(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len)
        data = json.loads(body)
        bvid = data.get("bvid", "").strip()
        title = data.get("title", "").strip()

        if not bvid:
            self.send_json({"error": "Missing bvid"})
            return

        try:
            cached = find_cached(bvid)
            if cached:
                song, path = cached
                log(f"Cache hit for {bvid}: {song['file']}")
                self.send_json({"ok": True, "cached": True, "song": song})
                return

            settings = load_settings()
            dl_dir = settings.get("dl_path", "")
            out_dir = dl_dir if dl_dir and os.path.isdir(dl_dir) else DIR
            log(f"Downloading {bvid} -> {out_dir} ...")
            filename = download_and_convert(bvid, title, dl_dir)
            full = os.path.join(out_dir, filename)
            size = os.path.getsize(full) if os.path.isfile(full) else 0
            is_custom = os.path.abspath(out_dir) != os.path.abspath(DIR)
            src = "/api/file?path=" + urllib.parse.quote(full, safe="") if is_custom else "/api/file?file=" + urllib.parse.quote(filename, safe="")
            song = {
                "name": filename.replace(".mp3", ""),
                "file": filename,
                "size": size,
                "src": src,
            }
            cache = load_cache()
            cache[bvid] = {"file": filename, "title": title, "time": int(time.time())}
            save_cache(cache)
            log(f"Download done: {filename}")
            self.send_json({"ok": True, "cached": False, "song": song})
        except Exception as e:
            log(f"DOWNLOAD ERROR: {e}")
            self.send_json({"error": str(e)})

    def api_songs(self):
        songs = []
        settings = load_settings()
        dl_path = settings.get("dl_path", "").strip()
        dirs = [(DIR, False)]
        if dl_path and os.path.isdir(dl_path) and os.path.abspath(dl_path) != os.path.abspath(DIR):
            dirs.append((dl_path, True))
        try:
            for base, is_custom in dirs:
                try:
                    files = sorted(os.listdir(base), key=lambda x: os.path.getmtime(os.path.join(base, x)), reverse=True)
                except Exception:
                    continue
                for f in files:
                    if f.endswith(".mp3") and not f.startswith("__temp"):
                        full = os.path.join(base, f)
                        entry = {
                            "name": f.replace(".mp3", ""),
                            "file": f,
                            "size": os.path.getsize(full),
                        }
                        if is_custom:
                            entry["src"] = "/api/file?path=" + urllib.parse.quote(full, safe="")
                        else:
                            entry["src"] = "/api/file?file=" + urllib.parse.quote(f, safe="")
                        songs.append(entry)
        except Exception as e:
            log(f"SONGS ERROR: {e}")
        self.send_json({"songs": songs})

    def send_json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_file(self, path):
        if not os.path.isfile(path):
            self.send_error(404)
            return

        file_size = os.path.getsize(path)
        range_header = self.headers.get("Range")

        if range_header:
            m = re.match(r"bytes=(\d+)-(\d*)", range_header)
            if m:
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else file_size - 1
                end = min(end, file_size - 1)
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                self.send_header("Content-Length", str(length))
            else:
                self.send_response(416)
                self.end_headers()
                return
        else:
            start = 0
            length = file_size
            self.send_response(200)
            self.send_header("Content-Length", str(file_size))

        ctype, _ = mimetypes.guess_type(path)
        if ctype:
            self.send_header("Content-Type", ctype)
        self.end_headers()

        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def api_delete(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        f = params.get("file", [""])[0]
        path = params.get("path", [""])[0]
        if path:
            target = os.path.abspath(path)
            if ".." in path or not target.endswith(".mp3"):
                self.send_json({"error": "Invalid file"})
                return
        elif f:
            if ".." in f or "/" in f or "\\" in f:
                self.send_json({"error": "Invalid file"})
                return
            target = os.path.join(DIR, f)
        else:
            self.send_json({"error": "Missing file"})
            return
        if os.path.isfile(target) and target.endswith(".mp3"):
            os.remove(target)
            self.send_json({"ok": True})
        else:
            self.send_json({"error": "File not found"})

    def api_settings_get(self):
        self.send_json(load_settings())

    def api_settings_post(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len)
        data = json.loads(body)
        dl_path = data.get("dl_path", "").strip()
        save_settings({"dl_path": dl_path})
        path_exists = os.path.isdir(dl_path) if dl_path else True
        self.send_json({"ok": True, "path_exists": path_exists})

    def api_file(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        path = params.get("path", [""])[0]
        file = params.get("file", [""])[0]
        if path and ".." not in path:
            path = os.path.abspath(path)
        elif file:
            if ".." in file or "/" in file or "\\" in file:
                self.send_error(404)
                return
            path = os.path.join(DIR, file)
        else:
            self.send_error(404)
            return
        if not os.path.isfile(path) or not path.endswith(".mp3"):
            self.send_error(404)
            return
        return self.serve_file(path)

    def api_app(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in ["serve.py", "index.html"]:
                fp = os.path.join(DIR, f)
                if os.path.isfile(fp):
                    zf.write(fp, f)
            zf.writestr("README.txt", (
                "========================================\n"
                "    B站视频转音频工具 — 本机部署教程\n"
                "========================================\n\n"
                "【环境要求】\n"
                "  - Python 3.8+\n"
                "  - ffmpeg（音频转码）\n"
                "  - yt-dlp（视频下载）\n\n"
                "【安装步骤】\n"
                "  1. 安装 Python 依赖:\n"
                "     pip install yt-dlp requests\n\n"
                "  2. 安装 ffmpeg:\n"
                "     Windows: winget install Gyan.FFmpeg\n"
                "             或 https://ffmpeg.org/download.html\n"
                "     macOS:   brew install ffmpeg\n"
                "     Linux:   sudo apt install ffmpeg\n\n"
                "【启动服务】\n"
                "  在解压目录打开终端，运行:\n"
                "    python serve.py\n\n"
                "  看到 \"Server ready: http://0.0.0.0:8888\" 即成功。\n\n"
                "【使用】\n"
                "  浏览器打开 http://localhost:8888\n"
                "  - 搜索框输入关键词 / BV号 / B站链接\n"
                "  - 点 + 下载音频到本机\n"
                "  - 点 ▶ 在线播放\n"
                "  - 播放过的歌曲自动缓存，再次播放秒开\n\n"
                "【修改端口】\n"
                "  编辑 serve.py，改 PORT = 8888 为其他端口。\n"
                "  外网访问需放行对应端口（防火墙/安全组）。\n\n"
                "【设置下载路径】\n"
                "  服务器默认保存在 serve.py 同目录。\n"
                "  MCP 工具可调用 /api/settings 修改路径。\n"
            ))
        buf.seek(0)
        body = buf.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", 'attachment; filename="music-server.zip"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def api_stream(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        bvid = params.get("bvid", [""])[0]
        if not bvid:
            self.send_json({"error": "Missing bvid"})
            return

        # If request comes from a browser, return a player page
        accept = self.headers.get("Accept", "")
        if "text/html" in accept:
            return self.stream_player_page(bvid)

        # Serve from cache if available
        cached = find_cached(bvid)
        if cached:
            song, full_path = cached
            log(f"Stream cache hit: {bvid} -> {song['file']}")
            return self.serve_file(full_path)

        # Not cached: start background download, proxy from CDN for now
        threading.Thread(target=cache_in_background, args=(bvid,), daemon=True).start()

        try:
            log(f"Streaming {bvid} from CDN...")
            result = subprocess.run(
                [YT_DLP, "-f", "bestaudio", "-g", f"https://www.bilibili.com/video/{bvid}/"] + yt_dlp_base_args(),
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                raise Exception(f"yt-dlp: {result.stderr[:200]}")
            url = result.stdout.strip()
            if not url:
                raise Exception("yt-dlp returned empty URL")

            range_header = self.headers.get("Range")
            req_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Referer": "https://www.bilibili.com",
            }
            if range_header:
                req_headers["Range"] = range_header

            log(f"Proxying stream from CDN...")
            r = requests.get(url, stream=True, timeout=(15, 120), headers=req_headers)

            if range_header and r.status_code == 206:
                self.send_response(206)
                self.send_header("Content-Type", r.headers.get("Content-Type", "audio/mp4"))
                self.send_header("Accept-Ranges", "bytes")
                if "Content-Range" in r.headers:
                    self.send_header("Content-Range", r.headers["Content-Range"])
                if "Content-Length" in r.headers:
                    self.send_header("Content-Length", r.headers["Content-Length"])
                self.end_headers()
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        self.wfile.write(chunk)
            elif r.status_code in (200, 206):
                self.send_response(200)
                self.send_header("Content-Type", r.headers.get("Content-Type", "audio/mp4"))
                self.send_header("Accept-Ranges", "bytes")
                if "Content-Length" in r.headers:
                    self.send_header("Content-Length", r.headers["Content-Length"])
                self.end_headers()
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        self.wfile.write(chunk)
            else:
                raise Exception(f"CDN returned HTTP {r.status_code}")

            log(f"Stream finished for {bvid}")
        except Exception as e:
            log(f"STREAM ERROR: {e}")
            self.send_json({"error": str(e)})

    def stream_player_page(self, bvid):
        """Return a minimal HTML page with an audio player for browser playback."""
        stream_url = f"/api/stream?bvid={bvid}"
        html = f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>正在播放</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    background: #0f172a;
    display: flex;
    justify-content: center;
    align-items: center;
    min-height: 100vh;
    font-family: -apple-system, sans-serif;
  }}
  .player {{
    text-align: center;
    padding: 40px;
    background: #1e293b;
    border-radius: 16px;
    box-shadow: 0 20px 60px rgba(0,0,0,0.5);
  }}
  .label {{
    color: #94a3b8;
    font-size: 13px;
    margin-bottom: 20px;
    letter-spacing: 1px;
  }}
  audio {{
    width: 400px;
    outline: none;
  }}
  audio::-webkit-media-controls-panel {{ background: #334155; }}
  audio::-webkit-media-controls-current-time-display,
  audio::-webkit-media-controls-time-remaining-display {{ color: #e2e8f0; }}
</style>
</head>
<body>
<div class="player">
  <div class="label">点击播放</div>
  <audio src="{stream_url}" controls></audio>
</div>
</body>
</html>"""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        if path == "/":
            path = "/index.html"

        if path == "/api/search":
            return self.api_search()
        elif path == "/api/info":
            return self.api_info()
        elif path == "/api/songs":
            return self.api_songs()
        elif path == "/api/delete":
            return self.api_delete()
        elif path == "/api/stream":
            return self.api_stream()
        elif path == "/api/settings":
            return self.api_settings_get()
        elif path == "/api/file":
            return self.api_file()
        elif path == "/api/app":
            return self.api_app()

        file_path = self.translate_path(path)
        if os.path.isfile(file_path):
            return self.serve_file(file_path)
        return super().do_GET()

    def do_POST(self):
        if self.path == "/api/download":
            return self.api_download()
        elif self.path == "/api/settings":
            return self.api_settings_post()
        self.send_error(404)

    def log_message(self, fmt, *args):
        log(f"[{self.client_address[0]}] {fmt % args}")


if __name__ == "__main__":
    os.chdir(DIR)

    if not YT_DLP:
        log("ERROR: yt-dlp not found in PATH. Install: pip install yt-dlp")
        sys.exit(1)
    if not FFMPEG:
        log("ERROR: ffmpeg not found in PATH. Install: apt install ffmpeg")
        sys.exit(1)

    log(f"DIR = {DIR}")
    log(f"YT_DLP = {YT_DLP}")
    log(f"FFMPEG = {FFMPEG}")
    log(f"Server ready: http://0.0.0.0:{PORT}")

    with socketserver.TCPServer(("", PORT), MusicServer) as httpd:
        httpd.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        httpd.serve_forever()
