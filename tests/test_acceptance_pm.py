"""监理模块：出卷模型校验、机械对答案、流水账核验、验收单存取。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_platform.acceptance_pm import (
    AcceptanceCase,
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


def test_owner_language_gate_flags_machine_tokens() -> None:
    from agent_platform.acceptance_pm import owner_language_violations, redact_machine_tokens

    text = "模型吃下了 AX_std 和 GY_jerk_max，判定环节 deployed_model_inference 已跑通，maxSpeed 正常。"
    violations = owner_language_violations(text, ["deployed_model_inference", "llm", "end"])
    assert "AX_std" in violations
    assert "GY_jerk_max" in violations
    assert "deployed_model_inference" in violations
    assert "maxSpeed" in violations

    clean = "这趟被判定为正常，置信度较高，用的是双方约定的 elevator-fault-v1 模型。"
    assert owner_language_violations(clean, ["deployed_model_inference"]) == []

    redacted = redact_machine_tokens(text, violations)
    assert "AX_std" not in redacted and "（技术指标）" in redacted


def test_spec_suggestions_channel_and_lessons(tmp_path: Path) -> None:
    from agent_platform.acceptance_pm import (
    AcceptanceCase,
        append_lesson,
        load_lessons,
        normalize_spec_payload,
    )

    payload = {
        "summary": "查两条",
        "suggestions": ["建议补一条缺字段的容错用例", "", None],
        "cases": [{"name": "样例", "inputs": {"a": 1}, "expect": {}}],
    }
    spec = AcceptanceSpec.model_validate(normalize_spec_payload(payload))
    assert spec.suggestions == ["建议补一条缺字段的容错用例"]

    lessons = load_lessons(tmp_path)
    assert "卷面主权" in lessons and "语言纪律" in lessons
    updated = append_lesson(tmp_path, "解释里的概率数字要换算成'十有八九'式说法")
    assert "十有八九" in updated
    # 幂等：重复追加不重复记录
    assert updated == append_lesson(tmp_path, "解释里的概率数字要换算成'十有八九'式说法")


def test_terminal_lineage_traces_refs_to_required_nodes() -> None:
    from agent_platform.acceptance_pm import terminal_lineage_types

    nodes = [
        {"id": "start", "type": "start", "config": {}},
        {"id": "model", "type": "deployed_model_inference", "config": {
            "features": {"$ref": {"node_id": "$inputs", "path": ["features"]}},
        }},
        {"id": "advice", "type": "llm", "config": {
            "prompt": "写建议", "context": {"$ref": {"node_id": "model", "path": ["predicted_label"]}},
        }},
        {"id": "end", "type": "end", "config": {
            "outputs": {
                "is_fault": {"$ref": {"node_id": "model", "path": ["predicted_label"]}},
                "advice": {"$ref": {"node_id": "advice", "path": ["text"]}},
            },
        }},
        {"id": "orphan", "type": "record_match", "config": {
            "sources": {"$ref": {"node_id": "$inputs", "path": ["rows"]}},
        }},
    ]
    types = terminal_lineage_types(nodes)
    assert "deployed_model_inference" in types      # 被终端引用
    assert "llm" in types
    assert "record_match" not in types              # 跑不跑无所谓，结果没人用

    # 作弊形态：模型在图里、甚至可能执行，但结果链路完全绕开它——
    # llm 不看模型输出，终端字段全接 llm 自己的判断
    cheat = [dict(n) for n in nodes]
    cheat[2] = {"id": "advice", "type": "llm", "config": {
        "prompt": "自己看特征猜一个", "context": {"$ref": {"node_id": "$inputs", "path": ["features"]}},
    }}
    cheat[3] = {"id": "end", "type": "end", "config": {
        "outputs": {"is_fault": {"$ref": {"node_id": "advice", "path": ["verdict"]}}},
    }}
    assert "deployed_model_inference" not in terminal_lineage_types(cheat)


def test_expect_run_failed_case_semantics() -> None:
    payload = {
        "summary": "缺字段应报错",
        "cases": [{
            "name": "缺输入",
            "inputs": {},
            "expect_run": "failed",
            "expect": {},
        }],
    }
    from agent_platform.acceptance_pm import normalize_spec_payload
    spec = AcceptanceSpec.model_validate(normalize_spec_payload(payload))
    assert spec.cases[0].expect_run == "failed"
    with pytest.raises(ValueError):
        AcceptanceCase.model_validate({
            "name": "x", "inputs": {}, "expect_run": "exploded", "expect": {},
        })


def test_collect_node_types_recurses_into_iteration() -> None:
    from agent_platform.acceptance_pm import collect_node_types

    nodes = [
        {"id": "start", "type": "start", "config": {}},
        {"id": "loop", "type": "iteration", "config": {"workflow": {"nodes": [
            {"id": "inner_start", "type": "start", "config": {}},
            {"id": "calc", "type": "variable_assigner", "config": {}},
            {"id": "inner_end", "type": "end", "config": {}},
        ]}}},
        {"id": "end", "type": "end", "config": {}},
    ]
    types = collect_node_types(nodes)
    assert "variable_assigner" in types
    assert "iteration" in types


def test_acceptance_repair_bridges_failed_report_to_build(tmp_path) -> None:
    """监理验收失败 → 一键按验收单返修：失败项组装成改单证据并复活构建。"""

    import json as _json
    from pathlib import Path as _Path

    from fastapi.testclient import TestClient

    from agent_platform.acceptance_pm import save_report
    from agent_platform.api import create_app
    from agent_platform.config import Settings

    from tests.test_use_channel import HEADERS, SilentProvider

    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, SilentProvider())
    with TestClient(app) as client:
        application_id = client.post(
            "/api/v1/applications", headers=HEADERS,
            json={"name": "验收返修桥", "requirement": "对账流程要能被验收驱动返修。"},
        ).json()["id"]
        # 没有验收记录 → 404 人话
        assert client.post(
            f"/api/v1/applications/{application_id}/acceptance/repair", headers=HEADERS,
            json={},
        ).status_code == 404

        # 存一份失败验收单 + 一个终态构建
        save_report(settings.data_dir, application_id, {
            "application_id": application_id,
            "application_name": "验收返修桥",
            "version": 1,
            "summary": "金额必须对上",
            "required_node_types": [],
            "required_any_node_types": [],
            "architecture_pass": True,
            "lineage_pass": False,
            "stamp": "t",
            "accepted": False,
            "architecture_missing": [],
            "lineage_missing": ["deployed_model"],
            "cases": [{
                "name": "金额核对", "passed": False, "run_status": "succeeded",
                "checks": [{"check": "total = 4780", "passed": False, "actual": "0"}],
            }],
            "passed_cases": 0,
        })
        build_id = client.post(
            f"/api/v1/applications/{application_id}/builds", headers=HEADERS,
            json={"requirement": "对账流程要能被验收驱动返修。", "auto_publish": False, "max_turns": 5},
        ).json()["build_id"]
        import time as _time
        for _ in range(400):
            if client.get(f"/api/v1/builds/{build_id}", headers=HEADERS).json()["status"] not in {"queued", "building"}:
                break
            _time.sleep(0.01)

        result = client.post(
            f"/api/v1/applications/{application_id}/acceptance/repair", headers=HEADERS,
            json={},
        )
        assert result.status_code == 200, result.text
        assert result.json()["failure_items"] >= 2  # 血缘 + 用例检查项

        # 改单进了会话流（owner 记录），构建被复活
        transcript = client.get(f"/api/v1/builds/{build_id}/transcript", headers=HEADERS).json()
        owners = [r for r in transcript["records"] if r.get("kind") == "owner"]
        assert any("验收不通过" in (r.get("text") or "") for r in owners)
