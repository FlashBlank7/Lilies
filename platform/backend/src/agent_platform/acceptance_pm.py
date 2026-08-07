"""监理——按需受邀的第二个智能体。

与建造者（莉莉丝）物理隔离：不同角色、不同提示词、互不见对方的工作内容
（验收规格绝不注入构建上下文——考生不能见卷）。只在业主点"请监理"时进场，
干三件事：

1. 出卷：读需求原文 + 业主用人话给的例子，翻译成声明式验收规格；
2. 监考：对发布版逐用例试运行，机械对答案，并核对运行流水账
   （节点是否真实执行——引擎写的账，建造者伪造不了）；
3. 陪看与解释：业主看不懂搭建过程时，用业务语言解释；应邀审查当前进度，
   出至多三条监理笔记。

它面对的只是业主视角的材料（需求包、会话、图、数据、输出），不碰平台内脏。
简单任务不需要它；这里没有任何自动触发。
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .models import ChatMessage, ContentBlock

PM_MAX_CASES = 8
PM_SPEC_VERSION = 1


class AcceptanceExpect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required_fields: list[str] = Field(default_factory=list, max_length=32)
    equals: dict[str, Any] = Field(default_factory=dict)
    contains: dict[str, list[str]] = Field(default_factory=dict)
    not_contains: dict[str, list[str]] = Field(default_factory=dict)
    # 过程要求：这条用例的运行流水账里必须出现/不许出现的节点类型
    must_execute: list[str] = Field(default_factory=list, max_length=16)
    must_not_execute: list[str] = Field(default_factory=list, max_length=16)


class AcceptanceCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    inputs: dict[str, Any]
    human_input: dict[str, Any] | None = None
    expect: AcceptanceExpect


class AcceptanceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = PM_SPEC_VERSION
    summary: str = Field(min_length=1, max_length=2_000)
    required_node_types: list[str] = Field(default_factory=list, max_length=16)
    required_any_node_types: list[str] = Field(default_factory=list, max_length=16)
    cases: list[AcceptanceCase] = Field(min_length=1, max_length=PM_MAX_CASES)
    # 监理的主动性只许进建议栏：业主不采纳就不生效，绝不混进 cases。
    suggestions: list[str] = Field(default_factory=list, max_length=8)


PM_SYSTEM = """你是一位独立的工作流监理。你不参与搭建，也永远不指导搭建方；
你只代表业主，把"什么算合格"翻译成可机械核验的验收规格。

输入给你：客户需求原文（含系统对接说明的固定字段名）、业主用大白话提供的例子和要求、
以及平台公开的节点类型词表。

输出一个 JSON 对象（不要任何其它文字），字段：
- summary: 用业主能看懂的话复述这次验收查什么（两三句）
- required_node_types: 工作流图里必须存在的节点类型（仅当业主要求里明确蕴含某类环节时才写，
  例如"必须真的调用我们训练的模型"→ deployed_model_inference；"必须做知识检索"→ knowledge_retrieval；
  没有这类要求就留空数组）
- required_any_node_types: 至少存在其一的节点类型（同上，通常留空）
- cases: 1 到 8 条用例，每条：
  - name: 业务化名字
  - inputs: 完整可运行的输入对象（字段名必须严格用系统对接说明里的；业主例子里给了数据就原样用，
    没给的合理补全）
  - human_input: 若流程需要人工确认，模拟值班员的应答对象；否则省略
  - expect:
    - required_fields: 输出里必须出现的字段名（来自对接说明）
    - equals: 输出字段必须相等的值（只写业主明确说了"该出什么"的；布尔/数字/短文本）
    - contains: 字段值必须包含的片段（数组）
    - not_contains: 字段值绝不许出现的片段（业主的红线，如"人民币"、编造数字）
    - must_execute: 这条用例运行时必须真实执行到的节点类型（有过程要求才写）
    - must_not_execute: 不许执行的节点类型（少用）

- suggestions: 字符串数组。你认为还值得验、但业主没有明说的点，全部写在这里
  （每条一句业务语言），供业主决定要不要采纳。

