from __future__ import annotations

import ctypes
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
import uuid
from contextlib import closing
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse
from ctypes import wintypes

from library_db import (
    connect,
    ensure_schema,
    export_user_metadata,
    import_user_metadata_json,
    migrate_legacy_images,
    set_user_metadata,
    user_metadata_map,
)
from albums_db import load_albums as load_albums_db, save_albums as save_albums_db


PROJECT_DIR_NAME = ".media-library"
APP_DIR = Path(__file__).resolve().parent
if os.environ.get("MEDIA_LIBRARY_ROOT"):
    ROOT = Path(os.environ["MEDIA_LIBRARY_ROOT"]).resolve()
    PROJECT_DIR = ROOT / PROJECT_DIR_NAME
elif APP_DIR.parent.name == PROJECT_DIR_NAME:
    PROJECT_DIR = APP_DIR.parent
    ROOT = PROJECT_DIR.parent
else:
    ROOT = Path(os.getcwd()).resolve()
    PROJECT_DIR = ROOT / PROJECT_DIR_NAME
OUT = PROJECT_DIR / "data"
DB_PATH = OUT / "图片索引.db"
META = OUT / "图片元数据.json"
META_CSV = OUT / "图片元数据.csv"
ALBUMS = OUT / "相册.json"
ALBUMS_DB = OUT / "相册.db"
TILE_CACHE = OUT / "地图缓存"
BUILDER = APP_DIR / "build_image_library.py"
PORT = 18766
EXIFTOOL = shutil.which("exiftool") or "exiftool"
ORIGIN = re.compile(r"^https?://(?:127\.0\.0\.1|localhost)(?::\d+)?$", re.I)
LOCK = threading.RLock()
REBUILD_LOCK = threading.Lock()
RAW_EXTS = {".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".orf", ".rw2", ".pef", ".srw"}
IMAGE_EXTS = RAW_EXTS | {
    ".jpg", ".jpeg", ".png", ".webp", ".avif", ".heic", ".tif", ".tiff",
    ".bmp", ".gif",
}
CITY_PREFIXES = [
    "北京", "上海", "杭州", "成都", "广州", "深圳", "南京", "苏州",
    "重庆", "西安", "武汉", "长沙", "青岛", "厦门", "天津", "郑州",
    "济南", "昆明", "大理", "丽江", "拉萨", "乌鲁木齐", "哈尔滨",
    "沈阳", "大连", "秦皇岛", "嘉兴", "宁波", "福州", "三亚",
]


def set_hidden(path: Path) -> None:
    try:
        ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x2)
    except Exception:
        pass


