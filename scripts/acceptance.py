"""模拟甲方验收：构建 → 逐用例试运行 → 出验收单。

用法：
    python scripts/acceptance.py path/to/project.json [--app-id APP --skip-build]

项目规格（JSON）：
    {
      "project_id": "jewelry-promo-report",
      "name": "促销分析报告生成",
      "requirement": "<客户口吻需求 + 系统对接说明（输入/输出字段名）>",
      "required_node_types": ["record_match"],          # 可选：平台审查层
      "max_turns": 40,                                   # 可选
      "cases": [
        {
          "name": "春节旺季样本",
          "inputs": {...},
          "human_input": {...},                          # 可选：human_input 节点的模拟人工
          "expect": {
            "required_fields": ["report"],              # 终端输出必须出现的字段
            "contains": {"report": ["VIP", "25.28%"]},  # 字段值必须包含的片段
            "not_contains": {"report": ["保证", "翻倍"]},
            "equals": {"total": 16663}                   # 字段值必须相等（数值宽松比较）
          }
        }
      ]
    }

产出：real- projects/results/<project_id>/<时间戳>/acceptance.md（甲方可读验收单）+ acceptance.json。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from run_benchmark import (  # noqa: E402
    flatten_keys,
    follow_build,
    request,
    run_workflow,
    terminal_outputs,
)


def resolve_path(value: object, dotted: str) -> object:
    node = value
    for part in dotted.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def loose_equal(actual: object, expected: object) -> bool:
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return abs(float(actual) - float(expected)) < 1e-6
    if isinstance(expected, (int, float)) and isinstance(actual, str):
        try:
            return abs(float(actual.replace(",", "")) - float(expected)) < 1e-6
        except ValueError:
            return False
    return actual == expected


def evaluate_case(case: dict, outputs: dict) -> list[dict]:
    expect = case.get("expect", {})
    checks: list[dict] = []
    present = flatten_keys(outputs)
    for field in expect.get("required_fields", []):
        checks.append({
            "check": f"输出包含字段 {field}",
            "passed": field in present,
            "actual": "存在" if field in present else f"缺失（实际字段：{sorted(present)[:12]}）",
        })
    for dotted, fragments in expect.get("contains", {}).items():
        value = resolve_path(outputs, dotted)
        text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
        for fragment in fragments:
            checks.append({
                "check": f"{dotted} 包含「{fragment}」",
                "passed": bool(text) and fragment in text,
                "actual": (text or "<空>")[:160],
            })
    for dotted, fragments in expect.get("not_contains", {}).items():
        value = resolve_path(outputs, dotted)
        text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
        for fragment in fragments:
            checks.append({
                "check": f"{dotted} 不出现「{fragment}」",
                "passed": not text or fragment not in text,
                "actual": (text or "<空>")[:160],
            })
    for dotted, expected in expect.get("equals", {}).items():
        actual = resolve_path(outputs, dotted)
        checks.append({
            "check": f"{dotted} = {expected}",
            "passed": loose_equal(actual, expected),
            "actual": json.dumps(actual, ensure_ascii=False)[:160],
        })
    return checks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", help="项目规格 JSON 路径")
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--token", default=None)
    parser.add_argument("--app-id", default=None, help="复用已有应用")
    parser.add_argument("--skip-build", action="store_true", help="不重新构建，直接验收现有工作流")
    args = parser.parse_args()

    token = args.token or os.environ.get("API_TOKEN", "")
    if not token:
        env_file = REPO / ".env"
        for line in env_file.read_text().splitlines() if env_file.is_file() else []:
            if line.startswith("API_TOKEN="):
                token = line.split("=", 1)[1].strip()
    if not token:
        raise SystemExit("no API token: pass --token or set API_TOKEN")

    spec = json.loads(Path(args.spec).read_text())
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = REPO / "real- projects" / "results" / spec["project_id"] / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    report: dict = {"project_id": spec["project_id"], "name": spec.get("name", ""), "stamp": stamp}

    app_id = args.app_id
    if not app_id:
        app = request(args.base_url, token, "POST", "/api/v1/applications", {
            "name": spec.get("name", spec["project_id"]),
            "requirement": spec["requirement"],
        })
        app_id = app["id"]
    report["application_id"] = app_id

    if not args.skip_build:
        build = request(args.base_url, token, "POST", f"/api/v1/applications/{app_id}/builds", {
            "requirement": spec["requirement"],
            "auto_publish": True,
            "max_turns": spec.get("max_turns", 40),
            "max_elapsed_seconds": spec.get("max_elapsed_seconds", 1800),
        })
        print(f"build={build['build_id']}")
        final = follow_build(args.base_url, token, build["build_id"], spec.get("max_elapsed_seconds", 1800))
        report["build_id"] = build["build_id"]
        conversation = []
        # 她 ask_owner 提问且规格备好了甲方答复时，自动回信继续（最多两轮）。
        for _ in range(2):
            question = (final.get("team_state") or {}).get("pending_question")
            reply = spec.get("owner_reply_when_asked")
            if final["status"] != "needs_attention" or not question or not reply:
                break
            print(f"莉莉丝提问：{question[:200]}…\n→ 以甲方口吻自动回复，继续搭建")
            conversation.append({"question": question, "reply": reply})
            request(args.base_url, token, "POST", f"/api/v1/builds/{build['build_id']}/resume", {
                "message": reply,
            })
            final = follow_build(args.base_url, token, build["build_id"], spec.get("max_elapsed_seconds", 1800))
        if conversation:
            report["conversation"] = conversation
        report["build_status"] = final["status"]
        report["build_error"] = final.get("error")
        question = (final.get("team_state") or {}).get("pending_question")
        if final["status"] not in {"ready", "published"} and question:
            report["pending_question"] = question
            print(f"莉莉丝仍在等待回复：{question}")

    draft = request(args.base_url, token, "GET", f"/api/v1/applications/{app_id}/draft")
    node_types = {node["type"] for node in draft["snapshot"]["workflow"]["nodes"]}
    report["node_types"] = sorted(node_types)
    required_types = spec.get("required_node_types", [])
    any_of = spec.get("required_any_node_types", [])
    report["architecture_missing"] = [t for t in required_types if t not in node_types]
    if any_of and not node_types.intersection(any_of):
        report["architecture_missing"].append("any-of:" + "|".join(any_of))
    report["architecture_pass"] = not report["architecture_missing"]

    case_rows = []
    for case in spec.get("cases", []):
        row: dict = {"name": case["name"]}
        try:
            run = run_workflow(
                args.base_url, token, app_id,
                case.get("inputs", {}),
                human_input=case.get("human_input"),
            )
            row["run_status"] = run.get("status")
            outputs = terminal_outputs(args.base_url, token, app_id, run)
            row["outputs"] = outputs
            row["checks"] = evaluate_case(case, outputs)
        except Exception as error:  # 验收记录一切，不中断
            row["run_status"] = f"error: {error}"
            row["checks"] = [{"check": "运行成功", "passed": False, "actual": str(error)}]
        row["passed"] = all(check["passed"] for check in row["checks"]) and row.get("run_status") == "succeeded"
        case_rows.append(row)
        print(f"  用例[{case['name']}] run={row['run_status']} 通过={row['passed']}")

    report["cases"] = case_rows
    passed_cases = sum(1 for row in case_rows if row["passed"])
    report["accepted"] = (
        report["architecture_pass"]
        and bool(case_rows)
        and passed_cases == len(case_rows)
        and report.get("build_status", "published") in {"ready", "published"}
    )

    (out_dir / "acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))

    lines = [
        f"# 验收单：{report['name'] or report['project_id']}",
        "",
        f"- 时间：{stamp} (UTC)",
        f"- 应用：`{app_id}`",
        f"- 构建：{report.get('build_status', '（复用现有工作流）')}"
        + (f"，异常：{report['build_error']}" if report.get("build_error") else ""),
        f"- 架构审查：{'通过' if report['architecture_pass'] else '不通过，缺少 ' + '、'.join(report['architecture_missing'])}"
        + f"（节点：{'、'.join(report['node_types'])}）",
        "",
        f"## 用例（{passed_cases}/{len(case_rows)} 通过）",
        "",
    ]
    for row in case_rows:
        lines.append(f"### {'✅' if row['passed'] else '❌'} {row['name']}（运行：{row['run_status']}）")
        lines.append("")
        lines.append("| 检查项 | 结果 | 实际 |")
        lines.append("| --- | --- | --- |")
        for check in row["checks"]:
            actual = str(check["actual"]).replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {check['check']} | {'通过' if check['passed'] else '不通过'} | {actual} |")
        lines.append("")
    lines.append(f"## 结论：{'✅ 验收通过' if report['accepted'] else '❌ 需要整改'}")
    (out_dir / "acceptance.md").write_text("\n".join(lines) + "\n")

    print(f"\n验收{'通过' if report['accepted'] else '不通过'} → {out_dir / 'acceptance.md'}")


if __name__ == "__main__":
    main()