规则：
- 【卷面主权】cases 只许来自业主明说的例子与要求：业主给了几个例子就出几条用例，
  一个不多、一个不少。你多想到的任何检查点一律进 suggestions，绝不自行加入 cases。
- 一切判定必须可机械核验；写不出机械判定的期望就不要写（宁缺毋滥）。
- 用例输入必须自洽完整，能直接跑。
- 绝不发明对接说明之外的字段名。
- 业主没提过程要求时，must_execute/required_node_types 留空——不要自作主张。"""


EXPLAIN_SYSTEM = """你是业主请的独立监理。业主看不懂搭建方（莉莉丝）正在做什么，请你解释。
你只依据业主自己也能看到的材料：需求原文、会话记录、当前工作流的节点清单、最近的运行输出。
用纯业务语言回答（两三段以内）：她现在在干什么、进展到哪一步、有没有值得业主留意的风险。
【硬性语言纪律】禁止出现任何机器词汇：节点类型名、配置项、字段名（凡是英文加下划线或
驼峰拼写的代码样词，如 some_field、camelCase）一律换成业务称呼（例如"某项振动指标"、
"判定结果"）。材料里出现过也不许照抄。
如果材料里看不出来，就直说看不出来，不要编。"""

REVIEW_SYSTEM = """你是业主请的独立监理，正在旁听一次工作流搭建。你不指导搭建方，只替业主把关。
你只依据业主视角的材料：需求原文、会话记录摘要、当前工作流的节点清单与连接、最近的运行输出。
请用业务语言输出至多三条"监理笔记"，每条一句话：指出可能的逻辑问题、与需求的偏差、
或输出上的风险（例如：需求要的某个结果目前的流程里看不到来源；某个环节看起来没有被用到）。
没有值得说的就输出一条："目前没有发现值得业主留意的问题。"
【硬性语言纪律】禁止任何机器词汇：节点类型名、配置项、代码样的字段名（英文下划线/驼峰）
一律换成业务称呼，材料里出现过也不许照抄。禁止给搭建方的操作建议——你的读者是业主。"""


_MACHINE_TOKEN = re.compile(
    r"\b(?:[A-Za-z]+_[A-Za-z0-9_]+|[a-z]+[A-Z][A-Za-z0-9]*)\b"
)
_MACHINE_ALLOWLIST = {"elevator-fault-v1"}  # 部署名等业主契约里的正式名称不算泄漏


def owner_language_violations(text: str, block_types: list[str]) -> list[str]:
    """面向业主的文本里不许出现的机器词汇：代码样标识符与积木类型名。"""

    hits = {
        token
        for token in _MACHINE_TOKEN.findall(text)
        if token not in _MACHINE_ALLOWLIST
    }
    lowered = text.lower()
    hits |= {
        block_type
        for block_type in block_types
        if ("_" in block_type or len(block_type) >= 8) and block_type in lowered
    }
    return sorted(hits)


def redact_machine_tokens(text: str, violations: list[str]) -> str:
    for token in violations:
        text = text.replace(token, "（技术指标）")
    return text


DEFAULT_LESSONS = """- 卷面主权：用例只来自业主明说的例子，一个不多一个不少；监理多想到的检查点只进建议栏。
- 语言纪律：面向业主的一切文字不出现机器词汇（字段名、节点名、配置项），材料里出现过也不照抄。
"""


def lessons_path(data_dir: Path) -> Path:
    return Path(data_dir) / "acceptance" / "pm_lessons.md"


def load_lessons(data_dir: Path) -> str:
    path = lessons_path(data_dir)
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_LESSONS)
    return path.read_text().strip()


def append_lesson(data_dir: Path, lesson: str) -> str:
    text = lesson.strip()
    if not text:
        raise ValueError("教训不能为空")
    path = lessons_path(data_dir)
    existing = load_lessons(data_dir)
    if text in existing:
        return existing
    path.write_text(existing + f"\n- {text}\n")
    return load_lessons(data_dir)


def _with_lessons(system: str, data_dir: Path) -> str:
    lessons = load_lessons(data_dir)
    return f"{system}\n\n【历届项目沉淀的监理纪律——逐条遵守】\n{lessons}"


def _json_object_from_text(text: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("监理没有返回 JSON 对象")
    return json.loads(match.group())


def normalize_spec_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """容错归一化模型笔误：空数组当空对象、null 当缺省。语义不放宽。"""

    def clean_map(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    def clean_list(value: Any) -> list[Any]:
        return value if isinstance(value, list) else []

    payload = dict(payload)
    payload["required_node_types"] = clean_list(payload.get("required_node_types"))
    payload["required_any_node_types"] = clean_list(payload.get("required_any_node_types"))
    payload["suggestions"] = [
        str(item).strip()
        for item in clean_list(payload.get("suggestions"))
        if item is not None and str(item).strip()
    ][:8]
    cases = []
    for case in clean_list(payload.get("cases")):
        if not isinstance(case, dict):
            continue
        case = dict(case)
        expect = case.get("expect")
        expect = dict(expect) if isinstance(expect, dict) else {}
        expect["required_fields"] = clean_list(expect.get("required_fields"))
        expect["equals"] = clean_map(expect.get("equals"))
        expect["contains"] = {
            key: clean_list(value) for key, value in clean_map(expect.get("contains")).items()
        }
        expect["not_contains"] = {
            key: clean_list(value) for key, value in clean_map(expect.get("not_contains")).items()
        }
        expect["must_execute"] = clean_list(expect.get("must_execute"))
        expect["must_not_execute"] = clean_list(expect.get("must_not_execute"))
        case["expect"] = expect
        if case.get("human_input") is None:
            case.pop("human_input", None)
        cases.append(case)
    payload["cases"] = cases
    return payload


def _catalog_lines(blocks: Any) -> str:
    return "\n".join(f"- {item.type}: {item.title}" for item in blocks.list())


async def _pm_chat(
    services: Any,
    application_id: str,
    system: str,
    prompt: str,
    phase: str,
    max_output_tokens: int = 2_000,
) -> str:
    from uuid import uuid4

    task_id = str(uuid4())
    model = services.settings.deepseek_runtime_model
    await services.harness.start_task(
        task_id,
        kind="acceptance_pm",
        owner_id=application_id,
        resource_id=task_id,
        metadata={"application_id": application_id, "phase": phase},
    )
    try:
        await services.harness.record_usage(
            task_id, "model_call", metadata={"model": model, "mode": phase}
        )
        stream = services.provider.stream(
            model=model,
            system=system,
            messages=[ChatMessage(role="user", content=[ContentBlock(type="text", text=prompt)])],
            tools=[],
            max_output_tokens=max_output_tokens,
            thinking_enabled=False,
            effort="low",
            user_id=task_id,
        )
        response = await services.runtime._collect_stream(
            task_id, stream, "acceptance_pm.model", model,
            timeout_seconds=min(services.settings.deepseek_timeout_seconds, 180.0),
        )
        await services.harness.record_model_usage(
            task_id, response.usage, model=model,
            provider=services.provider.provider_name_for(model),
            metadata={"phase": phase},
        )
        await services.harness.finish_task(task_id, status="succeeded")
        return "".join(
            block.text or "" for block in response.blocks if block.type == "text"
        ).strip()
    except Exception as error:
        await services.harness.finish_task(task_id, status="failed", error=str(error))
        raise


async def generate_spec(
    services: Any,
    application: dict[str, Any],
    owner_examples: str,
) -> AcceptanceSpec:
    """出卷：一次模型调用，把需求 + 业主例子翻译成验收规格。"""

    prompt = (
        f"客户需求原文：\n{application.get('requirement') or application.get('description') or ''}\n\n"
        f"业主的例子与要求（大白话）：\n{owner_examples}\n\n"
        f"平台公开节点类型词表：\n{_catalog_lines(services.blocks)}\n\n"
        "请输出验收规格 JSON。"
    )
    text = await _pm_chat(
        services,
        application["id"],
        _with_lessons(PM_SYSTEM, services.settings.data_dir),
        prompt,
        "spec",
        max_output_tokens=8_000,
    )
    return AcceptanceSpec.model_validate(
        normalize_spec_payload(_json_object_from_text(text))
    )


# ---------------- 监考（纯机械，零模型调用） ----------------


def _resolve_path(value: Any, dotted: str) -> Any:
    node = value
    for part in dotted.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def _loose_equal(actual: Any, expected: Any) -> bool:
    if isinstance(expected, bool) or isinstance(actual, bool):
        return bool(actual) == bool(expected)
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return abs(float(actual) - float(expected)) < 1e-6
    return actual == expected


def _flatten_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(key)
            keys |= _flatten_keys(item)
    elif isinstance(value, list):
        for item in value:
            keys |= _flatten_keys(item)
    return keys


def evaluate_case(
    expect: AcceptanceExpect,
    outputs: dict[str, Any],
    executed_types: set[str],
) -> list[dict[str, Any]]:
    """机械对答案：输出检查 + 运行流水账核验。纯函数，便于测试。"""

    checks: list[dict[str, Any]] = []
    present = _flatten_keys(outputs)
    for field in expect.required_fields:
        checks.append({
            "check": f"输出包含字段 {field}",
            "passed": field in present,
            "actual": "存在" if field in present else f"缺失（实际：{sorted(present)[:10]}）",
        })
    for dotted, expected in expect.equals.items():
        actual = _resolve_path(outputs, dotted)
        checks.append({
            "check": f"{dotted} = {json.dumps(expected, ensure_ascii=False)}",
            "passed": _loose_equal(actual, expected),
            "actual": json.dumps(actual, ensure_ascii=False)[:160],
        })
    for dotted, fragments in expect.contains.items():
        value = _resolve_path(outputs, dotted)
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        for fragment in fragments:
            checks.append({
                "check": f"{dotted} 包含「{fragment}」",
                "passed": bool(text) and fragment in text,
                "actual": (text or "<空>")[:160],
            })
    for dotted, fragments in expect.not_contains.items():
        value = _resolve_path(outputs, dotted)
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        for fragment in fragments:
            checks.append({
                "check": f"{dotted} 不出现「{fragment}」",
                "passed": not text or fragment not in text,
                "actual": (text or "<空>")[:160],
            })
    for node_type in expect.must_execute:
        checks.append({
            "check": f"流水账核验：{node_type} 真实执行",
            "passed": node_type in executed_types,
            "actual": "、".join(sorted(executed_types)) or "<无节点执行>",
        })
    for node_type in expect.must_not_execute:
        checks.append({
            "check": f"流水账核验：{node_type} 未被执行",
            "passed": node_type not in executed_types,
            "actual": "、".join(sorted(executed_types)) or "<无节点执行>",
        })
    return checks


async def run_acceptance(services: Any, application_id: str) -> dict[str, Any]:
    """监考：对发布版逐用例试运行 + 流水账核验。零模型调用。"""

    from .workflow_models import WorkflowRunRequest

    application = await services.workflow_store.get_application(application_id)
    spec = load_spec(services.settings.data_dir, application_id)
    if spec is None:
        raise KeyError("还没有验收方案——先请监理出卷")
    if application.get("active_version") is None:
        raise RuntimeError("工作流还没有发布版；验收对象是交付物（发布版），请先发布")

    version_row = await services.workflow_store.get_version(application_id)
    snapshot = version_row["snapshot"]
    graph_types = {node.type for node in snapshot.workflow.nodes}
    terminal_ids = {
        node.id for node in snapshot.workflow.nodes if node.type in ("end", "answer")
    }
    architecture_missing = [t for t in spec.required_node_types if t not in graph_types]
    if spec.required_any_node_types and not graph_types.intersection(spec.required_any_node_types):
        architecture_missing.append("any-of:" + "|".join(spec.required_any_node_types))

    case_rows: list[dict[str, Any]] = []
    for case in spec.cases:
        row: dict[str, Any] = {"name": case.name}
        try:
            started = await services.workflow_runtime.create_run(
                application_id,
                WorkflowRunRequest(inputs=case.inputs, use_draft=False),
            )
            run_id = started["run_id"]
            resumed = False
            record: dict[str, Any] = {}
            for _ in range(150):
                record = await services.workflow_store.get_run(run_id)
                status = record["status"]
                if status == "paused" and case.human_input and not resumed:
                    await services.workflow_runtime.resume(run_id, case.human_input)
                    resumed = True
                    continue
                if status in {"succeeded", "failed", "cancelled"}:
                    break
                if status == "paused" and (resumed or not case.human_input):
                    break
                await asyncio.sleep(2)
            row["run_status"] = record.get("status", "unknown")
            state = record.get("state")
            outputs_by_node = (
                state.outputs if state is not None and hasattr(state, "outputs") else {}
            ) or {}
            merged: dict[str, Any] = {}
            for node_id, value in outputs_by_node.items():
                if (not terminal_ids or node_id in terminal_ids) and isinstance(value, dict):
                    merged.update(value)
            events = await services.storage.list_events(run_id, 0)
            executed = {
                str(event.data.get("type") or "")
                for event in events
                if event.type == "node.started"
            } - {""}
            row["executed_node_types"] = sorted(executed)
            row["checks"] = evaluate_case(case.expect, merged, executed)
        except Exception as error:
            row["run_status"] = f"error: {error}"
            row["checks"] = [{"check": "运行成功", "passed": False, "actual": str(error)[:300]}]
            row["executed_node_types"] = []
        row["passed"] = (
            row["run_status"] == "succeeded"
            and all(check["passed"] for check in row["checks"])
        )
        case_rows.append(row)

    passed_cases = sum(1 for row in case_rows if row["passed"])
    report = {
        "application_id": application_id,
        "application_name": application.get("name", application_id),
        "version": version_row.get("version"),
        "stamp": utc_stamp(),
        "summary": spec.summary,
        "required_node_types": spec.required_node_types,
        "required_any_node_types": spec.required_any_node_types,
        "architecture_missing": architecture_missing,
        "architecture_pass": not architecture_missing,
        "cases": case_rows,
        "passed_cases": passed_cases,
        "accepted": not architecture_missing and passed_cases == len(case_rows),
    }
    save_report(services.settings.data_dir, application_id, report)
    return report


# ---------------- 陪看与解释（业主视角材料，人话输出） ----------------


async def _owner_materials(
    services: Any, application_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]], Any, dict[str, Any] | None]:
    application = await services.workflow_store.get_application(application_id)
    records: list[dict[str, Any]] = []
    builds = await services.workflow_store.list_builds(application_id)
    if builds:
        records = await asyncio.to_thread(
            services.build_transcripts.read, builds[0]["id"], after_turn=0, limit=500
        )
    try:
        draft = await services.workflow_store.get_draft(application_id)
        snapshot = draft["snapshot"]
    except KeyError:
        snapshot = None
    outputs: dict[str, Any] | None = None
    runs = await services.workflow_store.list_runs(application_id, limit=1)
    if runs:
        state = runs[0].get("state")
        merged: dict[str, Any] = {}
        for value in ((state.outputs if hasattr(state, "outputs") else {}) or {}).values():
            if isinstance(value, dict):
                merged.update(value)
        outputs = merged or None
    return application, records, snapshot, outputs


def _owner_view_prompt(
    application: dict[str, Any],
    transcript_records: list[dict[str, Any]],
    snapshot: Any,
    outputs: dict[str, Any] | None,
    question: str,
) -> str:
    turns: list[str] = []
    for record in transcript_records[-14:]:
        actor = "业主" if record.get("kind") == "owner" else "莉莉丝"
        text = (record.get("text") or "").strip()
        tools = record.get("tool_calls") or []
        action = f"（做了 {len(tools)} 个操作）" if tools else ""
        if text or action:
            turns.append(f"{actor}：{text[:300]}{action}")
    nodes = [
        f"- {node.title or node.id}"
        for node in (snapshot.workflow.nodes if snapshot else [])
    ]
    parts = [
        f"需求原文：\n{application.get('requirement') or ''}",
        "最近的会话：\n" + ("\n".join(turns) or "（还没有会话）"),
        "当前工作流包含的环节：\n" + ("\n".join(nodes) or "（还没有环节）"),
    ]
    if outputs:
        parts.append("最近一次运行输出（截断）：\n" + json.dumps(outputs, ensure_ascii=False)[:1_500])
    if question:
        parts.append(f"业主的问题：{question}")
    return "\n\n".join(parts)


async def _owner_facing_chat(
    services: Any, application_id: str, system: str, prompt: str, phase: str
) -> str:
    """经验注入 + 语言硬门：违规重写一次，仍违规则机械涂抹。"""

    data_dir = services.settings.data_dir
    block_types = [block.type for block in services.blocks.list()]
    text = await _pm_chat(
        services, application_id, _with_lessons(system, data_dir), prompt, phase
    )
    violations = owner_language_violations(text, block_types)
    if violations:
        correction = (
            prompt
            + "\n\n【重写要求】你上一稿出现了机器词汇："
            + "、".join(violations[:10])
            + "。全部换成业务称呼后重写，其余内容与判断保持不变。"
        )
        text = await _pm_chat(
            services,
            application_id,
            _with_lessons(system, data_dir),
            correction,
            f"{phase}_rewrite",
        )
        violations = owner_language_violations(text, block_types)
        if violations:
            text = redact_machine_tokens(text, violations)
    return text


async def explain_for_owner(services: Any, application_id: str, question: str) -> str:
    application, records, snapshot, outputs = await _owner_materials(services, application_id)
    prompt = _owner_view_prompt(application, records, snapshot, outputs, question)
    return await _owner_facing_chat(services, application_id, EXPLAIN_SYSTEM, prompt, "explain")


async def review_progress(services: Any, application_id: str) -> str:
    application, records, snapshot, outputs = await _owner_materials(services, application_id)
    prompt = _owner_view_prompt(application, records, snapshot, outputs, "")
    return await _owner_facing_chat(services, application_id, REVIEW_SYSTEM, prompt, "review")


# ---------------- 存取与验收单 ----------------


def render_report_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# 验收单：{report['application_name']}",
        "",
        f"- 时间：{report['stamp']}（UTC）",
        f"- 验收对象：发布版 v{report.get('version', '?')}",
        f"- 验收口径：{report['summary']}",
    ]
    if report.get("required_node_types") or report.get("required_any_node_types"):
        arch = (
            "通过"
            if report["architecture_pass"]
            else "不通过，缺 " + "、".join(report["architecture_missing"])
        )
        lines.append(f"- 结构核验：{arch}")
    lines += ["", f"## 用例（{report['passed_cases']}/{len(report['cases'])} 通过）", ""]
    for row in report["cases"]:
        lines.append(f"### {'✅' if row['passed'] else '❌'} {row['name']}（运行：{row['run_status']}）")
        lines.append("")
        lines.append("| 检查项 | 结果 | 实际 |")
        lines.append("| --- | --- | --- |")
        for check in row["checks"]:
            actual = str(check["actual"]).replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {check['check']} | {'通过' if check['passed'] else '不通过'} | {actual} |"
            )
        lines.append("")
    lines.append(f"## 结论：{'✅ 验收通过' if report['accepted'] else '❌ 需要整改'}")
    return "\n".join(lines) + "\n"


def spec_dir(data_dir: Path, application_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", application_id)[:80]
    return Path(data_dir) / "acceptance" / safe


def save_spec(data_dir: Path, application_id: str, spec: AcceptanceSpec) -> None:
    folder = spec_dir(data_dir, application_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "spec.json").write_text(
        json.dumps(spec.model_dump(mode="json"), ensure_ascii=False, indent=2)
    )


def load_spec(data_dir: Path, application_id: str) -> AcceptanceSpec | None:
    path = spec_dir(data_dir, application_id) / "spec.json"
    if not path.is_file():
        return None
    return AcceptanceSpec.model_validate(json.loads(path.read_text()))


def save_report(data_dir: Path, application_id: str, report: dict[str, Any]) -> None:
    folder = spec_dir(data_dir, application_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str)
    )
    (folder / "report.md").write_text(render_report_markdown(report))


def load_report(data_dir: Path, application_id: str) -> dict[str, Any] | None:
    path = spec_dir(data_dir, application_id) / "report.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