def activate_library(root_path: str) -> Path:
    global APP_DIR, PROJECT_DIR, ROOT, OUT, DB_PATH, META, META_CSV, ALBUMS, ALBUMS_DB, TILE_CACHE, BUILDER
    target_root = Path(root_path).expanduser().resolve()
    if not target_root.is_dir():
        raise FileNotFoundError(f"图片库目录不存在：{target_root}")
    target_project = target_root / PROJECT_DIR_NAME
    target_app = target_project / "app"
    target_data = target_project / "data"
    target_data.mkdir(parents=True, exist_ok=True)
    target_app.mkdir(parents=True, exist_ok=True)
    if target_app.resolve() != APP_DIR.resolve():
        shutil.copytree(
            APP_DIR,
            target_app,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    set_hidden(target_project)
    ROOT = target_root
    PROJECT_DIR = target_project
    APP_DIR = target_app
    OUT = target_data
    DB_PATH = OUT / "图片索引.db"
    META = OUT / "图片元数据.json"
    META_CSV = OUT / "图片元数据.csv"
    ALBUMS = OUT / "相册.json"
    ALBUMS_DB = OUT / "相册.db"
    TILE_CACHE = OUT / "地图缓存"
    BUILDER = APP_DIR / "build_image_library.py"
    return target_root


def choose_library_root() -> str:
    import tkinter as tk
    from tkinter import filedialog

    picker = tk.Tk()
    try:
        picker.withdraw()
        picker.attributes("-topmost", True)
        return filedialog.askdirectory(
            parent=picker,
            initialdir=str(ROOT),
            title="选择图片库根目录",
            mustexist=True,
        )
    finally:
        picker.destroy()


def safe(base: Path, relative: str) -> Path:
    target = (base / unquote(relative)).resolve()
    target.relative_to(base.resolve())
    return target


def load_meta() -> dict:
    with closing(connect(DB_PATH)) as conn:
        ensure_schema(conn)
        migrate_legacy_images(conn)
        import_user_metadata_json(conn, META)
        return user_metadata_map(conn)


def persist_meta(conn) -> None:
    export_user_metadata(conn, META, META_CSV)
    conn.commit()


def load_albums() -> dict:
    return {"albums": load_albums_db(ALBUMS_DB, ALBUMS)}


def write_albums(payload: dict) -> dict:
    albums = payload.get("albums")
    if not isinstance(albums, list):
        raise ValueError("相册数据格式错误")
    data = {"albums": albums}
    save_albums_db(ALBUMS_DB, albums)
    ALBUMS.parent.mkdir(parents=True, exist_ok=True)
    ALBUMS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", wintypes.WORD),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def recycle_file(path: Path) -> None:
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    operation_function = shell32.SHFileOperationW
    operation_function.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
    operation_function.restype = ctypes.c_int
    source = ctypes.c_wchar_p(str(path) + "\0")
    operation = SHFILEOPSTRUCTW(
        None,
        3,
        source,
        None,
        0x0040 | 0x0010 | 0x0004 | 0x0400,
        False,
        None,
        None,
    )
    result = operation_function(ctypes.byref(operation))
    if result or operation.fAnyOperationsAborted or path.exists():
        raise RuntimeError(f"无法移动到回收站（错误码 {result}）")


def load_map_tile(z: int, x: int, y: int) -> bytes:
    if not 0 <= z <= 19 or not 0 <= x < 2**z or not 0 <= y < 2**z:
        raise ValueError("地图瓦片坐标无效")
    cached = TILE_CACHE / str(z) / str(x) / f"{y}.png"
    if cached.is_file():
        return cached.read_bytes()
    subdomain = (x + y) % 4 + 1
    url = (
        f"https://webrd0{subdomain}.is.autonavi.com/appmaptile"
        f"?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}"
    )
    request = urllib.request.Request(url, headers={"User-Agent": "LocalMediaLibrary/1.0"})
    with urllib.request.urlopen(request, timeout=15) as response:
        data = response.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024 or not (
        data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8")
    ):
        raise ValueError("地图服务返回了无效瓦片")
    cached.parent.mkdir(parents=True, exist_ok=True)
    temporary = cached.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(cached)
    finally:
        temporary.unlink(missing_ok=True)
    return data


def write_media_metadata(path: Path, data: dict) -> None:
    link = path.parent / f".meta_{uuid.uuid4().hex}{path.suffix}"
    try:
        os.link(path, link)
        args = [EXIFTOOL, "-overwrite_original_in_place", "-P"]
        if data.get("rating") is not None:
            args.append(f"-XMP:Rating={int(data['rating'])}")
        if data.get("note") is not None:
            args.append(f"-XMP:Description={data['note']}")
        if data.get("keywords") is not None:
            args.append(f"-XMP:Subject={data['keywords']}")
        if data.get("latitude") is not None and data.get("longitude") is not None:
            args.append(f"-GPSLatitude={float(data['latitude']):.8f}")
            args.append(f"-GPSLongitude={float(data['longitude']):.8f}")
        args.append(link.name)
        env = os.environ.copy()
        env.update({"LC_ALL": "C", "LC_CTYPE": "C", "LANG": "C"})
        result = subprocess.run(
            args, cwd=path.parent, capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env, check=False,
        )
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip())
    finally:
        link.unlink(missing_ok=True)


