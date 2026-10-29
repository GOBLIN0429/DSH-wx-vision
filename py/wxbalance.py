#!/usr/bin/env python3
"""Print the DeepSeek account balance in one short line.

The whale widget in the DSH web UI shows exactly this number, but it renders it
on a canvas — reading it back would mean clicking the whale and OCR-ing a
screenshot. The widget itself simply calls /user/balance with
DEEPSEEK_API_KEY, so ask the same endpoint and skip the pixels.
"""
import json
import os
import pathlib
import re
import sys
import urllib.request

CRED = pathlib.Path.home() / "Library/Application Support/dsh-desktop/harness/.credentials.yaml"
URL = "https://api.deepseek.com/user/balance"


def api_key():
    try:
        m = re.search(r"DEEPSEEK_API_KEY:\s*(\S+)", CRED.read_text())
        return m.group(1) if m else None
    except Exception:
        return None


def main():
    key = api_key()
    if not key:
        print("查不到余额（没有找到 API key）")
        return 1
    req = urllib.request.Request(URL, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
    except Exception as e:
        print(f"查不到余额（接口出错：{type(e).__name__}）")
        return 2

    if not d.get("is_available", False):
        print("余额不可用（账户被停用或欠费）")
        return 3

    infos = d.get("balance_infos") or []
    if not infos:
        print("查不到余额（接口没返回余额项）")
        return 3

    def pick(x):
        if x.get("currency") == "CNY":
            return x
        return infos[0]

    info = pick(infos[0])
    total = info.get("total_balance", "?")
    granted = info.get("granted_balance", "0")
    topped = info.get("topped_up_balance", "0")
    cur = info.get("currency", "")
    symbol = "¥" if cur == "CNY" else f"{cur} "
    print(f"{symbol}{total}（充值 {topped}，赠送 {granted}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
