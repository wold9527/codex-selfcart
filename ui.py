"""
自动化绑卡支付 - Streamlit UI
运行: streamlit run ui.py --server.address 0.0.0.0 --server.port 8503
"""
import json
import logging
import os
import sys
import traceback
import threading
from datetime import datetime
from collections import deque

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config, CardInfo, BillingInfo, CaptchaConfig
from mail_provider import MailProvider
from auth_flow import AuthFlow, AuthResult, RegistrationRetryPolicy
from payment_flow import PaymentFlow
from logger import ResultStore
from database import init_db
from code_manager import validate_code, reserve_use, complete_use, update_execution, get_code_history, get_code_info
from email_service_manager import (
    list_email_services,
    get_email_service,
    get_default_email_service,
    create_email_service,
    update_email_service,
    delete_email_service,
    test_email_service,
)
from proxy_manager import (
    list_proxies,
    get_proxy,
    get_default_proxy as get_default_proxy_config,
    create_proxy,
    create_proxy_from_url,
    update_proxy,
    delete_proxy,
    set_default_proxy,
    mark_proxy_used,
    test_proxy,
)
from settings_store import get_setting, set_setting

init_db()

# ── 兑换码系统开关: 在 config.json 中设置 "code_system_enabled": true 开启 ──
_ENABLE_CODE_SYSTEM = False
try:
    _cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if os.path.isfile(_cfg_path):
        with open(_cfg_path, encoding="utf-8") as _f:
            _ENABLE_CODE_SYSTEM = bool(json.load(_f).get("code_system_enabled", False))
except Exception:
    pass

OUTPUT_DIR = "test_outputs"
REG_SETTING_FLOW_ATTEMPTS = "registration.max_flow_attempts"
REG_SETTING_OTP_ATTEMPTS = "registration.max_otp_attempts"
REG_SETTING_SESSION_ATTEMPTS = "registration.max_session_attempts"


def _sanitize_error(raw_error: str) -> str:
    """将技术性错误信息转为用户友好的简要提示"""
    if not raw_error:
        return "执行失败"
    e = raw_error.lower()
    if "payment element" in e or "stripe" in e and "未加载" in raw_error:
        return "支付页面加载失败，请稍后重试"
    if "cloudflare" in e or "请稍候" in raw_error or "just a moment" in e:
        return "网络验证失败，请稍后重试"
    if "支付被拒" in raw_error or "card_declined" in e or "declined" in e:
        return "支付被拒，请检查卡片信息"
    if "用户手动终止" in raw_error:
        return "已取消"
    if "session_token" in e or "sentinel" in e or "403" in raw_error:
        return "登录凭证失效，请更换 Token"
    if "curl" in e or "url rejected" in e or "connection" in e or "timeout" in e:
        return "网络连接失败，请检查代理配置"
    if "captcha" in e or "hcaptcha" in e:
        return "人机验证失败，请重试"
    if "oom" in e or "memory" in e:
        return "服务器资源不足，请稍后重试"
    if "额度" in raw_error or "已用完" in raw_error:
        return raw_error  # 兑换码相关信息直接显示
    # 兜底: 只显示简要信息
    return "执行失败，请重试"


import re as _re

# 国家名/后缀 → (country_code, currency) 映射
_COUNTRY_ALIAS = {
    "UK": ("GB", "GBP"), "GB": ("GB", "GBP"), "England": ("GB", "GBP"), "United Kingdom": ("GB", "GBP"), "英国": ("GB", "GBP"),
    "US": ("US", "USD"), "USA": ("US", "USD"), "United States": ("US", "USD"), "美国": ("US", "USD"),
    "DE": ("DE", "EUR"), "Germany": ("DE", "EUR"), "德国": ("DE", "EUR"),
    "JP": ("JP", "JPY"), "Japan": ("JP", "JPY"), "日本": ("JP", "JPY"),
    "FR": ("FR", "EUR"), "France": ("FR", "EUR"), "法国": ("FR", "EUR"),
    "SG": ("SG", "SGD"), "Singapore": ("SG", "SGD"), "新加坡": ("SG", "SGD"),
    "HK": ("HK", "HKD"), "Hong Kong": ("HK", "HKD"), "香港": ("HK", "HKD"),
    "KR": ("KR", "KRW"), "Korea": ("KR", "KRW"), "韩国": ("KR", "KRW"),
    "AU": ("AU", "AUD"), "Australia": ("AU", "AUD"), "澳大利亚": ("AU", "AUD"),
    "CA": ("CA", "CAD"), "Canada": ("CA", "CAD"), "加拿大": ("CA", "CAD"),
    "NL": ("NL", "EUR"), "Netherlands": ("NL", "EUR"), "荷兰": ("NL", "EUR"),
    "IT": ("IT", "EUR"), "Italy": ("IT", "EUR"), "意大利": ("IT", "EUR"),
    "ES": ("ES", "EUR"), "Spain": ("ES", "EUR"), "西班牙": ("ES", "EUR"),
    "CH": ("CH", "CHF"), "Switzerland": ("CH", "CHF"), "瑞士": ("CH", "CHF"),
}


def _parse_card_text(text: str) -> dict:
    """从粘贴文本中解析卡号、有效期、CVV、账单地址。
    支持两种格式:
    1) 纯文本: 卡号一行、MM/YY一行、CVV一行、账单地址一行
    2) 键值对: 卡号: xxx / 有效期: MMYY / CVV: xxx / 地址: xxx / 城市: xxx / 邮编: xxx / 国家: xxx
    """
    result = {}
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]

    # 构建键值映射 (支持 "键: 值" 和 "键：值")
    kv = {}
    for line in lines:
        m = _re.match(r'^(.+?)\s*[:：]\s*(.+)$', line)
        if m:
            kv[m.group(1).strip().lower()] = m.group(2).strip()

    # ── 卡号 ──
    # 从键值对获取
    for k in ("卡号", "card number", "card", "card_number"):
        if k in kv:
            digits = kv[k].replace(" ", "").replace("-", "")
            if digits.isdigit() and 13 <= len(digits) <= 19:
                result["card_number"] = digits
                break

    # 检查 "cardnum MM YY CVC" 单行格式 (如 "5481087136282260 03 32 221")
    if "card_number" not in result:
        for line in lines:
            m = _re.match(r'^(\d{13,19})\s+(0[1-9]|1[0-2])\s+(\d{2,4})\s+(\d{3,4})$', line.replace("-", "").strip())
            if m:
                result["card_number"] = m.group(1)
                result["exp_month"] = m.group(2)
                yr = m.group(3)
                if len(yr) == 2:
                    yr = "20" + yr
                result["exp_year"] = yr
                result["cvv"] = m.group(4)
                break

    # 回退: 纯数字行
    if "card_number" not in result:
        for line in lines:
            digits_only = line.replace(" ", "").replace("-", "")
            if digits_only.isdigit() and 13 <= len(digits_only) <= 19:
                result["card_number"] = digits_only
                break

    # ── 有效期 ──
    # 从键值对获取 (支持 MMYY, MM/YY, MM/YYYY)
    for k in ("有效期", "exp", "expiry", "expiration", "exp_date"):
        if k in kv:
            val = kv[k]
            # MM/YY 或 MM/YYYY
            m = _re.search(r'(0[1-9]|1[0-2])\s*/\s*(\d{2,4})', val)
            if m:
                result["exp_month"] = m.group(1)
                yr = m.group(2)
                if len(yr) == 2:
                    yr = "20" + yr
                result["exp_year"] = yr
                break
            # MMYY 或 MMYYYY (无分隔符)
            m = _re.search(r'^(0[1-9]|1[0-2])(\d{2,4})$', val.strip())
            if m:
                result["exp_month"] = m.group(1)
                yr = m.group(2)
                if len(yr) == 2:
                    yr = "20" + yr
                result["exp_year"] = yr
                break
    # 回退: 逐行寻找 MM/YY
    if "exp_month" not in result:
        for line in lines:
            m = _re.search(r'\b(0[1-9]|1[0-2])\s*/\s*(\d{2,4})\b', line)
            if m:
                result["exp_month"] = m.group(1)
                yr = m.group(2)
                if len(yr) == 2:
                    yr = "20" + yr
                result["exp_year"] = yr
                break

    # ── CVV ──
    for k in ("cvv", "cvc", "安全码"):
        if k in kv:
            m = _re.search(r'\b(\d{3,4})\b', kv[k])
            if m:
                result["cvv"] = m.group(1)
                break
    if "cvv" not in result:
        for i, line in enumerate(lines):
            if _re.search(r'(?i)\b(?:cvv|cvc|安全码)\b', line):
                m = _re.search(r'\b(\d{3,4})\b', line)
                if m:
                    result["cvv"] = m.group(1)
                elif i + 1 < len(lines):
                    m2 = _re.search(r'\b(\d{3,4})\b', lines[i + 1])
                    if m2:
                        result["cvv"] = m2.group(1)
                break

    # ── 地址: 键值对模式 (地址/城市/州/邮编/国家 分字段) ──
    kv_addr = None
    for k in ("地址", "address", "address_line1"):
        if k in kv:
            kv_addr = kv[k]
            break
    kv_city = None
    for k in ("城市", "city"):
        if k in kv:
            kv_city = kv[k]
            break
    kv_state = None
    for k in ("州", "state", "省"):
        if k in kv:
            kv_state = kv[k]
            break
    kv_zip = None
    for k in ("邮编", "postal_code", "zip", "zipcode", "zip_code"):
        if k in kv:
            kv_zip = kv[k]
            break
    kv_country = None
    for k in ("国家", "country", "地区"):
        if k in kv:
            kv_country = kv[k]
            break

    if kv_addr:
        result["address_line1"] = kv_addr
        if kv_city:
            result["address_city"] = kv_city
            result["address_state"] = kv_state or kv_city
        elif kv_state:
            result["address_state"] = kv_state
        if kv_zip:
            result["postal_code"] = kv_zip
        if kv_country:
            ci = _COUNTRY_ALIAS.get(kv_country)
            if ci:
                result["country_code"] = ci[0]
                result["currency"] = ci[1]
        # 构建 raw_address
        parts = [kv_addr]
        if kv_city:
            parts.append(kv_city)
        if kv_state:
            parts.append(kv_state)
        if kv_zip:
            parts.append(kv_zip)
        if kv_country:
            parts.append(kv_country)
        result["raw_address"] = ", ".join(parts)

    # ── 地址: 回退 "账单地址" / "billing address" 单行模式 ──
    if "address_line1" not in result:
        addr_text = ""
        for i, line in enumerate(lines):
            if _re.search(r'(?i)账单地址|billing\s*address', line):
                after = _re.sub(r'(?i)^.*?(账单地址|billing\s*address)\s*[:：]?\s*', '', line).strip()
                if after and len(after) > 3:
                    addr_text = after
                else:
                    for j in range(i + 1, min(i + 5, len(lines))):
                        candidate = lines[j]
                        if candidate and candidate not in ("复制", "copy", ""):
                            addr_text = candidate
                            break
                break

        if addr_text:
            result["raw_address"] = addr_text
            parts = [p.strip() for p in addr_text.split(",")]
            if len(parts) >= 2:
                last = parts[-1].strip()
                country_info = _COUNTRY_ALIAS.get(last)
                if country_info:
                    result["country_code"] = country_info[0]
                    result["currency"] = country_info[1]
                    parts = parts[:-1]

                for idx, p in enumerate(parts):
                    if _re.search(r'\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b', p.strip(), _re.IGNORECASE):
                        result["postal_code"] = p.strip()
                        parts.pop(idx)
                        break
                    elif _re.search(r'\b\d{5}(-\d{4})?\b', p.strip()):
                        result["postal_code"] = p.strip()
                        parts.pop(idx)
                        break
                    elif _re.search(r'\b\d{3}-\d{4}\b', p.strip()):
                        result["postal_code"] = p.strip()
                        parts.pop(idx)
                        break

                if len(parts) == 1:
                    result["address_line1"] = parts[0]
                elif len(parts) == 2:
                    result["address_line1"] = parts[0]
                    result["address_state"] = parts[1]
                elif len(parts) >= 3:
                    result["address_line1"] = parts[0]
                    result["address_state"] = parts[1]

    # ── 姓名 ──
    for k in ("姓名", "name", "cardholder", "持卡人"):
        if k in kv:
            result["billing_name"] = kv[k]
            break

    # ── 纯文本多行回退: 从非卡/非地址行中提取姓名 ──
    if "billing_name" not in result:
        for line in lines:
            # 跳过已被解析的行 (卡号行、地址行)
            stripped = line.strip()
            if not stripped:
                continue
            # 跳过纯数字/卡号行
            if _re.match(r'^[\d\s/\-]+$', stripped):
                continue
            # 跳过含邮编/地址的行
            if _re.search(r'\d{5}', stripped) and ',' in stripped:
                continue
            # 跳过键值对行
            if _re.match(r'^.+?[:：]', stripped):
                continue
            # 可能是姓名: 2-5 个英文单词 (首字母大写)
            if _re.match(r'^[A-Z][a-z]+(\s+[A-Z][a-z]+){0,4}$', stripped):
                result["billing_name"] = stripped
                break

    # ── 纯文本多行回退: 从第二行解析地址 (如 "38 Pearl Avenue, Louisville, MS 39339, US") ──
    if "address_line1" not in result and len(lines) >= 2:
        for line in lines:
            stripped = line.strip()
            # 跳过卡号行 (全数字+空格)
            if _re.match(r'^[\d\s/\-]+$', stripped):
                continue
            # 候选地址行: 含逗号、有数字(门牌号或邮编)
            if ',' in stripped and _re.search(r'\d', stripped):
                result["raw_address"] = stripped
                parts = [p.strip() for p in stripped.split(",")]
                # 检查最后部分是否是国家
                if len(parts) >= 2:
                    last = parts[-1].strip()
                    country_info = _COUNTRY_ALIAS.get(last)
                    if country_info:
                        result["country_code"] = country_info[0]
                        result["currency"] = country_info[1]
                        parts = parts[:-1]
                # 提取邮编
                for idx, p in enumerate(parts):
                    zip_match = _re.search(r'\b(\d{5}(?:-\d{4})?)\b', p)
                    if zip_match:
                        result["postal_code"] = zip_match.group(1)
                        # 带邮编的部分可能是 "MS 39339" 或 "Louisville, MS 39339"
                        # 提取 state 代码
                        state_match = _re.match(r'^([A-Z]{2})\s+\d{5}', p.strip())
                        if state_match:
                            result["address_state"] = state_match.group(1)
                            parts.pop(idx)
                        else:
                            # 邮编在地址部分中, 分离
                            clean = _re.sub(r'\s*\d{5}(?:-\d{4})?\s*', '', p).strip()
                            if clean:
                                parts[idx] = clean
                            else:
                                parts.pop(idx)
                        break
                # 分配剩余部分
                if len(parts) >= 1:
                    result["address_line1"] = parts[0]
                if len(parts) >= 2 and "address_state" not in result:
                    # 可能是 city 或 city, state
                    city_state = parts[1].strip()
                    csm = _re.match(r'^(.+?)\s+([A-Z]{2})$', city_state)
                    if csm:
                        result["address_city"] = csm.group(1)
                        result["address_state"] = csm.group(2)
                    else:
                        result["address_city"] = city_state
                elif len(parts) >= 2:
                    result["address_city"] = parts[1]
                break
            break

    return result


