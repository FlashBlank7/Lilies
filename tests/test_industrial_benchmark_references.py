"""工业任务包：参照解必须与验收原文一致，判卷器必须判得对自己的参照解。

效度纪律（日报基准事故的教训）：拿去测模型的任务，必须先有机器证明的参照解，
并且必须准备一组**验收中从未出现的输入**。缺任何一样，测出来的"模型不行"都
可能是无解考卷或硬编码作弊。这里把 benchmarks/industrial_v1/reference.py 的
两件事钉死：

  1. 参照解在样例输入上算出的数，与 tasks.json 里 review_notes 写的一致；
  2. 判卷器面对"正确输出"零问题、面对"典型错误输出"一定报错（否则它没在判卷）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "benchmarks" / "industrial_v1"))

from reference import CHECKERS, REFERENCES  # noqa: E402


def _tasks() -> dict[str, Any]:
    data = json.loads((REPO / "benchmarks" / "industrial_v1" / "tasks.json").read_text("utf-8"))
    return {task["id"]: task for task in data["tasks"]}


def test_reference_values_match_the_written_acceptance_notes() -> None:
    tasks = _tasks()

    t1 = REFERENCES["T1-procurement-reconciliation"][0](
        tasks["T1-procurement-reconciliation"]["sample_inputs"])
    # "PO-001 应判一致；PO-002 应报 unit_price 差异 0.2；PO-2026-009 应进无对应订单"
    assert t1["matched_count"] == 1
    assert t1["mismatched_orders"] == ["PO-2026-002"]
    assert t1["deltas"]["PO-2026-002"] == {"unit_price": 0.2}
    assert t1["unmatched_orders"] == ["PO-2026-009"]

    t3 = REFERENCES["T3-alarm-triage"][0](tasks["T3-alarm-triage"]["sample_inputs"])
    # "92 超阈值 53%，应判严重并走人工确认"
    assert t3["severity"] == "严重" and t3["needs_human"]
    assert 0.53 <= t3["over_ratio"] <= 0.54

    t4 = REFERENCES["T4-predictive-maintenance"][0](
        tasks["T4-predictive-maintenance"]["sample_inputs"])
    # "CNC-01 应因 max=9.4>8.0 判异常；CNC-02 正常"
    assert t4["abnormal_ids"] == ["CNC-01"]
    assert t4["devices"]["CNC-01"]["max"] == 9.4
    assert not t4["devices"]["CNC-02"]["abnormal"]

    t5 = REFERENCES["T5-bank-reconciliation-artifact"][0](
        tasks["T5-bank-reconciliation-artifact"]["sample_inputs"])
    # "8600 应核销；1200 应进待入账"
    assert t5["reconciled_amounts"] == [8600.0]
    assert t5["pending_receipt_amounts"] == [1200.0]

    t2 = REFERENCES["T2-aftersales-knowledge-qa"][0](
        tasks["T2-aftersales-knowledge-qa"]["sample_inputs"])
    # "答案应引用《冰箱异响排查》；不得引用权限外的《内部退款权限表》"
    assert t2["must_cite"] == ["冰箱异响排查"]
    assert t2["must_not_cite"] == ["内部退款权限表"]
    assert not t2["should_refuse"]
    # "换一个权限外问题时应回答查不到"
    t2_unseen = REFERENCES["T2-aftersales-knowledge-qa"][0](
        REFERENCES["T2-aftersales-knowledge-qa"][1])
    assert t2_unseen["should_refuse"]

    t6 = REFERENCES["T6-replenishment-planning"][0](
        tasks["T6-replenishment-planning"]["sample_inputs"])
    # "A-100 周销约 43 ... 应补且 ≥ max(4周销量≈172, moq=100)；B-200 应判不补"
    assert 42.5 <= t6["plan"]["A-100"]["weekly_avg"] <= 43.5
    assert t6["replenish_ids"] == ["A-100"]
    assert t6["plan"]["A-100"]["qty"] >= 172
    assert not t6["plan"]["B-200"]["replenish"]


def _correct_output(task_id: str, want: dict[str, Any]) -> dict[str, Any]:
    """一份"结构随意但数字全对"的输出——判卷器必须放行。"""
    if task_id == "T1-procurement-reconciliation":
        return {
            "matched": [{} for _ in range(want["matched_count"])],
            "mismatched": [
                {"order_no": order, "field": field, "delta": delta}
                for order, diffs in want["deltas"].items()
                for field, delta in diffs.items()
            ],
            "unmatched_shipments": [{"order_no": o} for o in want["unmatched_orders"]],
            "summary": "对账完成",
        }
    if task_id == "T2-aftersales-knowledge-qa":
        if want["should_refuse"]:
            return {"answer": "权限内的资料里查不到这个问题的依据。", "citations": []}
        return {"answer": "更换启动继电器（件号 QD-83）。",
                "citations": [{"title": title} for title in want["must_cite"]]}
    if task_id == "T3-alarm-triage":
        return {"severity": want["severity"], "reason": "超阈值",
                "recommendation": "派工检查", "human_confirmed": want["needs_human"]}
    if task_id == "T4-predictive-maintenance":
        return {"review_list": [
            {"device_id": device, "mean": stats["mean"], "max": stats["max"],
             "advice": "安排点检"}
            for device, stats in want["devices"].items() if stats["abnormal"]
        ]}
    if task_id == "T5-bank-reconciliation-artifact":
        return {
            "reconciled": [{"amount": a} for a in want["reconciled_amounts"]],
            "pending_receipt": [{"amount": a} for a in want["pending_receipt_amounts"]],
            "pending_book": [{"amount": a} for a in want["pending_book_amounts"]],
        }
    return {"plan": [
        {"sku": sku, "replenish": entry["replenish"], "qty": entry["qty"],
         "reason": "库存足够" if not entry["replenish"] else "交期内需求超库存"}
        for sku, entry in want["plan"].items()
    ]}


def test_checkers_pass_correct_output_on_both_sample_and_unseen_inputs() -> None:
    tasks = _tasks()
    for task_id, (reference, unseen) in REFERENCES.items():
        for label, inputs in (("样例", tasks[task_id]["sample_inputs"]), ("未见", unseen)):
            want = reference(inputs)
            problems = CHECKERS[task_id](_correct_output(task_id, want), want)
            assert problems == [], (task_id, label, problems)


def test_checkers_actually_reject_the_classic_failures() -> None:
    """判卷器必须抓得住典型错法，否则它只是在盖章。"""
    tasks = _tasks()

    # T1：差额算错（0.2 写成 0.5）
    want = REFERENCES["T1-procurement-reconciliation"][0](
        tasks["T1-procurement-reconciliation"]["sample_inputs"])
    bad = _correct_output("T1-procurement-reconciliation", want)
    bad["mismatched"] = [{"order_no": "PO-2026-002", "field": "unit_price", "delta": 0.5}]
    assert CHECKERS["T1-procurement-reconciliation"](bad, want)

    # T3：严重告警却没走人工确认
    want = REFERENCES["T3-alarm-triage"][0](tasks["T3-alarm-triage"]["sample_inputs"])
    bad = _correct_output("T3-alarm-triage", want)
    bad["human_confirmed"] = False
    assert CHECKERS["T3-alarm-triage"](bad, want)

    # T4：把正常设备也塞进复核清单
    want = REFERENCES["T4-predictive-maintenance"][0](
        tasks["T4-predictive-maintenance"]["sample_inputs"])
    bad = _correct_output("T4-predictive-maintenance", want)
    bad["review_list"].append({"device_id": "CNC-02", "mean": 2.325, "max": 2.6})
    assert CHECKERS["T4-predictive-maintenance"](bad, want)

    # T5：把待入账的 1200 也算成已核销
    want = REFERENCES["T5-bank-reconciliation-artifact"][0](
        tasks["T5-bank-reconciliation-artifact"]["sample_inputs"])
    bad = _correct_output("T5-bank-reconciliation-artifact", want)
    bad["reconciled"].append({"amount": 1200.0})
    bad["pending_receipt"] = []
    assert CHECKERS["T5-bank-reconciliation-artifact"](bad, want)

    # T2：引用了权限外的文档
    want = REFERENCES["T2-aftersales-knowledge-qa"][0](
        tasks["T2-aftersales-knowledge-qa"]["sample_inputs"])
    bad = _correct_output("T2-aftersales-knowledge-qa", want)
    bad["citations"].append({"title": "内部退款权限表"})
    assert CHECKERS["T2-aftersales-knowledge-qa"](bad, want)

    # T2：无据可依却编了个答案（该拒答而没拒）
    want_refuse = REFERENCES["T2-aftersales-knowledge-qa"][0](
        REFERENCES["T2-aftersales-knowledge-qa"][1])
    assert CHECKERS["T2-aftersales-knowledge-qa"](
        {"answer": "五百元以上请找主管审批。", "citations": []}, want_refuse)

    # T6：漏掉该补的 SKU
    want = REFERENCES["T6-replenishment-planning"][0](
        tasks["T6-replenishment-planning"]["sample_inputs"])
    bad = {"plan": [{"sku": "B-200", "replenish": False, "reason": "库存足够"}]}
    assert CHECKERS["T6-replenishment-planning"](bad, want)
