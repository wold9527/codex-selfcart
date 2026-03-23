"""
应用设置存储
- 基于 SQLite 的 key-value 设置
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from database import get_db, init_db


init_db()


def _now() -> str:
    return datetime.now().isoformat()


def get_setting(key: str, default: Any = None) -> Any:
    with get_db() as conn:
        row = conn.execute(
            "SELECT value_json FROM app_settings WHERE key=?",
            ((key or "").strip(),),
        ).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value_json"])
    except Exception:
        return default


def set_setting(key: str, value: Any):
    _key = (key or "").strip()
    if not _key:
        raise ValueError("设置 key 不能为空")
    payload = json.dumps(value, ensure_ascii=False)
    now = _now()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO app_settings(key, value_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
            (_key, payload, now),
        )


def delete_setting(key: str) -> bool:
    _key = (key or "").strip()
    if not _key:
        return False
    with get_db() as conn:
        conn.execute("DELETE FROM app_settings WHERE key=?", (_key,))
    return True


def list_settings(prefix: str = "") -> dict:
    _prefix = (prefix or "").strip()
    with get_db() as conn:
        if _prefix:
            rows = conn.execute(
                "SELECT key, value_json FROM app_settings WHERE key LIKE ? ORDER BY key ASC",
                (_prefix + "%",),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT key, value_json FROM app_settings ORDER BY key ASC"
            ).fetchall()

    out = {}
    for row in rows:
        try:
            out[row["key"]] = json.loads(row["value_json"])
        except Exception:
            out[row["key"]] = None
    return out

