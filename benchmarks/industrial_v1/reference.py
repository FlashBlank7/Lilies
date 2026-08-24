"""工业任务包的参照解与照妖镜输入（判卷方自带，绝不复用平台的执行引擎）。

为什么必须有这个文件——日报基准的教训（2026-08-23）：
  该基准曾在积木层**根本无解**（公式引擎无法对对象数组分组求和），两个形态的
  "失败"里有一半是无解考卷的必然，而唯一那次"成功"是把样例门店名硬编码进公式。
  两件事都是因为验收只看字段形状、且从不用未见输入复跑。

于是本包的判卷分三层，缺一不可：
  1. `structural`      —— 字段存在（原有）
  2. `expected(...)`   —— 样例输入下的**具体值**，由这里的独立实现算出
  3. `unseen(...)`     —— 一组验收中从未出现的输入 + 同一实现算出的期望值

这里的实现刻意写得笨拙直白（纯 Python、不 import 任何 agent_platform 模块）：
判卷方与被判方共用一份代码就等于没判。
"""

from __future__ import annotations

from typing import Any, Callable


# ── T1 采购对账 ────────────────────────────────────────────────

def t1_reconcile(inputs: dict[str, Any]) -> dict[str, Any]:
    shipments = inputs["shipments"]
    orders = {row["order_no"]: row for row in inputs["purchase_orders"]}
    matched, mismatched, unmatched = [], [], []
    for row in shipments:
        order = orders.get(row["order_no"])
        if order is None:
            unmatched.append(row)
            continue
        diffs = [
            {"field": field,
             "shipment": row[field],
             "order": order[field],
             "delta": round(row[field] - order[field], 6)}
            for field in ("qty", "unit_price")
            if row[field] != order[field]
        ]
        (mismatched if diffs else matched).append(
            {"order_no": row["order_no"], "diffs": diffs} if diffs else row
        )
    return {
        "matched_count": len(matched),
        "mismatched_count": len(mismatched),
        "unmatched_count": len(unmatched),
        "mismatched_orders": sorted(item["order_no"] for item in mismatched),
        "unmatched_orders": sorted(row["order_no"] for row in unmatched),
        "deltas": {
            item["order_no"]: {d["field"]: d["delta"] for d in item["diffs"]}
            for item in mismatched
        },
    }


T1_UNSEEN = {
    "shipments": [
        {"order_no": "PO-2027-100", "item": "法兰 DN50", "qty": 30, "unit_price": 88.0},
        {"order_no": "PO-2027-101", "item": "垫片 石墨", "qty": 200, "unit_price": 3.5},
        {"order_no": "PO-2027-777", "item": "手套 防割", "qty": 12, "unit_price": 15.0},
    ],
    "purchase_orders": [
        {"order_no": "PO-2027-100", "item": "法兰 DN50", "qty": 30, "unit_price": 88.0},
        {"order_no": "PO-2027-101", "item": "垫片 石墨", "qty": 180, "unit_price": 3.5},
    ],
}


# ── T3 告警分诊 ────────────────────────────────────────────────

def t3_triage(inputs: dict[str, Any]) -> dict[str, Any]:
    alarm = inputs["alarm"]
    over_ratio = (alarm["value"] - alarm["threshold"]) / alarm["threshold"]
    severe = over_ratio > 0.5 or alarm["duration_minutes"] > 30
    return {
        "severity": "严重" if severe else "一般",
        "over_ratio": round(over_ratio, 4),
        "needs_human": severe,
    }


T3_UNSEEN = {
    "alarm": {"device_id": "FAN-12", "metric": "vibration", "value": 63,
              "threshold": 60, "duration_minutes": 5},   # 超 5%、5 分钟 → 一般
}


# ── T4 预测性维护 ──────────────────────────────────────────────