# 国家 → (code, currency, state, address, postal_code)
COUNTRY_MAP = {
    "US - 美国": ("US", "USD", "California", "123 Main St", "90001"),
    "DE - 德国": ("DE", "EUR", "Berlin", "Hauptstraße 1", "10115"),
    "JP - 日本": ("JP", "JPY", "Tokyo", "1-1-1 Shibuya", "150-0002"),
    "GB - 英国": ("GB", "GBP", "London", "10 Downing St", "SW1A 2AA"),
    "FR - 法国": ("FR", "EUR", "Paris", "1 Rue de Rivoli", "75001"),
    "SG - 新加坡": ("SG", "SGD", "Singapore", "1 Raffles Place", "048616"),
    "HK - 香港": ("HK", "HKD", "Hong Kong", "1 Queen's Road", "000000"),
    "KR - 韩国": ("KR", "KRW", "Seoul", "1 Gangnam-daero", "06000"),
    "AU - 澳大利亚": ("AU", "AUD", "NSW", "1 George St", "2000"),
    "CA - 加拿大": ("CA", "CAD", "Ontario", "123 King St", "M5H 1A1"),
    "NL - 荷兰": ("NL", "EUR", "Amsterdam", "Damrak 1", "1012 LG"),
    "IT - 意大利": ("IT", "EUR", "Rome", "Via Roma 1", "00100"),
    "ES - 西班牙": ("ES", "EUR", "Madrid", "Calle Mayor 1", "28013"),
    "CH - 瑞士": ("CH", "CHF", "Zurich", "Bahnhofstrasse 1", "8001"),
}

st.set_page_config(page_title="Let's ABC", page_icon="A", layout="wide")

# ── CSS ──
st.markdown("""
<style>
    .block-container { max-width: 1100px; padding-top: 1.5rem; }
    /* 更精细的排版 */
    .stRadio > label { font-weight: 500; letter-spacing: 0.02em; }
    .stRadio [data-baseweb="radio"] { gap: 0.3rem; }
    .stTabs [data-baseweb="tab-list"] { gap: 0; border-bottom: 1px solid rgba(255,255,255,0.08); }
    .stTabs [data-baseweb="tab"] {
        padding: 0.6rem 1.5rem; font-weight: 500; letter-spacing: 0.05em;
        border-bottom: 2px solid transparent; transition: all 0.2s;
    }
    .stTabs [aria-selected="true"] { border-bottom-color: #7c3aed; }
    /* 进度条渐变 */
    .stProgress > div > div > div { background: linear-gradient(90deg, #7c3aed, #3b82f6); }
    /* 按键圆角 */
    .stButton > button { border-radius: 8px; font-weight: 500; letter-spacing: 0.03em; transition: all 0.15s; }
    .stButton > button[kind="primary"] { background: linear-gradient(135deg, #7c3aed, #6d28d9); border: none; }
    .stButton > button[kind="primary"]:hover { background: linear-gradient(135deg, #6d28d9, #5b21b6); }
    /* 输入框 */
    .stTextInput > div > div > input { border-radius: 6px; }
    /* Expander 样式 */
    .streamlit-expanderHeader { font-weight: 500; letter-spacing: 0.02em; }
    /* 分割线淡化 */
    hr { opacity: 0.15; }
</style>
""", unsafe_allow_html=True)

# 后台日志缓存 — 使用 cache_resource 确保跨 rerun 同一对象
@st.cache_resource
def _get_log_shared():
    return {"cache": deque(maxlen=5000), "lock": threading.Lock()}

_log_shared = _get_log_shared()


# ── 日志 ──
class LogCapture(logging.Handler):
    def __init__(self, shared):
        super().__init__()
        self._cache = shared["cache"]
        self._lock = shared["lock"]
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S"))

    def emit(self, record):
        msg = self.format(record)
        with self._lock:
            self._cache.append(msg)


def pull_captured_logs():
    """将后台日志搬运到 session_state，需在主线程调用。"""
    if "log_buffer" not in st.session_state:
        st.session_state.log_buffer = []
    cache = _log_shared["cache"]
    lock = _log_shared["lock"]
    with lock:
        if not cache:
            return
        st.session_state.log_buffer.extend(list(cache))
        cache.clear()


def clear_captured_logs():
    cache = _log_shared["cache"]
    lock = _log_shared["lock"]
    with lock:
        cache.clear()


def init_logging():
    handler = LogCapture(_log_shared)
    handler.setLevel(logging.INFO)
    handler._is_log_capture = True
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [h for h in root.handlers if not getattr(h, '_is_log_capture', False)]
    root.addHandler(handler)
    # 同时输出到 stdout (systemd/journalctl 可读)
    if not any(isinstance(h, logging.StreamHandler) and not getattr(h, '_is_log_capture', False) for h in root.handlers):
        sh = logging.StreamHandler()
        sh.setLevel(logging.INFO)
        sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S"))
        root.addHandler(sh)
    logging.getLogger("watchdog").setLevel(logging.WARNING)


