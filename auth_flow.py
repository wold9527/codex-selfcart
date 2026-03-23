"""
注册/登录流程 - 协议直连方式
完整链路:
  chatgpt_csrf -> chatgpt_signin_openai -> auth_oauth_init -> sentinel
  -> signup -> send_otp -> verify_otp -> create_account
  -> redirect_chain -> auth_session -> (optional) oauth_token_exchange
"""
import json
import logging
import random
import re
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from urllib.parse import urlparse, parse_qs, urljoin

from config import Config
from mail_provider import MailProvider
from http_client import create_http_session

logger = logging.getLogger(__name__)


class RegistrationStage(str, Enum):
    PRECHECK = "precheck"
    EMAIL_CREATE = "email_create"
    AUTHORIZE_PREPARE = "authorize_prepare"
    OTP_VERIFY = "otp_verify"
    ACCOUNT_CREATE = "account_create"
    SESSION_ACQUIRE = "session_acquire"
    RELAUNCH_LOGIN = "relaunch_login"
    DONE = "done"


@dataclass
class RegistrationRetryPolicy:
    max_flow_attempts: int = 2
    max_otp_attempts: int = 2
    otp_wait_timeout: int = 120
    max_session_attempts: int = 3
    retry_backoff_seconds: float = 2.0


class AuthResult:
    """认证结果"""

    def __init__(self):
        self.email: str = ""
        self.session_token: str = ""
        self.access_token: str = ""
        self.device_id: str = ""
        self.csrf_token: str = ""
        self.id_token: str = ""
        self.refresh_token: str = ""

    def is_valid(self) -> bool:
        return bool(self.session_token and self.access_token)

    def to_dict(self) -> dict:
        return {
            "email": self.email,
            "session_token": self.session_token,
            "access_token": self.access_token,
            "device_id": self.device_id,
            "csrf_token": self.csrf_token,
            "id_token": self.id_token,
            "refresh_token": self.refresh_token,
        }