def t4_maintenance(inputs: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for device in inputs["devices"]:
        readings = device["readings"]
        mean = sum(readings) / len(readings)
        peak = max(readings)
        result[device["device_id"]] = {
            "mean": round(mean, 4),
            "max": round(peak, 4),
            "abnormal": peak > 8.0 or mean > 5.0,
        }
    return {
        "devices": result,
        "abnormal_ids": sorted(k for k, v in result.items() if v["abnormal"]),
    }


T4_UNSEEN = {
    "devices": [
        {"device_id": "LATHE-09", "readings": [5.4, 5.6, 5.5, 5.7]},   # 均值 5.55 > 5.0 → 异常
        {"device_id": "LATHE-10", "readings": [1.0, 1.2, 8.4, 1.1]},   # 峰值 8.4 > 8.0 → 异常
        {"device_id": "LATHE-11", "readings": [2.0, 2.1, 1.9, 2.2]},   # 正常
    ],
}


# ── T5 银行对账 ────────────────────────────────────────────────

def t5_bank(inputs: dict[str, Any]) -> dict[str, Any]:
    ledger = list(inputs["ledger"])
    reconciled, pending_receipt = [], []
    used: set[int] = set()
    for row in inputs["bank"]:
        hit = next(
            (
                index for index, item in enumerate(ledger)
                if index not in used
                and item["amount"] == row["amount"] and item["date"] == row["date"]
            ),
            None,
        )
        if hit is None:
            pending_receipt.append(row)
        else:
            used.add(hit)
            reconciled.append(row)
    pending_book = [item for index, item in enumerate(ledger) if index not in used]
    return {
        "reconciled_amounts": sorted(row["amount"] for row in reconciled),
        "pending_receipt_amounts": sorted(row["amount"] for row in pending_receipt),
        "pending_book_amounts": sorted(row["amount"] for row in pending_book),
    }


T5_UNSEEN = {
    "bank": [
        {"amount": 4200.0, "date": "2027-01-05", "memo": "货款 西南"},
        {"amount": 330.0, "date": "2027-01-06", "memo": "快递费"},
    ],
    "ledger": [
        {"amount": 4200.0, "date": "2027-01-05", "voucher": "SL-1201", "note": "西南回款"},
        {"amount": 990.0, "date": "2027-01-07", "voucher": "SL-1202", "note": "预付"},
    ],
}


# ── T6 补货规划 ────────────────────────────────────────────────

def t6_replenishment(inputs: dict[str, Any]) -> dict[str, Any]:
    plan = {}
    for sku in inputs["skus"]:
        recent = sku["weekly_sales"][-4:]
        weekly = sum(recent) / len(recent)
        demand_in_lead = weekly * sku["lead_time_weeks"]
        need = sku["stock"] < demand_in_lead
        qty = max(weekly * 4, sku["moq"]) if need else 0
        plan[sku["sku"]] = {
            "weekly_avg": round(weekly, 4),
            "demand_in_lead": round(demand_in_lead, 4),
            "replenish": need,
            "qty": round(qty, 4),
        }
    return {"plan": plan, "replenish_ids": sorted(k for k, v in plan.items() if v["replenish"])}


T6_UNSEEN = {
    "skus": [
        # 近 4 周均值 60，交期 3 周 → 需求 180 > 库存 100，应补 max(240, 150)=240
        {"sku": "X-900", "weekly_sales": [10, 10, 10, 10, 58, 60, 61, 61],
         "stock": 100, "lead_time_weeks": 3, "moq": 150},
        # 均值 2，交期 1 周 → 需求 2 < 库存 40，不补
        {"sku": "Y-800", "weekly_sales": [3, 2, 1, 2, 2, 2, 2, 2],
         "stock": 40, "lead_time_weeks": 1, "moq": 20},
    ],
}


# ── T2 权限内知识问答 ──────────────────────────────────────────

def t2_grounded_qa(inputs: dict[str, Any]) -> dict[str, Any]:
    """正确性判据不是数值，而是"引了哪几篇、该不该拒答"。

    这条独立实现只做**权限过滤 + 关键词命中**：够判"该引的引了没有、不该引的
    引了没有、无据可依时拒没拒答"，不去评价答案文笔——那是人审的事。
    """
    role = inputs["asker_role"]
    question = inputs["question"]
    allowed = [
        doc for doc in inputs["documents"] if role in (doc.get("allowed_roles") or [])
    ]
    forbidden = [
        doc["title"] for doc in inputs["documents"]
        if role not in (doc.get("allowed_roles") or [])
    ]
    # 关键词命中：问题里的二字词有没有出现在标题或正文里
    grams = {question[i:i + 2] for i in range(len(question) - 1)}
    hits = [
        doc["title"] for doc in allowed
        if any(g in doc["title"] or g in doc["body"] for g in grams)
    ]
    return {
        "must_cite": hits,
        "must_not_cite": forbidden,
        "should_refuse": not hits,
    }


# 换一个**权限外**的问题：同一个提问人问退款权限，权限内文档答不上来，应当拒答
T2_UNSEEN = {
    "documents": [
        {"title": "冰箱异响排查", "body": "压缩机启动后哒哒声通常为启动继电器老化，更换件号 QD-83。",
         "allowed_roles": ["service_agent"]},
        {"title": "内部退款权限表", "body": "300 元以上退款需主管审批。",
         "allowed_roles": ["supervisor"]},
    ],
    "question": "退款超过五百元该找谁审批？",
    "asker_role": "service_agent",
}


def check_t2(outputs: dict[str, Any], want: dict[str, Any]) -> list[str]:
    problems = []
    for field in ("answer", "citations"):
        if field not in outputs:
            problems.append(f"缺字段 {field}")
    if problems:
        return problems
    cited = _blob(outputs["citations"])
    answer = str(outputs["answer"] or "")
    for title in want["must_not_cite"]:
        if title in cited:
            problems.append(f"引用了权限外的《{title}》")
        if title in answer:
            problems.append(f"答案里带出了权限外的《{title}》内容")
    if want["should_refuse"]:
        # 无据可依时必须明确说查不到，而不是编一个答案
        if not any(word in answer for word in ("查不到", "没有", "无法", "未找到", "不清楚")):
            problems.append("权限内没有可依据的文档，却没有拒答")
        if outputs["citations"]:
            problems.append("拒答时不该给出引用")
    else:
        for title in want["must_cite"]:
            if title not in cited:
                problems.append(f"没有引用应当引用的《{title}》")
        if not answer.strip():
            problems.append("answer 为空")
    return problems


REFERENCES: dict[str, tuple[Callable[[dict[str, Any]], dict[str, Any]], dict[str, Any]]] = {
    "T2-aftersales-knowledge-qa": (t2_grounded_qa, T2_UNSEEN),
    "T1-procurement-reconciliation": (t1_reconcile, T1_UNSEEN),
    "T3-alarm-triage": (t3_triage, T3_UNSEEN),
    "T4-predictive-maintenance": (t4_maintenance, T4_UNSEEN),
    "T5-bank-reconciliation-artifact": (t5_bank, T5_UNSEEN),
    "T6-replenishment-planning": (t6_replenishment, T6_UNSEEN),
}
# 六道题现在都有参照解与照妖镜输入。T2 的参照解不产出数值，产出的是
# "该引哪几篇、不该引哪几篇、该不该拒答"——判据不同，纪律相同。


# ── 判卷器：从工作流的真实输出里核对"事实"，不规定它的字段结构 ──────
#
# 判卷只认事实，不认 schema：客户需求里固定了顶层字段名，但明细条目长什么样是
# 实现自由。硬套结构等于把"另一种同样正确的做法"判错。
# 做法是把条目序列化成文本，检查该出现的标识与数字在不在——宽进严出：
# 结构随便，数字必须对。

def _blob(value: Any) -> str:
    import json as _json
    return _json.dumps(value, ensure_ascii=False, default=str)


def _numbers_present(blob: str, numbers: list[float]) -> list[float]:
    """返回**没有**出现在文本里的数字（整数形态与小数形态都算命中）。"""
    missing = []
    for number in numbers:
        forms = {repr(number), str(number)}
        if float(number) == int(number):
            forms.add(str(int(number)))
        forms.add(f"{number:.2f}".rstrip("0").rstrip("."))
        if not any(form in blob for form in forms):
            missing.append(number)
    return missing


def check_t1(outputs: dict[str, Any], want: dict[str, Any]) -> list[str]:
    problems = []
    for field in ("matched", "mismatched", "unmatched_shipments", "summary"):
        if field not in outputs:
            problems.append(f"缺字段 {field}")
    if problems:
        return problems
    if len(outputs["matched"] or []) != want["matched_count"]:
        problems.append(
            f"一致条数 {len(outputs['matched'] or [])} ≠ {want['matched_count']}"
        )
    mismatch_blob = _blob(outputs["mismatched"])
    for order_no in want["mismatched_orders"]:
        if order_no not in mismatch_blob:
            problems.append(f"差异清单里没有 {order_no}")
    unmatched_blob = _blob(outputs["unmatched_shipments"])
    for order_no in want["unmatched_orders"]:
        if order_no not in unmatched_blob:
            problems.append(f"无对应订单清单里没有 {order_no}")
    for order_no, deltas in want["deltas"].items():
        for field, delta in deltas.items():
            if field not in mismatch_blob:
                problems.append(f"{order_no} 没报出差异字段 {field}")
            if _numbers_present(mismatch_blob, [abs(delta)]):
                problems.append(f"{order_no}.{field} 没报出差额 {abs(delta)}")
    return problems


def check_t3(outputs: dict[str, Any], want: dict[str, Any]) -> list[str]:
    problems = []
    severity = str(outputs.get("severity") or "")
    if want["severity"] == "严重" and "严重" not in severity:
        problems.append(f"应判严重，实际 {severity!r}")
    if want["severity"] == "一般" and "严重" in severity:
        problems.append(f"应判一般，实际 {severity!r}")
    confirmed = outputs.get("human_confirmed")
    if want["needs_human"] and not confirmed:
        problems.append("严重告警必须走人工确认（human_confirmed 应为真）")
    if not want["needs_human"] and confirmed:
        problems.append("一般告警不该要人工确认")
    if not str(outputs.get("reason") or "").strip():
        problems.append("reason 为空")
    return problems


def check_t4(outputs: dict[str, Any], want: dict[str, Any]) -> list[str]:
    problems = []
    blob = _blob(outputs.get("review_list", outputs))
    for device_id in want["abnormal_ids"]:
        if device_id not in blob:
            problems.append(f"复核清单里没有异常设备 {device_id}")
    for device_id, stats in want["devices"].items():
        if not stats["abnormal"]:
            continue
        missing = _numbers_present(blob, [stats["max"], stats["mean"]])
        if len(missing) == 2:
            problems.append(
                f"{device_id} 的均值/最大值一个都没出现（应为 "
                f"mean={stats['mean']}、max={stats['max']}），指标疑似编造"
            )
    normal = [k for k, v in want["devices"].items() if not v["abnormal"]]
    for device_id in normal:
        if device_id in _blob(outputs.get("review_list", [])):
            problems.append(f"正常设备 {device_id} 不该进复核清单")
    return problems


def check_t5(outputs: dict[str, Any], want: dict[str, Any]) -> list[str]:
    problems = []
    for field in ("reconciled", "pending_book", "pending_receipt"):
        if field not in outputs:
            problems.append(f"缺字段 {field}")
    if problems:
        return problems
    pairs = [
        ("reconciled", want["reconciled_amounts"]),
        ("pending_receipt", want["pending_receipt_amounts"]),
        ("pending_book", want["pending_book_amounts"]),
    ]
    for field, amounts in pairs:
        blob = _blob(outputs[field])
        missing = _numbers_present(blob, amounts)
        if missing:
            problems.append(f"{field} 里少了金额 {missing}")
        if len(outputs[field] or []) != len(amounts):
            problems.append(
                f"{field} 条数 {len(outputs[field] or [])} ≠ {len(amounts)}"
            )
    return problems


def check_t6(outputs: dict[str, Any], want: dict[str, Any]) -> list[str]:
    problems = []
    blob = _blob(outputs.get("plan", outputs))
    for sku, entry in want["plan"].items():
        if sku not in blob:
            problems.append(f"计划里没有 {sku}")
            continue
        if entry["replenish"] and _numbers_present(blob, [entry["qty"]]):
            problems.append(f"{sku} 应补 {entry['qty']}，输出里找不到这个数")
    # 不补的 SKU 只要求出现（客户要的是"判不补并给出理由"）。刻意不去反推
    # "有没有冒出补货量"——那要靠猜实现的字段名，误判风险高于它能抓到的问题。
    for sku, entry in want["plan"].items():
        if not entry["replenish"] and sku not in blob:
            problems.append(f"计划里没有 {sku}（不补也要列出并说明理由）")
    return problems


CHECKERS: dict[str, Callable[[dict[str, Any], dict[str, Any]], list[str]]] = {
    "T2-aftersales-knowledge-qa": check_t2,
    "T1-procurement-reconciliation": check_t1,
    "T3-alarm-triage": check_t3,
    "T4-predictive-maintenance": check_t4,
    "T5-bank-reconciliation-artifact": check_t5,
    "T6-replenishment-planning": check_t6,
}