def _extract_mail_worker_config(service: dict) -> tuple[bool, str, dict]:
    if not service:
        return False, "邮箱服务不存在", {}
    if service.get("service_type") != "mail_worker":
        return False, f"暂不支持的服务类型: {service.get('service_type')}", {}
    cfg = service.get("config") or {}
    worker_domain = (cfg.get("worker_domain") or "").strip()
    admin_token = (cfg.get("admin_token") or "").strip()
    email_domain = (cfg.get("email_domain") or "").strip()
    if not (worker_domain and admin_token and email_domain):
        return False, "邮箱服务配置不完整", {}
    return True, "", {
        "worker_domain": worker_domain,
        "admin_token": admin_token,
        "email_domain": email_domain,
    }


def _int_setting(key: str, default: int, min_value: int, max_value: int) -> int:
    try:
        val = int(get_setting(key, default))
    except Exception:
        val = default
    return max(min_value, min(max_value, val))


def _get_registration_retry_defaults() -> tuple[int, int, int]:
    flow = _int_setting(REG_SETTING_FLOW_ATTEMPTS, 2, 1, 5)
    otp = _int_setting(REG_SETTING_OTP_ATTEMPTS, 2, 1, 5)
    session = _int_setting(REG_SETTING_SESSION_ATTEMPTS, 3, 1, 8)
    return flow, otp, session


def datetime_now_compact() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S")


def _render_proxy_selector(
    *,
    key_prefix: str,
    label: str = "代理",
    placeholder: str = "http://127.0.0.1:7897",
) -> tuple[str, int | None]:
    enabled = list_proxies(enabled_only=True, include_secret=True)
    default_proxy = get_default_proxy_config(enabled_only=True, include_secret=True)

    options = ["不使用代理", "手动输入"]
    id_map = {}
    for p in enabled:
        lb = f"#{p['id']} {p.get('name', '-')}" \
             f" ({p.get('proxy_type', 'http')}://{p.get('proxy_host', '')}:{p.get('proxy_port', '')})"
        options.append(lb)
        id_map[lb] = p["id"]

    default_idx = 0
    if default_proxy:
        for idx, opt in enumerate(options):
            if id_map.get(opt) == default_proxy["id"]:
                default_idx = idx
                break

    mode = st.selectbox(label, options, index=default_idx, key=f"{key_prefix}_proxy_mode")
    if mode == "不使用代理":
        return "", None
    if mode == "手动输入":
        manual = st.text_input(
            "手动代理地址",
            placeholder=placeholder,
            key=f"{key_prefix}_proxy_manual",
        ).strip()
        return manual, None

    proxy_id = id_map.get(mode)
    proxy_cfg = get_proxy(proxy_id, include_secret=True) if proxy_id else None
    return (proxy_cfg or {}).get("proxy_url", ""), proxy_id


def _normalize_plan_type(raw_plan: str) -> str:
    plan = (raw_plan or "").strip().lower()
    if "team" in plan or "business" in plan or "enterprise" in plan:
        return "Team"
    if "plus" in plan or "pro" in plan:
        return "Plus"
    if "free" in plan or "basic" in plan:
        return "Free"
    return "Unknown"


def _is_subscription_active(exec_status: str, result_data: dict) -> bool:
    rd = result_data or {}
    confirm_status = str(rd.get("confirm_status") or "").strip()
    confirm_resp = rd.get("confirm_response")
    error_msg = str(rd.get("error") or "")
    run_success = bool(rd.get("success"))

    if isinstance(confirm_resp, dict):
        if bool(confirm_resp.get("success")):
            return True
        status = str(confirm_resp.get("status") or "").lower()
        pi_status = str((confirm_resp.get("payment_intent") or {}).get("status") or "").lower()
        if status in ("complete", "succeeded"):
            return True
        if pi_status in ("succeeded", "processing"):
            return True
        if status == "open" and pi_status == "succeeded":
            return True

    if confirm_status == "200" and not error_msg:
        return True

    has_confirm_signal = bool(confirm_status or confirm_resp)
    if run_success and has_confirm_signal:
        return True
    if (exec_status or "").lower() == "success" and has_confirm_signal:
        return True
    return False


