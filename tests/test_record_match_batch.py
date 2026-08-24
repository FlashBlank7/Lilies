"""Batch record_match: one-node reconciliation without iteration or LLM."""

from __future__ import annotations

import pytest

from agent_platform.record_pipeline import (
    MatchCondition,
    RecordMatchConfig,
    match_records,
)


CONDITIONS = [
    MatchCondition(
        name="reference",
        source_path=["ref_no"],
        candidate_path=["invoice_no"],
        comparator="exact",
        weight=60,
        required=True,
    ),
    MatchCondition(
        name="amount",
        source_path=["amount"],
        candidate_path=["amount_due"],
        comparator="numeric",
        weight=40,
        required=True,
    ),
]

BANK_LINES = [
    {"ref_no": "INV-001", "amount": 1200.0},
    {"ref_no": "INV-002", "amount": 88.5},
    {"ref_no": "INV-404", "amount": 9.99},
]

LEDGER = [
    {"invoice_no": "INV-002", "amount_due": 88.5},
    {"invoice_no": "INV-001", "amount_due": 1200},
    {"invoice_no": "INV-777", "amount_due": 42.0},
]


def _run(sources=BANK_LINES, candidates=LEDGER, **overrides):
    kwargs = dict(
        conditions=CONDITIONS,
        conflict_checks=[],
        min_score=1.0,
        ambiguity_threshold=0.0,
        result_limit=20,
    )
    kwargs.update(overrides)
    return match_records(sources, candidates, **kwargs)


def test_batch_reconciliation_matches_pairs_and_reports_exceptions() -> None:
    result = _run()

    assert result["summary"] == {
        "total_sources": 3,
        "total_candidates": 3,
        "matched": 2,
        "unmatched_sources": 1,
        "ambiguous": 0,
        "conflicts": 0,
        "unmatched_candidates": 1,
    }
    pairs = {
        (item["source"]["ref_no"], item["candidate"]["invoice_no"])
        for item in result["matched"]
    }
    assert pairs == {("INV-001", "INV-001"), ("INV-002", "INV-002")}
    # numeric comparator bridges 1200.0 vs 1200 — no string equality trap
    assert result["unmatched_sources"][0]["source"]["ref_no"] == "INV-404"
    assert result["unmatched_candidates"][0]["candidate"]["invoice_no"] == "INV-777"
    # candidate_index refers to the ORIGINAL candidates list
    by_ref = {item["source"]["ref_no"]: item for item in result["matched"]}
    assert LEDGER[by_ref["INV-001"]["candidate_index"]]["invoice_no"] == "INV-001"


def test_batch_is_deterministic_for_identical_inputs() -> None:
    assert _run() == _run()


def test_consume_candidates_prevents_double_matching() -> None:
    duplicated_sources = [
        {"ref_no": "INV-001", "amount": 1200.0},
        {"ref_no": "INV-001", "amount": 1200.0},
    ]
    consumed = _run(sources=duplicated_sources)
    assert consumed["summary"]["matched"] == 1
    assert consumed["summary"]["unmatched_sources"] == 1

    shared = _run(sources=duplicated_sources, consume_candidates=False)
    assert shared["summary"]["matched"] == 2


def test_config_requires_exactly_one_of_source_and_sources() -> None:
    base = {
        "candidates": [],
        "conditions": [
            {
                "name": "reference",
                "source_path": ["a"],
                "candidate_path": ["b"],
            }
        ],
    }
    with pytest.raises(ValueError):
        RecordMatchConfig(**base)
    with pytest.raises(ValueError):
        RecordMatchConfig(source={"a": 1}, sources=[{"a": 1}], **base)
    assert RecordMatchConfig(sources=[{"a": 1}], **base).consume_candidates is True
    assert RecordMatchConfig(source={"a": 1}, **base).source == {"a": 1}


def test_conflict_carries_the_candidate_and_the_delta() -> None:
    """对账类需求普遍要求"列出是哪个字段不一致、差多少"——积木层必须做得到。

    效度纪律（日报基准事故的教训）：拿去测模型的任务必须先确认在积木层可解。
    工业任务包 T1/T5 的验收原文就要求报出差异字段与差额，而批量 record_match
    此前对冲突只回 {source, status, match: null}：既没有被比对的那条候选记录，
    冲突项里也只有一个 name，没有两边的值——这句话在积木层根本写不出来。
    这是任务效度问题，不是模型能力问题；若不先补，测出来的"模型不行"是假的。
    """

    from agent_platform.record_pipeline import ConflictCheck

    shipments = [
        {"order_no": "PO-2026-001", "item": "轴承 6204", "qty": 100, "unit_price": 12.5},
        {"order_no": "PO-2026-002", "item": "密封圈 FKM", "qty": 500, "unit_price": 2.0},
        {"order_no": "PO-2026-009", "item": "润滑脂 2kg", "qty": 20, "unit_price": 45.0},
    ]
    orders = [
        {"order_no": "PO-2026-001", "item": "轴承 6204", "qty": 100, "unit_price": 12.5},
        {"order_no": "PO-2026-002", "item": "密封圈 FKM", "qty": 500, "unit_price": 1.8},
    ]

    result = match_records(
        shipments,
        orders,
        conditions=[MatchCondition(
            name="order_no", source_path=["order_no"], candidate_path=["order_no"],
            comparator="exact", weight=1.0, required=True,
        )],
        conflict_checks=[
            ConflictCheck(name="qty", source_path=["qty"],
                          candidate_path=["qty"], comparator="numeric"),
            ConflictCheck(name="unit_price", source_path=["unit_price"],
                          candidate_path=["unit_price"], comparator="numeric"),
        ],
        min_score=0.9,
        ambiguity_threshold=0.05,
        result_limit=3,
        consume_candidates=True,
    )

    # 三个桶分得干净：一致 / 有差异 / 无对应订单
    assert [m["source"]["order_no"] for m in result["matched"]] == ["PO-2026-001"]
    assert [e["source"]["order_no"] for e in result["unmatched_sources"]] == ["PO-2026-009"]
    assert len(result["conflict_sources"]) == 1

    conflict = result["conflict_sources"][0]
    assert conflict["source"]["order_no"] == "PO-2026-002"
    # 被比对的那条订单必须带回来，否则"差多少"无从算起
    assert conflict["conflict_with"]["candidate"]["order_no"] == "PO-2026-002"
    details = {item["name"]: item for item in conflict["conflict_with"]["conflicts"]}
    assert set(details) == {"unit_price"}, details
    assert details["unit_price"]["source_value"] == 2.0
    assert details["unit_price"]["candidate_value"] == 1.8
    assert details["unit_price"]["delta"] == pytest.approx(0.2)
