"""金蝶云·星空 WebAPI 探针：三级 ERP 验证阶梯的第二级（真实厂商沙箱）。

只读验证，分四步走，每步都可单独停下检查：
1. 登录（ValidateUser）——验证凭证与会话机制；
2. 元数据摸底（查基础资料，如物料 BD_MATERIAL）——验证查询协议与字段映射；
3. 分页完整性（StartRow/Limit 翻页到尽头）——假 ERP 对练的同款课目；
4. 业务单据试读（如销售出库单 SAL_OUTSTOCK，形态由账套里实际有什么数据决定）。

凭证来源（按序）：环境变量 KINGDEE_BASE_URL / KINGDEE_ACCT_ID /
KINGDEE_USERNAME / KINGDEE_PASSWORD，或 .env 同名行。绝不写入代码或日志。

用法：
  python scripts/kingdee_probe.py --step login
  python scripts/kingdee_probe.py --step query --form BD_MATERIAL --fields FNumber,FName
  python scripts/kingdee_probe.py --step paginate --form BD_MATERIAL --fields FNumber --page-size 50
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOGIN_PATH = "Kingdee.BOS.WebApi.ServicesStub.AuthService.ValidateUser.common.kdsvc"
APPSECRET_LOGIN_PATH = "Kingdee.BOS.WebApi.ServicesStub.AuthService.LoginByAppSecret.common.kdsvc"
QUERY_PATH = "Kingdee.BOS.WebApi.ServicesStub.DynamicFormService.ExecuteBillQuery.common.kdsvc"


def _env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value and (REPO / ".env").is_file():
        for line in (REPO / ".env").read_text().splitlines():
            if line.startswith(f"{name}="):
                value = line.split("=", 1)[1].strip()
    return value


class KingdeeSession:
    def __init__(self) -> None:
        base = _env("KINGDEE_BASE_URL").rstrip("/")
        if not base:
            raise SystemExit(
                "缺少凭证：请在 .env 配置 KINGDEE_BASE_URL / KINGDEE_ACCT_ID / "
                "KINGDEE_USERNAME / KINGDEE_PASSWORD（环境 URL 形如 "
                "https://xxx.ik3cloud.com/k3cloud）"
            )
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def call(self, path: str, payload: dict) -> object:
        request = urllib.request.Request(
            f"{self.base}/{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with self.opener.open(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    def login(self) -> dict:
        # 两种模式自动择路：配置了 KINGDEE_APP_ID/APP_SECRET 走第三方应用
        # 授权（开放平台 MyAppList 里创建的应用），否则走用户名密码。
        app_id = _env("KINGDEE_APP_ID")
        app_secret = _env("KINGDEE_APP_SECRET")
        if app_id and app_secret:
            result = self.call(APPSECRET_LOGIN_PATH, {
                "acctid": _env("KINGDEE_ACCT_ID"),
                "username": _env("KINGDEE_USERNAME"),
                "appid": app_id,
                "appsecret": app_secret,
                "lcid": 2052,
            })
        else:
            result = self.call(LOGIN_PATH, {
                "acctid": _env("KINGDEE_ACCT_ID"),
                "username": _env("KINGDEE_USERNAME"),
                "password": _env("KINGDEE_PASSWORD"),
                "lcid": 2052,
            })
        # LoginResultType: 1=成功；其余带 Message 说明
        if not isinstance(result, dict) or result.get("LoginResultType") != 1:
            message = (result or {}).get("Message") if isinstance(result, dict) else result
            raise SystemExit(f"登录失败：{message}")
        return result

    def query(self, form_id: str, field_keys: str, *, start_row: int = 0, limit: int = 100,
              filter_string: str = "") -> list[list]:
        result = self.call(QUERY_PATH, {"data": {
            "FormId": form_id,
            "FieldKeys": field_keys,
            "FilterString": filter_string,
            "OrderString": "",
            "TopRowCount": 0,
            "StartRow": start_row,
            "Limit": limit,
        }})
        if isinstance(result, list) and result and isinstance(result[0], list) \
                and result[0] and isinstance(result[0][0], dict) and "Result" in result[0][0]:
            # 错误形态：[[{"Result":{"ResponseStatus":{...Errors}}}]]
            errors = result[0][0]["Result"].get("ResponseStatus", {}).get("Errors", [])
            raise SystemExit(f"查询失败：{json.dumps(errors, ensure_ascii=False)[:300]}")
        return result if isinstance(result, list) else []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--step", required=True, choices=["login", "query", "paginate"])
    parser.add_argument("--form", default="BD_MATERIAL")
    parser.add_argument("--fields", default="FNumber,FName")
    parser.add_argument("--page-size", type=int, default=50)
    args = parser.parse_args()

    session = KingdeeSession()
    login = session.login()
    print(f"✓ 登录成功（会话建立，账套用户 {login.get('Context', {}).get('UserName', '?')}）")
    if args.step == "login":
        return 0

    if args.step == "query":
        rows = session.query(args.form, args.fields, limit=5)
        print(f"✓ {args.form} 试读 {len(rows)} 行，首行：{json.dumps(rows[0] if rows else [], ensure_ascii=False)[:160]}")
        return 0

    # paginate：翻到尽头验证分页完整性
    total = 0
    start = 0
    pages = 0
    while True:
        rows = session.query(args.form, args.fields, start_row=start, limit=args.page_size)
        if not rows:
            break
        total += len(rows)
        pages += 1
        start += args.page_size
        if len(rows) < args.page_size:
            break
        if pages > 200:
            print("… 超过 200 页，停止（数据量足够验证分页）")
            break
    print(f"✓ 分页完整性：{pages} 页共 {total} 行（page_size={args.page_size}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