def _build_account_records(history_rows: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for r in history_rows:
        raw_json = r.get("result_json")
        if not raw_json:
            continue
        try:
            rd = json.loads(raw_json)
        except Exception:
            continue
        email = (rd.get("email") or r.get("email") or "").strip()
        if not email:
            continue
        plan = _normalize_plan_type(r.get("plan_type") or rd.get("plan_type") or "")
        subscribed = _is_subscription_active(r.get("status", ""), rd)
        sub_label = plan if (subscribed and plan in ("Plus", "Team")) else ("已开通" if subscribed else "未开通")
        item = {
            "exec_id": r["id"],
            "email": email,
            "plan_type": plan,
            "subscribed": subscribed,
            "subscription_label": sub_label,
            "created_at": r.get("created_at", "")[:19],
            "exec_status": r.get("status", ""),
            "error_msg": r.get("error_msg", "") or rd.get("error", ""),
            "has_token": bool(rd.get("access_token")),
            "access_token": rd.get("access_token", ""),
            "session_token": rd.get("session_token", ""),
            "device_id": rd.get("device_id", ""),
            "_data": rd,
        }
        grouped.setdefault(email, []).append(item)

    records = []
    for email, rows in grouped.items():
        rows.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        latest = rows[0]
        subscribed_rows = [x for x in rows if x["subscribed"]]
        picked = subscribed_rows[0] if subscribed_rows else latest
        picked = dict(picked)
        picked["run_count"] = len(rows)
        picked["latest_status"] = latest["exec_status"]
        picked["latest_error"] = latest["error_msg"]
        records.append(picked)

    records.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return records


def _run_registration_once(
    email_service_id: int,
    proxy: str = "",
    flow_attempts: int = 2,
    otp_attempts: int = 2,
    session_attempts: int = 3,
) -> dict:
    service = get_email_service(email_service_id)
    ok, msg, mail_cfg = _extract_mail_worker_config(service)
    if not ok:
        raise RuntimeError(msg)

    cfg = Config()
    cfg.proxy = proxy or None
    cfg.mail.worker_domain = mail_cfg["worker_domain"]
    cfg.mail.admin_token = mail_cfg["admin_token"]
    cfg.mail.email_domain = mail_cfg["email_domain"]

    mail = MailProvider(
        worker_domain=cfg.mail.worker_domain,
        admin_token=cfg.mail.admin_token,
        email_domain=cfg.mail.email_domain,
    )
    af = AuthFlow(cfg)
    auth = af.run_register(
        mail,
        policy=RegistrationRetryPolicy(
            max_flow_attempts=max(1, int(flow_attempts)),
            max_otp_attempts=max(1, int(otp_attempts)),
            max_session_attempts=max(1, int(session_attempts)),
        ),
    )

    store = ResultStore(output_dir=OUTPUT_DIR)
    store.save_credentials(auth.to_dict())
    store.append_credentials_csv(auth.to_dict())
    return auth.to_dict()


def _render_registration_page():
    st.subheader("注册")
    services = [s for s in list_email_services(enabled_only=True) if s.get("service_type") == "mail_worker"]
    if not services:
        st.warning("暂无可用邮箱服务。请先到「邮箱服务」分类新增并启用服务。")
        return

    opts = {
        f"#{s['id']} {s.get('name', '-')}" +
        f" ({(s.get('config') or {}).get('email_domain', '-')})": s["id"]
        for s in services
    }
    selected_label = st.selectbox("邮箱服务", list(opts.keys()), key="reg_mail_service")
    selected_id = opts[selected_label]
    reg_proxy, reg_proxy_id = _render_proxy_selector(
        key_prefix="reg",
        label="注册代理",
        placeholder="http://127.0.0.1:7897",
    )

    default_flow, default_otp, default_session = _get_registration_retry_defaults()
    c1, c2, c3, c4 = st.columns(4)
    reg_count = c1.number_input("注册数量", min_value=1, max_value=20, value=1, step=1, key="reg_count")
    reg_flow_attempts = c2.number_input("全流程重试", min_value=1, max_value=5, value=default_flow, step=1, key="reg_flow_attempts")
    reg_otp_attempts = c3.number_input("OTP 重试", min_value=1, max_value=5, value=default_otp, step=1, key="reg_otp_attempts")
    reg_session_attempts = c4.number_input("Session 重试", min_value=1, max_value=8, value=default_session, step=1, key="reg_session_attempts")
    st.caption("建议值: 全流程=2, OTP=2, Session=3。网络不稳定时可适当提高。")

    if "reg_results" not in st.session_state:
        st.session_state.reg_results = []

    if st.button("开始注册", type="primary", use_container_width=True, key="reg_start_btn"):
        results = []
        for idx in range(int(reg_count)):
            with st.spinner(f"注册中... ({idx + 1}/{int(reg_count)})"):
                try:
                    if reg_proxy_id:
                        mark_proxy_used(reg_proxy_id)
                    auth_dict = _run_registration_once(
                        selected_id,
                        reg_proxy,
                        int(reg_flow_attempts),
                        int(reg_otp_attempts),
                        int(reg_session_attempts),
                    )
                    results.append({
                        "status": "success",
                        "email": auth_dict.get("email", ""),
                        "session_token": auth_dict.get("session_token", ""),
                        "access_token": auth_dict.get("access_token", ""),
                        "device_id": auth_dict.get("device_id", ""),
                        "error": "",
                    })
                except Exception as e:
                    results.append({
                        "status": "failed",
                        "email": "",
                        "session_token": "",
                        "access_token": "",
                        "device_id": "",
                        "error": str(e),
                    })
        st.session_state.reg_results = results

    rows = st.session_state.get("reg_results", [])
    if rows:
        import pandas as pd
        st.divider()
        st.dataframe(
            pd.DataFrame([
                {
                    "状态": "✅ 成功" if r["status"] == "success" else "❌ 失败",
                    "邮箱": r["email"] or "-",
                    "错误": _sanitize_error(r.get("error", "")) if r["status"] == "failed" else "",
                }
                for r in rows
            ]),
            hide_index=True,
            use_container_width=True,
        )
        ok_count = len([r for r in rows if r["status"] == "success"])
        st.caption(f"完成: {ok_count}/{len(rows)}")


def _render_email_service_page():
    st.subheader("邮箱服务")
    st.caption("参考 codex-console2 的服务化思路：服务配置集中管理，注册/支付页面只做引用。")

    with st.expander("从 config.json 导入 Worker 配置", expanded=False):
        if st.button("导入为邮箱服务", key="import_mail_cfg_btn"):
            try:
                cfg = Config.from_file("config.json")
                mail_cfg = {
                    "worker_domain": cfg.mail.worker_domain,
                    "admin_token": cfg.mail.admin_token,
                    "email_domain": cfg.mail.email_domain,
                }
                if not all(mail_cfg.values()):
                    st.error("config.json 的 mail 配置不完整，导入失败")
                else:
                    create_email_service(
                        name="Config 导入服务",
                        service_type="mail_worker",
                        config=mail_cfg,
                        is_enabled=True,
                        priority=100,
                    )
                    st.success("已导入邮箱服务")
                    st.rerun()
            except Exception as e:
                st.error(f"导入失败: {e}")

    with st.expander("新增邮箱服务", expanded=True):
        col1, col2 = st.columns(2)
        svc_name = col1.text_input("服务名称", value="默认 Worker 服务", key="new_svc_name")
        svc_priority = col2.number_input("优先级(越小越优先)", min_value=1, max_value=9999, value=100, key="new_svc_pri")
        w1, w2, w3 = st.columns(3)
        worker_domain = w1.text_input("worker_domain", placeholder="https://mail-worker.example.com", key="new_worker_domain")
        admin_token = w2.text_input("admin_token", type="password", key="new_admin_token")
        email_domain = w3.text_input("email_domain", placeholder="example.com", key="new_email_domain")
        enabled = st.checkbox("启用", value=True, key="new_svc_enabled")

        if st.button("新增服务", type="primary", key="create_svc_btn"):
            if not (svc_name.strip() and worker_domain.strip() and admin_token.strip() and email_domain.strip()):
                st.error("请完整填写服务名称与 Worker 配置")
            else:
                try:
                    create_email_service(
                        name=svc_name.strip(),
                        service_type="mail_worker",
                        config={
                            "worker_domain": worker_domain.strip(),
                            "admin_token": admin_token.strip(),
                            "email_domain": email_domain.strip(),
                        },
                        is_enabled=enabled,
                        priority=int(svc_priority),
                    )
                    st.success("邮箱服务已新增")
                    st.rerun()
                except Exception as e:
                    st.error(f"新增失败: {e}")

    services = list_email_services(enabled_only=False)
    if not services:
        st.info("暂无邮箱服务配置")
        return

    import pandas as pd
    st.divider()
    st.dataframe(
        pd.DataFrame([
            {
                "ID": s["id"],
                "名称": s["name"],
                "类型": s["service_type"],
                "启用": "是" if s["is_enabled"] else "否",
                "优先级": s.get("priority", 100),
                "域名": (s.get("config") or {}).get("email_domain", "-"),
                "最近测试": s.get("last_test_status") or "-",
            }
            for s in services
        ]),
        hide_index=True,
        use_container_width=True,
    )

    for svc in services:
        cfg = svc.get("config") or {}
        with st.expander(f"#{svc['id']} {svc['name']} ({'启用' if svc['is_enabled'] else '禁用'})", expanded=False):
            c1, c2, c3 = st.columns(3)
            name = c1.text_input("服务名称", value=svc["name"], key=f"svc_name_{svc['id']}")
            priority = c2.number_input("优先级", min_value=1, max_value=9999, value=int(svc.get("priority", 100)), key=f"svc_pri_{svc['id']}")
            is_enabled = c3.checkbox("启用", value=svc["is_enabled"], key=f"svc_enabled_{svc['id']}")

            w1, w2, w3 = st.columns(3)
            worker_domain = w1.text_input("worker_domain", value=cfg.get("worker_domain", ""), key=f"svc_wd_{svc['id']}")
            admin_token = w2.text_input("admin_token", value=cfg.get("admin_token", ""), type="password", key=f"svc_token_{svc['id']}")
            email_domain = w3.text_input("email_domain", value=cfg.get("email_domain", ""), key=f"svc_domain_{svc['id']}")

            b1, b2, b3 = st.columns(3)
            if b1.button("保存", key=f"svc_save_{svc['id']}"):
                try:
                    update_email_service(
                        svc["id"],
                        name=name,
                        priority=int(priority),
                        is_enabled=is_enabled,
                        config={
                            "worker_domain": worker_domain.strip(),
                            "admin_token": admin_token.strip(),
                            "email_domain": email_domain.strip(),
                        },
                    )
                    st.success("已保存")
                    st.rerun()
                except Exception as e:
                    st.error(f"保存失败: {e}")
            if b2.button("测试", key=f"svc_test_{svc['id']}"):
                rs = test_email_service(svc["id"])
                if rs.get("success"):
                    st.success(f"测试成功，创建邮箱: {rs.get('mailbox')}")
                else:
                    st.error(f"测试失败: {rs.get('error', '')}")
                st.rerun()
            if b3.button("删除", key=f"svc_del_{svc['id']}"):
                delete_email_service(svc["id"])
                st.warning("已删除")
                st.rerun()


def _render_account_management_page():
    st.subheader("账号管理")
    tab_accounts, tab_history = st.tabs(["账号", "执行历史"])

    with tab_accounts:
        history = get_code_history(st.session_state.verified_code)
        records = _build_account_records(history)
        if not records:
            st.info("暂无账号记录")
            return

        subscribed_count = len([x for x in records if x["subscribed"]])
        unsubscribed_count = len(records) - subscribed_count
        token_count = len([x for x in records if x["has_token"]])

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("账号总数", len(records))
        c2.metric("已开通订阅", subscribed_count)
        c3.metric("未开通订阅", unsubscribed_count)
        c4.metric("可用 Token", token_count)
        if st.button("刷新数据", key="acct_mgmt_refresh_btn"):
            st.rerun()

        st.caption("订阅状态优先依据 Stripe confirm 返回判断，不再仅按执行状态着色。")

        f1, f2, f3 = st.columns([3, 1, 1])
        search_kw = f1.text_input("搜索邮箱", placeholder="输入邮箱关键字", key="acct_mgmt_search").strip().lower()
        plan_filter = f2.selectbox("计划筛选", ["全部", "Team", "Plus", "Free", "Unknown"], key="acct_mgmt_plan")
        sub_filter = f3.selectbox("订阅筛选", ["全部", "已开通", "未开通"], key="acct_mgmt_sub")

        filtered = []
        for row in records:
            if search_kw and search_kw not in row["email"].lower():
                continue
            if plan_filter != "全部" and row["plan_type"] != plan_filter:
                continue
            if sub_filter == "已开通" and not row["subscribed"]:
                continue
            if sub_filter == "未开通" and row["subscribed"]:
                continue
            filtered.append(row)

        import pandas as pd

        st.dataframe(
            pd.DataFrame([
                {
                    "邮箱": r["email"],
                    "计划": r["plan_type"],
                    "订阅": ("🟢 " + r["subscription_label"]) if r["subscribed"] else "🔴 未开通",
                    "Token": "✅" if r["has_token"] else "❌",
                    "执行次数": r["run_count"],
                    "最近执行": r["created_at"],
                    "最近状态": {"success": "✅ 成功", "failed": "❌ 失败", "running": "🔄 运行中", "pending": "⏳ 等待"}.get(r["latest_status"], r["latest_status"]),
                }
                for r in filtered
            ]),
            hide_index=True,
            use_container_width=True,
        )
        st.caption(f"共 {len(filtered)}/{len(records)} 个账号")

        st.divider()
        for idx, acct in enumerate(filtered):
            exp_title = f"{acct['email']}  {'🟢' if acct['subscribed'] else '🔴'} {acct['subscription_label']}"
            with st.expander(exp_title, expanded=False):
                x1, x2, x3 = st.columns(3)
                x1.text_input("计划", value=acct["plan_type"], disabled=True, key=f"acct_plan_{idx}")
                x2.text_input("最近执行", value=acct["created_at"], disabled=True, key=f"acct_time_{idx}")
                x3.text_input("执行次数", value=str(acct["run_count"]), disabled=True, key=f"acct_runs_{idx}")
                if acct["latest_error"]:
                    st.warning(_sanitize_error(acct["latest_error"]))
                if acct["has_token"]:
                    st.code(
                        f"access_token: {acct.get('access_token', '')}\n"
                        f"session_token: {acct.get('session_token', '')}\n"
                        f"device_id: {acct.get('device_id', '')}",
                        language="yaml",
                    )
                else:
                    st.caption("无可用 Token")
                with st.expander("查看原始结果", expanded=False):
                    st.json(acct["_data"])

    with tab_history:
        _history = get_code_history(st.session_state.verified_code)
        if not _history:
            st.info("暂无执行历史")
            return
        import pandas as pd
        st.dataframe(
            pd.DataFrame([
                {
                    "状态": {"success": "✅ 成功", "failed": "❌ 失败", "running": "🔄 运行中", "pending": "⏳ 等待"}.get(r["status"], r["status"]),
                    "邮箱": r.get("email") or "-",
                    "计划": r.get("plan_type") or "-",
                    "备注": _sanitize_error(r.get("error_msg") or "") if r["status"] == "failed" else "",
                    "时间": r["created_at"][:19],
                }
                for r in _history
            ]),
            hide_index=True,
            use_container_width=True,
        )
        st.caption(f"共 {len(_history)} 条记录")


def _render_settings_page():
    st.subheader("设置")
    tab_proxy, tab_reg, tab_sys = st.tabs(["代理设置", "注册配置", "系统信息"])

    with tab_proxy:
        st.caption("参考 codex-console2：代理集中管理，执行页/注册页仅选择使用。")

        with st.expander("导入现有代理", expanded=False):
            cfg_proxy = ""
            try:
                cfg_proxy = (Config.from_file("config.json").proxy or "").strip()
            except Exception:
                cfg_proxy = ""
            manual_proxy = (st.session_state.get("w_proxy_manual", "") or "").strip()
            reg_manual_proxy = (st.session_state.get("reg_proxy_manual", "") or "").strip()

            if cfg_proxy:
                st.code(cfg_proxy, language="bash")
                if st.button("导入 config.json 代理", key="import_cfg_proxy_btn"):
                    try:
                        create_proxy_from_url(
                            name=f"ConfigProxy-{datetime_now_compact()}",
                            proxy_url=cfg_proxy,
                            is_enabled=True,
                            is_default=(get_default_proxy_config(enabled_only=False) is None),
                            priority=100,
                        )
                        st.success("已导入 config.json 代理")
                        st.rerun()
                    except Exception as e:
                        st.error(f"导入失败: {e}")
            else:
                st.caption("config.json 中未检测到代理配置")

            if manual_proxy or reg_manual_proxy:
                to_import = manual_proxy or reg_manual_proxy
                st.code(to_import, language="bash")
                if st.button("导入当前手动代理", key="import_manual_proxy_btn"):
                    try:
                        create_proxy_from_url(
                            name=f"ManualProxy-{datetime_now_compact()}",
                            proxy_url=to_import,
                            is_enabled=True,
                            is_default=(get_default_proxy_config(enabled_only=False) is None),
                            priority=100,
                        )
                        st.success("已导入手动代理")
                        st.rerun()
                    except Exception as e:
                        st.error(f"导入失败: {e}")

        with st.expander("新增代理", expanded=True):
            a1, a2, a3, a4 = st.columns([3, 1, 3, 2])
            p_name = a1.text_input("代理名称", value="默认代理", key="new_proxy_name")
            p_type = a2.selectbox("类型", ["http", "socks5"], key="new_proxy_type")
            p_host = a3.text_input("Host", placeholder="127.0.0.1", key="new_proxy_host")
            p_port = a4.number_input("Port", min_value=1, max_value=65535, value=7897, key="new_proxy_port")
            b1, b2, b3, b4 = st.columns([2, 2, 1, 1])
            p_user = b1.text_input("用户名(可选)", key="new_proxy_user")
            p_pass = b2.text_input("密码(可选)", type="password", key="new_proxy_pass")
            p_enabled = b3.checkbox("启用", value=True, key="new_proxy_enabled")
            p_default = b4.checkbox("默认", value=False, key="new_proxy_default")
            p_priority = st.number_input("优先级(越小越优先)", min_value=1, max_value=9999, value=100, key="new_proxy_priority")
            if st.button("新增代理", type="primary", key="create_proxy_btn"):
                try:
                    create_proxy(
                        name=p_name,
                        proxy_type=p_type,
                        host=p_host,
                        port=int(p_port),
                        username=p_user,
                        password=p_pass,
                        is_enabled=p_enabled,
                        is_default=p_default,
                        priority=int(p_priority),
                    )
                    st.success("代理已新增")
                    st.rerun()
                except Exception as e:
                    st.error(f"新增失败: {e}")

        proxies = list_proxies(enabled_only=False, include_secret=True)
        if not proxies:
            st.info("暂无代理配置")
        else:
            import pandas as pd

            st.dataframe(
                pd.DataFrame([
                    {
                        "ID": p["id"],
                        "名称": p["name"],
                        "类型": p["proxy_type"],
                        "地址": f"{p['proxy_host']}:{p['proxy_port']}",
                        "默认": "是" if p["is_default"] else "否",
                        "启用": "是" if p["is_enabled"] else "否",
                        "最后使用": (p.get("last_used_at") or "")[:19] if p.get("last_used_at") else "-",
                    }
                    for p in proxies
                ]),
                hide_index=True,
                use_container_width=True,
            )

            for p in proxies:
                with st.expander(
                    f"#{p['id']} {p['name']} ({'默认' if p['is_default'] else '非默认'} / {'启用' if p['is_enabled'] else '禁用'})",
                    expanded=False,
                ):
                    c1, c2, c3, c4 = st.columns([3, 1, 3, 2])
                    e_name = c1.text_input("名称", value=p["name"], key=f"proxy_name_{p['id']}")
                    e_type = c2.selectbox("类型", ["http", "socks5"], index=0 if p["proxy_type"] == "http" else 1, key=f"proxy_type_{p['id']}")
                    e_host = c3.text_input("Host", value=p["proxy_host"], key=f"proxy_host_{p['id']}")
                    e_port = c4.number_input("Port", min_value=1, max_value=65535, value=int(p["proxy_port"]), key=f"proxy_port_{p['id']}")
                    d1, d2, d3 = st.columns(3)
                    e_user = d1.text_input("用户名", value=p.get("username") or "", key=f"proxy_user_{p['id']}")
                    e_pass = d2.text_input("密码(留空不改)", value="", type="password", key=f"proxy_pass_{p['id']}")
                    e_pri = d3.number_input("优先级", min_value=1, max_value=9999, value=int(p.get("priority", 100)), key=f"proxy_pri_{p['id']}")
                    e_enabled = st.checkbox("启用", value=bool(p["is_enabled"]), key=f"proxy_enabled_{p['id']}")

                    k1, k2, k3, k4 = st.columns(4)
                    if k1.button("保存", key=f"proxy_save_{p['id']}"):
                        try:
                            update_proxy(
                                p["id"],
                                name=e_name,
                                proxy_type=e_type,
                                host=e_host,
                                port=int(e_port),
                                username=e_user,
                                password=(e_pass if e_pass else None),
                                is_enabled=e_enabled,
                                priority=int(e_pri),
                            )
                            st.success("已保存")
                            st.rerun()
                        except Exception as e:
                            st.error(f"保存失败: {e}")
                    if k2.button("测试", key=f"proxy_test_{p['id']}"):
                        rs = test_proxy(p["id"])
                        if rs.get("success"):
                            st.success(f"连通成功，出口 IP: {rs.get('ip', '-')}")
                        else:
                            st.error(f"连通失败: {rs.get('error', '')}")
                    if k3.button("设为默认", key=f"proxy_default_{p['id']}"):
                        set_default_proxy(p["id"])
                        st.success("已设为默认代理")
                        st.rerun()
                    if k4.button("删除", key=f"proxy_del_{p['id']}"):
                        delete_proxy(p["id"])
                        st.warning("已删除")
                        st.rerun()

    with tab_reg:
        st.caption("注册重试默认值（注册页和支付页的新注册模式会读取这些默认配置）。")
        d_flow, d_otp, d_session = _get_registration_retry_defaults()
        r1, r2, r3 = st.columns(3)
        s_flow = r1.number_input("全流程重试默认值", min_value=1, max_value=5, value=d_flow, key="set_reg_flow")
        s_otp = r2.number_input("OTP 重试默认值", min_value=1, max_value=5, value=d_otp, key="set_reg_otp")
        s_session = r3.number_input("Session 重试默认值", min_value=1, max_value=8, value=d_session, key="set_reg_session")
        if st.button("保存注册配置", type="primary", key="save_reg_defaults_btn"):
            set_setting(REG_SETTING_FLOW_ATTEMPTS, int(s_flow))
            set_setting(REG_SETTING_OTP_ATTEMPTS, int(s_otp))
            set_setting(REG_SETTING_SESSION_ATTEMPTS, int(s_session))
            st.success("注册配置已保存")

    with tab_sys:
        all_proxy_count = len(list_proxies(enabled_only=False))
        all_proxy_enabled = len(list_proxies(enabled_only=True))
        st.code(
            f"code_system_enabled={_ENABLE_CODE_SYSTEM}\n"
            f"dev_mode={'--dev' in sys.argv}\n"
            f"output_dir={OUTPUT_DIR}\n"
            f"proxy_total={all_proxy_count}\n"
            f"proxy_enabled={all_proxy_enabled}\n"
            f"email_services={len(list_email_services(enabled_only=False))}",
            language="ini",
        )


for k, v in {"log_buffer": [], "running": False, "result": None}.items():
    if k not in st.session_state:
        st.session_state[k] = v

# 每次 rerun 先同步一次日志缓存
pull_captured_logs()

# ── widget 默认值初始化 (只在首次运行时设置) ──
_widget_defaults = {
    "w_exp_month": "12",
    "w_exp_year": "2030",
    "w_proxy_manual": "",
    "reg_proxy_manual": "",
    "w_billing_name": "",
}
for _dk, _dv in _widget_defaults.items():
    if _dk not in st.session_state:
        st.session_state[_dk] = _dv

# ── 延迟的解析结果应用 (必须在 widget 渲染之前) ──
_parse_just_applied = False
if "_pending_parse" in st.session_state:
    _pp = st.session_state.pop("_pending_parse")
    for _pk, _pv in _pp.items():
        st.session_state[_pk] = _pv
    _parse_just_applied = True


# ════════════════════════════════════════
# 顶部
# ════════════════════════════════════════
st.markdown(
    '<h1 style="text-align:center;letter-spacing:3px;">'
    "Let's "
    '<span style="font-family:\'Courier New\',monospace;font-weight:900;'
    'background:linear-gradient(135deg,#7c3aed,#3b82f6);-webkit-background-clip:text;'
    '-webkit-text-fill-color:transparent;font-size:1.15em;">ABC</span>'
    ' <span style="font-size:0.5em;opacity:0.6;vertical-align:middle;">(ets-abc-auto-bind-card)</span>'
    '</h1>',
    unsafe_allow_html=True,
)

# ── 开发者模式: 启动时通过 -- --dev 参数开启 ──
# 用法: streamlit run ui.py -- --dev
dev_mode = "--dev" in sys.argv

# ═══════════════════════════════════════
# 兑换码验证门禁 (仅在 code_system_enabled=true 时启用)
# ═══════════════════════════════════════
if "verified_code" not in st.session_state:
    st.session_state.verified_code = "" if _ENABLE_CODE_SYSTEM else "__disabled__"

if _ENABLE_CODE_SYSTEM and not st.session_state.verified_code:
    st.markdown(
        '<div style="text-align:center;margin:40px 0 20px;opacity:0.7">输入兑换码开始使用</div>',
        unsafe_allow_html=True,
    )
    _code_col1, _code_col2 = st.columns([3, 1])
    with _code_col1:
        _input_code = st.text_input("兑换码", placeholder="XXXX-XXXX-XXXX", label_visibility="collapsed")
    with _code_col2:
        _verify_btn = st.button("验证", type="primary", use_container_width=True)
    if _verify_btn and _input_code:
        _valid, _msg = validate_code(_input_code.strip())
        if _valid:
            st.session_state.verified_code = _input_code.strip()
            st.rerun()
        else:
            st.error(_msg)
    st.stop()

# ── 已验证: 显示兑换码状态 ──
_code_info = get_code_info(st.session_state.verified_code) if _ENABLE_CODE_SYSTEM else None
if _code_info:
    _remaining = _code_info["total_uses"] - _code_info["used_count"]
    _status_col1, _status_col2 = st.columns([5, 1])
    with _status_col1:
        st.caption(f"兑换码: `{st.session_state.verified_code}` — 剩余 {_remaining}/{_code_info['total_uses']} 次")
    with _status_col2:
        if st.button("退出", key="logout_code"):
            st.session_state.verified_code = ""
            st.rerun()

# ── 顶部分类导航 ──
category = st.radio(
    "分类",
    ["注册", "账号管理", "邮箱服务", "支付绑卡", "设置"],
    index=3,
    horizontal=True,
)

if category == "注册":
    _render_registration_page()
    st.stop()
if category == "账号管理":
    _render_account_management_page()
    st.stop()
if category == "邮箱服务":
    _render_email_service_page()
    st.stop()
if category == "设置":
    _render_settings_page()
    st.stop()

# ── 账号来源选择 ──
# 从数据库获取当前兑换码的有 token 的执行记录 (用于「选择已有账号」)
_code_history = get_code_history(st.session_state.verified_code) if _ENABLE_CODE_SYSTEM else []
_code_success_creds = []
for _h in _code_history:
    if _h.get("result_json"):
        try:
            _rd = json.loads(_h["result_json"])
            if _rd.get("email") and _rd.get("access_token"):
                _code_success_creds.append(_rd)
        except Exception:
            pass

acct_col, proxy_col = st.columns([3, 2])
with acct_col:
    account_source = st.radio(
        "账号来源",
        ["新注册", "选择已有账号", "手动输入 Token"],
        index=1 if _code_success_creds else 0,
        horizontal=True,
    )
    do_register = account_source == "新注册"

do_checkout = True
do_payment = True
selected_proxy_id = None

if dev_mode:
    with proxy_col:
        sc1, sc2 = st.columns(2)
        do_checkout = sc1.checkbox("创建 Checkout", value=True)
        do_payment = sc2.checkbox("提交支付", value=True)

with proxy_col:
    proxy, selected_proxy_id = _render_proxy_selector(
        key_prefix="w",
        label="代理",
        placeholder="http://127.0.0.1:7897",
    )

# ── 已有账号选择 / Token 输入 ──
cred_email = ""
cred_session_token = ""
cred_access_token = ""
cred_device_id = ""
use_existing_creds = not do_register

if account_source == "选择已有账号":
    if _code_success_creds:
        _cred_options = {}
        for _cd in _code_success_creds:
            _label = f"{_cd.get('email', '未知')}"
            _cred_options[_label] = _cd
        if _cred_options:
            sel_label = st.selectbox("选择账号", list(_cred_options.keys()), key="w_acct_select")
            _sel_data = _cred_options[sel_label]
            cred_email = _sel_data.get("email", "")
            cred_session_token = _sel_data.get("session_token", "")
            cred_access_token = _sel_data.get("access_token", "")
            cred_device_id = _sel_data.get("device_id", "")
            with st.expander("查看凭证详情", expanded=False):
                st.json({k: (v[:40] + "..." if isinstance(v, str) and len(v) > 50 else v) for k, v in _sel_data.items()})
        else:
            st.warning("未找到有效的凭证")
    else:
        st.warning("暂无已注册的账号，请先选择「新注册」")

elif account_source == "手动输入 Token":
    cred_access_token = st.text_input("access_token", placeholder="eyJhbGciOi...", type="password", key="w_manual_at")
    cred_session_token = st.text_input("session_token", placeholder="eyJhbGciOi...", type="password", key="w_manual_st",
                                        help="浏览器 F12 → Application → Cookies → __Secure-next-auth.session-token")
    cred_email = st.text_input("邮箱 (可选)", placeholder="user@example.com", key="w_manual_email")

# ── 注册模式下选择邮箱服务 ──
selected_email_service_id = None
_def_flow, _def_otp, _def_session = _get_registration_retry_defaults()
reg_flow_attempts = _def_flow
reg_otp_attempts = _def_otp
reg_session_attempts = _def_session
if do_register:
    with st.expander("邮箱服务", expanded=True):
        active_mail_services = [s for s in list_email_services(enabled_only=True) if s.get("service_type") == "mail_worker"]
        if not active_mail_services:
            st.error("暂无可用邮箱服务。请先到「邮箱服务」分类新增并启用服务。")
        else:
            default_svc = get_default_email_service("mail_worker")
            labels = [
                f"#{s['id']} {s.get('name', '-')}"
                f" ({(s.get('config') or {}).get('email_domain', '-')})"
                for s in active_mail_services
            ]
            id_map = {labels[idx]: active_mail_services[idx]["id"] for idx in range(len(active_mail_services))}
            default_idx = 0
            if default_svc:
                for idx, s in enumerate(active_mail_services):
                    if s["id"] == default_svc["id"]:
                        default_idx = idx
                        break
            selected_label = st.selectbox("选择邮箱服务", labels, index=default_idx, key="w_reg_mail_service_sel")
            selected_email_service_id = id_map[selected_label]
            selected_svc = get_email_service(selected_email_service_id) or {}
            selected_cfg = selected_svc.get("config") or {}
            st.caption(
                f"服务类型: {selected_svc.get('service_type', '-')} | "
                f"域名: {selected_cfg.get('email_domain', '-')}"
            )
            r1, r2, r3 = st.columns(3)
            reg_flow_attempts = r1.number_input(
                "全流程重试",
                min_value=1,
                max_value=5,
                value=_def_flow,
                step=1,
                key="w_reg_flow_attempts",
                help="注册全链路失败后，最多重新走完整流程的次数。",
            )
            reg_otp_attempts = r2.number_input(
                "OTP 重试",
                min_value=1,
                max_value=5,
                value=_def_otp,
                step=1,
                key="w_reg_otp_attempts",
                help="验证码等待/校验失败时，单轮流程内重试次数。",
            )
            reg_session_attempts = r3.number_input(
                "Session 重试",
                min_value=1,
                max_value=8,
                value=_def_session,
                step=1,
                key="w_reg_session_attempts",
                help="建号后获取 session/access_token 的重试次数。",
            )


# 默认值 (非开发者模式下不显示这些设置)
use_browser_mode = True
captcha_key = ""
captcha_api_url = ""
# 计划类型选择 (始终可见)
plan_type_label = st.radio(
    "选择计划",
    ["Business · 团队版免费试用 1 个月", "Plus · 个人版免费试用 1 个月"],
    index=0,
    horizontal=True,
)
plan_type = "plus" if "Plus" in plan_type_label else "team"
if plan_type == "plus":
    workspace_name = ""
    seat_quantity = 0
    promo_campaign = "plus-1-month-free"
else:
    workspace_name = "MyWorkspace"
    seat_quantity = 5
    promo_campaign = "team-1-month-free"

if dev_mode:
    with st.expander("高级设置", expanded=False):
        adv_col1, adv_col2 = st.columns(2)
        with adv_col1:
            payment_mode = st.radio(
                "支付模式",
                ["浏览器模式 (推荐)", "API 模式"],
                index=0,
                horizontal=True,
            )
            use_browser_mode = payment_mode.startswith("浏览")
        with adv_col2:
            if use_browser_mode:
                import subprocess as _sp
                _xvfb_running = False
                try:
                    _xvfb_pids = _sp.check_output(["pgrep", "-f", "Xvfb :99"], stderr=_sp.DEVNULL).decode().strip()
                    _xvfb_running = bool(_xvfb_pids)
                except Exception:
                    pass
                if _xvfb_running:
                    st.success("Xvfb 运行中 (:99)")
                else:
                    st.info("将自动启动 Xvfb :99")
            else:
                st.info("API 模式")

        if not use_browser_mode:
            captcha_col1, captcha_col2 = st.columns([3, 1])
            with captcha_col1:
                captcha_key = st.text_input("YesCaptcha API Key", placeholder="your-yescaptcha-key", type="password")
            with captcha_col2:
                captcha_api_url = st.text_input("打码 API", value="https://api.yescaptcha.com")

        st.markdown("---")
        st.markdown("**邮箱 & 计划设置**")
        st.caption("邮箱配置已统一迁移到「邮箱服务」分类管理。")
        if plan_type == "team":
            adv_tc1, adv_tc2, adv_tc3 = st.columns(3)
            workspace_name = adv_tc1.text_input("Workspace", value="MyWorkspace")
            seat_quantity = adv_tc2.number_input("席位数", min_value=2, max_value=50, value=5)
            promo_campaign = adv_tc3.text_input("活动 ID", value="team-1-month-free")
        else:
            promo_campaign = st.text_input("活动 ID", value="plus-1-month-free")

st.divider()

# ════════════════════════════════════════
# 配置区: 卡片信息优先
# ════════════════════════════════════════

if do_payment:
    with st.expander("粘贴卡片信息", expanded=True):
        paste_text = st.text_area(
            "粘贴卡片/账单文本",
            height=120,
            placeholder="支持两种格式:\n\n格式1 (键值对):\n卡号: 4242424242424242\n有效期: 1230\nCVV: 123\n姓名: John Smith\n地址: 123 Main Street\n城市: San Francisco\n州: CA\n邮编: 94102\n国家: United States\n\n格式2 (纯文本):\n4242 4242 4242 4242\n12/30\nCVV 123",
            key="paste_card_text",
        )
        if st.button("识别并填充", key="parse_btn", disabled=not paste_text):
            parsed = _parse_card_text(paste_text)
            pending = {}
            if parsed.get("card_number"):
                pending["w_card_number"] = parsed["card_number"]
            if parsed.get("exp_month"):
                pending["w_exp_month"] = parsed["exp_month"]
            if parsed.get("exp_year"):
                pending["w_exp_year"] = parsed["exp_year"]
            if parsed.get("cvv"):
                pending["w_card_cvc"] = parsed["cvv"]
            if parsed.get("address_line1"):
                pending["w_address_line1"] = parsed["address_line1"]
            if parsed.get("address_city"):
                pending["w_address_city"] = parsed["address_city"]
            if parsed.get("address_state"):
                pending["w_address_state"] = parsed["address_state"]
            if parsed.get("postal_code"):
                pending["w_postal_code"] = parsed["postal_code"]
            if parsed.get("country_code"):
                cc = parsed["country_code"]
                for i, label in enumerate(COUNTRY_MAP.keys()):
                    if label.startswith(cc):
                        pending["w_country"] = label
                        break
            if parsed.get("currency"):
                pending["w_currency"] = parsed["currency"]
            if parsed.get("billing_name"):
                pending["w_billing_name"] = parsed["billing_name"]
            st.session_state["_pending_parse"] = pending
            filled = []
            if parsed.get("card_number"):
                filled.append(f"卡号: {parsed['card_number'][:4]}****{parsed['card_number'][-4:]}")
            if parsed.get("exp_month"):
                filled.append(f"有效期: {parsed['exp_month']}/{parsed['exp_year']}")
            if parsed.get("cvv"):
                filled.append(f"CVV: ***")
            if parsed.get("raw_address"):
                filled.append(f"地址: {parsed['raw_address']}")
            if parsed.get("billing_name"):
                filled.append(f"姓名: {parsed['billing_name']}")
            if filled:
                st.success("已识别: " + " | ".join(filled))
            else:
                st.warning("未能识别卡片信息，请检查文本格式")
            st.rerun()

cfg_col1, cfg_col2 = st.columns(2)

with cfg_col1:
    if do_payment:
        with st.expander("信用卡", expanded=True):
            TEST_CARDS = {
                "4242 4242 4242 4242 (Visa 标准)": ("4242424242424242", "123"),
                "4000 0000 0000 0002 (Visa 被拒)": ("4000000000000002", "123"),
                "4000 0000 0000 0069 (Visa 过期)": ("4000000000000069", "123"),
                "4000 0000 0000 9995 (Visa 余额不足)": ("4000000000009995", "123"),
                "5555 5555 5555 4444 (Mastercard)": ("5555555555554444", "123"),
                "5200 8282 8282 8210 (MC Debit)": ("5200828282828210", "123"),
                "2223 0031 2200 3222 (MC 2系列)": ("2223003122003222", "123"),
                "3782 822463 10005 (Amex)": ("378282246310005", "1234"),
            }
            tc_sel = st.selectbox("快速填充测试卡", ["不填充"] + list(TEST_CARDS.keys()), key="tc_sel")
            if tc_sel != "不填充":
                tc_num, tc_cvc = TEST_CARDS[tc_sel]
                st.session_state["w_card_number"] = tc_num
                st.session_state["w_card_cvc"] = tc_cvc

            cc1, cc2, cc3, cc4 = st.columns([5, 2, 2, 2])
            card_number = cc1.text_input("卡号", placeholder="真实卡号", key="w_card_number")
            exp_month = cc2.text_input("月", key="w_exp_month")
            exp_year = cc3.text_input("年", key="w_exp_year")
            card_cvc = cc4.text_input("CVC", key="w_card_cvc")

            if card_number and card_number.startswith("4"):
                st.caption("Live 模式下所有测试卡都会被拒绝，仅用于验证流程")
    else:
        card_number = exp_month = exp_year = card_cvc = ""

with cfg_col2:
    with st.expander("账单地址", expanded=True):
        # 如果有解析出的国家，自动选择对应国家
        country_label = st.selectbox("国家", list(COUNTRY_MAP.keys()), key="w_country")
        country_code, default_currency, default_state, default_addr, default_zip = COUNTRY_MAP[country_label]
        # 当国家变更时，更新地址默认值 (但不覆盖刚解析的值)
        _prev_country = st.session_state.get("_prev_country", "")
        if _prev_country and _prev_country != country_label and not _parse_just_applied:
            st.session_state["w_currency"] = default_currency
            st.session_state["w_address_line1"] = default_addr
            st.session_state["w_address_state"] = default_state
            st.session_state["w_postal_code"] = default_zip
        st.session_state["_prev_country"] = country_label
        bc1, bc2 = st.columns(2)
        billing_name = bc1.text_input("姓名", key="w_billing_name")
        if "w_currency" not in st.session_state:
            st.session_state["w_currency"] = default_currency
        currency = bc2.text_input("货币", key="w_currency")
        bc3, bc4, bc5, bc6 = st.columns(4)
        if "w_address_line1" not in st.session_state:
            st.session_state["w_address_line1"] = default_addr
        if "w_address_city" not in st.session_state:
            st.session_state["w_address_city"] = ""
        if "w_address_state" not in st.session_state:
            st.session_state["w_address_state"] = default_state
        if "w_postal_code" not in st.session_state:
            st.session_state["w_postal_code"] = default_zip
        address_line1 = bc3.text_input("地址", key="w_address_line1")
        address_city = bc4.text_input("城市", key="w_address_city")
        address_state = bc5.text_input("州/省", key="w_address_state")
        postal_code = bc6.text_input("邮编", key="w_postal_code")

st.divider()

# ════════════════════════════════════════
# Tab
# ════════════════════════════════════════
steps_list = []
if do_register: steps_list.append("注册")
if do_checkout: steps_list.append("Checkout")
if do_payment: steps_list.append("支付")

tab_run, tab_accounts, tab_history = st.tabs(["执行", "账号", "历史"])

# 日志关键词 → 进度百分比映射
_PROGRESS_KEYWORDS = [
    ("使用已有凭证", 5),
    ("邮箱创建成功", 3),
    ("注册完成", 10),
    ("创建 Checkout Session", 12),
    ("Checkout 创建成功", 18),
    ("启动 Chrome", 22),
    ("Chrome ready", 28),
    ("通过 Cloudflare", 32),
    ("Cloudflare 已通过", 38),
    ("加载 checkout 页面", 42),
    ("Stripe Payment Element", 48),
    ("Stripe Element 已加载", 55),
    ("填写卡片信息", 60),
    ("已输入卡号", 65),
    ("已输入 CVC", 70),
    ("填写账单地址", 73),
    ("地址-邮编", 78),
    ("已点击提交按钮", 82),
    ("等待支付处理", 85),
    ("hCaptcha", 88),
    ("checkbox 已点击", 92),
    ("支付成功", 98),
    ("支付被拒", 98),
    ("支付失败", 98),
]

def _calc_progress_pct():
    """根据 session_state.log_buffer (累积) 计算当前进度百分比"""
    pull_captured_logs()  # 先把 _LOG_CACHE 搬运到 log_buffer
    logs = st.session_state.get("log_buffer", [])
    if not logs:
        return 1
    text = "\n".join(logs[-30:])
    best = 1
    for keyword, pct in _PROGRESS_KEYWORDS:
        if keyword in text and pct > best:
            best = pct
    return best


def _run_flow_thread(rd, cs):
    """在后台线程中执行完整流程 (cs = config_snapshot)"""
    try:
        if cs.get("proxy_id"):
            mark_proxy_used(int(cs["proxy_id"]))
        cfg = Config()
        cfg.proxy = cs["proxy"]
        cfg.team_plan.workspace_name = cs["workspace_name"]
        cfg.team_plan.seat_quantity = cs["seat_quantity"]
        cfg.team_plan.promo_campaign_id = cs["promo_campaign"]
        cfg.captcha = CaptchaConfig(api_url=cs["captcha_api_url"], client_key=cs["captcha_key"])
        cfg.billing = BillingInfo(
            name=cs["billing_name"], email="",
            country=cs["country_code"], currency=cs["currency"],
            address_line1=cs["address_line1"], address_state=cs["address_state"],
            postal_code=cs["postal_code"])
        if cs["do_payment"]:
            cfg.card = CardInfo(number=cs["card_number"], cvc=cs["card_cvc"],
                                exp_month=cs["exp_month"], exp_year=cs["exp_year"])

        store = ResultStore(output_dir=OUTPUT_DIR)
        auth_result = None
        af = None

        if cs["do_register"]:
            svc = get_email_service(cs.get("email_service_id", 0) or 0)
            ok, msg, mail_cfg = _extract_mail_worker_config(svc)
            if not ok:
                raise RuntimeError(msg)
            cfg.mail.worker_domain = mail_cfg["worker_domain"]
            cfg.mail.admin_token = mail_cfg["admin_token"]
            cfg.mail.email_domain = mail_cfg["email_domain"]

            mp = MailProvider(worker_domain=cfg.mail.worker_domain, admin_token=cfg.mail.admin_token, email_domain=cfg.mail.email_domain)
            af = AuthFlow(cfg)
            auth_result = af.run_register(
                mp,
                policy=RegistrationRetryPolicy(
                    max_flow_attempts=max(1, int(cs.get("reg_flow_attempts", 2) or 2)),
                    max_otp_attempts=max(1, int(cs.get("reg_otp_attempts", 2) or 2)),
                    max_session_attempts=max(1, int(cs.get("reg_session_attempts", 3) or 3)),
                ),
            )
            rd["email"] = auth_result.email
            rd["session_token"] = auth_result.session_token
            rd["access_token"] = auth_result.access_token
            rd["device_id"] = auth_result.device_id
            store.save_credentials(auth_result.to_dict())
            store.append_credentials_csv(auth_result.to_dict())
        elif cs["use_existing_creds"] and cs["do_checkout"]:
            if not cs["cred_access_token"]:
                raise RuntimeError("必须提供 access_token")
            af = AuthFlow(cfg)
            auth_result = af.from_existing_credentials(
                session_token=cs["cred_session_token"],
                access_token=cs["cred_access_token"],
                device_id=cs["cred_device_id"],
            )
            auth_result.email = cs["cred_email"] or "unknown@example.com"
            rd["email"] = auth_result.email

        if cs["do_checkout"]:
            if not auth_result:
                raise RuntimeError("需先注册或提供凭证")

            if cs["use_browser_mode"] and cs["do_payment"]:
                import subprocess as _sp
                _xvfb_ok = False
                try:
                    _sp.check_output(["pgrep", "-f", "Xvfb :99"], stderr=_sp.DEVNULL)
                    _xvfb_ok = True
                except Exception:
                    pass
                if not _xvfb_ok:
                    _sp.Popen(["Xvfb", ":99", "-screen", "0", "1920x1080x24", "-ac"],
                              stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
                    import time as _t; _t.sleep(1)
                os.environ["DISPLAY"] = ":99"

                from browser_payment import BrowserPayment
                bp = BrowserPayment(proxy=cfg.proxy, headless=False, slow_mo=80)
                br = bp.run_full_flow(
                    session_token=auth_result.session_token,
                    access_token=auth_result.access_token,
                    device_id=auth_result.device_id,
                    card_number=cs["card_number"], card_exp_month=cs["exp_month"],
                    card_exp_year=cs["exp_year"], card_cvc=cs["card_cvc"],
                    billing_name=cs["billing_name"], billing_country=cs["country_code"],
                    billing_zip=cs["postal_code"], billing_line1=cs["address_line1"],
                    billing_city=cs["address_city"], billing_state=cs["address_state"],
                    billing_email=auth_result.email, billing_currency=cs["currency"],
                    chatgpt_proxy=cfg.proxy, timeout=120,
                    plan_type=cs["plan_type"],
                )
                rd["checkout_data"] = br.get("checkout_data")
                rd["checkout_session_id"] = br.get("checkout_data", {}).get("checkout_session_id", "")
                rd["success"] = br.get("success", False)
                rd["error"] = br.get("error", "")
                rd["confirm_response"] = br

            else:
                cfg.billing.email = auth_result.email
                pf = PaymentFlow(cfg, auth_result)
                if af: pf.session = af.session
                cs_id = pf.create_checkout_session()
                rd["checkout_session_id"] = cs_id
                rd["checkout_data"] = pf.checkout_data
                pf.fetch_stripe_fingerprint()
                pf.extract_stripe_pk(pf.checkout_url)
                if cs["do_payment"]:
                    pf.payment_method_id = pf.create_payment_method()
                    pf.fetch_payment_page_details(cs_id)
                    pay = pf.confirm_payment(cs_id)
                    rd["confirm_status"] = pay.confirm_status
                    rd["confirm_response"] = pay.confirm_response
                    rd["success"] = pay.success
                    rd["error"] = pay.error
                else:
                    rd["success"] = True
        elif cs["do_register"]:
            rd["success"] = True

    except Exception as e:
        rd["error"] = str(e)
        logging.getLogger("ui").error(f"EXCEPTION: {traceback.format_exc()}")
    finally:
        rd["_done"] = True

    try:
        store = ResultStore(output_dir=OUTPUT_DIR)
        store.save_result(rd, "ui_run")
        if rd.get("email"):
            store.append_history(email=rd["email"], status="ui_run",
                                 checkout_session_id=rd.get("checkout_session_id", ""),
                                 payment_status=rd.get("confirm_status", ""),
                                 error=rd.get("error", ""))
    except Exception:
        pass


with tab_run:
    # 额度提示
    if do_register:
        st.info("新注册模式: 成功消耗 **2** 次额度，失败消耗 **1** 次")
    else:
        st.info("已有账号模式: 消耗 **1** 次额度")
    btn_col1, btn_col2 = st.columns([4, 1])
    with btn_col1:
        run_btn = st.button("开始执行", disabled=st.session_state.running or not steps_list,
                            type="primary", use_container_width=True)
    with btn_col2:
        stop_btn = st.button("终止", disabled=not st.session_state.running, use_container_width=True)

    # ── 点击开始: 表单验证 → 验证兑换码 → 预留额度 → 启动线程 ──
    if run_btn and not st.session_state.running:
        # 表单验证
        _errors = []
        if do_register:
            if not selected_email_service_id:
                _errors.append("请先在「邮箱服务」配置并选择可用服务")
        elif use_existing_creds and do_checkout:
            if not cred_access_token:
                _errors.append("请提供 access_token")
            if do_payment and not cred_session_token:
                _errors.append("请提供 session_token (支付时必须)")
        if do_payment:
            if not card_number:
                _errors.append("请填写卡号")
            if not card_cvc:
                _errors.append("请填写 CVC")
        if _errors:
            for _e in _errors:
                st.error(_e)
            st.stop()

        # 再次验证兑换码
        if _ENABLE_CODE_SYSTEM:
            _v, _vm = validate_code(st.session_state.verified_code)
            if not _v:
                st.error(f"兑换码不可用: {_vm}")
                st.stop()

        # 预留使用额度 (新注册=2, 其他=1)
        if _ENABLE_CODE_SYSTEM:
            _reserve_amount = 2 if do_register else 1
            _exec_id = reserve_use(st.session_state.verified_code, plan_type=plan_type, amount=_reserve_amount)
            if _exec_id is None:
                st.error("兑换码额度不足")
                st.stop()
        else:
            _exec_id = None

        st.session_state._execution_id = _exec_id
        if _exec_id:
            update_execution(_exec_id, status="running")

        st.session_state._flow_config = {
            "proxy": proxy or None,
            "proxy_id": selected_proxy_id,
            "email_service_id": selected_email_service_id,
            "reg_flow_attempts": int(reg_flow_attempts),
            "reg_otp_attempts": int(reg_otp_attempts),
            "reg_session_attempts": int(reg_session_attempts),
            "workspace_name": workspace_name, "seat_quantity": seat_quantity, "promo_campaign": promo_campaign,
            "plan_type": plan_type,
            "captcha_api_url": captcha_api_url, "captcha_key": captcha_key,
            "billing_name": billing_name, "country_code": country_code, "currency": currency,
            "address_line1": address_line1, "address_city": address_city,
            "address_state": address_state, "postal_code": postal_code,
            "card_number": card_number if do_payment else "",
            "card_cvc": card_cvc if do_payment else "",
            "exp_month": exp_month if do_payment else "",
            "exp_year": exp_year if do_payment else "",
            "do_register": do_register, "do_checkout": do_checkout, "do_payment": do_payment,
            "use_existing_creds": use_existing_creds, "use_browser_mode": use_browser_mode,
            "cred_session_token": cred_session_token, "cred_access_token": cred_access_token,
            "cred_device_id": cred_device_id, "cred_email": cred_email,
        }
        st.session_state._flow_result = {"success": False, "error": "", "email": "", "steps": {}}
        st.session_state.running = True
        st.session_state.log_buffer = []
        st.session_state.result = None
        clear_captured_logs()
        init_logging()
        _t = threading.Thread(
            target=_run_flow_thread,
            args=(st.session_state._flow_result, st.session_state._flow_config),
            daemon=True,
        )
        _t.start()
        st.rerun()

    # ── 点击终止 ──
    if stop_btn and st.session_state.running:
        import subprocess as _sp
        try:
            _sp.run(["pkill", "-f", "remote-debugging-port"], capture_output=True)
        except Exception:
            pass
        st.session_state.running = False
        st.session_state.result = {"success": False, "error": "用户手动终止", "email": ""}
        # 终止不扣额度
        _eid = st.session_state.get("_execution_id")
        if _eid:
            complete_use(_eid, success=False, error_msg="用户手动终止")
            st.session_state._execution_id = None
        st.warning("已终止执行")
        st.rerun()

    # ── 运行中: 显示进度 ──
    if st.session_state.running:
        pct = _calc_progress_pct()
        st.progress(pct / 100.0)
        st.markdown(
            f'<div style="text-align:center;font-size:28px;font-weight:bold;margin:-15px 0 10px">{pct}%</div>',
            unsafe_allow_html=True,
        )
        rd = st.session_state.get("_flow_result", {})
        if rd.get("_done"):
            st.session_state.running = False
            st.session_state.result = rd
            # ── 完成兑换码计次 ──
            _eid = st.session_state.get("_execution_id")
            if _eid:
                complete_use(
                    _eid,
                    success=rd.get("success", False),
                    email=rd.get("email", ""),
                    error_msg=rd.get("error", ""),
                    result_json=json.dumps(rd, ensure_ascii=False, default=str),
                )
                st.session_state._execution_id = None
            st.rerun()
        else:
            import time as _time
            _time.sleep(1)
            st.rerun()

    # ── 显示结果 ──
    if st.session_state.result and not st.session_state.running:
        r = st.session_state.result
        if r.get("success"):
            st.progress(1.0)
            st.success(f"全部完成 — {r.get('email', '')}")
        elif r.get("error"):
            st.error(_sanitize_error(r.get('error', '')))

        if dev_mode:
            st.divider()
            cols = st.columns(4)
            cols[0].metric("邮箱", r.get("email") or "-")
            cols[1].metric("Checkout", (r.get("checkout_session_id", "")[:20] + "...") if r.get("checkout_session_id") else "-")
            cols[2].metric("Confirm", r.get("confirm_status") or "-")
            cols[3].metric("状态", "成功" if r.get("success") else "失败")
            if r.get("confirm_response"):
                with st.expander("Stripe 原始响应", expanded=False):
                    st.json(r["confirm_response"])
            pull_captured_logs()
            if st.session_state.log_buffer:
                with st.expander("日志", expanded=False):
                    st.code("\n".join(st.session_state.log_buffer[-200:]), language="log")


# Tab: 账号
# ════════════════════════════════════════
with tab_accounts:
    _history = get_code_history(st.session_state.verified_code)
    _acct_rows = _build_account_records(_history)
    if _acct_rows:
        import pandas as pd
        _disp_rows = []
        for a in _acct_rows:
            _disp_rows.append({
                "邮箱": a["email"],
                "计划": a["plan_type"],
                "订阅": ("🟢 " + a["subscription_label"]) if a["subscribed"] else "🔴 未开通",
                "Token": "✅" if a["has_token"] else "❌",
                "时间": a["created_at"],
            })
        st.dataframe(pd.DataFrame(_disp_rows), hide_index=True, use_container_width=True)
        st.caption(f"共 {len(_acct_rows)} 个账号")

        st.divider()
        for idx, acct in enumerate(_acct_rows):
            _data = acct["_data"]
            with st.expander(f"{acct['email']}  {'🟢' if acct['subscribed'] else '🔴'} {acct['subscription_label']}", expanded=False):
                if _data.get("access_token"):
                    st.code(
                        f"access_token: {_data.get('access_token', 'N/A')}\n"
                        f"session_token: {_data.get('session_token', 'N/A')}\n"
                        f"device_id: {_data.get('device_id', 'N/A')}",
                        language="yaml",
                    )
                else:
                    st.caption("无 Token 信息")
    else:
        st.info("暂无已注册的账号。执行完成后自动显示。")

    if st.button("刷新", key="ref_acc"):
        st.rerun()


# ════════════════════════════════════════
# Tab: 历史
# ════════════════════════════════════════
with tab_history:
    _history = get_code_history(st.session_state.verified_code)
    if _history:
        import pandas as pd
        _disp = []
        for r in _history:
            _disp.append({
                "状态": {"success": "✅ 成功", "failed": "❌ 失败", "running": "🔄 运行中", "pending": "⏳ 等待"}.get(r["status"], r["status"]),
                "邮箱": r.get("email") or "-",
                "计划": r.get("plan_type") or "-",
                "备注": _sanitize_error(r.get("error_msg") or "") if r["status"] == "failed" else "",
                "时间": r["created_at"][:19],
            })
        st.dataframe(pd.DataFrame(_disp), hide_index=True, use_container_width=True)
        st.caption(f"共 {len(_history)} 条记录")
    else:
        st.info("暂无执行历史")

    if st.button("刷新", key="ref_hist"):
        st.rerun()
