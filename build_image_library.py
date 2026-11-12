from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

from library_db import (
    connect,
    delete_missing,
    ensure_schema,
    export_user_metadata,
    fetch_media,
    import_user_metadata_json,
    migrate_legacy_images,
    upsert_media,
    user_metadata_map,
)


PROJECT_DIR_NAME = ".media-library"
APP_DIR = Path(__file__).resolve().parent
if APP_DIR.parent.name == PROJECT_DIR_NAME:
    PROJECT_DIR = APP_DIR.parent
    ROOT = PROJECT_DIR.parent
else:
    ROOT = Path(os.environ.get("MEDIA_LIBRARY_ROOT", os.getcwd())).resolve()
    PROJECT_DIR = ROOT / PROJECT_DIR_NAME
OUT = PROJECT_DIR / "data"
THUMBS = OUT / "缩略图"
DB_PATH = OUT / "图片索引.db"
METADATA_PATH = OUT / "图片元数据.json"
HTML_PATH = OUT / "图片浏览.html"
EXIFTOOL = shutil.which("exiftool") or "exiftool"
IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".webp", ".avif", ".heic", ".tif", ".tiff",
    ".bmp", ".gif", ".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf",
    ".orf", ".rw2", ".pef", ".srw",
}
RAW_EXTS = {".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".orf", ".rw2", ".pef", ".srw"}


def rational(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            return value[0] / value[1]
        except Exception:
            return None


def gps_from_exif(exif):
    try:
        gps = exif.get(34853)
        if not gps:
            return None, None
        lat = gps.get(2)
        lon = gps.get(4)
        if not lat or not lon:
            return None, None
        lat_value = rational(lat[0]) + rational(lat[1]) / 60 + rational(lat[2]) / 3600
        lon_value = rational(lon[0]) + rational(lon[1]) / 60 + rational(lon[2]) / 3600
        if gps.get(1) == "S":
            lat_value = -lat_value
        if gps.get(3) == "W":
            lon_value = -lon_value
        return round(lat_value, 7), round(lon_value, 7)
    except Exception:
        return None, None


def city_for_coordinate(latitude: float, longitude: float) -> str:
    cities = [
        ("北京", 39.9042, 116.4074, 0.75),
        ("杭州", 30.2741, 120.1551, 0.65),
        ("上海", 31.2304, 121.4737, 0.65),
        ("成都", 30.5728, 104.0668, 0.65),
        ("秦皇岛", 39.9354, 119.6005, 0.55),
        ("嘉兴", 30.7461, 120.7555, 0.45),
        ("南京", 32.0603, 118.7969, 0.50),
        ("苏州", 31.2989, 120.5853, 0.45),
        ("西安", 34.3416, 108.9398, 0.60),
        ("长沙", 28.2282, 112.9388, 0.60),
        ("重庆", 29.5630, 106.5516, 0.70),
    ]
    best = min(
        cities,
        key=lambda item: (latitude - item[1]) ** 2 + (longitude - item[2]) ** 2,
    )
    distance = ((latitude - best[1]) ** 2 + (longitude - best[2]) ** 2) ** 0.5
    return best[0] if distance <= best[3] else "其他"


def extract_raw_preview(path: Path) -> Path | None:
    temp = path.parent / f".preview_{uuid.uuid4().hex}.jpg"
    link = path.parent / f".raw_{uuid.uuid4().hex}{path.suffix}"
    try:
        os.link(path, link)
        env = os.environ.copy()
        env.update({"LC_ALL": "C", "LC_CTYPE": "C", "LANG": "C"})
        for tag in ("PreviewImage", "JpgFromRaw", "ThumbnailImage"):
            result = subprocess.run(
                [EXIFTOOL, "-b", f"-{tag}", link.name],
                cwd=path.parent,
                capture_output=True,
                env=env,
                check=False,
            )
            if result.returncode == 0 and len(result.stdout) > 1000:
                temp.write_bytes(result.stdout)
                return temp
    finally:
        link.unlink(missing_ok=True)
    return None


def scan_one(path: Path) -> dict:
    stat = path.stat()
    rel = path.relative_to(ROOT)
    thumb_rel = rel.with_suffix(".jpg")
    thumb = THUMBS / thumb_rel
    thumb.parent.mkdir(parents=True, exist_ok=True)
    width = height = None
    make = model = ""
    date = ""
    time_label = ""
    lat = lon = None
    error = ""
    preview = None
    try:
        if path.suffix.lower() in RAW_EXTS:
            preview = extract_raw_preview(path)
            source_for_thumb = preview or path
        else:
            source_for_thumb = path
        with Image.open(source_for_thumb) as image:
            image = ImageOps.exif_transpose(image)
            width, height = image.size
            exif = image.getexif()
            make = str(exif.get(271, "") or "")
            model = str(exif.get(272, "") or "")
            date_raw = str(exif.get(36867, "") or exif.get(306, "") or "")
            lat, lon = gps_from_exif(exif)
            image.thumbnail((520, 520), Image.Resampling.LANCZOS)
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            image.save(thumb, "JPEG", quality=84, optimize=True)
        if date_raw:
            match = re.search(r"(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})", date_raw)
            if match:
                date = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
                time_label = f"{match.group(4)}:{match.group(5)}:{match.group(6)}"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if preview:
            preview.unlink(missing_ok=True)
    if not date:
        match = re.search(r"(20\d{2})[-_](\d{1,2})[-_](\d{1,2})", path.name)
        if match:
            date = f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    if not date:
        date = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d")
    return {
        "relative_path": rel.as_posix(),
        "name": path.name,
        "extension": path.suffix.lower(),
        "size_bytes": stat.st_size,
        "modified_ns": stat.st_mtime_ns,
        "date": date,
        "time": time_label,
        "width": width,
        "height": height,
        "make": make,
        "model": model,
        "lens": "",
        "tags": "",
        "rating": 0,
        "latitude": lat,
        "longitude": lon,
        "thumb": thumb_rel.as_posix() if thumb.exists() else "",
        "error": error,
    }


def load_metadata() -> dict:
    try:
        with closing(connect(DB_PATH)) as conn:
            ensure_schema(conn)
            migrate_legacy_images(conn)
            import_user_metadata_json(conn, METADATA_PATH)
            return user_metadata_map(conn)
    except Exception:
        return {}


def exiftool_rows(paths: list[Path] | None = None) -> dict[str, dict]:
    extensions = [
        ".jpg", ".jpeg", ".png", ".webp", ".heic", ".tif", ".tiff",
        ".bmp", ".gif", ".cr2", ".cr3", ".nef", ".arw", ".dng",
        ".raf", ".orf", ".rw2", ".pef", ".srw",
    ]
    tags = [
        "-GPSLatitude", "-GPSLongitude", "-GPSLatitudeRef", "-GPSLongitudeRef",
        "-DateTimeOriginal", "-Make", "-Model", "-ImageWidth", "-ImageHeight",
        "-LensModel", "-LensID", "-Keywords", "-HierarchicalSubject",
        "-XMP:Rating", "-XMP:Description", "-XMP:Subject",
    ]
    env = os.environ.copy()
    env.update({"LC_ALL": "C", "LC_CTYPE": "C", "LANG": "C"})
    items = []
    if paths is None:
        command = [EXIFTOOL, "-r", "-i", PROJECT_DIR.name, "-charset", "filename=utf8", "-json", "-n"]
        for ext in extensions:
            command += ["-ext", ext.lstrip(".")]
        command += [*tags, "."]
        result = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env, check=False,
        )
        try:
            items = json.loads(result.stdout)
        except Exception:
            return {}
    else:
        for start in range(0, len(paths), 100):
            batch = paths[start:start + 100]
            command = [
                EXIFTOOL, "-charset", "filename=utf8", "-json", "-n", *tags,
                *[path.relative_to(ROOT).as_posix() for path in batch],
            ]
            result = subprocess.run(
                command, cwd=ROOT, capture_output=True, text=True,
                encoding="utf-8", errors="replace", env=env, check=False,
            )
            try:
                items.extend(json.loads(result.stdout))
            except Exception:
                continue
    mapped = {}
    for item in items:
        source = item.get("FilePath") or item.get("SourceFile")
        if not source:
            continue
        try:
            source_path = Path(source)
            if not source_path.is_absolute():
                source_path = ROOT / source_path
            rel = source_path.resolve().relative_to(ROOT.resolve()).as_posix()
        except Exception:
            continue
        lat = rational(item.get("GPSLatitude"))
        lon = rational(item.get("GPSLongitude"))
        if lat is not None and item.get("GPSLatitudeRef") == "S":
            lat = -lat
        if lon is not None and item.get("GPSLongitudeRef") == "W":
            lon = -lon
        def joined(value):
            if isinstance(value, list):
                return ", ".join(str(item) for item in value)
            return str(value or "")

        tags = ", ".join(
            part for part in (
                joined(item.get("Subject")),
                joined(item.get("Keywords")),
                joined(item.get("HierarchicalSubject")),
            ) if part
        )
        mapped[rel] = {
            "latitude": round(lat, 7) if lat is not None else None,
            "longitude": round(lon, 7) if lon is not None else None,
            "exif_date": item.get("DateTimeOriginal", ""),
            "make": item.get("Make", ""),
            "model": item.get("Model", ""),
            "lens": item.get("LensModel") or item.get("LensID") or "",
            "tags": tags,
            "width": item.get("ImageWidth"),
            "height": item.get("ImageHeight"),
            "exif_rating": item.get("Rating"),
            "exif_note": item.get("Description", ""),
            "exif_keywords": item.get("Subject", ""),
        }
    return mapped


def load_index_rows() -> dict[str, dict]:
    if not DB_PATH.exists():
        return {}
    try:
        with closing(connect(DB_PATH)) as conn:
            ensure_schema(conn)
            migrate_legacy_images(conn)
            return {
                row["relative_path"]: row
                for row in fetch_media(conn, "image")
            }
    except Exception:
        return {}


def build_db(rows: list[dict]) -> None:
    with closing(connect(DB_PATH)) as conn:
        ensure_schema(conn)
        for row in rows:
            upsert_media(conn, "image", row)
        delete_missing(conn, "image", (row["relative_path"] for row in rows))
        import_user_metadata_json(conn, METADATA_PATH)
        export_user_metadata(conn, METADATA_PATH, OUT / "图片元数据.csv")
        conn.commit()


def build_html(rows: list[dict], metadata: dict) -> None:
    for row in rows:
        meta = metadata.get(row["relative_path"], {})
        row["rating"] = int(meta.get("rating", row.get("rating", 0)) or 0)
        tags = [x.strip() for x in str(row.get("tags") or "").split(",") if x.strip()]
        tags.extend(
            x.strip()
            for x in str(meta.get("keywords", "")).split(",")
            if x.strip() and x.strip() not in tags
        )
        row["featured"] = bool(meta.get("featured", False)) or "精选" in tags
        if row["featured"] and "精选" not in tags:
            tags.append("精选")
        row["tags"] = ",".join(tags)
        row["note"] = str(meta.get("note", ""))
        row["keywords"] = str(meta.get("keywords", ""))
        if meta.get("latitude") is not None:
            row["latitude"] = meta["latitude"]
        if meta.get("longitude") is not None:
            row["longitude"] = meta["longitude"]
        row["city"] = (
            city_for_coordinate(row["latitude"], row["longitude"])
            if row.get("latitude") is not None and row.get("longitude") is not None
            else "无 GPS"
        )
    data = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>图片整理索引</title><link rel="stylesheet" href="http://127.0.0.1:18766/library/vendor/leaflet/leaflet.css"><link rel="stylesheet" href="http://127.0.0.1:18766/library/vendor/leaflet/MarkerCluster.css"><script src="http://127.0.0.1:18766/library/vendor/image-map-region.js?v=20261023-18"></script><style>
:root{--bg:#111417;--panel:#181d21;--panel2:#20262b;--line:#343e44;--text:#eef1ed;--muted:#9ca8a8;--accent:#e1b866;--teal:#67b7ab}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 "Microsoft YaHei UI","Segoe UI",sans-serif}button,input,select,textarea{font:inherit}
header{padding:22px clamp(16px,4vw,48px) 16px;border-bottom:1px solid var(--line)}h1{margin:0;font-size:24px}.sub{color:var(--muted);margin-top:3px}
.stats{display:grid;grid-template-columns:repeat(5,1fr);gap:1px;background:var(--line);border:1px solid var(--line);margin-top:16px}.stat{background:var(--panel);padding:11px 13px}.stat strong{display:block;font-size:20px}.stat span{color:var(--muted);font-size:12px}
.toolbar{padding:14px clamp(16px,4vw,48px);display:flex;gap:8px;flex-wrap:wrap;border-bottom:1px solid var(--line)}.toolbar input{flex:1;min-width:240px}.toolbar input,.toolbar select{height:38px;border:1px solid var(--line);border-radius:5px;background:var(--panel);color:var(--text);padding:0 10px}
.preview.zoom-mode img{width:auto!important;height:auto!important;max-width:100vw!important;max-height:100vh!important}.preview.zoom-mode.is-zoomed img{width:auto!important;height:calc(100vh * var(--zoom,1))!important;max-width:none!important;max-height:none!important}body.mapview{overflow:hidden}body.mapview .mapwrap{height:calc(100vh - 455px);min-height:420px}
.quick-preview{position:fixed;inset:0;z-index:1000;display:none;align-items:center;justify-content:center;background:#000}.quick-preview.show{display:flex}.quick-preview img{display:block;width:auto;height:auto;max-width:100vw;max-height:100vh;object-fit:contain}
.card-tags{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}.tag-chip{display:inline-block;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;border:1px solid #3e4b4f;border-radius:4px;background:#20282b;color:#9eb5b0;padding:2px 6px;font-size:10px}
.tagbar{display:flex;flex-wrap:wrap;gap:6px;padding:10px clamp(16px,4vw,48px);border-bottom:1px solid var(--line)}.tag{border:1px solid var(--line);background:var(--panel);color:var(--muted);border-radius:4px;padding:4px 8px;white-space:nowrap;cursor:pointer}.tag.active{color:var(--accent);border-color:#6c5a33}
.tagbar.collapsed{height:42px;overflow:hidden}.tag-toggle{margin:0 clamp(16px,4vw,48px) 8px;border:1px solid var(--line);background:var(--panel);color:var(--accent);border-radius:4px;padding:4px 9px;cursor:pointer}
.tabs{display:flex;gap:4px;padding:0 clamp(16px,4vw,48px);border-bottom:1px solid var(--line)}.tab{border:0;border-bottom:2px solid transparent;background:transparent;color:var(--muted);padding:11px 16px;cursor:pointer}.tab.active{color:var(--text);border-bottom-color:var(--accent)}
.bar{width:100%;padding:12px clamp(16px,4vw,48px);display:flex;justify-content:flex-start;align-items:center;flex-wrap:wrap;gap:8px;color:var(--muted)}.bar button{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:4px;padding:6px 10px;cursor:pointer}.bar button.active{border-color:var(--accent);color:var(--accent)}.bar button:disabled{opacity:.55;cursor:wait}.size-control{display:flex;align-items:center;gap:6px;white-space:nowrap}.size-control input{width:120px;padding:0;accent-color:var(--accent)}.size-control output{min-width:30px;text-align:right;color:var(--text)}
body.controls-fixed{height:100vh;overflow:hidden;display:flex;flex-direction:column}body.controls-fixed #topControls{flex:0 0 auto;max-height:70vh;overflow:auto;overscroll-behavior:contain}body.controls-fixed #gallery{flex:1 1 auto;min-height:0;overflow-y:auto;overscroll-behavior:contain}
.album-strip{padding:8px clamp(16px,4vw,48px);border-bottom:1px solid var(--line);background:#151a1d}.album-strip-head{display:flex;align-items:center;gap:8px;min-width:0}.album-strip-head strong{font-size:14px;flex:0 0 auto}.album-strip-head button,.album-actions button{border:1px solid var(--line);background:var(--panel2);color:var(--text);border-radius:4px;padding:4px 8px;cursor:pointer}.album-actions{display:flex;gap:5px;flex:0 0 auto}.album-collapse{flex:0 0 auto}.album-list{display:flex;flex:1 1 auto;gap:6px;min-width:0;overflow-x:auto;scrollbar-width:thin}.album-item{display:flex;align-items:center;justify-content:space-between;gap:8px;min-width:max-content;border:1px solid var(--line);border-radius:4px;background:var(--panel);color:var(--muted);padding:6px 9px;text-align:left;cursor:pointer}.album-item:hover{color:var(--text);border-color:#53605f}.album-item.active{background:#29251b;color:var(--accent);border-color:#6c5a33}.album-item small{white-space:nowrap}body.albums-collapsed .album-actions{display:none}
.gallery{width:100%;max-width:none;margin:0;padding:0 clamp(16px,4vw,48px) 48px;display:grid;grid-template-columns:repeat(auto-fill,var(--card-min,210px));justify-content:start;gap:14px}.gallery.timeline,.gallery.mapmode,.gallery:has(.mapwrap){display:block}
.gallery.image-only{display:block;padding-right:112px}.masonry-stage{position:relative;width:100%}.gallery.image-only .card{position:absolute;border:0;border-radius:3px;background:#0b0d0e;box-shadow:0 0 0 1px #ffffff0d}.gallery.image-only .thumb{height:100%;aspect-ratio:auto;background:#0b0d0e}.gallery.image-only .thumb img{height:100%;object-fit:contain}.gallery.image-only .body,.gallery.image-only .sel,.gallery.image-only .badge{display:none}
.date-rail{position:fixed;z-index:20;right:10px;top:var(--rail-top,420px);bottom:24px;width:92px;display:flex;flex-direction:column;gap:2px;overflow-y:auto;overscroll-behavior:contain;padding:8px 5px;background:#101417e8;border:1px solid var(--line);border-radius:5px}.date-point{display:flex;align-items:center;justify-content:flex-end;gap:6px;border:0;background:transparent;color:var(--muted);padding:2px 1px;font-size:10px;white-space:nowrap;cursor:pointer}.date-point i{width:5px;height:5px;flex:0 0 auto;border-radius:50%;background:#60726f}.date-point:hover,.date-point.active{color:var(--accent)}.date-point:hover i,.date-point.active i{background:var(--accent)}
.card{position:relative;background:var(--panel);border:1px solid var(--line);border-radius:6px;overflow:hidden;cursor:pointer}.card:hover{border-color:var(--teal)}.thumb{position:relative;aspect-ratio:4/3;background:#090b0d}.thumb img{width:100%;height:100%;object-fit:contain;user-select:none;-webkit-user-drag:none}.portrait .thumb img{object-fit:cover}.sel{position:absolute;z-index:3;left:7px;top:7px;background:#111d;border:1px solid #fff4;border-radius:4px;padding:2px}.sel input{accent-color:var(--teal)}
.batchbar{display:none;gap:8px;align-items:center;flex-wrap:wrap;padding:9px clamp(16px,4vw,48px);background:#14201d;border-bottom:1px solid #355149;color:#b8ddd4}.batchbar.show{display:flex}.batchbar input,.batchbar select{height:32px;width:150px;background:#121619;color:var(--text);border:1px solid var(--line);padding:0 8px}.batchbar button{height:32px;background:var(--panel);color:var(--text);border:1px solid var(--line);padding:0 10px;cursor:pointer}.batchbar button.danger{border-color:#8c4b46;color:#f0b1a9}.batchbar button:disabled{opacity:.55;cursor:wait}
.badge{position:absolute;top:7px;right:7px;background:#111d;border:1px solid #fff3;border-radius:4px;padding:2px 6px;color:var(--accent);font-size:11px}.body{padding:9px 10px}.name{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:600}.meta{display:flex;flex-wrap:wrap;gap:2px 8px;color:var(--muted);font-size:11px;margin-top:4px}.meta span{white-space:nowrap}
.year{border-bottom:1px solid var(--line);margin-bottom:18px}.year summary{position:sticky;top:0;z-index:2;display:flex;justify-content:space-between;padding:10px 13px;background:#181d21ee;border:1px solid var(--line);border-radius:5px;margin-bottom:12px;cursor:pointer}.year small{color:var(--muted)}.day{display:grid;grid-template-columns:105px 1fr;gap:14px;padding-bottom:18px}.axis{text-align:right;color:var(--muted);border-right:1px solid var(--line);padding-right:18px}.axis strong{display:block;color:var(--text);font-size:15px}.daygrid{display:grid;grid-template-columns:repeat(auto-fill,var(--card-min,190px));justify-content:start;gap:9px}
.mapwrap{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(280px,.8fr);gap:14px;width:100%;min-width:0;height:min(72vh,760px);min-height:520px;overflow:hidden}#map{width:100%;height:100%;min-height:0;border:1px solid var(--line);border-radius:6px;background-color:#15191c;background-image:linear-gradient(rgba(103,183,171,.07) 1px,transparent 1px),linear-gradient(90deg,rgba(103,183,171,.07) 1px,transparent 1px);background-size:40px 40px}.mapside{height:100%;min-height:0;overflow-y:auto;overscroll-behavior:contain;border:1px solid var(--line);border-radius:6px;padding:12px;background:var(--panel)}.mapgallery{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.mapitem{border:1px solid var(--line);background:var(--panel2);color:var(--text);padding:0;overflow:hidden;border-radius:4px;cursor:pointer;text-align:left}.mapitem img{width:100%;aspect-ratio:1;object-fit:cover;display:block}.mapitem span{display:block;padding:5px;font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.thumbmarker{background:transparent;border:0}.thumbmarker img{width:54px;height:54px;object-fit:cover;border:2px solid var(--accent);border-radius:5px}
dialog{width:min(980px,calc(100vw - 28px));max-height:calc(100vh - 30px);background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:7px;padding:0}.head{display:flex;justify-content:space-between;padding:12px 15px;border-bottom:1px solid var(--line)}.head h2{font-size:16px;margin:0}.preview{background:#090b0d;text-align:center;max-height:62vh;overflow:auto}.preview img{max-width:100%;max-height:62vh;object-fit:contain;display:block;margin:auto}.preview.zoom-mode img{max-width:none;max-height:none;width:calc(100% * var(--zoom,1));cursor:grab;user-select:none}.preview.zoom-mode img:active{cursor:grabbing}.actions{padding:10px 15px;display:flex;gap:8px;flex-wrap:wrap}.actions button,.save{border:1px solid var(--line);background:var(--panel2);color:var(--text);border-radius:4px;padding:6px 10px;cursor:pointer}.fields{padding:12px 15px 18px;display:grid;grid-template-columns:130px 1fr;gap:7px 12px}.fields dt{color:var(--muted)}.fields dd{margin:0}.editor{padding:0 15px 15px}.stars button{background:transparent;border:0;color:#566;font-size:24px;cursor:pointer}.stars button.active{color:var(--accent)}.editor input,.editor textarea{width:100%;margin-top:7px;background:#121619;border:1px solid var(--line);color:var(--text);padding:8px;border-radius:4px}.editor textarea{min-height:70px;resize:vertical}.toast{padding:8px 15px;color:var(--muted)}
dialog{width:min(980px,calc(100vw - 28px));max-height:calc(100vh - 30px);background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:7px;padding:0}.head{display:flex;justify-content:space-between;padding:12px 15px;border-bottom:1px solid var(--line)}.head h2{font-size:16px;margin:0}.preview{background:#090b0d;text-align:center;max-height:62vh;overflow:auto}.preview img{max-width:100%;max-height:62vh;object-fit:contain;display:block;margin:auto}.preview.zoom-mode img{max-width:none;max-height:none;width:calc(100% * var(--zoom,1));cursor:grab;user-select:none}.preview.zoom-mode img:active{cursor:grabbing}.actions{padding:10px 15px;display:flex;gap:8px;flex-wrap:wrap}.actions button,.save{border:1px solid var(--line);background:var(--panel2);color:var(--text);border-radius:4px;padding:6px 10px;cursor:pointer}.fields{padding:12px 15px 18px;display:grid;grid-template-columns:130px 1fr;gap:7px 12px}.fields dt{color:var(--muted)}.fields dd{margin:0}.editor{padding:0 15px 15px}.stars button{background:transparent;border:0;color:#566;font-size:24px;cursor:pointer}.stars button.active{color:var(--accent)}.editor input,.editor textarea{width:100%;margin-top:7px;background:#121619;border:1px solid var(--line);color:var(--text);padding:8px;border-radius:4px}.editor textarea{min-height:70px;resize:vertical}.toast{padding:8px 15px;color:var(--muted)}dialog.photo-fullscreen{width:100vw;height:100vh;max-width:none;max-height:none;margin:0;border:0;border-radius:0;background:#000}dialog.photo-fullscreen .head,dialog.photo-fullscreen .actions,dialog.photo-fullscreen .fields,dialog.photo-fullscreen .editor{display:none}dialog.photo-fullscreen .preview{width:100vw;height:100vh;max-height:none;overflow:auto;display:block}dialog.photo-fullscreen .preview img{display:block;margin:0 auto}body.photo-lock{overflow:hidden}
@media(max-width:720px){.stats{grid-template-columns:repeat(2,1fr)}.album-strip{padding-left:12px;padding-right:12px}.gallery{grid-template-columns:repeat(auto-fill,min(var(--card-min,210px),45vw));justify-content:start;gap:9px}.gallery.image-only{padding-right:72px}.date-rail{right:2px;width:64px}.date-point{font-size:9px;gap:3px}.mapwrap{grid-template-columns:1fr}#map{height:55vh;min-height:380px}.day{grid-template-columns:1fr}.axis{text-align:left;border:0;padding:0}.fields{grid-template-columns:1fr}.preview img{max-height:55vh}}
</style></head><body><div id="topControls"><header><h1>图片整理索引</h1><div class="sub">评分与说明写入图片元数据，原文件不移动</div><div class="stats" id="stats"></div></header>
<section class="toolbar"><input id="search" type="search" placeholder="搜索文件名、目录、日期、地点、相机"><select id="year"><option value="">全部年份</option></select><select id="city"><option value="">全部地点</option></select><select id="camera"><option value="">全部相机</option></select><select id="lens"><option value="">全部镜头</option></select><select id="rating"><option value="">全部评分</option><option value="5">5 星</option><option value="4">4 星及以上</option><option value="3">3 星及以上</option><option value="2">2 星及以上</option><option value="1">1 星及以上</option></select></section>
<div class="tagbar collapsed" id="tagbar"></div><button class="tag-toggle" id="tagToggle">展开标签</button>
<section class="batchbar" id="batchbar"><span>已选 <strong id="selCount">0</strong> 张</span><input id="batchTag" placeholder="输入标签"><button id="applyBatchTag">设置标签</button><select id="batchRating"><option value="">设置评分</option><option value="0">清除评分</option><option value="1">1 星</option><option value="2">2 星</option><option value="3">3 星</option><option value="4">4 星</option><option value="5">5 星</option></select><button id="applyBatchRating">应用评分</button><input id="cityInput" placeholder="输入城市或景点查询 GPS"><button id="findCity">查询地点</button><input id="batchLat" placeholder="纬度"><input id="batchLon" placeholder="经度"><button id="applyGps">设置 GPS</button><button id="batchFeatured">添加到精选</button><select id="batchAlbum"><option value="">选择相册</option></select><button id="addToAlbum">添加到相册</button><button id="batchRecycle" class="danger">移到回收站</button><button id="clearSel">取消选择</button></section>
<nav class="tabs"><button class="tab active" data-view="all">全部 <span id="countAll"></span></button><button class="tab" data-view="featured">精选 <span id="countFeatured"></span></button><button class="tab" data-view="timeline">时间线 <span id="countTimeline"></span></button><button class="tab" data-view="map">地图 <span id="countMap"></span></button></nav>
<section class="album-strip" id="albumStrip"><div class="album-strip-head"><strong>相册</strong><div class="album-actions"><button id="newAlbum">新建相册</button><button id="newSmartAlbum">智能相册</button></div><div class="album-list" id="albumList"></div><button class="album-collapse" id="albumCollapse">收起</button></div></section>
<div class="bar"><span id="result"></span><button id="fixedControls">固定筛选</button><label class="size-control">大小 <input id="cardSize" type="range" min="140" max="420" step="10" value="210"><output id="cardSizeValue">210</output></label><span id="status">图片元数据浏览</span><button id="loadLibrary">加载图片库</button><button id="rebuildIncremental">增量更新</button><button id="rebuildFull">全部重建</button><button id="viewMode">纯图瀑布流</button></div></div><main class="gallery" id="gallery"></main>
<div id="quickPreview" class="quick-preview" aria-hidden="true"><img id="quickImage" alt=""></div>
<dialog id="dialog"><div class="head"><h2 id="title"></h2><button id="close">关闭</button></div><div class="preview"><img id="detailImage" alt=""></div><div class="actions"><button id="open">默认软件打开</button><button id="folder">在文件夹中显示</button><button id="featured">加入精选</button></div><dl class="fields" id="fields"></dl><div class="editor"><div class="stars" id="stars"><button data-rating="1">★</button><button data-rating="2">★</button><button data-rating="3">★</button><button data-rating="4">★</button><button data-rating="5">★</button></div><input id="keywords" placeholder="关键词，用逗号分隔"><textarea id="note" placeholder="说明"></textarea><button class="save" id="save">保存元数据</button><span class="toast" id="toast"></span></div></dialog>
<script src="http://127.0.0.1:18766/library/vendor/leaflet/leaflet.js"></script><script>const ROWS=__DATA__;const API='http://127.0.0.1:18766',PREVIEW_HOLD_MS=220;const query=new URLSearchParams(location.search),requestedView=query.get('view');let view=['featured','timeline','map'].includes(requestedView)?requestedView:'all',current=null,map=null,selectedRating=0,zoomMode=false,fullscreenMode=false,zoom=1,drag=null,selectedTag='',selected=new Set(),quickPreviewOpen=false,quickPreviewTimer=null,suppressQuickClick=false,imageOnly=query.get('mode')==='wall',resizeTimer=null,cardSize=Math.max(140,Math.min(420,Number(localStorage.getItem('mediaLibraryCardSize'))||210)),cardSizeTimer=null,fixedControls=localStorage.getItem('mediaLibraryFixedControls')==='1',albumsCollapsed=localStorage.getItem('mediaLibraryAlbumsCollapsed')==='1',albums=[],currentAlbumId='all';document.addEventListener('keydown',e=>{if(!$('dialog').open||['INPUT','TEXTAREA'].includes(e.target.tagName))return;if(e.code==='Space'){e.preventDefault();e.stopImmediatePropagation();fullscreenMode=!fullscreenMode;zoomMode=fullscreenMode;zoom=1;const p=$('detailImage').parentElement;$('dialog').classList.toggle('photo-fullscreen',fullscreenMode);document.body.classList.toggle('photo-lock',fullscreenMode);p.classList.toggle('zoom-mode',fullscreenMode);p.style.setProperty('--zoom',1)}else if(e.code==='Escape'&&fullscreenMode){e.preventDefault();e.stopImmediatePropagation();fullscreenMode=false;zoomMode=false;$('dialog').classList.remove('photo-fullscreen');document.body.classList.remove('photo-lock');$('detailImage').parentElement.classList.remove('zoom-mode')}},true);
const $=id=>document.getElementById(id),fmt=n=>n>=1073741824?(n/1073741824).toFixed(1)+' GB':(n/1048576).toFixed(1)+' MB';
function syncFixedControls(){document.body.classList.toggle('controls-fixed',fixedControls);$('fixedControls').classList.toggle('active',fixedControls);$('fixedControls').setAttribute('aria-pressed',String(fixedControls));$('fixedControls').textContent=fixedControls?'取消固定':'固定筛选'}function setCardSize(value,reflow=false){cardSize=Math.max(140,Math.min(420,Number(value)||210));document.documentElement.style.setProperty('--card-min',cardSize+'px');$('cardSize').value=cardSize;$('cardSizeValue').textContent=cardSize;try{localStorage.setItem('mediaLibraryCardSize',String(cardSize))}catch{}if(reflow&&view==='all'&&imageOnly){clearTimeout(cardSizeTimer);cardSizeTimer=setTimeout(render,80)}}
function syncAlbumsCollapsed(){document.body.classList.toggle('albums-collapsed',albumsCollapsed);$('albumCollapse').textContent=albumsCollapsed?'展开':'收起';$('albumCollapse').setAttribute('aria-expanded',String(!albumsCollapsed))}function albumMatchesFilters(r,f){const q=String(f.search||'').toLowerCase();if(q&&!`${r.name} ${r.relative_path} ${r.date} ${r.city} ${r.make} ${r.model} ${r.lens} ${r.tags}`.toLowerCase().includes(q))return false;if(f.year&&r.date.slice(0,4)!==f.year)return false;if(f.city&&r.city!==f.city)return false;if(f.camera&&`${r.make} ${r.model}`.trim()!==f.camera)return false;if(f.lens&&r.lens!==f.lens)return false;if(f.rating&&r.rating<Number(f.rating))return false;if(f.tag&&!String(r.tags||'').split(/[,，;；]/).map(x=>x.trim()).includes(f.tag))return false;return true}function renderAlbums(){const list=$('albumList'),target=$('batchAlbum'),selectedTarget=target.value;list.innerHTML=`<button class="album-item ${currentAlbumId==='all'?'active':''}" data-album="all"><span>全部</span><small>${ROWS.length}</small></button>`;albums.forEach(album=>{const count=album.type==='manual'?ROWS.filter(r=>(album.paths||[]).includes(r.relative_path)).length:ROWS.filter(r=>albumMatchesFilters(r,album.filters||{})).length;list.insertAdjacentHTML('beforeend',`<button class="album-item ${currentAlbumId===album.id?'active':''}" data-album="${album.id}"><span>${album.name}</span><small>${count}</small></button>`)});target.innerHTML='<option value="">选择相册</option>'+albums.filter(a=>a.type==='manual').map(a=>`<option value="${a.id}">${a.name}</option>`).join('');target.value=selectedTarget;list.onclick=e=>{const button=e.target.closest('[data-album]');if(!button)return;currentAlbumId=button.dataset.album;if(fixedControls)$('gallery').scrollTop=0;render()}}
async function saveAlbums(){try{const res=await fetch(API+'/api/albums',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({albums})});if(!res.ok)throw Error((await res.json()).message);}catch(e){$('status').textContent='相册保存失败：'+e.message;return false}renderAlbums();return true}async function loadAlbums(){try{const res=await fetch(API+'/api/albums',{cache:'no-store'});const data=await res.json();albums=Array.isArray(data.albums)?data.albums:[]}catch{}renderAlbums()}async function createAlbum(){const name=prompt('新相册名称');if(!name||!name.trim())return;const album={id:'album-'+Date.now().toString(36),name:name.trim(),type:'manual',paths:[]};albums.push(album);if(await saveAlbums()){currentAlbumId=album.id;render()}}async function createSmartAlbum(){const filters={search:$('search').value.trim(),year:$('year').value,city:$('city').value,camera:$('camera').value,lens:$('lens').value,rating:+($('rating').value||0),tag:selectedTag};if(!Object.values(filters).some(Boolean)){alert('请先设置至少一个筛选条件');return}const name=prompt('智能相册名称');if(!name||!name.trim())return;const album={id:'smart-'+Date.now().toString(36),name:name.trim(),type:'smart',filters};albums.push(album);if(await saveAlbums()){currentAlbumId=album.id;render()}}async function addSelectedToAlbum(){const id=$('batchAlbum').value,album=albums.find(a=>a.id===id&&a.type==='manual');if(!album){alert('请先选择普通相册');return}album.paths=[...new Set([...(album.paths||[]),...selected])];if(!await saveAlbums())return;selected.clear();batch();render();$('status').textContent=`已添加到相册：${album.name}`}
function stats(){const gps=ROWS.filter(r=>r.latitude!=null).length;$('stats').innerHTML=[[ROWS.length,'图片文件'],[(ROWS.reduce((s,r)=>s+r.size_bytes,0)/1073741824).toFixed(1)+' GB','总占用'],[gps,'带 GPS'],[ROWS.filter(r=>r.featured).length,'精选'],[ROWS.filter(r=>r.error).length,'异常']].map(x=>`<div class=stat><strong>${x[0]}</strong><span>${x[1]}</span></div>`).join('')}
function thumb(r){return r.thumb?'缩略图/'+r.thumb.split('/').map(encodeURIComponent).join('/'):''}
function media(r){return API+'/media/'+r.relative_path.split('/').map(encodeURIComponent).join('/')}
function filtered(){const q=$('search').value.toLowerCase(),y=$('year').value,city=$('city').value,min=+($('rating').value||0),cam=$('camera').value,lens=$('lens').value,album=albums.find(a=>a.id===currentAlbumId);return ROWS.filter(r=>{if(album?.type==='manual'&&!(album.paths||[]).includes(r.relative_path))return false;if(album?.type==='smart'&&!albumMatchesFilters(r,album.filters||{}))return false;if(q&&!`${r.name} ${r.relative_path} ${r.date} ${r.city} ${r.make} ${r.model} ${r.lens} ${r.tags}`.toLowerCase().includes(q))return false;if(y&&r.date.slice(0,4)!==y)return false;if(city&&r.city!==city)return false;if(cam&&`${r.make} ${r.model}`.trim()!==cam)return false;if(lens&&r.lens!==lens)return false;if(min&&r.rating<min)return false;if(selectedTag&&!String(r.tags||'').split(/[,，;；]/).map(x=>x.trim()).includes(selectedTag))return false;if(view==='featured'&&!r.featured)return false;if(view==='map'&&r.latitude==null)return false;return true}).sort((a,b)=>(b.date+(b.time||'')).localeCompare(a.date+(a.time||'')))}
function bindQuickPreview(img,src){if(!img)return;img.draggable=false;let origin=null;const cancel=()=>{clearTimeout(quickPreviewTimer);quickPreviewTimer=null;origin=null},start=e=>{if(e.button!==0)return;origin={x:e.clientX,y:e.clientY};clearTimeout(quickPreviewTimer);quickPreviewTimer=setTimeout(()=>{quickPreviewTimer=null;showQuickPreview(src)},PREVIEW_HOLD_MS)},move=e=>{if(origin&&Math.hypot(e.clientX-origin.x,e.clientY-origin.y)>8)cancel()};img.addEventListener('dragstart',e=>e.preventDefault());img.addEventListener('pointerdown',start);if(!window.PointerEvent)img.addEventListener('mousedown',start);img.addEventListener('pointermove',move);img.addEventListener('pointerleave',cancel)}
function card(r){const d=document.createElement('article');d.className='card'+(r.width<r.height?' portrait':'');const tags=String(r.tags||'').split(/[,，;；]/).map(x=>x.trim()).filter(Boolean).slice(0,3);d.innerHTML=`<div class=thumb>${r.thumb?`<img loading=lazy draggable=false src="${thumb(r)}">`:''}<label class=sel title="批量选择"><input type=checkbox ${selected.has(r.relative_path)?'checked':''}></label>${r.rating?`<span class=badge>${'★'.repeat(r.rating)}</span>`:''}</div><div class=body><div class=name title="${r.name}">${r.name}</div><div class=meta><span>${r.date}${r.time?' '+r.time:''}</span><span>${fmt(r.size_bytes)}</span><span>${r.width&&r.height?r.width+'×'+r.height:'分辨率未知'}</span><span>${r.model||r.make||'未知相机'}</span><span>${r.lens||''}</span><span>${r.city||'无 GPS'}</span></div>${tags.length?`<div class=card-tags>${tags.map(t=>`<span class=tag-chip>${t}</span>`).join('')}</div>`:''}</div>`;const c=d.querySelector('.sel');c.onclick=e=>e.stopPropagation();c.querySelector('input').onchange=e=>{e.target.checked?selected.add(r.relative_path):selected.delete(r.relative_path);batch()};bindQuickPreview(d.querySelector('.thumb img'),media(r));d.onclick=()=>{if(suppressQuickClick){suppressQuickClick=false;return}detail(r)};return d}
const baseCard=card;card=function(r){const d=baseCard(r);let detailTimer=null;d.title='单击查看详情，双击用默认软件打开';d.onclick=()=>{if(suppressQuickClick){suppressQuickClick=false;return}clearTimeout(detailTimer);detailTimer=setTimeout(()=>detail(r),240)};d.ondblclick=e=>{e.preventDefault();clearTimeout(detailTimer);detailTimer=null;openPath(r.relative_path)};return d};
function showQuickPreview(src){quickPreviewTimer=null;quickPreviewOpen=true;const image=$('quickImage'),overlay=$('quickPreview'),reveal=()=>{if(!quickPreviewOpen||image.dataset.requested!==src)return;overlay.classList.add('show');overlay.setAttribute('aria-hidden','false');document.body.classList.add('photo-lock')};image.dataset.requested=src;image.onload=reveal;image.onerror=hideQuickPreview;if(image.src===src&&image.complete&&image.naturalWidth){reveal()}else{image.src=src}}function hideQuickPreview(){clearTimeout(quickPreviewTimer);quickPreviewTimer=null;if(!quickPreviewOpen)return;quickPreviewOpen=false;suppressQuickClick=true;$('quickPreview').classList.remove('show');$('quickPreview').setAttribute('aria-hidden','true');document.body.classList.remove('photo-lock');setTimeout(()=>suppressQuickClick=false,0)}
function setFeaturedTag(r,featured){const tags=String(r.tags||'').split(/[,，;；]/).map(x=>x.trim()).filter(x=>x&&x!=='精选');if(featured)tags.push('精选');r.tags=tags.join(',')}
function days(rows){const m=new Map();rows.forEach(r=>{if(!m.has(r.date))m.set(r.date,[]);m.get(r.date).push(r)});return m}
function timeline(rows){const years=new Map();days(rows).forEach((items,date)=>{const y=date.slice(0,4);if(!years.has(y))years.set(y,[]);years.get(y).push([date,items])});let first=true;$('gallery').className='gallery timeline';years.forEach((ds,y)=>{const det=document.createElement('details');det.className='year';det.open=first;first=false;const count=ds.reduce((s,x)=>s+x[1].length,0),size=ds.reduce((s,x)=>s+x[1].reduce((a,r)=>a+r.size_bytes,0),0);det.innerHTML=`<summary><strong>${y} 年</strong><small>${count} 张 · ${fmt(size)}</small></summary>`;ds.forEach(([date,items])=>{const sec=document.createElement('section');sec.className='day';sec.innerHTML=`<div class=axis><strong>${date.slice(5)}</strong>${items.length} 张 · ${fmt(items.reduce((s,r)=>s+r.size_bytes,0))}</div>`;const grid=document.createElement('div');grid.className='daygrid';items.forEach(r=>grid.append(card(r)));sec.append(grid);det.append(sec)});$('gallery').append(det)})}
function syncImageMode(){const b=$('viewMode');b.hidden=view!=='all';b.textContent=imageOnly?'标准视图':'纯图瀑布流';b.title=imageOnly?'切换到带信息的标准视图':'隐藏信息并按原图比例排列';b.classList.toggle('active',imageOnly)}
function renderImageWall(rows){const gallery=$('gallery');gallery.className='gallery image-only';gallery.innerHTML='<div id=masonryStage class=masonry-stage></div>';const stage=$('masonryStage'),gap=10,minWidth=cardSize,columnCount=Math.max(1,Math.floor((stage.clientWidth+gap)/(minWidth+gap))),columnWidth=(stage.clientWidth-gap*(columnCount-1))/columnCount,heights=Array(columnCount).fill(0),months=new Map();rows.forEach(r=>{const node=card(r),ratioWidth=Number(r.width)>0?Number(r.width):4,ratioHeight=Number(r.height)>0?Number(r.height):3,cardHeight=Math.max(60,columnWidth*ratioHeight/ratioWidth),column=heights.indexOf(Math.min(...heights)),left=column*(columnWidth+gap),top=heights[column],month=String(r.date||'').slice(0,7)||'未知时间';node.style.width=columnWidth+'px';node.style.height=cardHeight+'px';node.style.left=left+'px';node.style.top=top+'px';if(!months.has(month)){node.id='photo-month-'+month.replace(/\D/g,'');months.set(month,{target:node.id,count:0})}months.get(month).count++;heights[column]=top+cardHeight+gap;stage.append(node)});stage.style.height=Math.max(0,Math.max(...heights)-gap)+'px';const rail=document.createElement('nav');rail.className='date-rail';rail.setAttribute('aria-label','照片时间定位');rail.style.setProperty('--rail-top',Math.max(16,gallery.getBoundingClientRect().top+8)+'px');months.forEach((month,label)=>{const button=document.createElement('button');button.className='date-point';button.dataset.target=month.target;button.title=label+' · '+month.count+' 张';button.innerHTML=`<i></i><span>${label.replace('-','.')}</span>`;rail.append(button)});rail.onclick=e=>{const button=e.target.closest('.date-point');if(!button)return;rail.querySelectorAll('.date-point').forEach(x=>x.classList.toggle('active',x===button));const target=document.getElementById(button.dataset.target);if(!target)return;if(fixedControls){gallery.scrollTo({top:Math.max(0,target.offsetTop-8),behavior:'smooth'})}else{window.scrollTo({top:target.getBoundingClientRect().top+scrollY-90,behavior:'smooth'})}};gallery.append(rail)}
function mapThumb(r){return thumb(r)}
async function openPath(path,explorer=false){try{const res=await fetch(API+(explorer?'/api/explorer':'/api/open'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path})});const j=await res.json();$('toast').textContent=j.message;$('status').textContent=j.message}catch(e){$('toast').textContent='后端离线';$('status').textContent=e.message||'后端离线'}}
async function rebuildIndex(mode){const incremental=mode==='incremental',button=$(incremental?'rebuildIncremental':'rebuildFull'),label=button.textContent;$('rebuildIncremental').disabled=true;$('rebuildFull').disabled=true;button.textContent=incremental?'正在检查...':'正在全量扫描...';$('status').textContent=incremental?'正在检查新增、改名或修改过的照片':'正在重新读取全部照片，可能需要约两分钟';try{const res=await fetch(API+'/api/rebuild',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode})});const j=await res.json();if(!res.ok)throw Error(j.message);$('status').textContent=j.message;const params=new URLSearchParams({view,v:Date.now()});if(view==='all'&&imageOnly)params.set('mode','wall');location.replace(location.pathname+'?'+params)}catch(e){$('status').textContent=e.message;$('rebuildIncremental').disabled=false;$('rebuildFull').disabled=false;button.textContent=label}}
async function loadLibrary(){if(!confirm('选择一个新的图片库根目录。\n程序会在其中创建隐藏的 .media-library，并建立该库自己的索引，是否继续？'))return;const button=$('loadLibrary');button.disabled=true;button.textContent='正在加载...';$('status').textContent='请在弹出的窗口中选择图片库根目录';try{const res=await fetch(API+'/api/library/select',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});const j=await res.json();if(!res.ok)throw Error(j.message);$('status').textContent=j.message;location.href=j.url+'?v='+Date.now()}catch(e){$('status').textContent=e.message;button.disabled=false;button.textContent='加载图片库'}}
async function recycleSelected(){if(!selected.size)return;const paths=[...selected];if(!confirm(`确定将所选 ${paths.length} 张照片移到系统回收站吗？\n移动后会自动更新索引。`))return;const button=$('batchRecycle');button.disabled=true;button.textContent='正在移动...';$('status').textContent='正在移动到系统回收站';try{const res=await fetch(API+'/api/recycle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths})});const j=await res.json();if(!res.ok)throw Error(j.message);$('status').textContent=j.message+'，正在更新索引';if(j.message.includes('失败'))alert(j.message);const refresh=await fetch(API+'/api/rebuild',{method:'POST',headers:{'Content-Type':'application/json'},body:'{"mode":"incremental"}'});const rebuilt=await refresh.json();if(!refresh.ok)throw Error(j.message+'；索引更新失败：'+rebuilt.message);const params=new URLSearchParams({view,v:Date.now()});if(view==='all'&&imageOnly)params.set('mode','wall');location.replace(location.pathname+'?'+params)}catch(e){$('status').textContent=e.message;button.disabled=false;button.textContent='移到回收站'}}
async function detail(r){current=r;selectedRating=r.rating;zoomMode=false;fullscreenMode=false;zoom=1;$('dialog').classList.remove('photo-fullscreen');document.body.classList.remove('photo-lock');$('detailImage').parentElement.classList.remove('zoom-mode');$('detailImage').parentElement.style.setProperty('--zoom',1);$('title').textContent=r.name;$('keywords').value=r.keywords||'';$('note').value=r.note||'';$('stars').querySelectorAll('button').forEach(b=>b.classList.toggle('active',+b.dataset.rating<=selectedRating));$('featured').textContent=r.featured?'取消精选':'加入精选';$('fields').innerHTML=[['日期',r.date+(r.time?' '+r.time:'')],['分辨率',r.width+'×'+r.height],['大小',fmt(r.size_bytes)],['相机',(r.make+' '+r.model).trim()],['镜头',r.lens||'无'],['地点',r.city||'无 GPS'],['标签',String(r.tags||'').split(/[,，;；]/).map(x=>x.trim()).filter(Boolean).join('、')||'无'],['GPS',r.latitude!=null?`${r.latitude}, ${r.longitude}`:'无'],['目录',r.relative_path.split('/').slice(0,-1).join('/')]].map(x=>`<dt>${x[0]}</dt><dd>${x[1]}</dd>`).join('');$('open').onclick=()=>openPath(r.relative_path);$('folder').onclick=()=>openPath(r.relative_path,true);$('featured').onclick=toggleFeatured;const src=media(r),loader=new Image();loader.src=src;try{await loader.decode()}catch{}if(current!==r)return;$('detailImage').src=src;$('toast').textContent='空格进入全屏缩放';if(!$('dialog').open)$('dialog').showModal()}
async function save(data){if(!current)return;try{const res=await fetch(API+'/api/metadata',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:current.relative_path,rating:selectedRating,note:$('note').value,keywords:$('keywords').value,featured:current.featured,...data})});const j=await res.json();if(!res.ok)throw Error(j.message);Object.assign(current,j.metadata);if('featured' in data)setFeaturedTag(current,current.featured);$('toast').textContent='已保存';render();detail(current)}catch(e){$('toast').textContent=e.message}}
async function toggleFeatured(){if(!current)return;const featured=!current.featured;try{const res=await fetch(API+'/api/featured',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths:[current.relative_path],featured})});const j=await res.json();if(!res.ok)throw Error(j.message);current.featured=featured;setFeaturedTag(current,featured);$('featured').textContent=featured?'取消精选':'加入精选';$('toast').textContent=j.message;updateCounts();stats();render()}catch(e){$('toast').textContent=e.message}}
function batch(){$('selCount').textContent=selected.size;$('batchbar').classList.toggle('show',selected.size>0)}function updateCounts(){if(!$('countAll'))return;$('countAll').textContent=ROWS.length;$('countFeatured').textContent=ROWS.filter(r=>r.featured).length;$('countTimeline').textContent=ROWS.length;$('countMap').textContent=ROWS.filter(r=>r.latitude!=null).length}async function applyGps(){if(!selected.size)return;const latitude=+$('batchLat').value,longitude=+$('batchLon').value;if(!Number.isFinite(latitude)||!Number.isFinite(longitude)){alert('请输入经纬度');return}try{const res=await fetch(API+'/api/gps',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths:[...selected],latitude,longitude})});const j=await res.json();if(!res.ok)throw Error(j.message);ROWS.forEach(r=>{if(selected.has(r.relative_path)){r.latitude=latitude;r.longitude=longitude}});selected.clear();batch();updateCounts();render();alert(j.message)}catch(e){alert(e.message)}}async function findCity(){const city=$('cityInput').value.trim();if(!city)return;try{const res=await fetch(API+'/api/geocode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({city})});const j=await res.json();if(!res.ok)throw Error(j.message);$('batchLat').value=j.latitude;$('batchLon').value=j.longitude;alert(j.display_name)}catch(e){alert(e.message)}}async function applyFeatured(){if(!selected.size)return;try{const res=await fetch(API+'/api/featured',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths:[...selected],featured:true})});const j=await res.json();if(!res.ok)throw Error(j.message);ROWS.forEach(r=>{if(selected.has(r.relative_path)){r.featured=true;setFeaturedTag(r,true)}});selected.clear();batch();updateCounts();stats();render();alert(j.message)}catch(e){alert(e.message)}}
async function applyBatchTag(){if(!selected.size)return;const tag=$('batchTag').value.trim();if(!tag||/[,，;；]/.test(tag)){alert('请输入单个标签，不能包含逗号或分号');return}try{const res=await fetch(API+'/api/keywords',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths:[...selected],tag})});const j=await res.json();if(!res.ok)throw Error(j.message);ROWS.forEach(r=>{if(selected.has(r.relative_path)){const tags=String(r.tags||'').split(/[,，;；]/).map(x=>x.trim()).filter(Boolean);if(!tags.includes(tag))tags.push(tag);r.tags=tags.join(',')}});selected.clear();$('batchTag').value='';batch();updateCounts();stats();render();alert(j.message)}catch(e){alert(e.message)}}async function applyBatchRating(){if(!selected.size||$('batchRating').value==='')return;const rating=Number($('batchRating').value);try{const res=await fetch(API+'/api/ratings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths:[...selected],rating})});const j=await res.json();if(!res.ok)throw Error(j.message);ROWS.forEach(r=>{if(selected.has(r.relative_path))r.rating=rating});selected.clear();$('batchRating').value='';batch();updateCounts();stats();render();alert(j.message)}catch(e){alert(e.message)}}
$('stars').onclick=e=>{if(e.target.dataset.rating){selectedRating=+e.target.dataset.rating;$('stars').querySelectorAll('button').forEach(b=>b.classList.toggle('active',+b.dataset.rating<=selectedRating));save({})}}
document.addEventListener('keydown',e=>{if(e.code==='Escape'&&quickPreviewOpen)hideQuickPreview();if(e.code==='Space'&&$('dialog').open&&!['INPUT','TEXTAREA'].includes(e.target.tagName)){e.preventDefault();zoomMode=!zoomMode;if(!zoomMode)zoom=1;const p=$('detailImage').parentElement;p.classList.toggle('zoom-mode',zoomMode);p.style.setProperty('--zoom',zoom);$('toast').textContent=zoomMode?'滚轮缩放模式已开启':'滚轮缩放模式已关闭'}});const endQuickPreview=e=>{if(e.button===0)hideQuickPreview()};document.addEventListener('pointerup',endQuickPreview);document.addEventListener('mouseup',endQuickPreview);document.addEventListener('pointercancel',hideQuickPreview);window.addEventListener('blur',hideQuickPreview);$('detailImage').parentElement.addEventListener('wheel',e=>{if(!zoomMode)return;e.preventDefault();zoom=Math.max(1,Math.min(8,zoom+(e.deltaY<0?.25:-.25)));e.currentTarget.style.setProperty('--zoom',zoom)},{passive:false});$('detailImage').parentElement.addEventListener('pointerdown',e=>{if(!zoomMode)return;const p=e.currentTarget;drag={x:e.clientX,y:e.clientY,left:p.scrollLeft,top:p.scrollTop};p.setPointerCapture(e.pointerId)});$('detailImage').parentElement.addEventListener('pointermove',e=>{if(!drag)return;e.currentTarget.scrollLeft=drag.left-(e.clientX-drag.x);e.currentTarget.scrollTop=drag.top-(e.clientY-drag.y)});$('detailImage').parentElement.addEventListener('pointerup',()=>{drag=null});
function render(){syncImageMode();batch();renderAlbums();const rows=filtered(),size=rows.reduce((s,r)=>s+r.size_bytes,0),album=albums.find(a=>a.id===currentAlbumId);$('result').textContent=`${album?album.name+' · ':''}显示 ${rows.length} / ${ROWS.length} 张 · ${fmt(size)}`;$('gallery').innerHTML='';if(!rows.length){$('gallery').className='gallery';$('gallery').innerHTML='<div class=stat>没有匹配图片</div>';return}if(view==='all'&&imageOnly){renderImageWall(rows);return}if(view==='featured'||view==='timeline'){timeline(rows);return}if(view==='map'){renderMap(rows);setTimeout(()=>{if(!map)return;map.setView([35.5,104.5],4);fetch(API+'/library/vendor/china-provinces.json').then(r=>r.json()).then(g=>L.geoJSON(g,{style:{color:'#497873',weight:.8,fillColor:'#1c2c2a',fillOpacity:.03}}).addTo(map)).catch(()=>{});map.invalidateSize()},350);return}$('gallery').className='gallery';rows.forEach(r=>$('gallery').append(card(r)))}
function renderMap(rows){$('gallery').className='gallery';$('gallery').innerHTML='<div class=mapwrap><div id=map></div><aside class=mapside><h2>地图画廊</h2><div class=mapgallery id=mapGallery></div></aside></div>';if(!window.L){$('map').innerHTML='地图组件加载失败';return}map=L.map('map',{preferCanvas:true});L.tileLayer(API+'/api/map-tile/{z}/{x}/{y}',{maxZoom:19,attribution:'&copy; 高德地图'}).addTo(map);const mapElement=$('map'),mapGalleryElement=$('mapGallery'),startMapPreview=e=>{const img=e.target.closest('.thumbmarker img,.mapitem img');if(!img||e.button!==0)return;clearTimeout(quickPreviewTimer);quickPreviewTimer=setTimeout(()=>{quickPreviewTimer=null;showQuickPreview(img.dataset.media)},PREVIEW_HOLD_MS)};mapElement.addEventListener('pointerdown',startMapPreview);mapElement.addEventListener('mousedown',startMapPreview);mapGalleryElement.addEventListener('pointerdown',startMapPreview);mapGalleryElement.addEventListener('mousedown',startMapPreview);mapElement.addEventListener('click',e=>{if(suppressQuickClick){e.preventDefault();e.stopImmediatePropagation();suppressQuickClick=false}},true);const groups=new Map();rows.forEach(r=>{const k=Number(r.latitude).toFixed(4)+','+Number(r.longitude).toFixed(4);if(!groups.has(k))groups.set(k,[]);groups.get(k).push(r)});const layer=L.featureGroup().addTo(map);groups.forEach(items=>{const r=items[0],icon=L.divIcon({className:'thumbmarker',html:`<img draggable=false data-media="${media(r)}" src="${thumb(r)}">`,iconSize:[54,54]}),marker=L.marker([+r.latitude,+r.longitude],{icon});marker.on('click',()=>{if(suppressQuickClick){suppressQuickClick=false;return}showMapGallery(items)});marker.addTo(layer)});setTimeout(()=>{map.invalidateSize();const b=layer.getBounds();if(b.isValid())map.fitBounds(b.pad(.08))},200);const first=[...groups.values()].sort((a,b)=>b.length-a.length)[0];if(first)showMapGallery(first)}function showMapGallery(rows){$('mapGallery').innerHTML='';rows.forEach(r=>{const b=document.createElement('button');b.className='mapitem';b.innerHTML=`<img data-media="${media(r)}" src="${thumb(r)}"><span>${r.name}</span>`;b.onclick=()=>{if(suppressQuickClick){suppressQuickClick=false;return}openPath(r.relative_path)};$('mapGallery').append(b)})}
function fill(){const years=[...new Set(ROWS.map(r=>r.date.slice(0,4)))].sort().reverse();$('year').insertAdjacentHTML('beforeend',years.map(y=>`<option>${y}</option>`).join(''));const camCounts=new Map(),lensCounts=new Map(),cityCounts=new Map();ROWS.forEach(r=>{const cam=`${r.make||''} ${r.model||''}`.trim();if(cam)camCounts.set(cam,(camCounts.get(cam)||0)+1);if(r.lens)lensCounts.set(r.lens,(lensCounts.get(r.lens)||0)+1);cityCounts.set(r.city||'无 GPS',(cityCounts.get(r.city||'无 GPS')||0)+1)});const camEl=$('camera'),lensEl=$('lens'),cityEl=$('city');[...cityCounts].sort((a,b)=>b[1]-a[1]).forEach(([v,n])=>cityEl.add(new Option(`${v} (${n})`,v)));[...camCounts].sort((a,b)=>a[0].localeCompare(b[0],'zh-CN')).forEach(([v,n])=>camEl.add(new Option(`${v} (${n})`,v)));[...lensCounts].sort((a,b)=>a[0].localeCompare(b[0],'zh-CN')).forEach(([v,n])=>lensEl.add(new Option(`${v} (${n})`,v)));const counts=new Map();ROWS.forEach(r=>String(r.tags||'').split(/[,，;；]/).map(x=>x.trim()).filter(Boolean).forEach(t=>counts.set(t,(counts.get(t)||0)+1)));$('tagbar').innerHTML=[...counts].sort((a,b)=>b[1]-a[1]).slice(0,80).map(([t,n])=>`<button class="tag" data-tag="${t}">${t} ${n}</button>`).join('');$('tagbar').onclick=e=>{const b=e.target.closest('[data-tag]');if(!b)return;selectedTag=selectedTag===b.dataset.tag?'':b.dataset.tag;document.querySelectorAll('.tag').forEach(x=>x.classList.toggle('active',x.dataset.tag===selectedTag));render()};$('status').textContent=`相机 ${camCounts.size} 种 · 镜头 ${lensCounts.size} 种`;$('countAll').textContent=ROWS.length;$('countFeatured').textContent=ROWS.filter(r=>r.featured).length}
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{view=b.dataset.view;document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x===b));if(map){map.remove();map=null}if(fixedControls)$('gallery').scrollTop=0;render()});$('search').oninput=render;$('year').onchange=render;$('city').onchange=render;$('camera').onchange=render;$('lens').onchange=render;$('rating').onchange=render;$('tagToggle').onclick=()=>{const bar=$('tagbar'),collapsed=bar.classList.toggle('collapsed');$('tagToggle').textContent=collapsed?'展开标签':'收起标签'};$('viewMode').onclick=()=>{imageOnly=!imageOnly;render()};$('fixedControls').onclick=()=>{fixedControls=!fixedControls;try{localStorage.setItem('mediaLibraryFixedControls',fixedControls?'1':'0')}catch{};syncFixedControls();window.scrollTo(0,0);render()};$('cardSize').oninput=e=>setCardSize(e.target.value,true);$('albumCollapse').onclick=()=>{albumsCollapsed=!albumsCollapsed;try{localStorage.setItem('mediaLibraryAlbumsCollapsed',albumsCollapsed?'1':'0')}catch{};syncAlbumsCollapsed()};$('newAlbum').onclick=createAlbum;$('newSmartAlbum').onclick=createSmartAlbum;$('addToAlbum').onclick=addSelectedToAlbum;$('applyBatchTag').onclick=applyBatchTag;$('applyBatchRating').onclick=applyBatchRating;$('loadLibrary').onclick=loadLibrary;$('rebuildIncremental').onclick=()=>rebuildIndex('incremental');$('rebuildFull').onclick=()=>rebuildIndex('full');window.addEventListener('resize',()=>{if(!imageOnly||view!=='all')return;clearTimeout(resizeTimer);resizeTimer=setTimeout(render,120)});$('save').onclick=()=>save({});$('applyGps').onclick=applyGps;$('clearSel').onclick=()=>{selected.clear();batch();render()};$('batchRecycle').onclick=recycleSelected;$('close').onclick=()=>{zoomMode=false;drag=null;$('dialog').close()};document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x.dataset.view===view));setCardSize(cardSize);syncFixedControls();syncAlbumsCollapsed();stats();fill();render();loadAlbums();</script></body></html>"""
    HTML_PATH.write_text(html.replace("__DATA__", data), encoding="utf-8")


def main(incremental: bool = False) -> int:
    OUT.mkdir(exist_ok=True)
    THUMBS.mkdir(exist_ok=True)
    paths = []
    for current, dirs, files in os.walk(ROOT):
        dirs[:] = [
            d for d in dirs
            if d not in {PROJECT_DIR.name, ".bf", ".dtrash"} and not d.startswith(".")
        ]
        for name in files:
            path = Path(current) / name
            if path.suffix.lower() in IMAGE_EXTS:
                paths.append(path)
    paths.sort(key=lambda p: p.as_posix().lower())
    existing = load_index_rows() if incremental else {}
    rows = []
    changed_paths = []
    for path in paths:
        stat = path.stat()
        rel = path.relative_to(ROOT).as_posix()
        old = existing.get(rel)
        thumb = THUMBS / old["thumb"] if old and old.get("thumb") else None
        thumb_ok = thumb.is_file() if thumb else bool(old and old.get("error"))
        unchanged = (
            old is not None
            and old.get("modified_ns") == stat.st_mtime_ns
            and old.get("size_bytes") == stat.st_size
            and thumb_ok
        )
        if unchanged:
            rows.append(old)
        else:
            changed_paths.append(path)
    print(
        f"Scanning {len(changed_paths)} changed of {len(paths)} images..."
        if incremental else f"Scanning {len(paths)} images...",
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(scan_one, p): p for p in changed_paths}
        for i, future in enumerate(as_completed(futures), 1):
            rows.append(future.result())
            if i % 100 == 0 or i == len(changed_paths):
                print(f"  {i}/{len(changed_paths)}", flush=True)
    print("Reading metadata with ExifTool...", flush=True)
    exif = exiftool_rows(changed_paths if incremental else None)
    for row in rows:
        meta = exif.get(row["relative_path"], {})
        for key in ("make", "model", "lens", "tags", "rating", "width", "height", "latitude", "longitude"):
            if meta.get(key) not in (None, ""):
                row[key] = meta[key]
        raw_date = meta.get("exif_date", "")
        if meta.get("exif_rating") not in (None, ""):
            try:
                row["rating"] = int(meta["exif_rating"])
            except (TypeError, ValueError):
                pass
        if raw_date:
            match = re.match(r"(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})", raw_date)
            if match:
                row["date"] = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
                row["time"] = f"{match.group(4)}:{match.group(5)}:{match.group(6)}"
    rows.sort(key=lambda r: r["relative_path"].lower())
    build_db(rows)
    build_html(rows, load_metadata())
    print(f"Wrote {DB_PATH}")
    print(f"Wrote {HTML_PATH}")
    print(
        "RESULT " + json.dumps({
            "mode": "incremental" if incremental else "full",
            "total": len(rows),
            "changed": len(changed_paths),
        }),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--incremental" in sys.argv[1:]))
