"""
代理管理
- 代理列表 CRUD
- 默认代理管理
- 代理连通性测试
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from urllib.parse import urlparse, unquote

from database import get_db, init_db
from http_client import create_http_session


init_db()


def _now() -> str:
    return datetime.now().isoformat()


def _proxy_url_from_parts(
    proxy_type: str,
    host: str,
    port: int,
    username: str = "",
    password: str = "",
) -> str:
    scheme = (proxy_type or "http").strip().lower()
    if scheme not in ("http", "socks5", "socks5h"):
        scheme = "http"
    auth = ""
    if username:
        auth = username
        if password:
            auth += f":{password}"
        auth += "@"
    return f"{scheme}://{auth}{host}:{int(port)}"


def _row_to_dict(row, include_secret: bool = False) -> dict:
    data = dict(row)
    username = data.get("username") or ""
    password = data.get("password") or ""
    data["is_enabled"] = bool(data.get("is_enabled", 0))
    data["is_default"] = bool(data.get("is_default", 0))
    data["proxy_url"] = _proxy_url_from_parts(
        data.get("proxy_type") or "http",
        data.get("proxy_host") or "",
        data.get("proxy_port") or 0,
        username,
        password,
    )
    data["has_password"] = bool(password)
    if not include_secret:
        data["password"] = ""
    return data


def parse_proxy_url(proxy_url: str) -> dict:
    raw = (proxy_url or "").strip()
    if not raw:
        raise ValueError("代理地址不能为空")

    if "://" not in raw:
        raw = "http://" + raw

    p = urlparse(raw)
    if not p.hostname or not p.port:
        raise ValueError("代理格式错误，应为 http://host:port 或 socks5://host:port")

    proxy_type = (p.scheme or "http").lower()
    if proxy_type not in ("http", "socks5", "socks5h"):
        raise ValueError(f"不支持的代理类型: {proxy_type}")

    return {
        "proxy_type": proxy_type,
        "proxy_host": p.hostname,
        "proxy_port": int(p.port),
        "username": unquote(p.username) if p.username else "",
        "password": unquote(p.password) if p.password else "",
    }


def list_proxies(enabled_only: bool = False, include_secret: bool = False) -> list[dict]:
    with get_db() as conn:
        if enabled_only:
            rows = conn.execute(
                "SELECT * FROM proxies WHERE is_enabled=1 "
                "ORDER BY is_default DESC, priority ASC, id ASC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM proxies "
                "ORDER BY is_default DESC, is_enabled DESC, priority ASC, id ASC"
            ).fetchall()
    return [_row_to_dict(r, include_secret=include_secret) for r in rows]


def get_proxy(proxy_id: int, include_secret: bool = True) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM proxies WHERE id=?", (proxy_id,)).fetchone()
    return _row_to_dict(row, include_secret=include_secret) if row else None


def get_default_proxy(enabled_only: bool = True, include_secret: bool = True) -> Optional[dict]:
    with get_db() as conn:
        if enabled_only:
            row = conn.execute(
                "SELECT * FROM proxies WHERE is_default=1 AND is_enabled=1 LIMIT 1"
            ).fetchone()
            if row:
                return _row_to_dict(row, include_secret=include_secret)
            row = conn.execute(
                "SELECT * FROM proxies WHERE is_enabled=1 ORDER BY priority ASC, id ASC LIMIT 1"
            ).fetchone()
            return _row_to_dict(row, include_secret=include_secret) if row else None
        row = conn.execute(
            "SELECT * FROM proxies WHERE is_default=1 LIMIT 1"
        ).fetchone()
        return _row_to_dict(row, include_secret=include_secret) if row else None


def _ensure_single_default(conn, proxy_id: int):
    conn.execute("UPDATE proxies SET is_default=0")
    conn.execute("UPDATE proxies SET is_default=1 WHERE id=?", (proxy_id,))


def _reselect_default(conn):
    row = conn.execute(
        "SELECT id FROM proxies WHERE is_enabled=1 ORDER BY priority ASC, id ASC LIMIT 1"
    ).fetchone()
    if not row:
        row = conn.execute("SELECT id FROM proxies ORDER BY id ASC LIMIT 1").fetchone()
    if row:
        _ensure_single_default(conn, int(row["id"]))


def create_proxy(
    *,
    name: str,
    proxy_type: str,
    host: str,
    port: int,
    username: str = "",
    password: str = "",
    is_enabled: bool = True,
    is_default: bool = False,
    priority: int = 100,
) -> int:
    proxy_name = (name or "").strip()
    if not proxy_name:
        raise ValueError("代理名称不能为空")
    if not host or not str(host).strip():
        raise ValueError("代理主机不能为空")
    if int(port) <= 0:
        raise ValueError("代理端口无效")

    scheme = (proxy_type or "http").strip().lower()
    if scheme not in ("http", "socks5", "socks5h"):
        raise ValueError(f"不支持的代理类型: {scheme}")

    now = _now()
    with get_db() as conn:
        existed = conn.execute(
            "SELECT id FROM proxies WHERE lower(name)=lower(?) LIMIT 1",
            (proxy_name,),
        ).fetchone()
        if existed:
            raise ValueError("代理名称已存在")

        cur = conn.execute(
            "INSERT INTO proxies "
            "(name, proxy_type, proxy_host, proxy_port, username, password, is_enabled, is_default, priority, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
            (
                proxy_name,
                scheme,
                str(host).strip(),
                int(port),
                (username or "").strip(),
                (password or "").strip(),
                1 if is_enabled else 0,
                int(priority),
                now,
                now,
            ),
        )
        new_id = int(cur.lastrowid)

        count_row = conn.execute("SELECT COUNT(1) AS n FROM proxies").fetchone()
        should_default = bool(is_default) or (count_row and int(count_row["n"]) == 1)
        if should_default:
            _ensure_single_default(conn, new_id)

    return new_id


def create_proxy_from_url(
    *,
    name: str,
    proxy_url: str,
    is_enabled: bool = True,
    is_default: bool = False,
    priority: int = 100,
) -> int:
    parsed = parse_proxy_url(proxy_url)
    return create_proxy(
        name=name,
        proxy_type=parsed["proxy_type"],
        host=parsed["proxy_host"],
        port=parsed["proxy_port"],
        username=parsed["username"],
        password=parsed["password"],
        is_enabled=is_enabled,
        is_default=is_default,
        priority=priority,
    )


def update_proxy(
    proxy_id: int,
    *,
    name: str = None,
    proxy_type: str = None,
    host: str = None,
    port: int = None,
    username: str = None,
    password: str = None,
    is_enabled: bool = None,
    is_default: bool = None,
    priority: int = None,
) -> bool:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM proxies WHERE id=?", (proxy_id,)).fetchone()
        if not row:
            return False

        fields = []
        params = []

        if name is not None:
            new_name = (name or "").strip()
            if not new_name:
                raise ValueError("代理名称不能为空")
            existed = conn.execute(
                "SELECT id FROM proxies WHERE lower(name)=lower(?) AND id<>? LIMIT 1",
                (new_name, proxy_id),
            ).fetchone()
            if existed:
                raise ValueError("代理名称已存在")
            fields.append("name=?")
            params.append(new_name)

        if proxy_type is not None:
            scheme = (proxy_type or "").strip().lower()
            if scheme not in ("http", "socks5", "socks5h"):
                raise ValueError(f"不支持的代理类型: {scheme}")
            fields.append("proxy_type=?")
            params.append(scheme)

        if host is not None:
            _host = str(host).strip()
            if not _host:
                raise ValueError("代理主机不能为空")
            fields.append("proxy_host=?")
            params.append(_host)

        if port is not None:
            _port = int(port)
            if _port <= 0:
                raise ValueError("代理端口无效")
            fields.append("proxy_port=?")
            params.append(_port)

        if username is not None:
            fields.append("username=?")
            params.append((username or "").strip())

        if password is not None:
            fields.append("password=?")
            params.append((password or "").strip())

        if is_enabled is not None:
            fields.append("is_enabled=?")
            params.append(1 if is_enabled else 0)

        if priority is not None:
            fields.append("priority=?")
            params.append(int(priority))

        if not fields and is_default is None:
            return False

        if fields:
            fields.append("updated_at=?")
            params.append(_now())
            params.append(proxy_id)
            conn.execute(f"UPDATE proxies SET {', '.join(fields)} WHERE id=?", params)

        if is_default is True:
            _ensure_single_default(conn, proxy_id)
        elif is_default is False:
            conn.execute("UPDATE proxies SET is_default=0 WHERE id=?", (proxy_id,))
            _reselect_default(conn)

    return True


def set_default_proxy(proxy_id: int) -> bool:
    with get_db() as conn:
        row = conn.execute("SELECT id FROM proxies WHERE id=?", (proxy_id,)).fetchone()
        if not row:
            return False
        _ensure_single_default(conn, proxy_id)
    return True


def delete_proxy(proxy_id: int) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, is_default FROM proxies WHERE id=?",
            (proxy_id,),
        ).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM proxies WHERE id=?", (proxy_id,))
        if bool(row["is_default"]):
            _reselect_default(conn)
    return True


def mark_proxy_used(proxy_id: int) -> bool:
    with get_db() as conn:
        row = conn.execute("SELECT id FROM proxies WHERE id=?", (proxy_id,)).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE proxies SET last_used_at=?, updated_at=? WHERE id=?",
            (_now(), _now(), proxy_id),
        )
    return True


def test_proxy_by_url(proxy_url: str, timeout: int = 10) -> dict:
    try:
        s = create_http_session(proxy=proxy_url)
        r = s.get("https://api.ipify.org?format=json", timeout=timeout)
        if r.status_code == 200:
            ip = r.json().get("ip", "")
            return {"success": True, "ip": ip}
        return {"success": False, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def test_proxy(proxy_id: int, timeout: int = 10) -> dict:
    proxy = get_proxy(proxy_id, include_secret=True)
    if not proxy:
        return {"success": False, "error": "代理不存在"}
    return test_proxy_by_url(proxy.get("proxy_url", ""), timeout=timeout)

