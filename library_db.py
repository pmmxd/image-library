from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable


SCHEMA_VERSION = "1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY,
    relative_path TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    extension TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('image', 'video')),
    size_bytes INTEGER NOT NULL DEFAULT 0,
    modified_ns INTEGER NOT NULL DEFAULT 0,
    capture_date TEXT NOT NULL DEFAULT '',
    capture_time TEXT NOT NULL DEFAULT '',
    width INTEGER,
    height INTEGER,
    camera_make TEXT NOT NULL DEFAULT '',
    camera_model TEXT NOT NULL DEFAULT '',
    lens TEXT NOT NULL DEFAULT '',
    latitude REAL,
    longitude REAL,
    thumb TEXT NOT NULL DEFAULT '',
    portrait_thumb TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    indexed_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS image_meta (
    media_id INTEGER PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE,
    exif_rating INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS video_meta (
    media_id INTEGER PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE,
    duration_sec REAL,
    fps REAL,
    rotation INTEGER,
    video_codec TEXT NOT NULL DEFAULT '',
    audio_codec TEXT NOT NULL DEFAULT '',
    has_audio INTEGER NOT NULL DEFAULT 0,
    creation_time TEXT NOT NULL DEFAULT '',
    encoder TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    camera TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    metadata_modified_ns INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS media_tags (
    media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'metadata',
    PRIMARY KEY (media_id, tag, source)
);

CREATE TABLE IF NOT EXISTS user_metadata (
    media_id INTEGER PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE,
    rating INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    keywords TEXT NOT NULL DEFAULT '',
    featured INTEGER NOT NULL DEFAULT 0,
    latitude REAL,
    longitude REAL,
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS thumbnails (
    media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    profile TEXT NOT NULL,
    relative_path TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    dhash TEXT NOT NULL DEFAULT '',
    brightness REAL,
    contrast REAL,
    sharpness REAL,
    colorfulness REAL,
    cover_score REAL,
    orientation TEXT NOT NULL DEFAULT '',
    analysis_modified_ns INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (media_id, profile)
);

CREATE TABLE IF NOT EXISTS duplicate_groups (
    id INTEGER PRIMARY KEY,
    directory TEXT NOT NULL DEFAULT '',
    captured_at TEXT NOT NULL DEFAULT '',
    versions INTEGER NOT NULL DEFAULT 0,
    exact_size_match INTEGER NOT NULL DEFAULT 0,
    max_visual_distance INTEGER NOT NULL DEFAULT 0,
    canonical_media_id INTEGER REFERENCES media(id) ON DELETE SET NULL,
    total_size_bytes INTEGER NOT NULL DEFAULT 0,
    reclaim_bytes INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS duplicate_members (
    group_id INTEGER NOT NULL REFERENCES duplicate_groups(id) ON DELETE CASCADE,
    media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, media_id)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    capture_date TEXT NOT NULL DEFAULT '',
    start_time TEXT NOT NULL DEFAULT '',
    end_time TEXT NOT NULL DEFAULT '',
    clip_count INTEGER NOT NULL DEFAULT 0,
    duration_sec REAL NOT NULL DEFAULT 0,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    directory TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS event_members (
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    PRIMARY KEY (event_id, media_id)
);

CREATE INDEX IF NOT EXISTS idx_media_kind_date ON media(kind, capture_date);
CREATE INDEX IF NOT EXISTS idx_media_camera ON media(camera_make, camera_model);
CREATE INDEX IF NOT EXISTS idx_media_lens ON media(lens);
CREATE INDEX IF NOT EXISTS idx_media_location ON media(latitude, longitude);
CREATE INDEX IF NOT EXISTS idx_media_tags_tag ON media_tags(tag);
CREATE INDEX IF NOT EXISTS idx_user_rating ON user_metadata(rating);
CREATE INDEX IF NOT EXISTS idx_user_featured ON user_metadata(featured);
"""


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SCHEMA_VERSION,),
    )
    conn.commit()


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM schema_meta WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row else default


def split_tags(value: object) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple, set)) else re.split(r"[,，;；|]", str(value))
    return sorted({str(item).strip() for item in values if str(item).strip()})


def _modified_ns(row: dict) -> int:
    value = row.get("modified_ns")
    if value not in (None, ""):
        return int(value)
    value = row.get("modified_time")
    if value:
        try:
            return int(datetime.fromisoformat(str(value)).timestamp() * 1_000_000_000)
        except ValueError:
            pass
    return 0


def _capture_fields(row: dict) -> tuple[str, str]:
    date = str(row.get("date") or row.get("capture_date") or "")
    time_label = str(row.get("time") or row.get("capture_time") or "")
    name = str(row.get("name") or "")
    creation = str(row.get("creation_time") or "")
    match = re.search(r"(\d{4}-\d{2}-\d{2})-(\d{2})(\d{2})(\d{2})", name)
    if match:
        date = date or match.group(1)
        time_label = time_label or f"{match.group(2)}:{match.group(3)}:{match.group(4)}"
    if not date and creation:
        date = creation[:10]
    if date and time_label.startswith("0000-00-00"):
        time_label = ""
    if not time_label and len(creation) >= 19:
        time_label = creation[11:19]
    return date, time_label


def upsert_media(conn: sqlite3.Connection, kind: str, row: dict) -> int:
    date, time_label = _capture_fields(row)
    values = {
        "relative_path": str(row["relative_path"]),
        "name": str(row.get("name") or Path(str(row["relative_path"])).name),
        "extension": str(row.get("extension") or Path(str(row["relative_path"])).suffix.lower()),
        "kind": kind,
        "size_bytes": int(row.get("size_bytes") or 0),
        "modified_ns": _modified_ns(row),
        "capture_date": date,
        "capture_time": time_label,
        "width": row.get("width"),
        "height": row.get("height"),
        "camera_make": str(row.get("camera_make") or row.get("make") or ""),
        "camera_model": str(row.get("camera_model") or row.get("model") or row.get("camera") or ""),
        "lens": str(row.get("lens") or ""),
        "latitude": row.get("latitude"),
        "longitude": row.get("longitude"),
        "thumb": str(row.get("thumb") or row.get("thumbnail_path") or ""),
        "portrait_thumb": str(
            row.get("portrait_thumb") or row.get("portrait_thumbnail_path") or ""
        ),
        "error": str(
            row.get("error")
            or row.get("probe_error")
            or row.get("thumbnail_error")
            or ""
        ),
        "indexed_at": now_iso(),
    }
    conn.execute(
        """
        INSERT INTO media(
            relative_path, name, extension, kind, size_bytes, modified_ns,
            capture_date, capture_time, width, height, camera_make, camera_model,
            lens, latitude, longitude, thumb, portrait_thumb, error, indexed_at
        ) VALUES(
            :relative_path, :name, :extension, :kind, :size_bytes, :modified_ns,
            :capture_date, :capture_time, :width, :height, :camera_make, :camera_model,
            :lens, :latitude, :longitude, :thumb, :portrait_thumb, :error, :indexed_at
        )
        ON CONFLICT(relative_path) DO UPDATE SET
            name = excluded.name,
            extension = excluded.extension,
            kind = excluded.kind,
            size_bytes = excluded.size_bytes,
            modified_ns = excluded.modified_ns,
            capture_date = excluded.capture_date,
            capture_time = excluded.capture_time,
            width = excluded.width,
            height = excluded.height,
            camera_make = excluded.camera_make,
            camera_model = excluded.camera_model,
            lens = excluded.lens,
            latitude = excluded.latitude,
            longitude = excluded.longitude,
            thumb = excluded.thumb,
            portrait_thumb = excluded.portrait_thumb,
            error = excluded.error,
            indexed_at = excluded.indexed_at
        """,
        values,
    )
    media_id = int(
        conn.execute(
            "SELECT id FROM media WHERE relative_path = ?", (values["relative_path"],)
        ).fetchone()[0]
    )
    if kind == "video":
        conn.execute(
            """
            INSERT INTO video_meta(
                media_id, duration_sec, fps, rotation, video_codec, audio_codec,
                has_audio, creation_time, encoder, location, camera, description,
                metadata_modified_ns
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(media_id) DO UPDATE SET
                duration_sec = excluded.duration_sec,
                fps = excluded.fps,
                rotation = excluded.rotation,
                video_codec = excluded.video_codec,
                audio_codec = excluded.audio_codec,
                has_audio = excluded.has_audio,
                creation_time = excluded.creation_time,
                encoder = excluded.encoder,
                location = excluded.location,
                camera = CASE WHEN excluded.camera = '' THEN video_meta.camera ELSE excluded.camera END,
                description = CASE WHEN excluded.description = '' THEN video_meta.description ELSE excluded.description END,
                metadata_modified_ns = CASE
                    WHEN excluded.metadata_modified_ns > 0 THEN excluded.metadata_modified_ns
                    ELSE video_meta.metadata_modified_ns
                END
            """,
            (
                media_id,
                row.get("duration_sec") or row.get("duration"),
                row.get("fps"),
                row.get("rotation"),
                str(row.get("video_codec") or ""),
                str(row.get("audio_codec") or ""),
                int(row.get("has_audio") or 0),
                str(row.get("creation_time") or ""),
                str(row.get("encoder") or ""),
                str(row.get("location") or ""),
                str(row.get("camera") or ""),
                str(row.get("description") or ""),
                int(row.get("metadata_modified_ns") or 0),
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO image_meta(media_id, exif_rating) VALUES(?, ?)
            ON CONFLICT(media_id) DO UPDATE SET exif_rating = excluded.exif_rating
            """,
            (media_id, int(row.get("exif_rating") or row.get("rating") or 0)),
        )
    sync_tags(conn, media_id, row.get("tags"), "exif")
    return media_id


def sync_tags(
    conn: sqlite3.Connection,
    media_id: int,
    tags: object,
    source: str,
) -> None:
    conn.execute(
        "DELETE FROM media_tags WHERE media_id = ? AND source = ?", (media_id, source)
    )
    conn.executemany(
        "INSERT OR IGNORE INTO media_tags(media_id, tag, source) VALUES(?, ?, ?)",
        [(media_id, tag, source) for tag in split_tags(tags)],
    )


def delete_missing(conn: sqlite3.Connection, kind: str, relative_paths: Iterable[str]) -> None:
    keep = list(relative_paths)
    if not keep:
        # An empty scan is more likely a transient mount failure than an empty library.
        return
    placeholders = ",".join("?" for _ in keep)
    conn.execute(
        f"DELETE FROM media WHERE kind = ? AND relative_path NOT IN ({placeholders})",
        (kind, *keep),
    )


def fetch_media(conn: sqlite3.Connection, kind: str) -> list[dict]:
    if kind == "video":
        query = """
            SELECT m.*, m.capture_date AS date, m.capture_time AS time,
                   m.camera_make AS make, m.camera_model AS model,
                   COALESCE((SELECT group_concat(tag, ', ') FROM (
                       SELECT tag FROM media_tags
                       WHERE media_id = m.id AND source = 'exif' ORDER BY tag
                   )), '') AS tags,
                   COALESCE(u.rating, 0) AS rating, COALESCE(u.note, '') AS note,
                   COALESCE(u.keywords, '') AS keywords,
                   COALESCE(u.featured, 0) AS featured,
                   u.latitude AS user_latitude, u.longitude AS user_longitude,
                   u.updated_at AS user_updated_at, m.error AS probe_error,
                   v.duration_sec, v.fps, v.rotation, v.video_codec, v.audio_codec,
                   v.has_audio, v.creation_time, v.encoder, v.location, v.camera,
                   v.description, v.metadata_modified_ns,
                   t.relative_path AS thumbnail_path,
                   t.status AS thumbnail_error, t.dhash, t.brightness,
                   t.contrast, t.sharpness, t.colorfulness, t.cover_score,
                   t.orientation, t.analysis_modified_ns
            FROM media m
            LEFT JOIN user_metadata u ON u.media_id = m.id
            LEFT JOIN video_meta v ON v.media_id = m.id
            LEFT JOIN thumbnails t
                ON t.media_id = m.id AND t.profile = 'landscape'
            WHERE m.kind = 'video'
            ORDER BY m.relative_path COLLATE NOCASE
        """
    else:
        query = """
        SELECT
            m.*,
            m.capture_date AS date,
            m.capture_time AS time,
            m.camera_make AS make,
            m.camera_model AS model,
            COALESCE((
                SELECT group_concat(tag, ', ')
                FROM (
                    SELECT tag FROM media_tags
                    WHERE media_id = m.id AND source = 'exif' ORDER BY tag
                )
            ), '') AS tags,
            COALESCE(u.rating, im.exif_rating, 0) AS rating,
            COALESCE(u.note, '') AS note,
            COALESCE(u.keywords, '') AS keywords,
            COALESCE(u.featured, 0) AS featured,
            u.latitude AS user_latitude,
            u.longitude AS user_longitude,
            u.updated_at AS user_updated_at
        FROM media m
        LEFT JOIN user_metadata u ON u.media_id = m.id
        LEFT JOIN image_meta im ON im.media_id = m.id
        WHERE m.kind = 'image'
        ORDER BY m.relative_path COLLATE NOCASE
        """
    return [dict(row) for row in conn.execute(query)]


def user_metadata_map(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute(
        """
        SELECT m.relative_path, u.rating, u.note, u.keywords, u.featured,
               u.latitude, u.longitude, u.updated_at
        FROM user_metadata u
        JOIN media m ON m.id = u.media_id
        ORDER BY m.relative_path COLLATE NOCASE
        """
    )
    return {
        row["relative_path"]: {
            "rating": int(row["rating"] or 0),
            "note": str(row["note"] or ""),
            "keywords": str(row["keywords"] or ""),
            "featured": bool(row["featured"]),
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "updated_at": str(row["updated_at"] or ""),
        }
        for row in rows
    }


def set_user_metadata(
    conn: sqlite3.Connection,
    relative_path: str,
    values: dict,
) -> dict:
    row = conn.execute(
        "SELECT id FROM media WHERE relative_path = ?", (relative_path,)
    ).fetchone()
    if not row:
        raise FileNotFoundError(relative_path)
    media_id = int(row[0])
    existing_row = conn.execute(
        "SELECT * FROM user_metadata WHERE media_id = ?", (media_id,)
    ).fetchone()
    existing = dict(existing_row) if existing_row else {}
    merged = {
        "rating": int(values.get("rating", existing.get("rating", 0)) or 0),
        "note": str(values.get("note", existing.get("note", "")) or ""),
        "keywords": str(values.get("keywords", existing.get("keywords", "")) or ""),
        "featured": int(bool(values.get("featured", existing.get("featured", 0)))),
        "latitude": values.get("latitude", existing.get("latitude")),
        "longitude": values.get("longitude", existing.get("longitude")),
        "updated_at": str(values.get("updated_at") or now_iso()),
    }
    conn.execute(
        """
        INSERT INTO user_metadata(
            media_id, rating, note, keywords, featured, latitude, longitude, updated_at
        ) VALUES(:media_id, :rating, :note, :keywords, :featured, :latitude, :longitude, :updated_at)
        ON CONFLICT(media_id) DO UPDATE SET
            rating = excluded.rating,
            note = excluded.note,
            keywords = excluded.keywords,
            featured = excluded.featured,
            latitude = excluded.latitude,
            longitude = excluded.longitude,
            updated_at = excluded.updated_at
        """,
        {"media_id": media_id, **merged},
    )
    sync_tags(conn, media_id, merged["keywords"], "user")
    return merged


def import_user_metadata_json(conn: sqlite3.Connection, path: Path) -> int:
    if not path.is_file() or conn.execute("SELECT 1 FROM user_metadata LIMIT 1").fetchone():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    imported = 0
    for relative_path, values in payload.items():
        if not isinstance(values, dict):
            continue
        try:
            set_user_metadata(conn, str(relative_path), values)
            imported += 1
        except FileNotFoundError:
            continue
    conn.commit()
    return imported


def export_user_metadata(
    conn: sqlite3.Connection,
    json_path: Path,
    csv_path: Path,
) -> dict[str, dict]:
    payload = user_metadata_map(conn)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = json_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, json_path)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        fields = [
            "relative_path",
            "rating",
            "note",
            "keywords",
            "featured",
            "latitude",
            "longitude",
            "updated_at",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for relative_path, values in payload.items():
            writer.writerow({"relative_path": relative_path, **values})
    return payload


def write_csv(path: Path, rows: Iterable[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def migrate_legacy_images(conn: sqlite3.Connection) -> int:
    if conn.execute("SELECT 1 FROM media WHERE kind = 'image' LIMIT 1").fetchone():
        return 0
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'images'"
    ).fetchone()
    if not exists:
        return 0
    imported = 0
    for row in conn.execute("SELECT * FROM images"):
        item = dict(row)
        item["exif_rating"] = item.get("rating")
        upsert_media(conn, "image", item)
        imported += 1
    conn.execute("DROP TABLE images")
    conn.commit()
    return imported
