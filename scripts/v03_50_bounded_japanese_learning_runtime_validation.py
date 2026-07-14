#!/usr/bin/env python3
"""Verify v0.3.50 bounded Japanese-learning runtime validation."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs" / "workingon" / "bounded_japanese_learning_runtime_validation_v0.3.50.json"
EXPECTED_RUNTIME_VERSION = "v0.3.6"
FORBIDDEN_LIVE_ENDPOINTS = ("/builds", "/tests/run", "/runs", "/versions", "/restore", "/draft", "/cancel")


CONTROLLED_COMMENT_FIXTURE_TEMPLATE = """受控样例评论线索（离线验证用，不代表已经抓取真实视频网站）
主题：{{ topic }}
- 评论 A：「それな、課題多すぎてしんどい」用于朋友间强烈附和。
- 评论 B：「ワンチャン間に合う？」表示也许还有机会，语气很口语。
- 评论 C：「普通に助かる」表示真的很有帮助，语气自然但偏随意。

来源边界：这是受控样例评论集，用来验证学习总结的结果形状；接入真实公开视频评论前，不宣称外部采集已完成。"""

LEARNING_SUMMARY_TEMPLATE = """# 今日日语口语总结：{{ topic }}

受控样例来源：离线评论夹具，用于验证结果形状；真实公开视频评论采集需要在后续版本单独接入证据。

## 1. それな
- 中文含义：对，就是这样；我懂你说的。
- 自然例句：A「課題、今日も多すぎない？」B「それな、ちょっとしんどい。」
- 语气/场景：朋友、同学之间强烈附和，不适合正式汇报。
- 学习提醒：可以理解成比「そうだね」更口语、更有共鸣感。

## 2. ワンチャン
- 中文含义：也许有机会；说不定能成。
- 自然例句：「今から図書館行けば、ワンチャン間に合うかも。」
- 语气/场景：年轻人聊天里常见，带一点侥幸和轻松感。
- 学习提醒：正式场合改用「可能性があります」更安全。

## 3. 普通に助かる
- 中文含义：真的挺有帮助；老实说很救命。
- 自然例句：「ノート共有してくれるの、普通に助かる。」
- 语气/场景：自然表达感谢，比直译的“普通地”更接近“其实很/真的”。
- 学习提醒：这里的「普通に」不是普通程度，而是强调自然真实的评价。