def write_subject_keyword(path: Path, keyword: str, add: bool = True) -> None:
    link = path.parent / f".meta_{uuid.uuid4().hex}{path.suffix}"
    args_file = path.parent / f".meta_{uuid.uuid4().hex}.args"
    try:
        os.link(path, link)
        operations = [f"-XMP:Subject-={keyword}"]
        if add:
            operations.append(f"-XMP:Subject+={keyword}")
        args_file.write_text(
            "\n".join([
                "-charset", "filename=utf8", "-overwrite_original_in_place", "-P",
                *operations, link.name,
            ]) + "\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env.update({"LC_ALL": "C", "LC_CTYPE": "C", "LANG": "C"})
        result = subprocess.run(
            [EXIFTOOL, "-charset", "filename=utf8", "-@", args_file.name],
            cwd=path.parent, capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env, check=False,
        )
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip())
    finally:
        link.unlink(missing_ok=True)
        args_file.unlink(missing_ok=True)


def write_featured_keyword(path: Path, featured: bool) -> None:
    write_subject_keyword(path, "精选", featured)


BOOTSTRAP_HTML = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>正在初始化图片库</title>
<style>body{margin:0;background:#111417;color:#eef1ed;font:14px/1.6 "Microsoft YaHei UI",sans-serif;display:grid;place-items:center;min-height:100vh}main{width:min(520px,90vw);border:1px solid #343e44;background:#181d21;padding:24px;border-radius:6px}h1{font-size:20px;margin:0 0 8px}p{color:#9ca8a8}pre{white-space:pre-wrap;color:#f0b1a9}</style>
<main><h1>正在初始化当前图片库</h1><p id="message">首次使用会扫描该目录下的所有照片并生成独立索引，请稍候。</p><pre id="error"></pre></main>
<script>
(async()=>{try{const response=await fetch('/api/rebuild',{method:'POST',headers:{'Content-Type':'application/json'},body:'{"mode":"incremental"}'});const result=await response.json();if(!response.ok)throw Error(result.message||'索引创建失败');document.getElementById('message').textContent=result.message;location.replace('/library/?v='+Date.now())}catch(error){document.getElementById('message').textContent='初始化失败';document.getElementById('error').textContent=error.message}})();
</script></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def cors(self):
        origin = self.headers.get("Origin", "")
        if origin and ORIGIN.match(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_file(self, target: Path, cache_control: str) -> None:
        if not target.is_file():
            return self.json(404, {"message": "文件不存在"})
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache_control)
        self.cors()
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path == "/api/status":
            return self.json(200, {"ok": True, "root": str(ROOT)})
        if path == "/api/library":
            return self.json(200, {"root": str(ROOT), "data": str(OUT)})
        if path == "/api/metadata":
            return self.json(200, load_meta())
        if path == "/api/albums":
            return self.json(200, load_albums())
        if path in {"/library", "/library/"}:
            page = OUT / "图片浏览.html"
            if page.is_file():
                return self.send_file(page, "no-store")
            body = BOOTSTRAP_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.cors()
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/library/"):
            relative = path[len("/library/"):]
            base = APP_DIR if relative.startswith("vendor/") else OUT
            target = safe(base, relative)
            cache = "public,max-age=86400" if relative.startswith("vendor/") else "no-store"
            return self.send_file(target, cache)
        tile_match = re.fullmatch(r"/api/map-tile/(\d+)/(\d+)/(\d+)", path)
        if tile_match:
            try:
                data = load_map_tile(*(int(value) for value in tile_match.groups()))
            except Exception:
                return self.json(404, {"message": "地图瓦片不可用"})
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public,max-age=31536000,immutable")
            self.cors()
            self.end_headers()
            self.wfile.write(data)
            return
        if path.startswith("/media/"):
            target = safe(ROOT, path[7:])
            if not target.is_file():
                return self.json(404, {"message": "文件不存在"})
            media = target
            if target.suffix.lower() in RAW_EXTS:
                media = OUT / "缩略图" / target.relative_to(ROOT).with_suffix(".jpg")
            data = media.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(media.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private,max-age=300")
            self.cors()
            self.end_headers()
            self.wfile.write(data)
            return
        self.json(404, {"message": "Not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        origin = self.headers.get("Origin", "")
        if origin and not ORIGIN.match(origin):
            return self.json(403, {"message": "Origin not allowed"})
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 4 * 1024 * 1024)
            payload = json.loads(self.rfile.read(length) or b"{}")
            if path == "/api/albums":
                with LOCK:
                    return self.json(200, {"message": "相册已保存", **write_albums(payload)})
            if path == "/api/shutdown":
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return self.json(200, {"message": "图片库后端已停止"})
            if path == "/api/library/select":
                root_path = str(payload.get("root") or "").strip()
                if not root_path:
                    root_path = choose_library_root()
                if not root_path:
                    return self.json(400, {"message": "未选择图片库"})
                if not REBUILD_LOCK.acquire(blocking=False):
                    return self.json(409, {"message": "图片库正在更新，请稍候"})
                try:
                    target_root = activate_library(root_path)
                    result = subprocess.run(
                        [sys.executable, str(BUILDER), "--incremental"],
                        cwd=APP_DIR,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=900,
                        check=False,
                    )
                    if result.returncode:
                        detail = (result.stderr or result.stdout).strip().splitlines()
                        raise RuntimeError(detail[-1] if detail else "图片库加载失败")
                    stats = {}
                    for line in reversed(result.stdout.splitlines()):
                        if line.startswith("RESULT "):
                            stats = json.loads(line[7:])
                            break
                    return self.json(200, {
                        "message": f"图片库已加载：{stats.get('total', '?')} 张",
                        "root": str(target_root),
                        "url": "http://127.0.0.1:18766/library/",
                        **stats,
                    })
                except subprocess.TimeoutExpired:
                    return self.json(504, {"message": "图片库扫描超时"})
                except Exception as exc:
                    return self.json(500, {"message": f"{type(exc).__name__}: {exc}"})
                finally:
                    REBUILD_LOCK.release()
            if path == "/api/geocode":
                city = str(payload.get("city", "")).strip()
                if not city:
                    raise ValueError("请输入城市或景点")
                expected_city = ""
                search_text = city
                for prefix in sorted(CITY_PREFIXES, key=len, reverse=True):
                    if city.startswith(prefix) and len(city) > len(prefix):
                        expected_city = prefix
                        search_text = city[len(prefix):]
                        break
                query = urllib.parse.urlencode({"limit": 20, "q": search_text})
                request = urllib.request.Request(
                    f"https://photon.komoot.io/api/?{query}",
                    headers={"User-Agent": "LocalMediaLibrary/1.0"},
                )
                with urllib.request.urlopen(request, timeout=15) as response:
                    results = json.loads(response.read().decode("utf-8"))
                features = [
                    item for item in results.get("features", [])
                    if item.get("properties", {}).get("country") == "中国"
                ]
                if expected_city:
                    features = [
                        item for item in features
                        if expected_city in "".join(
                            str(item.get("properties", {}).get(key, ""))
                            for key in ("city", "state", "district", "county")
                        )
                    ]
                if not features:
                    raise ValueError(f"没有找到地点：{city}")
                item = features[0]
                properties = item.get("properties", {})
                longitude, latitude = item["geometry"]["coordinates"][:2]
                display_name = " · ".join(
                    str(value)
                    for value in (
                        properties.get("name"),
                        properties.get("city"),
                        properties.get("state"),
                        properties.get("country"),
                    )
                    if value
                ) or city
                return self.json(
                    200,
                    {
                        "latitude": float(latitude),
                        "longitude": float(longitude),
                        "display_name": display_name,
                    },
                )
            if path == "/api/rebuild":
                mode = str(payload.get("mode", "incremental"))
                if mode not in {"incremental", "full"}:
                    return self.json(400, {"message": "未知的索引重建模式"})
                if not REBUILD_LOCK.acquire(blocking=False):
                    return self.json(409, {"message": "索引正在重建，请稍候"})
                try:
                    if not BUILDER.is_file():
                        return self.json(500, {"message": "索引构建器不存在"})
                    started = datetime.now()
                    command = [sys.executable, str(BUILDER)]
                    if mode == "incremental":
                        command.append("--incremental")
                    result = subprocess.run(
                        command,
                        cwd=OUT,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=900,
                        check=False,
                    )
                    if result.returncode:
                        detail = (result.stderr or result.stdout).strip().splitlines()
                        raise RuntimeError(detail[-1] if detail else "索引重建失败")
                    seconds = round((datetime.now() - started).total_seconds(), 1)
                    stats = {}
                    for line in reversed(result.stdout.splitlines()):
                        if line.startswith("RESULT "):
                            stats = json.loads(line[7:])
                            break
                    if mode == "incremental":
                        message = f"增量更新完成：更新 {stats.get('changed', '?')} 张，共 {stats.get('total', '?')} 张，用时 {seconds} 秒"
                    else:
                        message = f"全部重建完成：{stats.get('total', '?')} 张，用时 {seconds} 秒"
                    return self.json(200, {"message": message, **stats})
                except subprocess.TimeoutExpired:
                    return self.json(504, {"message": "索引重建超时"})
                except Exception as exc:
                    return self.json(500, {"message": f"{type(exc).__name__}: {exc}"})
                finally:
                    REBUILD_LOCK.release()
            if path == "/api/recycle":
                paths = payload.get("paths")
                if not isinstance(paths, list) or not paths:
                    return self.json(400, {"message": "没有选择照片"})
                recycled, failed = [], []
                with LOCK:
                    with closing(connect(DB_PATH)) as conn:
                        ensure_schema(conn)
                        for rel_path in paths:
                            try:
                                item = safe(ROOT, str(rel_path))
                                relative = item.relative_to(ROOT)
                                if (
                                    not item.is_file()
                                    or item.suffix.lower() not in IMAGE_EXTS
                                    or PROJECT_DIR.name in relative.parts
                                ):
                                    raise ValueError("不是可移动的媒体文件")
                                recycle_file(item)
                                recycled.append(str(rel_path))
                                conn.execute(
                                    "DELETE FROM media WHERE relative_path = ? AND kind = 'image'",
                                    (str(rel_path),),
                                )
                            except Exception as exc:
                                failed.append({"path": str(rel_path), "message": str(exc)})
                        if recycled:
                            persist_meta(conn)
                if not recycled:
                    return self.json(400, {"message": "没有文件被移动", "failed": failed})
                message = f"已移动 {len(recycled)} 张照片到系统回收站"
                if failed:
                    message += f"，{len(failed)} 张失败"
                return self.json(200, {"message": message, "recycled": recycled, "failed": failed})
            rel = str(payload.get("path", ""))
            if path in ("/api/gps", "/api/featured", "/api/keywords", "/api/ratings"):
                rel = str((payload.get("paths") or [""])[0])
            target = safe(ROOT, rel)
            if not target.is_file():
                raise FileNotFoundError(rel)
            if path == "/api/open":
                os.startfile(str(target))
                return self.json(200, {"message": "已用默认软件打开"})
            if path == "/api/explorer":
                subprocess.Popen(["explorer.exe", "/select,", str(target)])
                return self.json(200, {"message": "已在文件夹中显示"})
            if path == "/api/metadata":
                with LOCK:
                    with closing(connect(DB_PATH)) as conn:
                        ensure_schema(conn)
                        migrate_legacy_images(conn)
                        current = user_metadata_map(conn).get(rel, {})
                        values = {
                            key: payload[key]
                            for key in ("rating", "note", "keywords", "featured", "latitude", "longitude")
                            if key in payload
                        }
                        if any(key in payload for key in ("rating", "note", "keywords", "latitude", "longitude")):
                            write_media_metadata(target, payload)
                        if payload.get("featured") is not None:
                            write_featured_keyword(target, bool(payload["featured"]))
                        entry = set_user_metadata(
                            conn,
                            rel,
                            {
                                "rating": values.get("rating", current.get("rating", 0)),
                                "note": values.get("note", current.get("note", "")),
                                "keywords": values.get("keywords", current.get("keywords", "")),
                                "featured": values.get("featured", current.get("featured", False)),
                                "latitude": values.get("latitude", current.get("latitude")),
                                "longitude": values.get("longitude", current.get("longitude")),
                            },
                        )
                        persist_meta(conn)
                        entry["featured"] = bool(entry["featured"])
                return self.json(200, {"message": "图片元数据已保存", "metadata": entry})
            if path == "/api/gps":
                paths = payload.get("paths", [])
                latitude = float(payload["latitude"])
                longitude = float(payload["longitude"])
                if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
                    raise ValueError("GPS 坐标超出范围")
                with LOCK:
                    with closing(connect(DB_PATH)) as conn:
                        ensure_schema(conn)
                        for rel_path in paths:
                            item = safe(ROOT, str(rel_path))
                            if not item.is_file():
                                continue
                            write_media_metadata(item, {"latitude": latitude, "longitude": longitude})
                            set_user_metadata(
                                conn,
                                str(rel_path),
                                {"latitude": latitude, "longitude": longitude},
                            )
                        persist_meta(conn)
                return self.json(200, {"message": f"已设置 {len(paths)} 张图片的 GPS", "latitude": latitude, "longitude": longitude})
            if path == "/api/featured":
                paths = payload.get("paths", [])
                featured = bool(payload.get("featured", True))
                with LOCK:
                    with closing(connect(DB_PATH)) as conn:
                        ensure_schema(conn)
                        for rel_path in paths:
                            item = safe(ROOT, str(rel_path))
                            if not item.is_file():
                                continue
                            write_featured_keyword(item, featured)
                            set_user_metadata(conn, str(rel_path), {"featured": featured})
                        persist_meta(conn)
                return self.json(200, {"message": f"已{'添加' if featured else '取消'} {len(paths)} 张图片的精选", "featured": featured})
            if path == "/api/keywords":
                paths = payload.get("paths", [])
                tag = str(payload.get("tag", "")).strip()
                if not tag or len(tag) > 100:
                    raise ValueError("标签不能为空且不能超过 100 个字符")
                with LOCK:
                    with closing(connect(DB_PATH)) as conn:
                        ensure_schema(conn)
                        current = user_metadata_map(conn)
                        for rel_path in paths:
                            item = safe(ROOT, str(rel_path))
                            if not item.is_file():
                                continue
                            write_subject_keyword(item, tag, True)
                            tags = [
                                value.strip()
                                for value in str(current.get(str(rel_path), {}).get("keywords", "")).split(",")
                                if value.strip()
                            ]
                            if tag not in tags:
                                tags.append(tag)
                            set_user_metadata(conn, str(rel_path), {"keywords": ", ".join(tags)})
                        persist_meta(conn)
                return self.json(200, {"message": f"已为 {len(paths)} 张图片添加标签：{tag}", "tag": tag})
            if path == "/api/ratings":
                paths = payload.get("paths", [])
                rating = int(payload.get("rating", 0))
                if rating not in range(0, 6):
                    raise ValueError("评分必须在 0 到 5 之间")
                with LOCK:
                    with closing(connect(DB_PATH)) as conn:
                        ensure_schema(conn)
                        for rel_path in paths:
                            item = safe(ROOT, str(rel_path))
                            if not item.is_file():
                                continue
                            write_media_metadata(item, {"rating": rating})
                            set_user_metadata(conn, str(rel_path), {"rating": rating})
                        persist_meta(conn)
                return self.json(200, {"message": f"已为 {len(paths)} 张图片设置评分：{rating} 星", "rating": rating})
            self.json(404, {"message": "Unknown endpoint"})
        except Exception as exc:
            self.json(400, {"message": f"{type(exc).__name__}: {exc}"})


if __name__ == "__main__":
    print(f"图片服务: http://127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
