#!/usr/bin/env python3
"""
兑换码管理 CLI

用法:
  python3 admin_cli.py generate 10              # 生成 10 个一次性兑换码
  python3 admin_cli.py generate 5 --uses 3      # 生成 5 个可用 3 次的兑换码
  python3 admin_cli.py generate 1 --uses 99 --expires 30 --note "VIP"
  python3 admin_cli.py list                      # 列出所有兑换码
  python3 admin_cli.py info ABC-DEF-GHI          # 查看单个兑换码详情
  python3 admin_cli.py history ABC-DEF-GHI       # 查看兑换码执行历史
"""
import sys

from database import init_db
from code_manager import create_codes, list_all_codes, get_code_info, get_code_history

init_db()


def cmd_generate(args):
    count = int(args[0]) if args else 1
    uses = 1
    expires = None
    note = ""
    i = 1
    while i < len(args):
        if args[i] == "--uses" and i + 1 < len(args):
            uses = int(args[i + 1]); i += 2
        elif args[i] == "--expires" and i + 1 < len(args):
            expires = int(args[i + 1]); i += 2
        elif args[i] == "--note" and i + 1 < len(args):
            note = args[i + 1]; i += 2
        else:
            i += 1

    codes = create_codes(count=count, total_uses=uses, expires_days=expires, note=note)
    print(f"生成 {len(codes)} 个兑换码 (每个可用 {uses} 次):")
    for c in codes:
        print(f"  {c}")


def cmd_list(_args):
    codes = list_all_codes()
    if not codes:
        print("没有兑换码")
        return
    print(f"{'兑换码':<20} {'使用/总量':>10} {'成功':>6} {'失败':>6} {'备注'}")
    print("-" * 70)
    for c in codes:
        print(f"{c['code']:<20} {c['used_count']}/{c['total_uses']:>8} "
              f"{c.get('success_count', 0):>6} {c.get('fail_count', 0):>6} "
              f"{c.get('note', '') or ''}")


def cmd_info(args):
    if not args:
        print("用法: admin_cli.py info <code>"); return
    info = get_code_info(args[0])
    if not info:
        print("兑换码不存在"); return
    for k, v in info.items():
        print(f"  {k}: {v}")


def cmd_history(args):
    if not args:
        print("用法: admin_cli.py history <code>"); return
    rows = get_code_history(args[0])
    if not rows:
        print("无执行记录"); return
    for r in rows:
        print(f"  [{r['status']}] {r['created_at']} | {r.get('email', '-')} | {r.get('error_msg', '') or ''}")


COMMANDS = {
    "generate": cmd_generate,
    "list": cmd_list,
    "info": cmd_info,
    "history": cmd_history,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]](sys.argv[2:])