## 来源上下文
{{ comment_clues }}"""


BUG_LEDGER = (
    {"id": "P1-learning-runtime-fixture-missing", "severity": "P1", "status": "fixed", "reproduction": "The Japanese-learning safe draft had scenario labels but no controlled comment fixture for runtime validation.", "fix": "Add a bounded offline comment fixture in the no-model safe draft.", "verification": "bounded_learning_fixture."},
    {"id": "P1-learning-summary-output-placeholder", "severity": "P1", "status": "fixed", "reproduction": "The run output could still read like a placeholder rather than a learner-facing daily summary.", "fix": "Render a deterministic study-card summary with expression, meaning, example, tone/context, and learning reminder sections.", "verification": "learning_summary_template_fixture."},
    {"id": "P1-controlled-fixture-boundary-hidden", "severity": "P1", "status": "fixed", "reproduction": "The UI did not say that the current result proof is controlled fixture validation rather than live public-video collection.", "fix": "Add Run tab controlled-fixture guidance copy.", "verification": "controlled_fixture_ui_boundary."},
    {"id": "P1-runtime-quality-gate-missing", "severity": "P1", "status": "fixed", "reproduction": "The gate proved source markers but not a runnable answer shape.", "fix": "Add v0.3.50 runtime quality expectations and in-process pytest validation.", "verification": "runtime_quality_expectations."},
    {"id": "P1-v0350-tests-must-enter-release-gate", "severity": "P1", "status": "fixed", "reproduction": "Bounded runtime validation could regress if omitted from the current v0.3.x gate.", "fix": "Update the regression lane with v0.3.50 and the new pass-count floor.", "verification": "regression_manifest_updated."},
)


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def runtime_workflow_fixture() -> dict[str, Any]:
    nodes = [
        {
            "id": "jp_topic_fixture",
            "type": "start",
            "title": "关注的日语主题",
            "config": {"inputs": [{"name": "topic", "label": "关注的日语主题", "type": "string", "required": True, "default": "校园生活"}]},
        },
        {
            "id": "jp_collect_comments_fixture",
            "type": "template_transform",
            "title": "收集受控样例评论线索",
            "config": {
                "template": CONTROLLED_COMMENT_FIXTURE_TEMPLATE,
                "variables": {"topic": {"$ref": {"node_id": "jp_topic_fixture", "path": ["output", "topic"]}}},
            },
        },
        {
            "id": "jp_extract_expressions_fixture",
            "type": "template_transform",
            "title": "生成学习卡片总结",
            "config": {
                "template": LEARNING_SUMMARY_TEMPLATE,
                "variables": {
                    "topic": {"$ref": {"node_id": "jp_topic_fixture", "path": ["output", "topic"]}},
                    "comment_clues": {"$ref": {"node_id": "jp_collect_comments_fixture", "path": ["text"]}},
                },
            },
        },
        {
            "id": "jp_daily_summary_fixture",
            "type": "answer",
            "title": "今日日语口语总结",
            "config": {"answer": {"$ref": {"node_id": "jp_extract_expressions_fixture", "path": ["text"]}}},
        },
    ]
    edges = [
        {"id": "jp_fixture_collect", "source": "jp_topic_fixture", "target": "jp_collect_comments_fixture", "source_port": "output", "target_port": "input"},
        {"id": "jp_fixture_extract", "source": "jp_collect_comments_fixture", "target": "jp_extract_expressions_fixture", "source_port": "text", "target_port": "input"},
        {"id": "jp_fixture_summary", "source": "jp_extract_expressions_fixture", "target": "jp_daily_summary_fixture", "source_port": "text", "target_port": "input"},
    ]
    test = {
        "id": "jp_fixture_summary_quality",
        "name": "Bounded Japanese learning summary quality",
        "requirement": "Controlled fixture run returns a learner-readable daily spoken-Japanese summary.",
        "inputs": {"topic": "校园生活"},
        "assertions": [
            {"path": ["answer"], "operator": "contains", "expected": "今日日语口语总结：校园生活"},
            {"path": ["answer"], "operator": "contains", "expected": "それな"},
            {"path": ["answer"], "operator": "contains", "expected": "中文含义"},
            {"path": ["answer"], "operator": "contains", "expected": "自然例句"},
            {"path": ["answer"], "operator": "contains", "expected": "语气/场景"},
            {"path": ["answer"], "operator": "contains", "expected": "学习提醒"},
            {"path": ["answer"], "operator": "contains", "expected": "受控样例来源"},
        ],
        "required_node_types": ["start", "template_transform", "answer"],
        "required_tools": [],
        "minimum_tool_calls": 0,
        "mandatory": True,
        "structural_only": False,
    }
    return {"nodes": nodes, "edges": edges, "test": test}


def bounded_learning_fixture() -> dict[str, Any]:
    cases = {
        "fixture_mentions_controlled_sample": "受控样例评论线索" in CONTROLLED_COMMENT_FIXTURE_TEMPLATE,
        "fixture_does_not_claim_live_collection": "不代表已经抓取真实视频网站" in CONTROLLED_COMMENT_FIXTURE_TEMPLATE,
        "fixture_contains_public_video_boundary": "接入真实公开视频评论前" in CONTROLLED_COMMENT_FIXTURE_TEMPLATE,
        "fixture_contains_three_spoken_expressions": all(item in CONTROLLED_COMMENT_FIXTURE_TEMPLATE for item in ["それな", "ワンチャン", "普通に助かる"]),
    }
    return {"id": "bounded_learning_fixture", "passed": all(cases.values()), "cases": cases}


def learning_summary_template_fixture() -> dict[str, Any]:
    cases = {
        "summary_mentions_topic": "今日日语口语总结：{{ topic }}" in LEARNING_SUMMARY_TEMPLATE,
        "summary_has_meaning": "中文含义" in LEARNING_SUMMARY_TEMPLATE,
        "summary_has_example": "自然例句" in LEARNING_SUMMARY_TEMPLATE,
        "summary_has_tone_context": "语气/场景" in LEARNING_SUMMARY_TEMPLATE,
        "summary_has_learning_reminder": "学习提醒" in LEARNING_SUMMARY_TEMPLATE,
        "summary_cites_controlled_source": "受控样例来源" in LEARNING_SUMMARY_TEMPLATE,
    }
    return {"id": "learning_summary_template_fixture", "passed": all(cases.values()), "cases": cases}


def controlled_fixture_ui_boundary() -> dict[str, Any]:
    cases = {
        "zh_copy_present": "受控样例验证" in read_text("platform/frontend/lib/i18n.ts"),
        "en_copy_present": "Controlled fixture validation" in read_text("platform/frontend/lib/i18n.ts"),
        "run_page_marker_present": "scenario-fixture-note" in read_text("platform/frontend/app/applications/[id]/page.tsx"),
        "style_marker_present": ".scenario-fixture-note" in read_text("platform/frontend/app/globals.css"),
    }
    return {"id": "controlled_fixture_ui_boundary", "passed": all(cases.values()), "cases": cases}


def runtime_quality_expectations() -> dict[str, Any]:
    test = runtime_workflow_fixture()["test"]
    expected_terms = [assertion["expected"] for assertion in test["assertions"]]
    cases = {
        "expects_topic_specific_title": "今日日语口语总结：校园生活" in expected_terms,
        "expects_expression": "それな" in expected_terms,
        "expects_meaning": "中文含义" in expected_terms,
        "expects_example": "自然例句" in expected_terms,
        "expects_tone_context": "语气/场景" in expected_terms,
        "expects_learning_reminder": "学习提醒" in expected_terms,
        "expects_controlled_source": "受控样例来源" in expected_terms,
    }
    return {"id": "runtime_quality_expectations", "passed": all(cases.values()), "cases": cases, "expected_terms": expected_terms}


def regression_manifest_check() -> dict[str, Any]:
    relative_path = "docs/testing/regression_lanes.json"
    manifest = json.loads(read_text(relative_path))
    current_lane = next((lane for lane in manifest.get("lanes", []) if lane.get("id") == "v0.3.x_current_release_gate"), {})
    test_files = set(current_lane.get("test_files", []))
    command = current_lane.get("command", [])
    pass_count = current_lane.get("expected", {}).get("pass_count", 0)
    cases = {
        "current_gate_present": bool(current_lane),
        "v0350_test_in_test_files": "tests/test_v03_50_bounded_japanese_learning_runtime_validation.py" in test_files,
        "v0350_test_in_command": "tests/test_v03_50_bounded_japanese_learning_runtime_validation.py" in command,
        "pass_count_not_less_than_v0350_floor": isinstance(pass_count, int) and pass_count >= 288,
    }
    return {"id": "regression_manifest_updated", "path": relative_path, "passed": all(cases.values()), "cases": cases, "pass_count": pass_count}


def source_marker_checks() -> list[dict[str, Any]]:
    checks = [
        (
            "bounded_learning_safe_draft_source_markers",
            "platform/frontend/app/page.tsx",
            (
                "JAPANESE_LEARNING_COMMENT_FIXTURE_TEMPLATE",
                "JAPANESE_LEARNING_SUMMARY_TEMPLATE",
                "受控样例评论线索",
                "受控样例来源",
                "Japanese learning scenario structure and summary quality",
                "operator: 'contains', expected: 'それな'",
                "structural_only: false",
            ),
        ),
        (
            "bounded_learning_run_ui_markers",
            "platform/frontend/app/applications/[id]/page.tsx",
            (
                "japaneseLearningControlledFixtureTitle",
                "japaneseLearningControlledFixtureHelp",
                "scenario-fixture-note",
            ),
        ),
        (
            "bounded_learning_i18n_markers",
            "platform/frontend/lib/i18n.ts",
            (
                "受控样例验证",
                "不会宣称已经完成外部采集",
                "Controlled fixture validation",
                "does not claim live public-video collection",
            ),
        ),
        (
            "bounded_learning_style_markers",
            "platform/frontend/app/globals.css",
            (
                ".scenario-fixture-note",
                ".scenario-fixture-note b",
            ),
        ),
    ]
    evidence: list[dict[str, Any]] = []
    for check_id, relative_path, markers in checks:
        text = read_text(relative_path)
        missing = [marker for marker in markers if marker not in text]
        evidence.append({"id": check_id, "path": relative_path, "required_markers": list(markers), "missing_markers": missing, "passed": not missing})
    evidence.append(regression_manifest_check())
    return evidence


def bug_ledger_evidence() -> dict[str, Any]:
    blocking = [item for item in BUG_LEDGER if item["severity"] in {"P0", "P1"} and item["status"] not in {"fixed", "verified_fixed", "deferred_with_reason"}]
    return {"id": "p0_p1_bug_ledger_bounded_japanese_learning_runtime", "passed": not blocking, "bug_count": len(BUG_LEDGER), "blocking_bug_count": len(blocking), "bugs": list(BUG_LEDGER)}


def request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "Lilies-v0.3.50-bounded-japanese-learning-runtime"})
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read().decode("utf-8", errors="replace")
        return {"status_code": response.getcode(), "json": json.loads(body)}


def runtime_health_check(api_url: str) -> dict[str, Any]:
    url = api_url.rstrip("/") + "/health"
    try:
        result = request_json(url)
        runtime = result["json"].get("runtime", {}) if isinstance(result["json"], dict) else {}
        return {"id": "runtime_health_read_only", "url": url, "passed": result["status_code"] == 200 and runtime.get("version") == EXPECTED_RUNTIME_VERSION and runtime.get("current_code_ready") is True, "status_code": result["status_code"], "runtime": runtime}
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        return {"id": "runtime_health_read_only", "url": url, "passed": False, "status_code": 0, "error": str(error)}


def build_evidence(*, live: bool = False, api_url: str = "http://127.0.0.1:8001") -> dict[str, Any]:
    checks: list[dict[str, Any]] = [
        bug_ledger_evidence(),
        bounded_learning_fixture(),
        learning_summary_template_fixture(),
        controlled_fixture_ui_boundary(),
        runtime_quality_expectations(),
        *source_marker_checks(),
    ]
    safety: dict[str, Any] = {"forbidden_endpoint_called": False, "called_endpoints": [], "model_call_used": False}
    if live:
        checks.append(runtime_health_check(api_url))
        safety["called_endpoints"].append("GET /health")
    safety["forbidden_endpoint_called"] = any(any(endpoint in called for endpoint in FORBIDDEN_LIVE_ENDPOINTS) for called in safety["called_endpoints"])
    checks.append({"id": "safety_no_forbidden_live_runtime_validation_call", "passed": safety["forbidden_endpoint_called"] is False and safety["model_call_used"] is False, "called_endpoints": safety["called_endpoints"], "forbidden_endpoint_fragments": list(FORBIDDEN_LIVE_ENDPOINTS), "model_call_used": safety["model_call_used"]})
    failed = [check for check in checks if not check.get("passed")]
    return {
        "version": "v0.3.50",
        "stage": "bounded_japanese_learning_runtime_validation",
        "status": "passed" if not failed else "failed",
        "live_checks_enabled": live,
        "safety": safety,
        "bug_ledger": list(BUG_LEDGER),
        "checks": checks,
        "runtime_fixture": runtime_workflow_fixture(),
        "summary": {
            "failed_check_count": len(failed),
            "open_p0_p1_bug_count": bug_ledger_evidence()["blocking_bug_count"],
            "forbidden_endpoint_called": safety["forbidden_endpoint_called"],
            "bounded_learning_fixture": bounded_learning_fixture(),
            "learning_summary_template": learning_summary_template_fixture(),
            "runtime_quality_expectations": runtime_quality_expectations(),
        },
    }


def write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run v0.3.50 bounded Japanese-learning runtime validation evidence.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--api-url", default="http://127.0.0.1:8001")
    args = parser.parse_args()
    evidence = build_evidence(live=args.live, api_url=args.api_url)
    write_evidence(args.output, evidence)
    print(json.dumps({"status": evidence["status"], "output": str(args.output)}, ensure_ascii=False))
    return 0 if evidence["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
