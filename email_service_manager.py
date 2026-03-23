"""
邮箱服务管理
- 邮箱服务 CRUD
- 默认服务选择
- 连通性测试
"""
import json
from datetime import datetime
from typing import Optional

from database import get_db, init_db
from mail_provider import MailProvider


init_db()


def _now() -> str:
    return datetime.now().isoformat()


def _row_to_dict(row) -> dict:
    data = dict(row)
    try:
        data["config"] = json.loads(data.get("config_json") or "{}")
    except Exception:
        data["config"] = {}
    data["is_enabled"] = bool(data.get("is_enabled", 0))
    return data


def list_email_services(enabled_only: bool = False) -> list[dict]:
    with get_db() as conn:
        if enabled_only:
            rows = conn.execute(
                "SELECT * FROM email_services WHERE is_enabled=1 ORDER BY priority ASC, id ASC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM email_services ORDER BY is_enabled DESC, priority ASC, id ASC"
            ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_email_service(service_id: int) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM email_services WHERE id=?", (service_id,)).fetchone()
    return _row_to_dict(row) if row else None


def get_default_email_service(service_type: str = "mail_worker") -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM email_services "
            "WHERE is_enabled=1 AND service_type=? "
            "ORDER BY priority ASC, id ASC LIMIT 1",
            (service_type,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def create_email_service(
    *,
    name: str,
    service_type: str,
    config: dict,
    is_enabled: bool = True,
    priority: int = 100,
) -> int:
    svc_name = (name or "").strip()
    svc_type = (service_type or "").strip()
    if not svc_name:
        raise ValueError("服务名称不能为空")
    if not svc_type:
        raise ValueError("服务类型不能为空")

    now = _now()
    with get_db() as conn:
        existed = conn.execute(
            "SELECT id FROM email_services WHERE lower(name)=lower(?) LIMIT 1",
            (svc_name,),
        ).fetchone()
        if existed:
            raise ValueError("服务名称已存在")

        cur = conn.execute(
            "INSERT INTO email_services "
            "(name, service_type, config_json, is_enabled, priority, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                svc_name,
                svc_type,
                json.dumps(config or {}, ensure_ascii=False),
                1 if is_enabled else 0,
                int(priority),
                now,
                now,
            ),
        )
        return cur.lastrowid


def update_email_service(
    service_id: int,
    *,
    name: str = None,
    config: dict = None,
    is_enabled: bool = None,
    priority: int = None,
) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, name, config_json FROM email_services WHERE id=?",
            (service_id,),
        ).fetchone()
        if not row:
            return False

        fields = []
        params = []

        if name is not None:
            new_name = (name or "").strip()
            if not new_name:
                raise ValueError("服务名称不能为空")
            existed = conn.execute(
                "SELECT id FROM email_services WHERE lower(name)=lower(?) AND id<>? LIMIT 1",
                (new_name, service_id),
            ).fetchone()
            if existed:
                raise ValueError("服务名称已存在")
            fields.append("name=?")
            params.append(new_name)

        if config is not None:
            try:
                current_config = json.loads(row["config_json"] or "{}")
            except Exception:
                current_config = {}
            merged = dict(current_config)
            for k, v in (config or {}).items():
                if v is None:
                    continue
                merged[k] = v
            fields.append("config_json=?")
            params.append(json.dumps(merged, ensure_ascii=False))

        if is_enabled is not None:
            fields.append("is_enabled=?")
            params.append(1 if is_enabled else 0)

        if priority is not None:
            fields.append("priority=?")
            params.append(int(priority))

        if not fields:
            return False

        fields.append("updated_at=?")
        params.append(_now())
        params.append(service_id)

        conn.execute(
            f"UPDATE email_services SET {', '.join(fields)} WHERE id=?",
            params,
        )
    return True


def delete_email_service(service_id: int) -> bool:
    with get_db() as conn:
        conn.execute("DELETE FROM email_services WHERE id=?", (service_id,))
    return True


def _update_test_result(service_id: int, ok: bool, error_msg: str = ""):
    with get_db() as conn:
        conn.execute(
            "UPDATE email_services SET last_test_status=?, last_error=?, last_test_at=?, updated_at=? WHERE id=?",
            (
                "ok" if ok else "failed",
                error_msg[:500] if error_msg else "",
                _now(),
                _now(),
                service_id,
            ),
        )


def test_email_service(service_id: int, timeout: int = 30) -> dict:
    service = get_email_service(service_id)
    if not service:
        return {"success": False, "error": "邮箱服务不存在"}

    service_type = service.get("service_type")
    cfg = service.get("config") or {}

    if service_type != "mail_worker":
        msg = f"暂不支持测试该服务类型: {service_type}"
        _update_test_result(service_id, False, msg)
        return {"success": False, "error": msg}

    worker_domain = (cfg.get("worker_domain") or "").strip()
    admin_token = (cfg.get("admin_token") or "").strip()
    email_domain = (cfg.get("email_domain") or "").strip()

    if not (worker_domain and admin_token and email_domain):
        msg = "服务配置不完整 (worker_domain/admin_token/email_domain)"
        _update_test_result(service_id, False, msg)
        return {"success": False, "error": msg}

    try:
        mp = MailProvider(worker_domain, admin_token, email_domain)
        mailbox = mp.create_mailbox()
        _update_test_result(service_id, True, "")
        return {"success": True, "mailbox": mailbox}
    except Exception as e:
        msg = str(e)
        _update_test_result(service_id, False, msg)
        return {"success": False, "error": msg}
