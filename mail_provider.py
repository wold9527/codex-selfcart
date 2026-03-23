"""
邮箱服务 - 临时邮箱创建 & OTP 获取
复用现有的 Worker 邮箱 API
"""
import re
import time
import random
import string
import logging

from http_client import create_http_session

logger = logging.getLogger(__name__)


class MailProvider:
    """临时邮箱提供者"""

    def __init__(self, worker_domain: str, admin_token: str, email_domain: str):
        self.worker_domain = worker_domain.rstrip("/")
        self.admin_token = admin_token
        self.email_domain = email_domain
        self.session = create_http_session()
        self.jwt: str | None = None

    def _random_name(self) -> str:
        letters1 = "".join(random.choices(string.ascii_lowercase, k=5))
        numbers = "".join(random.choices(string.digits, k=random.randint(1, 3)))
        letters2 = "".join(random.choices(string.ascii_lowercase, k=random.randint(1, 3)))
        return letters1 + numbers + letters2

    def create_mailbox(self) -> str:
        """创建临时邮箱，返回邮箱地址"""
        name = self._random_name()
        headers = {
            "x-admin-auth": self.admin_token,
            "Content-Type": "application/json",
        }
        resp = self.session.post(
            f"{self.worker_domain}/admin/new_address",
            json={"enablePrefix": True, "name": name, "domain": self.email_domain},
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()
        email = result.get("address")
        self.jwt = result.get("jwt")
        if not email:
            raise RuntimeError(f"邮箱创建失败: {result}")
        logger.info(f"临时邮箱已创建: {email}")
        return email

    def _fetch_emails(self):
        """获取邮件列表"""
        headers = {"Authorization": f"Bearer {self.jwt}"}
        resp = self.session.get(
            f"{self.worker_domain}/api/mails",
            params={"limit": 10, "offset": 0},
            headers=headers,
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json().get("results", [])
        return []

    @staticmethod
    def _extract_otp(content: str) -> str | None:
        """从邮件内容中提取 OTP"""
        patterns = [r"代码为\s*(\d{6})", r"code is\s*(\d{6})", r"(\d{6})"]
        for pattern in patterns:
            matches = re.findall(pattern, content)
            if matches:
                return matches[0]
        return None

    def wait_for_otp(self, email: str, timeout: int = 120) -> str:
        """阻塞等待 OTP 验证码"""
        logger.info(f"等待 OTP 验证码 (最长 {timeout}s)...")
        start = time.time()
        while time.time() - start < timeout:
            emails = self._fetch_emails()
            for item in emails:
                sender = item.get("source", "").lower()
                raw = item.get("raw", "")
                if "openai" in sender or "openai" in raw.lower():
                    otp = self._extract_otp(raw)
                    if otp:
                        logger.info(f"收到 OTP: {otp}")
                        return otp
            time.sleep(3)
        raise TimeoutError(f"等待 OTP 超时 ({timeout}s)")
