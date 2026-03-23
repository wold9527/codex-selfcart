"""
支付流程 - Checkout + Confirm
主链路:
  1. POST /backend-api/payments/checkout  -> checkout_session_id + publishable_key
  2. 获取 Stripe 指纹 (guid/muid/sid)
  3. POST /v1/payment_methods -> 卡片 tokenization
  4. POST /v1/payment_pages/{checkout_session_id}/confirm -> 支付确认
  5. 如需要: 解决 Stripe hCaptcha 挑战 (intent_confirmation_challenge)
"""
import json
import logging
import re
import uuid
from typing import Optional

from config import Config, CardInfo, BillingInfo
from auth_flow import AuthResult
from stripe_fingerprint import StripeFingerprint
from captcha_solver import CaptchaSolver
from http_client import create_http_session

logger = logging.getLogger(__name__)


class PaymentResult:
    """支付结果"""

    def __init__(self):
        self.checkout_session_id: str = ""
        self.confirm_status: str = ""
        self.confirm_response: dict = {}
        self.checkout_data: dict = {}  # ChatGPT checkout 原始返回
        self.success: bool = False
        self.error: str = ""

    def to_dict(self) -> dict:
        return {
            "checkout_session_id": self.checkout_session_id,
            "confirm_status": self.confirm_status,
            "success": self.success,
            "error": self.error,
            "confirm_response": self.confirm_response,
            "checkout_data": self.checkout_data,
        }


