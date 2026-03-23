"""
自动化绑卡支付 - 主入口
用法:
  1. 全流程（注册 + 支付）:
     python main.py --config config.json

  2. 仅支付（已有凭证）:
     python main.py --config config.json --skip-register

  3. 交互式输入卡信息:
     python main.py --config config.json --interactive
"""
import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime

from config import Config, CardInfo
from mail_provider import MailProvider
from auth_flow import AuthFlow, AuthResult
from payment_flow import PaymentFlow
from logger import setup_logging, ResultStore

logger = logging.getLogger("main")


def interactive_card_input() -> CardInfo:
    """交互式输入卡信息"""
    print("\n=== 请输入信用卡信息 ===")
    card = CardInfo()
    card.number = input("卡号: ").strip().replace(" ", "")
    card.exp_month = input("到期月份 (MM): ").strip()
    card.exp_year = input("到期年份 (YY or YYYY): ").strip()
    card.cvc = input("CVC: ").strip()
    return card


def save_result(result: dict, prefix: str = "result"):
    """保存结果到文件（兼容直接调用）"""
    os.makedirs("outputs", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = f"outputs/{prefix}_{ts}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info(f"结果已保存: {path}")
    return path


def run_full_flow(config: Config, skip_register: bool = False):
    """执行完整流程"""

    store = ResultStore()

    final_result = {
        "timestamp": datetime.now().isoformat(),
        "auth": {},
        "payment": {},
    }

    # ── 阶段 1: 注册/登录 ──
    auth_flow = AuthFlow(config)

    if skip_register:
        if not (config.session_token and config.access_token):
            logger.error("跳过注册模式需要提供 session_token 和 access_token")
            sys.exit(1)
        auth_result = auth_flow.from_existing_credentials(
            session_token=config.session_token,
            access_token=config.access_token,
            device_id=config.device_id or "",
        )
        logger.info("使用已有凭证，跳过注册")
    else:
        mail = MailProvider(
            worker_domain=config.mail.worker_domain,
            admin_token=config.mail.admin_token,
            email_domain=config.mail.email_domain,
        )
        auth_result = auth_flow.run_register(mail)
        logger.info(f"注册成功: {auth_result.email}")
        # 保存凭证到 JSON 和 CSV
        cred_path = store.save_credentials(auth_result.to_dict())
        csv_path = store.append_credentials_csv(auth_result.to_dict())
        logger.info(f"凭证已保存: {cred_path}")
        logger.info(f"凭证已追加到: {csv_path}")

    final_result["auth"] = auth_result.to_dict()

    # ── 阶段 2: 支付 ──
    if not config.card.number:
        logger.error("未配置信用卡信息，无法执行支付")
        path = store.save_result(final_result, "register_only")
        store.append_history(
            email=auth_result.email,
            status="register_only",
            detail_file=path,
        )
        return final_result

    # 如果 billing email 为空，使用注册邮箱
    if not config.billing.email:
        config.billing.email = auth_result.email

    payment_flow = PaymentFlow(config, auth_result)
    payment_result = payment_flow.run_payment()
    final_result["payment"] = payment_result.to_dict()

    # ── 保存结果 ──
    prefix = "success" if payment_result.success else "failed"
    path = store.save_result(final_result, prefix)

    # ── 追加历史 ──
    store.append_history(
        email=auth_result.email,
        status=prefix,
        checkout_session_id=payment_result.checkout_session_id,
        payment_status=payment_result.confirm_status,
        error=payment_result.error,
        detail_file=path,
    )

    # ── 输出摘要 ──
    print("\n" + "=" * 60)
    if payment_result.success:
        print("✅ 绑卡支付成功!")
    elif payment_result.error == "requires_3ds_verification":
        print("⚠️  支付需要 3DS 验证，请手动完成")
    else:
        print(f"❌ 支付失败: {payment_result.error}")
    print(f"   邮箱: {auth_result.email}")
    print(f"   Checkout Session: {payment_result.checkout_session_id[:30]}...")
    print("=" * 60)

    return final_result


def main():
    parser = argparse.ArgumentParser(description="自动化绑卡支付")
    parser.add_argument("--config", "-c", default="config.json", help="配置文件路径")
    parser.add_argument("--skip-register", action="store_true", help="跳过注册，使用已有凭证")
    parser.add_argument("--interactive", "-i", action="store_true", help="交互式输入卡信息")
    parser.add_argument("--debug", action="store_true", help="启用调试日志")
    args = parser.parse_args()

    if args.debug:
        log_file = setup_logging(debug=True)
    else:
        log_file = setup_logging(debug=False)
    logger.info(f"日志文件: {log_file}")

    # 加载配置
    if os.path.exists(args.config):
        config = Config.from_file(args.config)
        logger.info(f"配置已加载: {args.config}")
    else:
        config = Config()
        logger.warning(f"配置文件 {args.config} 不存在，使用默认配置")

    # 交互式卡信息
    if args.interactive:
        config.card = interactive_card_input()

    run_full_flow(config, skip_register=args.skip_register)


if __name__ == "__main__":
    main()
