"""
hCaptcha 打码服务 - 通过 YesCaptcha API 解决 Stripe intent_confirmation_challenge
"""
import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class CaptchaSolver:
    """YesCaptcha hCaptcha 打码"""

    def __init__(self, api_url: str, client_key: str):
        self.api_url = api_url.rstrip("/")
        self.client_key = client_key

    def solve_hcaptcha(
        self,
        site_key: str,
        site_url: str,
        rqdata: str = "",
        user_agent: str = "",
        proxy: str = "",
        timeout: int = 120,
        poll_interval: int = 5,
        is_invisible: bool = True,
    ) -> Optional[dict]:
        """
        提交 hCaptcha 任务并等待结果。
        返回 {"token": ..., "ekey": ...}，失败返回 None。
        """
        # 如果有代理，用带代理的任务类型（token 和 API 请求同 IP）
        if proxy:
            task = {
                "type": "HCaptchaTask",
                "websiteURL": site_url,
                "websiteKey": site_key,
                "isEnterprise": True,
                "isInvisible": is_invisible,
            }
            # 解析代理格式: socks5://user:pass@host:port 或 http://host:port
            task["proxyType"] = "socks5" if "socks" in proxy.lower() else "http"
            # 提取 host:port
            proxy_clean = proxy.split("://")[-1]
            if "@" in proxy_clean:
                auth, hostport = proxy_clean.rsplit("@", 1)
                if ":" in auth:
                    task["proxyLogin"], task["proxyPassword"] = auth.split(":", 1)
            else:
                hostport = proxy_clean
            if ":" in hostport:
                task["proxyAddress"], port = hostport.rsplit(":", 1)
                task["proxyPort"] = int(port)
            else:
                task["proxyAddress"] = hostport
                task["proxyPort"] = 1080
        else:
            task = {
                "type": "HCaptchaTaskProxyless",
                "websiteURL": site_url,
                "websiteKey": site_key,
                "isEnterprise": True,
                "isInvisible": is_invisible,
            }
        if rqdata:
            task["enterprisePayload"] = {"rqdata": rqdata}
        if user_agent:
            task["userAgent"] = user_agent

        create_body = {
            "clientKey": self.client_key,
            "task": task,
        }

        logger.info(f"提交 hCaptcha 任务: site_key={site_key[:20]}...")
        try:
            resp = requests.post(
                f"{self.api_url}/createTask",
                json=create_body,
                timeout=30,
            )
            data = resp.json()
        except Exception as e:
            logger.error(f"创建打码任务失败: {e}")
            return None

        if data.get("errorId", 0) != 0:
            logger.error(f"打码任务创建失败: {data.get('errorDescription', data)}")
            return None

        task_id = data.get("taskId")
        if not task_id:
            logger.error(f"未返回 taskId: {data}")
            return None

        logger.info(f"打码任务已创建: taskId={task_id}")

        # 轮询结果
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(poll_interval)
            try:
                result_resp = requests.post(
                    f"{self.api_url}/getTaskResult",
                    json={"clientKey": self.client_key, "taskId": task_id},
                    timeout=30,
                )
                result_data = result_resp.json()
            except Exception as e:
                logger.warning(f"查询打码结果异常: {e}")
                continue

            status = result_data.get("status", "")
            if status == "ready":
                solution = result_data.get("solution", {})
                token = solution.get("gRecaptchaResponse", "")
                ekey = solution.get("eKey", "") or solution.get("respKey", "")
                if token:
                    logger.info(f"hCaptcha 已解决, token 长度: {len(token)}, ekey: {bool(ekey)}")
                    return {"token": token, "ekey": ekey}
                logger.error(f"打码结果缺少 token: {result_data}")
                return None
            elif status == "processing":
                logger.debug(f"打码中... (已等待 {int(time.time() - (deadline - timeout))}s)")
            else:
                logger.error(f"打码失败: {result_data}")
                return None

        logger.error(f"打码超时 ({timeout}s)")
        return None