class PaymentFlow:
    """支付协议流"""

    def __init__(self, config: Config, auth_result: AuthResult, stripe_proxy: str = None):
        self.config = config
        self.auth = auth_result
        self.session = create_http_session(proxy=config.proxy)  # ChatGPT 用 proxy
        # Stripe 调用的代理 (None=直连, 或设为与 ChatGPT 同代理实现 IP 一致性)
        self._stripe_proxy = stripe_proxy
        self.fingerprint = StripeFingerprint(proxy=stripe_proxy)
        self.result = PaymentResult()
        self.stripe_pk: str = ""  # Stripe publishable key
        self.checkout_url: str = ""  # Stripe checkout URL
        self.checkout_data: dict = {}  # 完整 checkout 响应
        self.payment_method_id: str = ""  # tokenized payment method ID

        # 设置认证 cookie
        self.session.cookies.set(
            "__Secure-next-auth.session-token",
            auth_result.session_token,
            domain=".chatgpt.com",
        )
        if auth_result.device_id:
            self.session.cookies.set("oai-did", auth_result.device_id, domain=".chatgpt.com")

    def _get_sentinel_token(self) -> str:
        """获取支付场景的 sentinel token"""
        device_id = self.auth.device_id or str(uuid.uuid4())
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
            raise RuntimeError(f"Sentinel Token 获取失败: {resp.status_code}")
        token = resp.json().get("token", "")
        return json.dumps({
            "p": "", "t": "", "c": token, "id": device_id, "flow": "authorize_continue"
        })

    # ── Step 1: 创建 Checkout Session ──
    def create_checkout_session(self) -> str:
        """
        POST /backend-api/payments/checkout
        返回 checkout_session_id
        """
        logger.info("[支付 1/3] 创建 Checkout Session...")

        sentinel = self._get_sentinel_token()

        headers = {
            "Authorization": f"Bearer {self.auth.access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
            "oai-device-id": self.auth.device_id,
            "openai-sentinel-token": sentinel,
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
            ),
        }

        plan = self.config.team_plan
        billing = self.config.billing

        body = {
            "plan_name": plan.plan_name,
            "team_plan_data": {
                "workspace_name": plan.workspace_name,
                "price_interval": plan.price_interval,
                "seat_quantity": plan.seat_quantity,
            },
            "billing_details": {
                "country": billing.country,
                "currency": billing.currency,
            },
            "cancel_url": f"https://chatgpt.com/?promo_campaign={plan.promo_campaign_id}#team-pricing",
            "promo_campaign": {
                "promo_campaign_id": plan.promo_campaign_id,
                "is_coupon_from_query_param": True,
            },
            "checkout_ui_mode": "custom",
        }

        resp = self.session.post(
            "https://chatgpt.com/backend-api/payments/checkout",
            headers=headers,
            json=body,
            timeout=30,
        )

        if resp.status_code != 200:
            raise RuntimeError(
                f"创建 Checkout Session 失败: {resp.status_code} - {resp.text[:300]}"
            )

        data = resp.json()
        logger.info(f"Checkout 返回字段: {list(data.keys())}")
        logger.debug(f"Checkout 返回内容: {json.dumps(data, ensure_ascii=False)[:2000]}")
        if data.get("client_secret"):
            logger.info(f"client_secret: {data['client_secret'][:40]}...")

        # 保存 checkout_url 和 publishable_key
        self.checkout_url = data.get("url", "") or data.get("checkout_url", "")
        pk_from_response = data.get("publishable_key", "")
        if pk_from_response:
            self.stripe_pk = pk_from_response
            logger.info(f"Stripe PK (from checkout): {self.stripe_pk[:30]}...")

        # 保存完整 checkout 返回数据
        self.checkout_data = data
        self.result.checkout_data = data

        # 记录 discount 相关字段
        for dk in ("scheduled_discount_preview", "immediate_discount_settings"):
            if data.get(dk):
                logger.info(f"Checkout {dk}: {json.dumps(data[dk], ensure_ascii=False)[:500]}")

        # 从返回提取 checkout_session_id
        cs_id = (
            data.get("checkout_session_id")
            or data.get("session_id")
            or ""
        )

        # 从 checkout_url 中提取
        if not cs_id:
            checkout_url = self.checkout_url
            if "cs_" in checkout_url:
                m = re.search(r"(cs_[A-Za-z0-9_]+)", checkout_url)
                if m:
                    cs_id = m.group(1)

        # 从 client_secret 中提取
        if not cs_id:
            secret = data.get("client_secret", "")
            if secret and "_secret_" in secret:
                cs_id = secret.split("_secret_")[0]

        if not cs_id:
            raise RuntimeError(f"未能从返回中提取 checkout_session_id: {data}")

        self.result.checkout_session_id = cs_id
        logger.info(f"Checkout Session ID: {cs_id[:30]}...")
        return cs_id

    # ── Step 2: 获取 Stripe 指纹 ──
    def fetch_stripe_fingerprint(self):
        """获取 guid/muid/sid"""
        logger.info("[支付 2/4] 获取 Stripe 设备指纹...")
        self.fingerprint.fetch_from_m_stripe()

    # ── Step 2.5: 提取 Stripe publishable key ──
    def extract_stripe_pk(self, checkout_url: str) -> str:
        """
        从 checkout 页面或 payment_pages 接口提取 Stripe publishable key.
        pk_live_xxx 是公开的，嵌入在 checkout 页面中。
        """
        logger.info("[支付 3/4] 获取 Stripe Publishable Key...")

        # 如果已经从 checkout 响应中获取到了，直接返回
        if self.stripe_pk:
            logger.info(f"已有 Stripe PK: {self.stripe_pk[:30]}...")
            return self.stripe_pk

        cs_id = self.result.checkout_session_id

        # 如果没有 checkout_url，尝试构造
        if not checkout_url and cs_id:
            checkout_url = f"https://checkout.stripe.com/c/pay/{cs_id}"

        # 方法 1: 从 checkout 页面提取
        if checkout_url:
            try:
                headers = {
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
                    ),
                }
                resp = self.session.get(checkout_url, headers=headers, timeout=30, allow_redirects=True)
                logger.debug(f"Checkout 页面状态: {resp.status_code}, 长度: {len(resp.text)}")
                if resp.status_code == 200:
                    m = re.search(r'(pk_(?:live|test)_[A-Za-z0-9]+)', resp.text)
                    if m:
                        self.stripe_pk = m.group(1)
                        logger.info(f"Stripe PK: {self.stripe_pk[:20]}...")
                        return self.stripe_pk
                    else:
                        logger.debug(f"checkout 页面中未找到 pk_ 模式")
            except Exception as e:
                logger.warning(f"从 checkout 页面提取 PK 失败: {e}")

        # 方法 2: 从 payment_pages/{cs_id} 获取 (无需auth, 返回包含pk)
        if cs_id:
            try:
                resp = self.session.get(
                    f"https://api.stripe.com/v1/payment_pages/{cs_id}",
                    headers={"Accept": "application/json"},
                    timeout=30,
                )
                logger.debug(f"payment_pages 状态: {resp.status_code}")
                if resp.status_code == 200:
                    data = resp.json()
                    pk = data.get("merchant", {}).get("publishable_key", "")
                    if not pk:
                        # 尝试更深层查找
                        pk = data.get("publishable_key", "")
                    if pk:
                        self.stripe_pk = pk
                        logger.info(f"Stripe PK (from payment_pages): {self.stripe_pk[:20]}...")
                        return self.stripe_pk
                    else:
                        logger.debug(f"payment_pages 返回字段: {list(data.keys())}")
            except Exception as e:
                logger.warning(f"从 payment_pages 提取 PK 失败: {e}")

        # 方法 3: 从 elements/sessions 获取
        if cs_id:
            try:
                client_secret = f"{cs_id}_secret_placeholder"
                resp = self.session.get(
                    "https://api.stripe.com/v1/elements/sessions",
                    params={"client_secret": client_secret, "type": "payment_intent"},
                    headers={"Accept": "application/json"},
                    timeout=30,
                )
                logger.debug(f"elements/sessions 状态: {resp.status_code}")
            except Exception:
                pass

        raise RuntimeError("无法获取 Stripe publishable key")

    # ── Step 3: 创建支付方式 (卡片 tokenization) ──
    def create_payment_method(self) -> str:
        """
        POST /v1/payment_methods
        先将卡片信息 tokenize, 返回 pm_xxx ID
        Stripe 限制直接在 confirm 中提交原始卡号
        """
        logger.info("[支付 3.5/5] 创建 Payment Method (卡片 tokenization)...")

        card = self.config.card
        billing = self.config.billing
        fp = self.fingerprint.get_params()

        form_data = {
            "type": "card",
            "card[number]": card.number,
            "card[cvc]": card.cvc,
            "card[exp_month]": card.exp_month,
            "card[exp_year]": card.exp_year,
            "billing_details[name]": billing.name,
            "billing_details[email]": billing.email or self.auth.email,
            "billing_details[address][country]": billing.country,
            "billing_details[address][line1]": billing.address_line1,
            "billing_details[address][state]": billing.address_state,
            "billing_details[address][postal_code]": billing.postal_code,
            "allow_redisplay": "always",
            "guid": fp["guid"],
            "muid": fp["muid"],
            "sid": fp["sid"],
            "payment_user_agent": f"stripe.js/{self.config.stripe_build_hash}; stripe-js-v3/{self.config.stripe_build_hash}; checkout",
        }

        headers = {
            "Authorization": f"Bearer {self.stripe_pk}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Origin": "https://js.stripe.com",
            "Referer": "https://js.stripe.com/",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
            ),
        }

        # Stripe API 使用配置的代理 (可能直连或走代理)
        stripe_session = create_http_session(proxy=self._stripe_proxy)
        resp = stripe_session.post(
            "https://api.stripe.com/v1/payment_methods",
            headers=headers,
            data=form_data,
            timeout=30,
        )

        if resp.status_code != 200:
            # 保存原始 Stripe 响应供 UI 展示
            try:
                self.result.confirm_response = resp.json()
            except Exception:
                self.result.confirm_response = {"raw": resp.text[:500]}
            self.result.confirm_status = str(resp.status_code)

            err = resp.text[:300]
            try:
                err = resp.json().get("error", {}).get("message", err)
            except Exception:
                pass
            raise RuntimeError(f"创建 Payment Method 失败 ({resp.status_code}): {err}")

        pm_data = resp.json()
        pm_id = pm_data.get("id", "")
        logger.info(f"Payment Method ID: {pm_id[:20]}...")
        return pm_id

    # 数字商品 VAT/GST 税率表 (用于 automatic_tax 场景下计算 expected_amount)
    COUNTRY_TAX_RATES = {
        "US": 0.00,     # 大部分州数字商品免税 (但有例外)
        "GB": 0.20,     # UK VAT 20%
        "DE": 0.19,     # Germany 19%
        "FR": 0.20,     # France 20%
        "JP": 0.10,     # Japan 10%
        "SG": 0.09,     # Singapore GST 9%
        "HK": 0.00,     # Hong Kong 0%
        "KR": 0.10,     # Korea 10%
        "AU": 0.10,     # Australia GST 10%
        "CA": 0.05,     # Canada GST 5% (最低, HST varies)
        "NL": 0.21,     # Netherlands 21%
        "IT": 0.22,     # Italy 22%
        "ES": 0.21,     # Spain 21%
        "CH": 0.081,    # Switzerland 8.1%
        "IE": 0.23,     # Ireland 23%
        "SE": 0.25,     # Sweden 25%
        "NO": 0.25,     # Norway 25%
        "DK": 0.25,     # Denmark 25%
        "BE": 0.21,     # Belgium 21%
        "AT": 0.20,     # Austria 20%
        "PT": 0.23,     # Portugal 23%
        "FI": 0.255,    # Finland 25.5%
        "PL": 0.23,     # Poland 23%
        "CZ": 0.21,     # Czech Republic 21%
    }

    # ── Step 3.7: 初始化支付页面 + 获取 expected_amount ──
    def fetch_payment_page_details(self, checkout_session_id: str) -> int:
        """
        初始化 Stripe 支付页面并获取 expected_amount (含税):
        1) POST /v1/payment_pages/{cs_id}/init  → 获取 base amount, eid, init_checksum
        2) 根据 billing_country 的 automatic_tax 税率计算含税金额
        """
        logger.info("[支付 3.7/5] 初始化支付页面 & 获取 expected_amount...")

        stripe_session = create_http_session(proxy=self._stripe_proxy)

        headers_form = {
            "Authorization": f"Bearer {self.stripe_pk}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Origin": "https://js.stripe.com",
            "Referer": "https://js.stripe.com/",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
            ),
        }

        # 1) payment_pages/{cs}/init
        init_url = f"https://api.stripe.com/v1/payment_pages/{checkout_session_id}/init"
        init_form = {
            "key": self.stripe_pk,
            "browser_locale": "en",
        }
        init_resp = stripe_session.post(init_url, headers=headers_form, data=init_form, timeout=30)
        if init_resp.status_code != 200:
            logger.warning(f"payment_pages/init 失败 {init_resp.status_code}: {init_resp.text[:500]}")
            self._expected_amount = "0"
            self._init_eid = ""
            self._init_checksum = ""
            return 0

        init_data = init_resp.json()

        # 保存 eid 和 init_checksum (confirm 时需要)
        self._init_eid = init_data.get("eid", "")
        self._init_checksum = init_data.get("init_checksum", "")
        # 保存 stripe_hosted_url (hCaptcha 打码用)
        self._stripe_hosted_url = init_data.get("stripe_hosted_url", "")

        # 提取基础金额 (税前)
        total_summary = init_data.get("total_summary", {})
        base_amount = total_summary.get("due", 0)
        logger.info(f"init base amount: {base_amount} (total_summary.due)")

        # 检查是否需要计算税金
        tax_meta = init_data.get("tax_meta", {})
        auto_tax = init_data.get("tax_context", {}).get("automatic_tax_enabled", False)

        if auto_tax and tax_meta.get("status") == "requires_location_inputs":
            # 需要根据 billing country 的税率计算含税金额
            billing_country = self.config.billing.country
            tax_rate = self.COUNTRY_TAX_RATES.get(billing_country, 0.0)
            amount_with_tax = round(base_amount * (1 + tax_rate))
            logger.info(f"automatic_tax: country={billing_country}, rate={tax_rate*100:.1f}%, "
                        f"base={base_amount}, with_tax={amount_with_tax}")
            self._expected_amount = str(amount_with_tax)
            return amount_with_tax
        else:
            # 税已包含或不需要税
            logger.info(f"expected_amount (no tax adj): {base_amount}")
            self._expected_amount = str(base_amount) if base_amount else "0"
            return base_amount

    # ── Step 4: 确认支付 ──
    def confirm_payment(self, checkout_session_id: str) -> PaymentResult:
        """
        POST /v1/payment_pages/{checkout_session_id}/confirm
        使用已 tokenized 的 payment_method 确认支付
        """
        logger.info("[支付 4/5] 确认支付...")

        fp = self.fingerprint.get_params()
        expected = getattr(self, '_expected_amount', "0")
        eid = getattr(self, '_init_eid', "")
        checksum = getattr(self, '_init_checksum', "")

        # Stripe confirm 使用 application/x-www-form-urlencoded
        form_data = {
            "payment_method": self.payment_method_id,
            "guid": fp["guid"],
            "muid": fp["muid"],
            "sid": fp["sid"],
            "expected_amount": expected,
            "key": self.stripe_pk,
        }
        # 包含 init 上下文 (如果有)
        if eid:
            form_data["eid"] = eid
        if checksum:
            form_data["init_checksum"] = checksum

        logger.info(f"confirm 参数: expected_amount={expected}, pm={self.payment_method_id[:20]}...")

        headers = {
            "Authorization": f"Bearer {self.stripe_pk}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Origin": "https://js.stripe.com",
            "Referer": "https://js.stripe.com/",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
            ),
        }

        url = f"https://api.stripe.com/v1/payment_pages/{checkout_session_id}/confirm"
        stripe_session = create_http_session(proxy=self._stripe_proxy)
        resp = stripe_session.post(url, headers=headers, data=form_data, timeout=60)

        self.result.confirm_status = str(resp.status_code)
        try:
            self.result.confirm_response = resp.json()
        except Exception:
            self.result.confirm_response = {"raw": resp.text[:500]}

        if resp.status_code == 200:
            data = resp.json()
            status = data.get("status", "")
            pi = data.get("payment_intent") or {}
            pi_status = pi.get("status", "")
            next_action = pi.get("next_action", {})

            if status == "complete" or (status == "open" and pi_status == "succeeded"):
                self.result.success = True
                logger.info("支付确认成功!")
            elif pi_status == "requires_action" and next_action:
                # Stripe Radar challenge / 3DS
                sdk_info = next_action.get("use_stripe_sdk", {})
                challenge_type = sdk_info.get("type", "")

                if challenge_type == "intent_confirmation_challenge":
                    logger.info("Stripe 要求 hCaptcha 挑战验证 (intent_confirmation_challenge)")
                    stripe_js = sdk_info.get("stripe_js", {})
                    site_key = stripe_js.get("site_key", "")
                    rqdata = stripe_js.get("rqdata", "")
                    verification_url = stripe_js.get("verification_url", "")
                    pi_id = pi.get("id", "")
                    pi_client_secret = pi.get("client_secret", "")

                    # ── 策略1: 用真实浏览器处理 hCaptcha ──
                    browser_result = self._handle_challenge_with_browser(
                        pi_client_secret, site_key=site_key, rqdata=rqdata
                    )
                    if browser_result and browser_result.get("success"):
                        self.result.success = True
                        self.result.confirm_response = browser_result
                        logger.info("浏览器处理 hCaptcha 成功!")
                    else:
                        browser_err = browser_result.get("error", "") if browser_result else "browser not available"
                        logger.warning(f"浏览器处理失败: {browser_err}, 回退到打码服务...")

                        # ── 策略2: YesCaptcha 打码 (回退) ──
                        max_rounds = 5
                        for round_num in range(1, max_rounds + 1):
                            logger.info(f"打码 第{round_num}轮 (最多{max_rounds}轮)")
                            if not (site_key and verification_url and pi_id):
                                self.result.error = f"挑战参数不完整: site_key={bool(site_key)}, url={bool(verification_url)}"
                                logger.error(self.result.error)
                                break

                            challenge_result = self._handle_stripe_challenge(
                                pi_id=pi_id,
                                site_key=site_key,
                                rqdata=rqdata,
                                verification_url=verification_url,
                                client_secret=pi_client_secret,
                            )

                            if challenge_result is True:
                                self.result.success = True
                                logger.info("打码挑战验证完成, 支付成功!")
                                break
                            elif isinstance(challenge_result, dict):
                                site_key = challenge_result.get("site_key", site_key)
                                rqdata = challenge_result.get("rqdata", "")
                                verification_url = challenge_result.get("verification_url", verification_url)
                                pi_client_secret = challenge_result.get("client_secret", pi_client_secret)
                                logger.info(f"第{round_num}轮通过, 但 Stripe 发起新一轮挑战...")
                                continue
                            else:
                                self.result.error = "hCaptcha 挑战验证失败"
                                break
                        else:
                            self.result.error = f"hCaptcha 挑战超过最大轮数 ({max_rounds})"
                elif next_action.get("type") == "redirect_to_url":
                    logger.warning("支付需要 3DS 网页验证，无法自动完成")
                    self.result.error = "requires_3ds_redirect"
                else:
                    logger.warning(f"未知的 next_action 类型: {challenge_type or next_action.get('type')}")
                    self.result.error = f"requires_action: {challenge_type or next_action.get('type')}"
            elif status in ("succeeded", "complete"):
                self.result.success = True
                logger.info("支付确认成功!")
            else:
                self.result.error = f"支付状态异常: session={status}, pi={pi_status}"
                logger.error(self.result.error)
        else:
            error_msg = ""
            try:
                err_data = resp.json()
                error_msg = err_data.get("error", {}).get("message", resp.text[:300])
            except Exception:
                error_msg = resp.text[:300]
            self.result.error = f"支付确认失败 ({resp.status_code}): {error_msg}"
            logger.error(self.result.error)

        return self.result

    # ── Step 4b: 代理切换重试 confirm ──
    def confirm_payment_with_proxy(self, checkout_session_id: str, proxy: str = None) -> PaymentResult:
        """
        使用指定代理重新走 tokenize + init + confirm (代理开关策略)。
        PaymentMethod 在首次 confirm 后已被消费，必须重新创建。
        """
        logger.info(f"[支付 4b] 代理切换全流程重试 (proxy={proxy})...")

        # 1) 重新创建 PaymentMethod (用新代理)
        stripe_session = create_http_session(proxy=proxy)
        card = self.config.card
        billing = self.config.billing
        fp = self.fingerprint.get_params()

        pm_form = {
            "type": "card",
            "card[number]": card.number,
            "card[cvc]": card.cvc,
            "card[exp_month]": card.exp_month,
            "card[exp_year]": card.exp_year,
            "billing_details[name]": billing.name,
            "billing_details[email]": billing.email or self.auth.email,
            "billing_details[address][country]": billing.country,
            "billing_details[address][line1]": billing.address_line1,
            "billing_details[address][state]": billing.address_state,
            "billing_details[address][postal_code]": billing.postal_code,
            "allow_redisplay": "always",
            "guid": fp["guid"],
            "muid": fp["muid"],
            "sid": fp["sid"],
            "payment_user_agent": f"stripe.js/{self.config.stripe_build_hash}; stripe-js-v3/{self.config.stripe_build_hash}; checkout",
        }

        headers = {
            "Authorization": f"Bearer {self.stripe_pk}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Origin": "https://js.stripe.com",
            "Referer": "https://js.stripe.com/",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
            ),
        }

        resp = stripe_session.post(
            "https://api.stripe.com/v1/payment_methods",
            headers=headers,
            data=pm_form,
            timeout=30,
        )
        if resp.status_code != 200:
            self.result.error = f"代理切换: 创建 PaymentMethod 失败 ({resp.status_code})"
            logger.error(self.result.error)
            return self.result

        new_pm_id = resp.json().get("id", "")
        logger.info(f"代理切换: 新 PaymentMethod: {new_pm_id[:20]}...")

        # 2) 重新 init (获取新 eid/checksum, 但复用原始 expected_amount)
        init_url = f"https://api.stripe.com/v1/payment_pages/{checkout_session_id}/init"
        init_form = {"key": self.stripe_pk, "browser_locale": "en"}
        init_resp = stripe_session.post(init_url, headers=headers, data=init_form, timeout=30)
        eid = ""
        checksum = ""
        # 复用首次 confirm 的 expected_amount (代理 IP 可能导致税率不同)
        expected = getattr(self, '_expected_amount', "0")
        if init_resp.status_code == 200:
            init_data = init_resp.json()
            eid = init_data.get("eid", "")
            checksum = init_data.get("init_checksum", "")
            logger.info(f"代理切换: init eid={eid[:10]}... expected_amount={expected} (复用原值)")

        # 3) Confirm
        form_data = {
            "payment_method": new_pm_id,
            "guid": fp["guid"],
            "muid": fp["muid"],
            "sid": fp["sid"],
            "expected_amount": expected,
            "key": self.stripe_pk,
        }
        if eid:
            form_data["eid"] = eid
        if checksum:
            form_data["init_checksum"] = checksum

        url = f"https://api.stripe.com/v1/payment_pages/{checkout_session_id}/confirm"
        resp = stripe_session.post(url, headers=headers, data=form_data, timeout=60)

        self.result.confirm_status = str(resp.status_code)
        try:
            self.result.confirm_response = resp.json()
        except Exception:
            self.result.confirm_response = {"raw": resp.text[:500]}

        if resp.status_code == 200:
            data = resp.json()
            status = data.get("status", "")
            pi = data.get("payment_intent") or {}
            pi_status = pi.get("status", "")
            next_action = pi.get("next_action", {})

            if status == "complete" or (status == "open" and pi_status == "succeeded"):
                self.result.success = True
                self.result.error = ""
                logger.info("代理切换 confirm 支付成功!")
            elif pi_status == "requires_action" and next_action:
                sdk_info = next_action.get("use_stripe_sdk", {})
                challenge_type = sdk_info.get("type", "")
                if challenge_type == "intent_confirmation_challenge":
                    logger.info("代理切换 confirm 仍触发 hCaptcha，尝试打码...")
                    stripe_js = sdk_info.get("stripe_js", {})
                    pi_id = pi.get("id", "")
                    pi_client_secret = pi.get("client_secret", "")
                    challenge_result = self._handle_stripe_challenge(
                        pi_id=pi_id,
                        site_key=stripe_js.get("site_key", ""),
                        rqdata=stripe_js.get("rqdata", ""),
                        verification_url=stripe_js.get("verification_url", ""),
                        client_secret=pi_client_secret,
                    )
                    if challenge_result is True:
                        self.result.success = True
                        self.result.error = ""
                        logger.info("代理切换 + 打码: 支付成功!")
                    else:
                        self.result.error = "代理切换 confirm 后 hCaptcha 打码失败"
                else:
                    self.result.error = f"代理切换 confirm requires_action: {challenge_type}"
            else:
                self.result.error = f"代理切换 confirm 状态异常: session={status}, pi={pi_status}"
        else:
            error_msg = ""
            try:
                err_data = resp.json()
                error_msg = err_data.get("error", {}).get("message", resp.text[:300])
            except Exception:
                error_msg = resp.text[:300]
            self.result.error = f"代理切换 confirm 失败 ({resp.status_code}): {error_msg}"
            logger.error(self.result.error)

        return self.result

    # ── Step 5a: 浏览器处理 hCaptcha 挑战 ──
    def _handle_challenge_with_browser(self, pi_client_secret: str, site_key: str = "", rqdata: str = "") -> dict:
        """
        用真实 Chromium 浏览器处理 Stripe hCaptcha 挑战。
        两种策略: 先试 Stripe.js handleNextAction, 再试直接 hCaptcha。
        """
        try:
            from browser_challenge import BrowserChallengeSolver
        except ImportError:
            logger.warning("browser_challenge 模块不可用")
            return {"success": False, "error": "browser_challenge not available"}

        solver = BrowserChallengeSolver(
            stripe_pk=self.stripe_pk,
            proxy=self._stripe_proxy,
            headless=False,  # 需要 WSLg 显示
        )

        # 策略1: undetected-chromedriver (最佳反检测)
        if site_key:
            logger.info("尝试 undetected-chromedriver 处理 hCaptcha...")
            site_url = getattr(self, '_stripe_hosted_url', '') or self.checkout_url or "https://js.stripe.com"
            hc_result = solver.solve_hcaptcha_uc(
                site_key=site_key,
                site_url=site_url,
                rqdata=rqdata,
                timeout=60,
            )
            if hc_result.get("success"):
                # 提交浏览器 token — 必须同时发送 token 和 ekey 两个字段
                captcha_token = hc_result["token"]
                captcha_ekey = hc_result.get("ekey", "")
                pi_id = pi_client_secret.split("_secret_")[0] if "_secret_" in pi_client_secret else ""
                headers = {
                    "Authorization": f"Bearer {self.stripe_pk}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                    "Origin": "https://js.stripe.com",
                    "Referer": "https://js.stripe.com/",
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
                    ),
                }
                form_data = {
                    "client_secret": pi_client_secret,
                    "challenge_response_token": captcha_token,
                    "challenge_response_ekey": captcha_ekey,
                    "key": self.stripe_pk,
                }
                verify_url = f"https://api.stripe.com/v1/payment_intents/{pi_id}/verify_challenge"
                logger.info(f"[Browser] 提交 token(len={len(captcha_token)}) + ekey({captcha_ekey[:20]}...)")
                stripe_session = create_http_session(proxy=self._stripe_proxy)
                resp = stripe_session.post(verify_url, headers=headers, data=form_data, timeout=60)
                logger.info(f"[Browser] verify_challenge 状态: {resp.status_code}")
                if resp.status_code == 200:
                    data = resp.json()
                    pi_status = data.get("status", "")
                    last_err = data.get("last_payment_error", {})
                    logger.info(f"[Browser] verify 后 pi 状态: {pi_status}")
                    if last_err:
                        logger.info(f"[Browser] last_payment_error: {last_err.get('code', '')} - {last_err.get('message', '')[:200]}")
                    if pi_status in ("succeeded", "processing"):
                        return {"success": True, "status": pi_status}
                    if pi_status == "requires_payment_method":
                        # hCaptcha 已通过! 但卡被拒绝 — 需要新卡或重试
                        err_msg = last_err.get("message", "card declined") if last_err else "requires new payment method"
                        return {"success": False, "error": f"hCaptcha PASSED but card declined: {err_msg}", "hcaptcha_passed": True, "pi_status": pi_status}
                    return {"success": False, "error": f"browser token → {pi_status}"}
                else:
                    try:
                        err_detail = resp.json().get("error", {}).get("message", resp.text[:300])
                    except Exception:
                        err_detail = resp.text[:300]
                    logger.warning(f"[Browser] verify_challenge 失败: {resp.status_code}: {err_detail}")
                    return {"success": False, "error": f"verify_challenge {resp.status_code}: {err_detail}"}

        # 策略2: Stripe.js handleNextAction (备选)
        logger.info("尝试 Stripe.js handleNextAction 处理挑战...")
        result = solver.solve(pi_client_secret, timeout=60)
        return result

    # ── Step 5b: YesCaptcha 打码处理 hCaptcha 挑战 ──
    def _handle_stripe_challenge(
        self, pi_id: str, site_key: str, rqdata: str, verification_url: str,
        client_secret: str = "",
    ):
        """
        解决 Stripe intent_confirmation_challenge:
        1. 用 YesCaptcha 打码 hCaptcha
        2. POST /v1/payment_intents/{pi_id}/verify_challenge
        返回: True (成功), dict (需要新一轮挑战), False (失败)
        """
        if not self.config.captcha.client_key:
            logger.error("未配置打码服务 API Key，无法解决 hCaptcha 挑战")
            return False

        solver = CaptchaSolver(
            api_url=self.config.captcha.api_url,
            client_key=self.config.captcha.client_key,
        )

        # hCaptcha 的 siteURL 应该是真实的 Stripe checkout 页面
        site_url = getattr(self, '_stripe_hosted_url', '') or self.checkout_url or "https://js.stripe.com"
        ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36")
        captcha_result = solver.solve_hcaptcha(
            site_key=site_key,
            site_url=site_url,
            rqdata=rqdata,
            user_agent=ua,
            proxy="",  # proxyless 模式 (YesCaptcha 无法连接本地代理)
        )
        if not captcha_result:
            return False

        captcha_token = captcha_result["token"]
        captcha_ekey = captcha_result.get("ekey", "")

        # 提交验证
        logger.info(f"[支付 5/5] 提交 hCaptcha 挑战验证: {pi_id[:20]}...")

        verify_url = f"https://api.stripe.com{verification_url}" if verification_url.startswith("/") else verification_url

        headers = {
            "Authorization": f"Bearer {self.stripe_pk}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Origin": "https://js.stripe.com",
            "Referer": "https://js.stripe.com/",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
            ),
        }

        form_data = {}
        if client_secret:
            form_data["client_secret"] = client_secret
        form_data["challenge_response_token"] = captcha_token
        form_data["challenge_response_ekey"] = captcha_ekey
        form_data["key"] = self.stripe_pk

        stripe_session = create_http_session(proxy=self._stripe_proxy)
        resp = stripe_session.post(verify_url, headers=headers, data=form_data, timeout=60)

        logger.info(f"verify_challenge 状态: {resp.status_code}")
        logger.debug(f"verify_challenge 响应: {resp.text[:500]}")
        try:
            result = resp.json()
            self.result.confirm_response = result

            if resp.status_code != 200:
                err_msg = result.get("error", {}).get("message", "")
                err_code = result.get("error", {}).get("code", "")
                logger.error(f"verify_challenge 错误: {resp.status_code} code={err_code} msg={err_msg}")
                return False

            pi_status = result.get("status", "")
            logger.info(f"verify_challenge 后 payment_intent 状态: {pi_status}")
            if pi_status in ("succeeded", "processing"):
                return True
            elif pi_status == "requires_action":
                # 检查是否又是 intent_confirmation_challenge (需要再来一轮)
                next_act = result.get("next_action", {})
                sdk_info = next_act.get("use_stripe_sdk", {})
                if sdk_info.get("type") == "intent_confirmation_challenge":
                    new_stripe_js = sdk_info.get("stripe_js", {})
                    return {
                        "site_key": new_stripe_js.get("site_key", ""),
                        "rqdata": new_stripe_js.get("rqdata", ""),
                        "verification_url": new_stripe_js.get("verification_url", ""),
                        "client_secret": result.get("client_secret", client_secret),
                    }
                logger.warning(f"verify_challenge 后需要非 hCaptcha 验证: {next_act}")
                return False
            else:
                logger.error(f"verify_challenge 后状态异常: {pi_status}")
                return False
        except Exception as e:
            logger.error(f"verify_challenge 响应解析失败: {e}, raw={resp.text[:300]}")
            return False

    # ── 完整支付流程 ──
    def run_payment(self) -> PaymentResult:
        """执行完整支付链路: checkout -> fingerprint -> extract PK -> tokenize card -> fetch amount -> confirm -> challenge"""
        try:
            cs_id = self.create_checkout_session()
            self.fetch_stripe_fingerprint()
            self.extract_stripe_pk(self.checkout_url)
            self.payment_method_id = self.create_payment_method()
            self.fetch_payment_page_details(cs_id)
            return self.confirm_payment(cs_id)
        except Exception as e:
            self.result.error = str(e)
            logger.error(f"支付流程异常: {e}")
            return self.result
