"""监理模块：出卷模型校验、机械对答案、流水账核验、验收单存取。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_platform.acceptance_pm import (
    AcceptanceExpect,
    AcceptanceSpec,
    evaluate_case,
    load_report,
    load_spec,
    render_report_markdown,
    save_report,
    save_spec,
)


def _spec() -> AcceptanceSpec:
    return AcceptanceSpec.model_validate({
        "summary": "对账结果必须可复算，且必须真实调用匹配环节。",
        "required_node_types": ["record_match"],
        "cases": [
            {
                "name": "两笔流水一对一",
                "inputs": {"bank": [], "ledger": []},
                "expect": {
                    "required_fields": ["reconciled"],
                    "equals": {"summary.matched": 1, "is_fault": True},
                    "not_contains": {"report": ["人民币"]},
                    "must_execute": ["record_match"],
                    "must_not_execute": ["llm"],
                },
            }
        ],
    })


def test_spec_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError):
        AcceptanceSpec.model_validate({"summary": "x", "cases": [], "extra": 1})
    with pytest.raises(ValueError):
        AcceptanceSpec.model_validate({"summary": "x", "cases": []})  # min 1 case


def test_evaluate_case_checks_outputs_and_ledger() -> None:
    expect = _spec().cases[0].expect
    outputs = {
        "reconciled": [],
        "summary": {"matched": 1},
        "is_fault": True,
        "report": "全部对平，金额单位为港币。",
    }
    checks = evaluate_case(expect, outputs, {"start", "record_match", "end"})
    assert all(check["passed"] for check in checks), checks

    # llm 出现在流水账 → must_not_execute 失败；matched 数字不符 → equals 失败
    bad = evaluate_case(
        expect,
        {"reconciled": [], "summary": {"matched": 2}, "is_fault": False, "report": "人民币 100"},
        {"start", "llm", "end"},
    )
    failed = {check["check"] for check in bad if not check["passed"]}
    assert any("record_match 真实执行" in item for item in failed)
    assert any("llm 未被执行" in item for item in failed)
    assert any("matched" in item for item in failed)
    assert any("is_fault" in item for item in failed)
    assert any("人民币" in item for item in failed)


def test_boolean_equals_is_not_numeric_coerced() -> None:
    expect = AcceptanceExpect(equals={"flag": True})
    ok = evaluate_case(expect, {"flag": True}, set())
    assert ok[0]["passed"] is True
    wrong = evaluate_case(expect, {"flag": False}, set())
    assert wrong[0]["passed"] is False


def test_spec_and_report_roundtrip(tmp_path: Path) -> None:
    spec = _spec()
    save_spec(tmp_path, "app-1", spec)
    loaded = load_spec(tmp_path, "app-1")
    assert loaded is not None and loaded.summary == spec.summary

    report = {
        "application_id": "app-1",
        "application_name": "对账",
        "version": 2,
        "stamp": "2026-08-07 05:00:00",
        "summary": spec.summary,
        "required_node_types": spec.required_node_types,
        "required_any_node_types": [],
        "architecture_missing": [],
        "architecture_pass": True,
        "cases": [{
            "name": "两笔流水一对一",
            "run_status": "succeeded",
            "passed": True,
            "executed_node_types": ["end", "record_match", "start"],
            "checks": [{"check": "输出包含字段 reconciled", "passed": True, "actual": "存在"}],
        }],
        "passed_cases": 1,
        "accepted": True,
    }
    save_report(tmp_path, "app-1", report)
    assert load_report(tmp_path, "app-1")["accepted"] is True
    markdown = render_report_markdown(report)
    assert "✅ 验收通过" in markdown
    assert "发布版 v2" in markdown


def test_normalize_tolerates_model_slips() -> None:
    from agent_platform.acceptance_pm import normalize_spec_payload

    payload = {
        "summary": "查两条",
        "required_node_types": None,
        "cases": [{
            "name": "样例",
            "inputs": {"a": 1},
            "human_input": None,
            "expect": {
                "required_fields": None,
                "equals": [],
                "contains": [],
                "not_contains": {"report": "人民币"},
                "must_execute": None,
            },
        }],
    }
    spec = AcceptanceSpec.model_validate(normalize_spec_payload(payload))
    assert spec.cases[0].expect.equals == {}
    assert spec.cases[0].expect.contains == {}
    assert spec.cases[0].expect.not_contains == {"report": []}
    assert spec.cases[0].human_input is None
