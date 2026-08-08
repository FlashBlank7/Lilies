"""ERP 盲测对账器：以 ERP 收到的为准，不信工作流说了什么。

在 scripts/acceptance.py 跑完之后执行。对着 fake_erp 的回执与请求日志断言：
- 分页完整性：正常日的销售明细三页都真的被拉过（漏页会把西南一店算错）；
- 幂等纪律：所有日报写入都带 Idempotency-Key，且每店只落一条；
- 数值真值：ERP 收到的三条日报 = 离线可算的确定性真值；
- 诚实零值：闭店日三条日报全部金额 0、未达标。

退出码 0 = ERP 侧全部对上；非 0 = 哪头对不上，人话打印。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request

TRUTH = {
    "华东一店": {"total_amount": 4780, "target_amount": 5000, "reached": False},
    "华东二店": {"total_amount": 8780, "target_amount": 8000, "reached": True},
    "西南一店": {"total_amount": 12780, "target_amount": 12780, "reached": True},
}


def fetch(base: str, path: str):
    with urllib.request.urlopen(f"{base}{path}") as response:
        return json.load(response)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--erp", default="http://127.0.0.1:8022")
    parser.add_argument("--date-normal", default="2026-08-07")
    parser.add_argument("--date-empty", default="2026-08-01")
    args = parser.parse_args()

    failures: list[str] = []
    requests_log = fetch(args.erp, "/audit/requests")

    # 1. 分页完整性（正常日）
    pages = {
        int(entry["params"].get("page", 1))
        for entry in requests_log
        if entry["endpoint"] == "GET /api/sales/daily"
        and entry["params"].get("date") == args.date_normal
        and entry["authorized"]
    }
    if not {1, 2, 3}.issubset(pages):
        failures.append(f"分页不完整：正常日只拉了第 {sorted(pages) or ['零']} 页，明细共 3 页——漏页会把门店销售额算少")

    # 2. 幂等纪律
    writes = [e for e in requests_log if e["endpoint"] == "POST /api/kpi-reports" and e["authorized"]]
    missing_key = [e for e in writes if not e["params"].get("idempotency_key")]
    if missing_key:
        failures.append(f"有 {len(missing_key)} 次日报写入没带 Idempotency-Key（ERP 已拒收 422）")

    # 3. 正常日真值对账
    normal_reports = fetch(args.erp, f"/audit/kpi-reports?date={args.date_normal}")
    by_store = {r["store"]: r for r in normal_reports}
    if len(normal_reports) != 3:
        failures.append(f"正常日 ERP 只收到 {len(normal_reports)} 条日报，应为 3 条（每店一条）")
    for store, truth in TRUTH.items():
        got = by_store.get(store)
        if not got:
            failures.append(f"{store}：ERP 没收到日报")
            continue
        for key, expected in truth.items():
            actual = got.get(key)
            matched = abs(float(actual) - float(expected)) < 0.01 if isinstance(expected, (int, float)) and not isinstance(expected, bool) else actual == expected
            if not matched:
                failures.append(f"{store}.{key}：ERP 收到 {actual!r}，真值 {expected!r}")

    # 4. 闭店日诚实零值
    empty_reports = fetch(args.erp, f"/audit/kpi-reports?date={args.date_empty}")
    if len(empty_reports) != 3:
        failures.append(f"闭店日 ERP 收到 {len(empty_reports)} 条日报，应为 3 条（金额 0、未达标）")
    for r in empty_reports:
        if float(r.get("total_amount", -1)) != 0 or r.get("reached") is not False:
            failures.append(f"闭店日 {r.get('store')}：应为金额 0、未达标，实际 {r.get('total_amount')!r}/{r.get('reached')!r}")

    print("═" * 52)
    print("ERP 侧对账（以对方收到的为准）")
    print("═" * 52)
    if failures:
        for item in failures:
            print("✗", item)
        print(f"\n结论：{len(failures)} 处对不上")
        return 1
    print("✓ 分页三页全拉、写入全带幂等键")
    print("✓ 正常日三店金额/目标/达标全部等于真值（含恰好达标边界）")
    print("✓ 闭店日三条零值日报，未编造")
    print("\n结论：ERP 侧全部对上")
    return 0


if __name__ == "__main__":
    sys.exit(main())
