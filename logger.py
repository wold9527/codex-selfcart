"""
日志管理 & 结果持久化
- 文件日志 (logs/)
- 结构化结果存储 (outputs/)
- 运行历史追踪
"""
import json
import logging
import os
import csv
from datetime import datetime
from typing import Optional


def setup_logging(debug: bool = False, log_dir: str = "logs") -> str:
    """
    配置日志系统:
    - 终端: INFO 级别彩色输出
    - 文件: DEBUG 级别完整日志
    返回日志文件路径
    """
    os.makedirs(log_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(log_dir, f"run_{ts}.log")

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)

    # 清除旧 handler
    root.handlers.clear()

    # 终端 handler
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if debug else logging.INFO)
    console.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))
    root.addHandler(console)

    # 文件 handler (始终 DEBUG)
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(name)s (%(filename)s:%(lineno)d): %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(fh)

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("curl_cffi").setLevel(logging.WARNING)

    return log_file


class ResultStore:
    """结果持久化管理"""

    def __init__(self, output_dir: str = "outputs"):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
        self.history_file = os.path.join(output_dir, "history.csv")
        self._ensure_history_header()

    def _ensure_history_header(self):
        if not os.path.exists(self.history_file):
            with open(self.history_file, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "email", "status",
                    "checkout_session_id", "payment_status",
                    "error", "detail_file",
                ])

    def save_result(self, result: dict, prefix: str = "result") -> str:
        """保存完整结果到 JSON"""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{prefix}_{ts}.json"
        path = os.path.join(self.output_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        return path

    def append_history(
        self,
        email: str,
        status: str,
        checkout_session_id: str = "",
        payment_status: str = "",
        error: str = "",
        detail_file: str = "",
    ):
        """追加到历史 CSV"""
        with open(self.history_file, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                datetime.now().isoformat(),
                email,
                status,
                checkout_session_id,
                payment_status,
                error,
                detail_file,
            ])

    def save_credentials(self, auth_dict: dict, filename: Optional[str] = None) -> str:
        """单独保存凭证到 JSON"""
        if not filename:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"credentials_{ts}.json"
        path = os.path.join(self.output_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(auth_dict, f, ensure_ascii=False, indent=2)
        return path

    def append_credentials_csv(self, auth_dict: dict):
        """追加凭证到 CSV (accounts.csv) - token 仅保存前 50 字符 + 后 20 字符"""
        csv_path = os.path.join(self.output_dir, "accounts.csv")
        file_exists = os.path.exists(csv_path)

        def _truncate(val: str, head: int = 50, tail: int = 20) -> str:
            if len(val) <= head + tail + 5:
                return val
            return f"{val[:head]}...{val[-tail:]}"

        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow([
                    "timestamp", "email", "password",
                    "session_token", "access_token", "device_id",
                ])
            writer.writerow([
                datetime.now().isoformat(),
                auth_dict.get("email", ""),
                auth_dict.get("password", ""),
                _truncate(auth_dict.get("session_token", "")),
                _truncate(auth_dict.get("access_token", "")),
                auth_dict.get("device_id", ""),
            ])
        return csv_path

    def save_debug_info(self, debug_data: dict) -> str:
        """保存调试信息"""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self.output_dir, f"debug_{ts}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(debug_data, f, ensure_ascii=False, indent=2)
        return path
