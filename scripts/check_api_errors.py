"""API 错误响应自检：畸形请求该得到能行动的 4xx，而不是无用的 500。

存在的理由：平台是自托管的，用户拿到 "Internal Server Error" 时无从判断
是自己请求写错了、还是平台坏了、还是该看日志。本该 422/404 的输入错误
逃逸成 500，等于把排查成本全甩给用户。

只读为主：所有请求要么是 GET，要么是必然被校验拦下的畸形 POST
（拦不下的话正好就是要找的缺陷——脚本会报出来）。

用法：
    .venv/bin/python scripts/check_api_errors.py --token smallmodel-lab
退出码：0 全部合理 · 1 有无用 500。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

GREEN, RED, YELLOW, DIM, RESET = ("\x1b[32m", "\x1b[31m", "\x1b[33m", "\x1b[2m", "\x1b[0m")


def call(server: str, token: str, method: str, path: str, body=None):
    safe_path = urllib.parse.quote(path, safe="/?=&")   # 请求行只能是 ascii
    request = urllib.request.Request(
        server.rstrip("/") + safe_path, method=method,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read().decode("utf-8", errors="replace")[:300]
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", errors="replace")[:300]
    except Exception as error:  # noqa: BLE001
        return 0, str(error)[:200]


class Check:
    def __init__(self, server: str, token: str):
        self.server, self.token = server, token
        self.bad: list[str] = []

    def probe(self, name: str, method: str, path: str, body=None) -> None:
        """畸形请求：4xx 算正确（拦住了），500 算缺陷，2xx 视情况提示。"""
        status, text = call(self.server, self.token, method, path, body)
        detail = text.replace("\n", " ")[:90]
        if 400 <= status < 500:
            print(f"  {GREEN}✓{RESET} {name}  {DIM}{status} · {detail}{RESET}")
        elif status >= 500 or status == 0:
            print(f"  {RED}✕{RESET} {name}  {DIM}{status} · {detail}{RESET}")
            self.bad.append(f"{name}: {status} {detail}")
        else:
            print(f"  {YELLOW}!{RESET} {name}  {DIM}{status}（没被拦下）· {detail}{RESET}")


def main() -> int:
    parser = argparse.ArgumentParser(description="API 错误响应自检")
    parser.add_argument("--server", default=os.getenv("SMOKE_SERVER", "http://127.0.0.1:8000"))
    parser.add_argument("--token", default=os.getenv("API_TOKEN", ""))
    args = parser.parse_args()
    if not args.token:
        print("需要 --token", file=sys.stderr)
        return 2

    check = Check(args.server, args.token)
    print(f"API 错误响应自检 · {args.server}")

    print(f"\n{DIM}— 路径参数不存在 / 畸形 —{RESET}")
    check.probe("未知运行", "GET", "/api/v1/runs/不存在的id")
    check.probe("未知运行的事件", "GET", "/api/v1/runs/nope/events/list")
    check.probe("未知运行的产物", "GET", "/api/v1/runs/nope/artifacts")
    check.probe("未知应用的草稿", "GET", "/api/v1/applications/nope/draft")
    check.probe("未知应用的运行列表", "GET", "/api/v1/applications/nope/runs")
    check.probe("未知构建", "GET", "/api/v1/builds/nope")
    check.probe("路径里带斜杠", "GET",
                "/api/v1/runs/" + urllib.parse.quote("a/b", safe=""))
    check.probe("路径里带 emoji", "GET",
                "/api/v1/runs/" + urllib.parse.quote("🙂", safe=""))

    print(f"\n{DIM}— 查询参数越界 —{RESET}")
    check.probe("limit=0", "GET", "/api/v1/health-report?days=0")
    check.probe("days 负数", "GET", "/api/v1/health-report?days=-5")
    check.probe("days 超大", "GET", "/api/v1/health-report?days=99999")
    check.probe("days 不是数字", "GET", "/api/v1/health-report?days=abc")
    check.probe("事件 limit 负数", "GET", "/api/v1/runs/nope/events/list?limit=-1")

    print(f"\n{DIM}— 请求体畸形（必然被校验拦下的）—{RESET}")
    check.probe("建应用：缺 name", "POST", "/api/v1/applications", {"description": "x"})
    check.probe("建应用：name 是数组", "POST", "/api/v1/applications",
                {"name": [1, 2], "requirement": "x"})
    check.probe("建应用：name 空串", "POST", "/api/v1/applications",
                {"name": "", "requirement": "x"})
    check.probe("建应用：name 超长", "POST", "/api/v1/applications",
                {"name": "x" * 5000, "requirement": "y"})
    check.probe("草稿操作：未知 op", "POST", "/api/v1/applications/nope/draft",
                {"expected_revision": 0, "idempotency_key": "k", "op": "什么操作",
                 "data": {}})
    check.probe("草稿操作：revision 负数", "POST", "/api/v1/applications/nope/draft",
                {"expected_revision": -1, "idempotency_key": "k", "op": "add_node",
                 "data": {}})
    check.probe("登录：缺密码", "POST", "/api/v1/auth/login", {"name": "x"})
    check.probe("注册：令牌不对", "POST", "/api/v1/auth/register",
                {"register_token": "错的", "name": "x", "password": "y"})
    check.probe("管家：messages 不是数组", "POST", "/api/v1/assistant/agent",
                {"messages": "你好"})
    check.probe("管家：空 messages", "POST", "/api/v1/assistant/agent", {"messages": []})

    print(f"\n{DIM}— 鉴权 —{RESET}")
    # 令牌用 ASCII：HTTP 头是 latin-1，非 ASCII 会卡在客户端，测不到平台
    saved, check.token = check.token, "definitely-not-a-valid-token"
    check.probe("错令牌访问总览", "GET", "/api/v1/overview")
    check.probe("错令牌建应用", "POST", "/api/v1/applications",
                {"name": "x", "requirement": "y"})
    check.token = saved

    print()
    if check.bad:
        print(f"{RED}✕ {len(check.bad)} 个无用 500{RESET}")
        for item in check.bad:
            print(f"  · {item}")
        return 1
    print(f"{GREEN}✓ 畸形请求都得到了能行动的回应{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
