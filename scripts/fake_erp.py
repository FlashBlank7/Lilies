"""假 ERP：ERP 连接能力的对练靶子（盲测用）。

刻意长得难伺候，把真实对接会踩的坑全部内置：
- 鉴权：Bearer token，错了 401；
- 分页读：销售明细分页返回，漏翻页就少数据（数据排布让漏页直接改变达标结论）；
- 幂等写：日报回写必须带 Idempotency-Key，同键重复只落一条；
- 回执可查：/audit/* 让验收器问"你到底收到了什么"——以 ERP 收到的为准。

数据完全确定（无随机），真值可离线手算：
  2026-08-07 三家门店各 40 行，行金额 = 店序*100 + 行号：
    华东一店 总额 4780，目标 5000 → 未达标
    华东二店 总额 8780，目标 8000 → 达标
    西南一店 总额 12780，目标 12780 → 达标（恰好等于，边界埋点）
  行序为店1×40 → 店2×40 → 店3×40，page_size=50 共 3 页：
  漏第 3 页会把西南一店算成 6390 → 误判未达标。
  2026-08-01 无销售数据（诚实空测试）。
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

TOKEN = "erp-demo-token"
STORES = ["华东一店", "华东二店", "西南一店"]
DATA_DATE = "2026-08-07"
EMPTY_DATE = "2026-08-01"
TARGETS = {"华东一店": 5000, "华东二店": 8000, "西南一店": 12780}
PAGE_SIZE_MAX = 50

app = FastAPI(title="假ERP（对练靶子）")

_audit_requests: list[dict[str, Any]] = []
_kpi_reports: dict[str, dict[str, Any]] = {}  # idempotency_key -> report


def _rows_for(date: str) -> list[dict[str, Any]]:
    if date != DATA_DATE:
        return []
    rows: list[dict[str, Any]] = []
    for store_index, store in enumerate(STORES):
        for line in range(40):
            rows.append({
                "order_no": f"SO-{date}-{store_index + 1}-{line + 1:03d}",
                "store": store,
                "sku": f"SKU-{line % 8 + 1}",
                "qty": 1 + line % 3,
                "amount": (store_index + 1) * 100 + line,
            })
    return rows


def _record(endpoint: str, params: dict[str, Any], authorized: bool) -> None:
    _audit_requests.append({
        "at": datetime.now(timezone.utc).isoformat(),
        "endpoint": endpoint,
        "params": params,
        "authorized": authorized,
    })


def _auth(authorization: str | None, endpoint: str, params: dict[str, Any]) -> None:
    ok = authorization == f"Bearer {TOKEN}"
    _record(endpoint, params, ok)
    if not ok:
        raise HTTPException(401, "无效凭证：请带 Authorization: Bearer <token>")


@app.get("/api/sales/daily")
def sales_daily(
    date: str = Query(...),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=PAGE_SIZE_MAX),
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    _auth(authorization, "GET /api/sales/daily", {"date": date, "page": page, "page_size": page_size})
    rows = _rows_for(date)
    start = (page - 1) * page_size
    return {
        "date": date,
        "page": page,
        "page_size": page_size,
        "total": len(rows),
        "total_pages": (len(rows) + page_size - 1) // page_size if rows else 0,
        "rows": rows[start:start + page_size],
    }


@app.get("/api/targets")
def targets(date: str = Query(...), authorization: str | None = Header(None)) -> dict[str, Any]:
    _auth(authorization, "GET /api/targets", {"date": date})
    return {
        "date": date,
        "targets": [{"store": store, "target_amount": TARGETS[store]} for store in STORES],
    }


class KpiReport(BaseModel):
    date: str
    store: str
    total_amount: float
    target_amount: float
    reached: bool
    note: str = Field(default="", max_length=500)


@app.post("/api/kpi-reports")
def write_kpi_report(
    body: KpiReport,
    authorization: str | None = Header(None),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    _auth(authorization, "POST /api/kpi-reports", {"store": body.store, "date": body.date,
                                                   "idempotency_key": idempotency_key or ""})
    if not idempotency_key:
        raise HTTPException(422, "缺少 Idempotency-Key 请求头：日报写入必须防重复")
    if idempotency_key in _kpi_reports:
        return {"status": "duplicate_ignored", "report": _kpi_reports[idempotency_key]}
    record = {**body.model_dump(), "received_at": datetime.now(timezone.utc).isoformat()}
    _kpi_reports[idempotency_key] = record
    return {"status": "created", "report": record}


# ── 验收侧：两头对账用，不需要鉴权（模拟甲方 IT 直接查库） ──

@app.get("/audit/requests")
def audit_requests() -> list[dict[str, Any]]:
    return _audit_requests


@app.get("/audit/kpi-reports")
def audit_kpi_reports(date: str = Query("")) -> list[dict[str, Any]]:
    reports = list(_kpi_reports.values())
    return [r for r in reports if not date or r["date"] == date]


@app.post("/audit/reset")
def audit_reset() -> dict[str, str]:
    _audit_requests.clear()
    _kpi_reports.clear()
    return {"status": "reset"}


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8022)
    args = parser.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=args.port)
