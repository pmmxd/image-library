from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS albums (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            data_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def _read_legacy(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("albums", []) if isinstance(data.get("albums"), list) else []
    except Exception:
        return []


def save_albums(path: Path, albums: list[dict]) -> list[dict]:
    now = datetime.now().isoformat(timespec="seconds")
    with connect(path) as conn:
        conn.execute("DELETE FROM albums")
        conn.executemany(
            "INSERT INTO albums(id,name,type,data_json,updated_at) VALUES(?,?,?,?,?)",
            [
                (
                    str(album.get("id", "")),
                    str(album.get("name", "")),
                    str(album.get("type", "manual")),
                    json.dumps(album, ensure_ascii=False),
                    now,
                )
                for album in albums
                if album.get("id")
            ],
        )
        conn.commit()
    return albums


def load_albums(path: Path, legacy_path: Path) -> list[dict]:
    with connect(path) as conn:
        rows = list(conn.execute("SELECT data_json FROM albums ORDER BY name COLLATE NOCASE"))
    if rows:
        albums = []
        for row in rows:
            try:
                albums.append(json.loads(row["data_json"]))
            except json.JSONDecodeError:
                continue
        return albums
    legacy = _read_legacy(legacy_path)
    if legacy:
        save_albums(path, legacy)
    return legacy
