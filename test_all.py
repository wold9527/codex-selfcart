"""
自动化绑卡支付 - 单元测试
测试可脱离外部服务运行的模块逻辑
"""
import json
import os
import sys
import uuid
import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

from config import Config, CardInfo, BillingInfo, MailConfig, TeamPlanConfig
from mail_provider import MailProvider
from stripe_fingerprint import StripeFingerprint
from auth_flow import AuthFlow, AuthResult
from payment_flow import PaymentFlow, PaymentResult
from logger import setup_logging, ResultStore


class TestConfig(unittest.TestCase):
    """配置模块测试"""

    def test_default_config(self):
        cfg = Config()
        self.assertEqual(cfg.billing.country, "JP")
        self.assertEqual(cfg.team_plan.plan_name, "chatgptteamplan")
        self.assertIsNone(cfg.proxy)
        self.assertEqual(cfg.stripe_build_hash, "f197c9c0f0")

    def test_config_from_file(self):
        test_data = {
            "card": {"number": "4242424242424242", "cvc": "123", "exp_month": "12", "exp_year": "2030"},
            "billing": {"name": "Test", "country": "US", "currency": "USD"},
            "proxy": "http://127.0.0.1:7890",
        }
        test_path = "/tmp/test_config_bindcard.json"
        with open(test_path, "w") as f:
            json.dump(test_data, f)
        try:
            cfg = Config.from_file(test_path)
            self.assertEqual(cfg.card.number, "4242424242424242")
            self.assertEqual(cfg.billing.country, "US")
            self.assertEqual(cfg.proxy, "http://127.0.0.1:7890")
        finally:
            os.remove(test_path)

    def test_config_to_dict(self):
        cfg = Config()
        d = cfg.to_dict()
        self.assertIn("card", d)
        self.assertIn("billing", d)
        self.assertIn("mail", d)
        self.assertEqual(d["billing"]["country"], "JP")


class TestMailProvider(unittest.TestCase):
    """邮箱服务测试"""

    def test_random_name_format(self):
        mp = MailProvider("https://test.com", "token", "test.com")
        name = mp._random_name()
        self.assertTrue(len(name) >= 7)
        self.assertTrue(name[0].isalpha())

    def test_extract_otp_code_is(self):
        self.assertEqual(MailProvider._extract_otp("Your code is 123456"), "123456")

    def test_extract_otp_chinese(self):
        self.assertEqual(MailProvider._extract_otp("你的代码为 654321"), "654321")

    def test_extract_otp_plain_digits(self):
        self.assertEqual(MailProvider._extract_otp("验证码: 789012"), "789012")

    def test_extract_otp_no_code(self):
        self.assertIsNone(MailProvider._extract_otp("Hello world"))

    @patch.object(MailProvider, '_fetch_emails')
    def test_wait_for_otp_success(self, mock_fetch):
        mock_fetch.return_value = [
            {"source": "noreply@openai.com", "raw": "Your code is 999888"}
        ]
        mp = MailProvider("https://test.com", "token", "test.com")
        mp.jwt = "fake_jwt"
        otp = mp.wait_for_otp("test@test.com", timeout=5)
        self.assertEqual(otp, "999888")

    @patch.object(MailProvider, '_fetch_emails')
    def test_wait_for_otp_timeout(self, mock_fetch):
        mock_fetch.return_value = []
        mp = MailProvider("https://test.com", "token", "test.com")
        mp.jwt = "fake_jwt"
        with self.assertRaises(TimeoutError):
            mp.wait_for_otp("test@test.com", timeout=1)


