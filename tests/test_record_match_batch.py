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
