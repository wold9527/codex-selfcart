"""
Stripe 设备指纹 - guid / muid / sid 获取
通过请求 m.stripe.com/6 获取真实风控参数
"""
import json
import logging
import re
import uuid
from typing import Optional

from http_client import create_http_session

logger = logging.getLogger(__name__)


class StripeFingerprint:
    """Stripe 设备指纹管理"""

    def __init__(self, proxy: Optional[str] = None):
        self.session = create_http_session(proxy=proxy)
        self.guid: str = ""
        self.muid: str = str(uuid.uuid4())  # __stripe_mid
        self.sid: str = str(uuid.uuid4())    # __stripe_sid

    def fetch_from_m_stripe(self) -> bool:
        """
        从 m.stripe.com/6 获取 guid/muid/sid。
        这是 Stripe 的设备指纹采集端点。
        """
        logger.info("获取 Stripe 设备指纹 (m.stripe.com/6)...")
        try:
            headers = {
                "Accept": "*/*",
                "Origin": "https://m.stripe.network",
                "Referer": "https://m.stripe.network/",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
                ),
            }
            resp = self.session.post(
                "https://m.stripe.com/6",
                headers=headers,
                json={
                    "v": "m-outer-3437aaddcdf6922d623e172c2d6f9278",
                    "t": 0,
                },
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json() if hasattr(resp, 'json') else json.loads(resp.text)
                # 从返回数据中提取指纹
                self.guid = data.get("guid", data.get("id", ""))
                if data.get("muid"):
                    self.muid = data["muid"]
                if data.get("sid"):
                    self.sid = data["sid"]
                logger.info(f"Stripe 指纹获取成功 - guid: {self.guid[:12]}...")
                return True
            else:
                logger.warning(f"m.stripe.com/6 返回 {resp.status_code}, 使用模拟值")
        except Exception as e:
            logger.warning(f"获取 Stripe 指纹失败: {e}, 使用模拟值")

        # fallback: 生成模拟值
        if not self.guid:
            self.guid = str(uuid.uuid4())
            logger.info("使用模拟 guid (注意：可能触发 3DS 验证)")

        return False

    def get_params(self) -> dict:
        """返回用于 confirm 请求的指纹参数"""
        return {
            "guid": self.guid,
            "muid": self.muid,
            "sid": self.sid,
        }