class TestStripeFingerprint(unittest.TestCase):
    """Stripe 指纹测试"""

    def test_default_values(self):
        fp = StripeFingerprint()
        self.assertTrue(len(fp.muid) == 36)  # UUID v4
        self.assertTrue(len(fp.sid) == 36)

    def test_get_params(self):
        fp = StripeFingerprint()
        fp.guid = "test-guid"
        fp.muid = "test-muid"
        fp.sid = "test-sid"
        params = fp.get_params()
        self.assertEqual(params["guid"], "test-guid")
        self.assertEqual(params["muid"], "test-muid")
        self.assertEqual(params["sid"], "test-sid")

    @patch('stripe_fingerprint.create_http_session')
    def test_fallback_on_failure(self, mock_session_fn):
        mock_session = MagicMock()
        mock_session.post.side_effect = Exception("Network error")
        mock_session_fn.return_value = mock_session
        fp = StripeFingerprint()
        fp.session = mock_session
        result = fp.fetch_from_m_stripe()
        self.assertFalse(result)
        self.assertTrue(len(fp.guid) > 0)  # fallback 生成了值


class TestAuthResult(unittest.TestCase):
    """认证结果测试"""

    def test_is_valid_true(self):
        ar = AuthResult()
        ar.session_token = "token123"
        ar.access_token = "access123"
        self.assertTrue(ar.is_valid())

    def test_is_valid_false(self):
        ar = AuthResult()
        ar.session_token = "token123"
        self.assertFalse(ar.is_valid())

    def test_to_dict(self):
        ar = AuthResult()
        ar.email = "test@test.com"
        ar.session_token = "st"
        d = ar.to_dict()
        self.assertEqual(d["email"], "test@test.com")
        self.assertIn("device_id", d)


class TestPaymentResult(unittest.TestCase):
    """支付结果测试"""

    def test_default(self):
        pr = PaymentResult()
        self.assertFalse(pr.success)
        self.assertEqual(pr.error, "")

    def test_to_dict(self):
        pr = PaymentResult()
        pr.checkout_session_id = "cs_test_xxx"
        pr.success = True
        d = pr.to_dict()
        self.assertEqual(d["checkout_session_id"], "cs_test_xxx")
        self.assertTrue(d["success"])


class TestResultStore(unittest.TestCase):
    """结果持久化测试"""

    def setUp(self):
        self.test_dir = "/tmp/test_bindcard_outputs"
        self.store = ResultStore(output_dir=self.test_dir)

    def tearDown(self):
        import shutil
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def test_save_result(self):
        path = self.store.save_result({"test": True}, "test")
        self.assertTrue(os.path.exists(path))
        with open(path) as f:
            data = json.load(f)
        self.assertTrue(data["test"])

    def test_append_history(self):
        self.store.append_history(
            email="test@test.com",
            status="success",
            checkout_session_id="cs_xxx",
        )
        self.assertTrue(os.path.exists(self.store.history_file))
        with open(self.store.history_file) as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 2)  # header + 1 row

    def test_save_credentials(self):
        path = self.store.save_credentials({"session_token": "xxx"})
        self.assertTrue(os.path.exists(path))


class TestPaymentFlowConfirmFormData(unittest.TestCase):
    """支付 confirm 参数构造测试"""

    def test_confirm_form_data_structure(self):
        """验证 confirm 请求的表单数据包含所有必需字段"""
        config = Config()
        config.card = CardInfo(
            number="4242424242424242", cvc="123",
            exp_month="12", exp_year="2030"
        )
        config.billing = BillingInfo(
            name="Test", email="test@x.com",
            country="JP", currency="JPY",
        )

        auth = AuthResult()
        auth.session_token = "fake_session"
        auth.access_token = "fake_access"
        auth.device_id = str(uuid.uuid4())
        auth.email = "test@x.com"

        pf = PaymentFlow(config, auth)

        # 验证指纹参数存在
        fp_params = pf.fingerprint.get_params()
        self.assertIn("guid", fp_params)
        self.assertIn("muid", fp_params)
        self.assertIn("sid", fp_params)


class TestSetupLogging(unittest.TestCase):
    """日志配置测试"""

    def test_setup_logging_creates_file(self):
        import shutil
        test_dir = "/tmp/test_bindcard_logs"
        try:
            log_file = setup_logging(debug=False, log_dir=test_dir)
            self.assertTrue(os.path.exists(log_file))
        finally:
            if os.path.exists(test_dir):
                shutil.rmtree(test_dir)


if __name__ == "__main__":
    unittest.main(verbosity=2)