class AuthFlow:
    """注册/登录协议流"""

    def __init__(self, config: Config):
        self.config = config
        self._impersonate_candidates = ["chrome136", "chrome124", "chrome120"]
        self._impersonate_idx = 0
        self.session = create_http_session(
            proxy=config.proxy,
            impersonate=self._impersonate_candidates[self._impersonate_idx],
        )
        self.result = AuthResult()
        self._last_stage: RegistrationStage | None = None

    def _log_stage(self, stage: RegistrationStage, attempt: int, detail: str):
        self._last_stage = stage
        logger.info(f"[注册][尝试{attempt}] [{stage.value}] {detail}")

    def _reset_auth_flow(self):
        """重置会话，准备重新发起授权流程（参考 codex-console2 的恢复模式）。"""
        try:
            self.session.close()
        except Exception:
            pass
        self.session = create_http_session(
            proxy=self.config.proxy,
            impersonate=self._impersonate_candidates[self._impersonate_idx],
        )
        # 保留邮箱，重置 token 相关字段
        keep_email = self.result.email
        self.result = AuthResult()
        self.result.email = keep_email

    def _prepare_authorize_flow(self, attempt: int) -> tuple[str, str]:
        """初始化授权流程，返回 (device_id, sentinel_token)。"""
        self._log_stage(RegistrationStage.AUTHORIZE_PREPARE, attempt, "准备授权链路")
        csrf_token = self.get_csrf_token()
        auth_url = self.get_auth_url(csrf_token)
        device_id = self.auth_oauth_init(auth_url)
        sentinel = self.get_sentinel_token(device_id)
        return device_id, sentinel

    def _send_and_verify_otp_with_retry(
        self,
        mail_provider: MailProvider,
        email: str,
        attempt: int,
        policy: RegistrationRetryPolicy,
    ):
        self._log_stage(RegistrationStage.OTP_VERIFY, attempt, "发送并校验 OTP")
        self.send_otp()

        last_error = ""
        for otp_try in range(1, policy.max_otp_attempts + 1):
            try:
                otp_code = mail_provider.wait_for_otp(email, timeout=policy.otp_wait_timeout)
                self.verify_otp(otp_code)
                logger.info(f"[注册][尝试{attempt}] OTP 校验成功 (第{otp_try}次)")
                return
            except Exception as e:
                last_error = str(e)
                if otp_try < policy.max_otp_attempts:
                    logger.warning(f"[注册][尝试{attempt}] OTP 第{otp_try}次失败，准备重发: {last_error}")
                    self.send_otp()
                    time.sleep(2)

        raise RuntimeError(f"OTP 验证失败: {last_error}")

    def _acquire_session_with_retry(
        self,
        continue_url: str,
        attempt: int,
        policy: RegistrationRetryPolicy,
    ):
        self._log_stage(RegistrationStage.SESSION_ACQUIRE, attempt, "获取 session/access_token")
        last_error = ""
        callback_url = ""

        for sess_try in range(1, policy.max_session_attempts + 1):
            try:
                callback_url, _ = self.follow_redirect_chain(continue_url)
                self.get_auth_session()
                if callback_url and continue_url:
                    self.oauth_token_exchange(callback_url, continue_url)
                if self.result.is_valid():
                    logger.info(f"[注册][尝试{attempt}] Session 获取成功 (第{sess_try}次)")
                    return
                last_error = "session_token 或 access_token 为空"
            except Exception as e:
                last_error = str(e)

            logger.warning(f"[注册][尝试{attempt}] Session 获取失败 (第{sess_try}次): {last_error}")
            if sess_try < policy.max_session_attempts:
                time.sleep(policy.retry_backoff_seconds * sess_try)

        raise RuntimeError(f"获取认证 Session 失败: {last_error}")

    def _restart_login_for_token(
        self,
        mail_provider: MailProvider,
        email: str,
        attempt: int,
        policy: RegistrationRetryPolicy,
    ) -> str:
        """建号后 token 缺失时，重走一次登录链路获取 token。"""
        self._log_stage(RegistrationStage.RELAUNCH_LOGIN, attempt, "重启登录链路获取 token")
        self._reset_auth_flow()
        _, sentinel = self._prepare_authorize_flow(attempt)
        self.signup(email, sentinel)
        self._send_and_verify_otp_with_retry(mail_provider, email, attempt, policy)
        workspace_id = self._extract_workspace_id()
        if not workspace_id:
            raise RuntimeError("重启登录后未获取 workspace_id")
        continue_url = self._workspace_select(workspace_id)
        if not continue_url:
            raise RuntimeError("重启登录后 workspace/select 未返回 continue_url")
        return continue_url

    @staticmethod
    def _is_tls_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        markers = ["curl: (35)", "tls connect error", "openssl_internal", "sslerror"]
        return any(m in msg for m in markers)

    def _rotate_impersonate_session(self) -> bool:
        """仅在 curl_cffi 指纹模式内切换 UA 指纹版本重试。"""
        if self._impersonate_idx >= len(self._impersonate_candidates) - 1:
            return False
        self._impersonate_idx += 1
        imp = self._impersonate_candidates[self._impersonate_idx]
        logger.warning(f"TLS 异常，切换指纹重试: impersonate={imp}")
        self.session = create_http_session(proxy=self.config.proxy, impersonate=imp)
        return True

    def _common_headers(self, referer: str = "https://chatgpt.com/") -> dict:
        return {
            "Accept": "application/json",
            "Referer": referer,
            "Origin": "https://chatgpt.com",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
            ),
        }

    # ── Step 1: 检查代理连通性 ──
    def check_proxy(self) -> bool:
        logger.info("检查网络连通性...")
        try:
            resp = self.session.get("https://cloudflare.com/cdn-cgi/trace", timeout=15)
            if resp.status_code == 200:
                loc = re.search(r"loc=(\w+)", resp.text)
                ip = re.search(r"ip=([^\n]+)", resp.text)
                logger.info(f"网络正常 - IP: {ip.group(1) if ip else 'N/A'}, "
                            f"地区: {loc.group(1) if loc else 'N/A'}")
            else:
                logger.warning(f"网络探测异常: cloudflare trace {resp.status_code}")

            # 关键链路探测: chatgpt csrf
            csrf_headers = self._common_headers("https://chatgpt.com/auth/login")
            csrf_resp = self.session.get(
                "https://chatgpt.com/api/auth/csrf",
                headers=csrf_headers,
                timeout=20,
            )
            if csrf_resp.status_code == 200:
                logger.info("chatgpt csrf 连通正常")
                return True

            logger.warning(f"chatgpt csrf 连通异常: {csrf_resp.status_code}")
            return False
        except Exception as e:
            logger.error(f"网络检查失败: {e}")
        return False

    # ── Step 2: 获取 CSRF Token ──
    def get_csrf_token(self) -> str:
        logger.info("[1/10] 获取 CSRF Token...")
        headers = self._common_headers("https://chatgpt.com/auth/login")

        # Cloudflare 可能在短时间内多次请求后返回 403，重试 3 次
        for attempt in range(3):
            try:
                resp = self.session.get(
                    "https://chatgpt.com/api/auth/csrf",
                    headers=headers,
                    timeout=30,
                )
            except Exception as e:
                if self._is_tls_error(e) and self._rotate_impersonate_session():
                    continue
                if self._is_tls_error(e):
                    raise RuntimeError(
                        "chatgpt.com TLS 握手失败，当前网络无法建立到 /api/auth/csrf 的 HTTPS 连接。"
                        "请切换可直连 chatgpt.com 的网络或在界面中配置可用代理后重试。"
                    ) from e
                raise
            if resp.status_code == 403 and attempt < 2:
                wait = (attempt + 1) * 5
                logger.warning(f"Cloudflare 403, {wait}s 后重试 ({attempt + 1}/3)...")
                import time
                time.sleep(wait)
                continue
            resp.raise_for_status()
            break

        csrf = resp.json().get("csrfToken", "")
        if not csrf:
            raise RuntimeError("CSRF Token 获取失败")
        self.result.csrf_token = csrf
        logger.info(f"CSRF Token: {csrf[:20]}...")
        return csrf

    # ── Step 3: 获取 auth URL ──
    def get_auth_url(self, csrf_token: str) -> str:
        logger.info("[2/10] 获取 OpenAI 授权地址...")
        headers = self._common_headers("https://chatgpt.com/auth/login")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        resp = self.session.post(
            "https://chatgpt.com/api/auth/signin/openai",
            headers=headers,
            data={
                "csrfToken": csrf_token,
                "callbackUrl": "https://chatgpt.com/",
                "json": "true",
            },
            timeout=30,
        )
        resp.raise_for_status()
        auth_url = resp.json().get("url", "")
        if not auth_url:
            raise RuntimeError("Auth URL 获取失败")
        logger.info(f"Auth URL: {auth_url[:80]}...")
        return auth_url

    # ── Step 4: OAuth 初始化 & 获取 device_id ──
    def auth_oauth_init(self, auth_url: str) -> str:
        logger.info("[3/10] OAuth 初始化...")
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://chatgpt.com/auth/login",
            "User-Agent": self._common_headers()["User-Agent"],
        }
        resp = self.session.get(auth_url, headers=headers, timeout=30, allow_redirects=True)

        # 从 cookie 获取 oai-did
        device_id = ""
        for cookie in self.session.cookies:
            if hasattr(cookie, "name"):
                if cookie.name == "oai-did":
                    device_id = cookie.value
                    break
            elif isinstance(cookie, str) and cookie == "oai-did":
                device_id = self.session.cookies.get("oai-did", "")
                break

        # curl_cffi cookies 访问方式
        if not device_id:
            try:
                device_id = self.session.cookies.get("oai-did", "")
            except Exception:
                pass

        # fallback: 从 HTML 提取
        if not device_id:
            m = re.search(r'oai-did["\s:=]+([a-f0-9-]{36})', resp.text)
            if m:
                device_id = m.group(1)

        if not device_id:
            device_id = str(uuid.uuid4())
            logger.warning(f"未从响应中获取 device_id，使用生成值: {device_id}")

        self.result.device_id = device_id
        logger.info(f"Device ID: {device_id}")
        return device_id

    # ── Step 5: 获取 Sentinel Token ──
    def get_sentinel_token(self, device_id: str) -> str:
        logger.info("[4/10] 获取 Sentinel Token...")
        body = json.dumps({"p": "", "id": device_id, "flow": "authorize_continue"})
        headers = {
            "Origin": "https://sentinel.openai.com",
            "Referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=20260219f9f6",
            "Content-Type": "text/plain;charset=UTF-8",
        }
        resp = self.session.post(
            "https://sentinel.openai.com/backend-api/sentinel/req",
            headers=headers,
            data=body,
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Sentinel 异常，状态码: {resp.status_code}")
        token = resp.json().get("token", "")
        sentinel_header = json.dumps({
            "p": "", "t": "", "c": token, "id": device_id, "flow": "authorize_continue"
        })
        logger.info("Sentinel Token 获取成功")
        return sentinel_header

    # ── Step 6: 提交注册邮箱 ──
    def signup(self, email: str, sentinel_token: str):
        logger.info("[5/10] 提交注册邮箱...")
        headers = self._common_headers("https://auth.openai.com/create-account")
        headers["Content-Type"] = "application/json"
        headers["openai-sentinel-token"] = sentinel_token
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/authorize/continue",
            headers=headers,
            json={
                "username": {"value": email, "kind": "email"},
                "screen_hint": "signup",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            logger.error(f"注册失败: {resp.status_code} - {resp.text[:500]}")
            raise RuntimeError(f"注册失败: HTTP {resp.status_code} - {resp.text[:300]}")
        logger.info("注册邮箱已提交")

    # ── Step 7: 发送 OTP ──
    def send_otp(self):
        logger.info("[6/10] 发送 OTP...")
        headers = self._common_headers("https://auth.openai.com/create-account/password")
        headers["Content-Type"] = "application/json"
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/passwordless/send-otp",
            headers=headers,
            json={},
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"发送 OTP 失败: {resp.status_code} - {resp.text[:200]}")
        logger.info("OTP 已发送到邮箱")

    # ── Step 8: 验证 OTP ──
    def verify_otp(self, otp_code: str):
        logger.info("[7/10] 验证 OTP...")
        headers = self._common_headers("https://auth.openai.com/email-verification")
        headers["Content-Type"] = "application/json"
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/email-otp/validate",
            headers=headers,
            json={"code": otp_code},
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"OTP 验证失败: {resp.status_code}")
        logger.info("OTP 验证成功")

    # ── Step 9: 创建账户 ──
    def create_account(self) -> str:
        logger.info("[8/10] 创建账户...")
        headers = self._common_headers("https://auth.openai.com/about-you")
        headers["Content-Type"] = "application/json"
        name = "Neo"
        birthdate = f"{random.randint(1985, 2000)}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/create_account",
            headers=headers,
            json={"name": name, "birthdate": birthdate},
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"创建账户失败: {resp.status_code}")
        data = resp.json()
        continue_url = data.get("continue_url", "")

        # 尝试 workspace select
        if not continue_url:
            workspace_id = self._extract_workspace_id()
            if workspace_id:
                continue_url = self._workspace_select(workspace_id)

        if not continue_url:
            raise RuntimeError("创建账户后未获取到 continue_url")

        logger.info("账户创建成功")
        return continue_url

    def _extract_workspace_id(self) -> str:
        """从 cookie 中提取 workspace_id"""
        try:
            auth_session = self.session.cookies.get("oai-client-auth-session", "")
            if auth_session:
                # base64 解码 JWT payload
                import base64
                parts = auth_session.split(".")
                if len(parts) >= 2:
                    payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
                    decoded = json.loads(base64.b64decode(payload))
                    return decoded.get("workspace_id", "")
        except Exception:
            pass
        return ""

    def _workspace_select(self, workspace_id: str) -> str:
        logger.info("执行 workspace 选择...")
        headers = self._common_headers("https://auth.openai.com/sign-in-with-chatgpt/codex/consent")
        headers["Content-Type"] = "application/json"
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/workspace/select",
            headers=headers,
            json={"workspace_id": workspace_id},
            timeout=30,
        )
        return resp.json().get("continue_url", "") if resp.status_code == 200 else ""

    # ── Step 10: 跟踪重定向链 ──
    def follow_redirect_chain(self, start_url: str) -> tuple[str, str]:
        """手动跟踪重定向，返回 (callback_url, final_url)"""
        logger.info("[9/10] 跟踪重定向链...")
        current_url = start_url
        callback_url = ""
        max_hops = 12

        for i in range(max_hops):
            headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://chatgpt.com/",
                "User-Agent": self._common_headers()["User-Agent"],
            }
            resp = self.session.get(
                current_url, headers=headers, timeout=30, allow_redirects=False
            )

            if "/api/auth/callback/openai" in current_url:
                callback_url = current_url

            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location", "")
                if not location:
                    break
                if location.startswith("/"):
                    parsed = urlparse(current_url)
                    location = f"{parsed.scheme}://{parsed.netloc}{location}"
                current_url = location
                logger.debug(f"  重定向 {i + 1}: {current_url[:80]}...")
            else:
                break

        # 补一跳首页
        if not current_url.rstrip("/").endswith("chatgpt.com"):
            self.session.get(
                "https://chatgpt.com/",
                headers={"Referer": current_url},
                timeout=30,
            )

        logger.info(f"重定向链完成, callback: {'有' if callback_url else '无'}")
        return callback_url, current_url

    # ── Step 11: 获取 session ──
    def get_auth_session(self) -> tuple[str, str]:
        """获取 session_token 和 access_token"""
        logger.info("[10/10] 获取认证 Session...")
        headers = self._common_headers("https://chatgpt.com/")
        resp = self.session.get(
            "https://chatgpt.com/api/auth/session",
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()

        session_token = self.session.cookies.get("__Secure-next-auth.session-token", "")
        access_token = resp.json().get("accessToken", "")

        if session_token:
            self.result.session_token = session_token
        if access_token:
            self.result.access_token = access_token

        logger.info(f"session_token: {'有' if session_token else '无'}, "
                     f"access_token: {'有' if access_token else '无'}")
        return session_token, access_token

    # ── 可选: OAuth Token 交换 ──
    def oauth_token_exchange(self, callback_url: str, continue_url: str):
        """用 auth_code + login_verifier 交换完整 token"""
        parsed_cb = parse_qs(urlparse(callback_url).query)
        parsed_cu = parse_qs(urlparse(continue_url).query)
        auth_code = parsed_cb.get("code", [None])[0]
        login_verifier = parsed_cu.get("login_verifier", [None])[0]

        if not (auth_code and login_verifier):
            logger.info("缺少 auth_code 或 login_verifier, 跳过 token 交换")
            return

        logger.info("执行 OAuth Token 交换...")
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Origin": "https://chatgpt.com",
            "Referer": callback_url,
        }
        resp = self.session.post(
            "https://auth.openai.com/oauth/token",
            headers=headers,
            data={
                "grant_type": "authorization_code",
                "client_id": "app_X8zY6vW2pQ9tR3dE7nK1jL5gH",
                "code": auth_code,
                "redirect_uri": "https://chatgpt.com/api/auth/callback/openai",
                "code_verifier": login_verifier,
            },
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            self.result.id_token = data.get("id_token", "")
            self.result.access_token = data.get("access_token", self.result.access_token)
            self.result.refresh_token = data.get("refresh_token", "")
            logger.info("Token 交换成功")
        else:
            logger.warning(f"Token 交换失败: {resp.status_code}")

    # ── 完整注册流程 ──
    def run_register(
        self,
        mail_provider: MailProvider,
        policy: RegistrationRetryPolicy | None = None,
    ) -> AuthResult:
        """
        执行完整注册流程（增强版）:
        - 按阶段推进（precheck/email/authorize/otp/account/session）
        - 失败时按策略重试
        - token 获取失败时，重启登录链路回补一次
        """
        policy = policy or RegistrationRetryPolicy()
        policy.max_flow_attempts = max(1, int(policy.max_flow_attempts))
        policy.max_otp_attempts = max(1, int(policy.max_otp_attempts))
        policy.max_session_attempts = max(1, int(policy.max_session_attempts))
        policy.otp_wait_timeout = max(30, int(policy.otp_wait_timeout))
        policy.retry_backoff_seconds = max(0.5, float(policy.retry_backoff_seconds))
        last_error = ""

        # 预检查失败不终止，只做告警
        self._log_stage(RegistrationStage.PRECHECK, 1, "网络预检查")
        if not self.check_proxy():
            logger.warning("网络预检查未通过，继续尝试注册链路以获取精确错误...")

        for flow_try in range(1, policy.max_flow_attempts + 1):
            try:
                if flow_try > 1:
                    self._reset_auth_flow()
                    logger.warning(f"[注册] 进入第 {flow_try}/{policy.max_flow_attempts} 次全流程重试")

                self._log_stage(RegistrationStage.EMAIL_CREATE, flow_try, "创建临时邮箱")
                email = mail_provider.create_mailbox()
                self.result.email = email

                _, sentinel = self._prepare_authorize_flow(flow_try)
                self.signup(email, sentinel)
                self._send_and_verify_otp_with_retry(mail_provider, email, flow_try, policy)

                self._log_stage(RegistrationStage.ACCOUNT_CREATE, flow_try, "创建账号并获取 continue_url")
                continue_url = self.create_account()

                try:
                    self._acquire_session_with_retry(continue_url, flow_try, policy)
                except Exception as session_error:
                    logger.warning(f"[注册][尝试{flow_try}] 首次 Session 获取失败: {session_error}")
                    # 参考 codex-console2：建号后重启登录链路回补 token
                    relogin_continue_url = self._restart_login_for_token(
                        mail_provider, email, flow_try, policy
                    )
                    self._acquire_session_with_retry(relogin_continue_url, flow_try, policy)

                if not self.result.is_valid():
                    raise RuntimeError("注册完成但未获取有效凭证")

                self._log_stage(RegistrationStage.DONE, flow_try, "注册成功")
                logger.info("注册流程完成!")
                return self.result
            except Exception as e:
                last_error = str(e)
                logger.error(f"[注册][尝试{flow_try}] 失败: {last_error}")
                if flow_try < policy.max_flow_attempts:
                    time.sleep(policy.retry_backoff_seconds * flow_try)

        stage = self._last_stage.value if self._last_stage else "unknown"
        raise RuntimeError(
            f"注册失败(已重试 {policy.max_flow_attempts} 次, 最后阶段: {stage}): {last_error}"
        )

    # ── 从已有凭证初始化 ──
    def from_existing_credentials(
        self, session_token: str, access_token: str, device_id: str
    ) -> AuthResult:
        """使用已有凭证（跳过注册）"""
        self.result.device_id = device_id or str(uuid.uuid4())
        self.session.cookies.set("oai-did", self.result.device_id, domain=".chatgpt.com")

        # 如果有 session_token, 用它刷新 access_token (旧 access_token 可能已过期)
        if session_token:
            self.session.cookies.set(
                "__Secure-next-auth.session-token",
                session_token,
                domain=".chatgpt.com",
            )
            logger.info("使用 session_token 刷新 access_token...")
            try:
                headers = self._common_headers("https://chatgpt.com/")
                resp = self.session.get(
                    "https://chatgpt.com/api/auth/session",
                    headers=headers,
                    timeout=30,
                )
                new_access_token = resp.json().get("accessToken", "")
                new_session_token = self.session.cookies.get("__Secure-next-auth.session-token", "")
                if new_access_token:
                    access_token = new_access_token
                    logger.info("access_token 刷新成功")
                else:
                    logger.warning(f"access_token 刷新失败 (status={resp.status_code}), 使用原 token")
                if new_session_token:
                    session_token = new_session_token
            except Exception as e:
                logger.warning(f"刷新 access_token 失败: {e}, 使用原 token")
        elif access_token:
            # 没有 session_token, 尝试通过 access_token 获取
            logger.info("未提供 session_token, 尝试通过 access_token 获取...")
            try:
                headers = self._common_headers("https://chatgpt.com/")
                headers["Authorization"] = f"Bearer {access_token}"
                resp = self.session.get(
                    "https://chatgpt.com/api/auth/session",
                    headers=headers,
                    timeout=30,
                )
                session_token = self.session.cookies.get("__Secure-next-auth.session-token", "")
                if session_token:
                    logger.info("通过 access_token 获取 session_token 成功")
                else:
                    logger.warning("未能获取 session_token, 可能需要手动提供")
            except Exception as e:
                logger.warning(f"获取 session_token 失败: {e}")

        self.result.access_token = access_token
        self.result.session_token = session_token
        if session_token:
            self.session.cookies.set(
                "__Secure-next-auth.session-token",
                session_token,
                domain=".chatgpt.com",
            )
        logger.info("使用已有凭证初始化完成")
        return self.result
