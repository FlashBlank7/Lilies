"""服务端工作流管家：CLI/客户端对话背后的智能体循环（工具在服务端执行）。

bench 北极星的招牌特性——终端里与智能体对话来生成/运行/统筹工作流。
本地永远只是薄 REPL：语言理解、工具选择、工具执行、结果核对全部发生在
服务端，动作与结果可审计（events），不依赖客户端诚实。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from .agent_core import collect_model_stream
from .models import ChatMessage, ContentBlock, ToolDefinition

AGENT_SYSTEM = (
    "你是工作流平台的管家智能体，通过工具帮用户生成、运行、统筹工作流。"
    "规则：查数据必须用工具，绝不虚构结果或历史；回答给出工具返回的真实数字；"
    "问'有什么坏了/不正常'用 health_report（它已带失败原因，别再逐个查）；"
    "要修坏掉的工作流用 repair_workflow，把体检给出的失败原因原样传进 instruction；"
    "注意 broken（跑起来出错，可以修）与 stale（压根没跑/定时没开火，"
    "该做的是手动跑一次确认再查调度器）是两回事，别给同一条建议；"
    "运行前先用 list_workflows 确认输入声明；生成工作流用 generate_workflow"
    "（提交后告知用户构建已开始，可用 build_status 跟进）；语气简洁友好。"
)

TOOLS = [
    ToolDefinition(name="list_workflows", description="列出工作流（名称、是否发布、版本、输入声明）",
                   input_schema={"type": "object", "properties": {"only_published": {"type": "boolean"}}}),
    ToolDefinition(name="run_workflow", description="运行已发布的工作流并等待结果。inputs 必须符合其输入声明。",
                   input_schema={"type": "object", "properties": {
                       "name_or_id": {"type": "string"}, "inputs": {"type": "object"}},
                       "required": ["name_or_id"]}),
    ToolDefinition(name="recent_runs", description="查询某工作流最近运行历史",
                   input_schema={"type": "object", "properties": {
                       "name_or_id": {"type": "string"}, "limit": {"type": "integer"}},
                       "required": ["name_or_id"]}),
    ToolDefinition(name="generate_workflow", description="用业务需求生成新工作流（远端莉莉丝构建，异步）",
                   input_schema={"type": "object", "properties": {
                       "requirement": {"type": "string"},
                       "name": {"type": "string",
                                "description": "工作流短名（≤20字，名词短语，例：文本行数统计）——不给则从需求首句截取"},
                       "thinking_enabled": {"type": "boolean"},
                   }, "required": ["requirement"]}),
    ToolDefinition(name="platform_overview", description="平台统筹总览：今日运行统计、定时任务、近期失败、进行中的构建",
                   input_schema={"type": "object", "properties": {}}),
    ToolDefinition(name="repair_workflow",
                   description="修一个已存在但跑不通的工作流：在原应用上开一次修复构建，"
                               "莉莉丝从现有草稿改起（不是从零重做）。修复完会重新发布。"
                               "用户说'修一下 X''X 坏了帮我修'用这个",
                   input_schema={"type": "object", "properties": {
                       "name_or_id": {"type": "string"},
                       "instruction": {"type": "string",
                                       "description": "要修什么——把失败原因原样写进来最有效"},
                   }, "required": ["name_or_id"]}),
    ToolDefinition(name="health_report",
                   description="工作流体检：哪些已发布工作流坏了（窗口内全败/最近连败）或停摆了"
                               "（有定时却没运行），带最近一次失败原因——回答"
                               "'有什么坏了吗''最近哪些不正常'用这个",
                   input_schema={"type": "object", "properties": {
                       "days": {"type": "integer", "description": "回看天数，默认 7"}}}),
    ToolDefinition(name="recent_builds", description="最近的生成任务（构建）列表：状态、需求摘要——找'刚才那个构建'用",
                   input_schema={"type": "object", "properties": {"limit": {"type": "integer"}}}),
    ToolDefinition(name="resume_build", description="续跑一个暂停/失败的构建，可附带给莉莉丝的指示或对她提问的回答",
                   input_schema={"type": "object", "properties": {
                       "build_id": {"type": "string"}, "message": {"type": "string"}},
                       "required": ["build_id"]}),
    ToolDefinition(name="build_status", description="查询生成任务（构建）的状态",
                   input_schema={"type": "object", "properties": {"build_id": {"type": "string"}},
                                 "required": ["build_id"]}),
]


class WorkflowConcierge:
    def __init__(self, services: Any, settings: Any):
        self.services = services
        self.settings = settings

    async def _resolve_app(self, name_or_id: str) -> dict | None:
        apps = await self.services.workflow_store.list_applications()
        for app in apps:
            if app["id"] == name_or_id or app.get("name") == name_or_id:
                return app
        matches = [a for a in apps if name_or_id in (a.get("name") or "")]
        return matches[0] if len(matches) == 1 else None

    async def _exec(self, name: str, args: dict, user: dict) -> dict:
        services = self.services
        if name == "list_workflows":
            apps = await services.workflow_store.list_applications()
            items = []
            for app in apps:
                if args.get("only_published", True) and not app.get("active_version"):
                    continue
                items.append({"name": app.get("name"), "id": app["id"],
                              "published_version": app.get("active_version")})
            return {"workflows": items[:50], "total": len(items)}
        if name == "run_workflow":
            app = await self._resolve_app(str(args.get("name_or_id") or ""))
            if not app:
                return {"error": f"找不到唯一匹配的工作流: {args.get('name_or_id')}"}
            from .workflow_models import WorkflowRunRequest
            created = await services.workflow_runtime.create_run(
                app["id"], WorkflowRunRequest(inputs=dict(args.get("inputs") or {})),
                origin="assistant-agent")
            run_id = created["run_id"]
            for _ in range(40):
                current = await services.workflow_store.get_run(run_id)
                if current["status"] in ("succeeded", "failed", "paused", "cancelled"):
                    # workflow_store.get_run 返回的 state 是 WorkflowRunState 模型不是 dict，
                    # 对它 .get() 会抛 AttributeError（真机 500）；outputs/error 的权威来源
                    # 都在顶层，直接取。
                    outputs = current.get("outputs")
                    if not isinstance(outputs, dict):
                        outputs = {}
                    return {"run_id": run_id, "status": current["status"],
                            "outputs": outputs, "error": current.get("error")}
                await asyncio.sleep(1.5)
            return {"run_id": run_id, "status": "running", "note": "仍在运行，可稍后用 recent_runs 查看"}
        if name == "recent_runs":
            app = await self._resolve_app(str(args.get("name_or_id") or ""))
            if not app:
                return {"error": "找不到该工作流"}
            runs = await services.workflow_store.list_runs(app["id"], limit=int(args.get("limit") or 5))
            from .overview import _brief_error

            return {"runs": [{"id": r["id"], "status": r["status"],
                              "created_at": r.get("created_at"),
                              "error": _brief_error(r.get("error") or "")}
                             for r in runs]}
        if name == "generate_workflow":
            requirement = str(args.get("requirement") or "").strip()
            if len(requirement) < 10:
                return {"error": "需求太短（至少10字）"}
            from uuid import uuid4
            from .workflow_models import ApplicationCreateRequest
            app_name = str(args.get("name") or "").strip()[:24] or _derive_app_name(requirement)
            app = await services.workflow_store.create_application(
                ApplicationCreateRequest(name=app_name, requirement=requirement))
            build_id = str(uuid4())
            await services.workflow_store.create_build(
                build_id, app["id"], requirement, True, 36, 3, 1800.0, "auto",
                thinking_enabled=bool(args.get("thinking_enabled", False)), effort="low")
            services.builders.get("classic").start(build_id)
            return {"build_id": build_id, "app_id": app["id"],
                    "note": "莉莉丝已开工（后台构建），用 build_status 跟进"}
        if name == "repair_workflow":
            app = await self._resolve_app(str(args.get("name_or_id") or ""))
            if not app:
                return {"error": "找不到该工作流"}
            from uuid import uuid4

            from .overview import build_health

            instruction = str(args.get("instruction") or "").strip()
            if not instruction:
                # 没给指示就去体检取原因——但只认"确实坏了且说得出原因"的情况。
                # 这是全系统唯一会自动花钱的路径，不能凭"看起来不正常"就开构建。
                report = await build_health(services, days=7)
                hit = next((item for item in report["items"]
                            if item["application_id"] == app["id"]), None)
                if hit and hit["state"] == "broken" and hit.get("last_error"):
                    instruction = hit["reason"]
                else:
                    state = (hit or {}).get("state", "unknown")
                    return {"error": f"这个工作流没有可归因的失败原因（当前状态：{state}）。"
                                     "要修什么请说清楚，比如把报错原文贴给我。"}
            requirement = (
                f"修复现有工作流「{app.get('name')}」。它已发布但运行失败。\n"
                f"失败情况：{instruction or '运行报错，原因见最近运行记录'}\n"
                "请在现有草稿基础上定位并修正问题（不要推倒重来），"
                "补齐能覆盖该故障的验收用例，通过后重新发布。"
            )
            build_id = str(uuid4())
            await services.workflow_store.create_build(
                build_id, app["id"], requirement, True, 36, 3, 1800.0, "auto",
                thinking_enabled=False, effort="low")
            services.builders.get("classic").start(build_id)
            return {"build_id": build_id, "app_id": app["id"],
                    "workflow": app.get("name"), "repairing": True,
                    "instruction": instruction,
                    "note": "修复构建已开工，用 build_status 跟进"}
        if name == "health_report":
            from .overview import build_health

            days = int(args.get("days") or 7)
            report = await build_health(services, days=max(1, min(days, 90)))
            bad = [i for i in report["items"] if i["state"] != "ok"]
            return {
                "counts": report["counts"],
                "problems": [{"workflow": i["workflow"], "state": i["state"],
                              "reason": i["reason"], "application_id": i["application_id"],
                              "runs": i["runs"], "succeeded": i["succeeded"]}
                             for i in bad[:10]],
                "note": "problems 为空表示所有已发布工作流都正常",
            }
        if name == "platform_overview":
            from .overview import build_overview
            return await build_overview(self.services)
        if name == "recent_builds":
            builds = await services.workflow_store.list_recent_builds(limit=int(args.get("limit") or 5))
            return {"builds": [{
                "build_id": b["id"], "status": b["status"],
                "requirement": (b.get("requirement") or "")[:60],
                "pending_question": (b["team_state"].pending_question or "")[:120] or None,
            } for b in builds]}
        if name == "resume_build":
            build_id = str(args.get("build_id") or "")
            build = await services.workflow_store.get_build(build_id)
            if build["status"] in ("queued", "building"):
                return {"error": "该构建正在进行中，无需续跑"}
            message = str(args.get("message") or "").strip()
            engine = services.builders.for_build(build)
            if message:
                engine.queue_resume_message(build_id, message)
            await services.workflow_store.update_build(build_id, status="queued", error="")
            engine.start(build_id)
            return {"build_id": build_id, "status": "queued", "note": "已续跑，可用 build_status 跟进"}
        if name == "build_status":
            build = await services.workflow_store.get_build(str(args.get("build_id") or ""))
            state = build["team_state"]
            return {"status": build["status"], "revision": state.revision,
                    "published_version": state.published_version,
                    "error": (build.get("error") or "")[:200]}
        return {"error": f"unknown tool: {name}"}

    async def reply(self, history: list[dict], user: dict, emit=None) -> tuple[list[dict], str]:
        async def _emit(event: dict) -> None:
            if emit is not None:
                await emit(event)

        messages = [ChatMessage(role="assistant" if m.get("role") == "assistant" else "user",
                                content=[ContentBlock(type="text", text=str(m.get("text", ""))[:8000])])
                    for m in history[-12:]]
        actions: list[dict] = []
        for _ in range(6):
            stream = self.services.provider.stream(
                model=self.settings.deepseek_runtime_model,
                system=AGENT_SYSTEM, messages=messages, tools=TOOLS,
                max_output_tokens=2048, thinking_enabled=False, effort="low",
                tool_choice={"type": "auto"})
            async def forward(kind: str, data: dict) -> None:
                if kind.endswith(".text.delta"):
                    await _emit({"type": "delta", "text": data.get("text", "")})

            response = await collect_model_stream(
                stream, model=self.settings.deepseek_runtime_model,
                emit=forward if emit is not None else None)
            calls = [b for b in response.blocks if b.type == "tool_use"]
            if not calls:
                text = " ".join(b.text or "" for b in response.blocks if b.type == "text").strip()
                await _emit({"type": "final", "text": text or "（无回复）"})
                return actions, text or "（无回复）"
            messages.append(ChatMessage(role="assistant", content=response.blocks))
            result_blocks = []
            for call in calls:
                result = await self._exec(call.name or "", call.input or {}, user)
                await self.services.storage.append_event(
                    "assistant-agent", "agent.tool", {
                        "user": user.get("name"), "tool": call.name,
                        "ok": "error" not in result})
                entry = {"tool": call.name, "summary": _summarize(result)}
                for key in ("build_id", "app_id", "run_id"):
                    if isinstance(result, dict) and result.get(key):
                        entry[key] = result[key]
                actions.append(entry)
                await _emit({"type": "action", **entry})
                result_blocks.append(ContentBlock(
                    type="tool_result", tool_use_id=call.id,
                    content=json.dumps(result, ensure_ascii=False)[:4000]))
            messages.append(ChatMessage(role="user", content=result_blocks))
        await _emit({"type": "final", "text": "（动作轮次到达上限，请把要求说得更具体些）"})
        return actions, "（动作轮次到达上限，请把要求说得更具体些）"


def _summarize(result: dict) -> str:
    if result.get("error"):
        return "✕ " + str(result["error"])[:60]
    if "workflows" in result:
        return f"{result['total']} 个工作流"
    if "outputs" in result:
        pairs = [f"{k}={str(v)[:30]}" for k, v in list(result["outputs"].items())[:3]]
        return ("✓ " if result.get("status") == "succeeded" else "⚠ ") + " · ".join(pairs)
    if "build_id" in result:
        return "⚙ 构建已提交"
    if "runs" in result:
        return f"{len(result['runs'])} 条历史"
    if "builds" in result:
        return f"{len(result['builds'])} 个构建"
    if "problems" in result:
        problems = result["problems"]
        if not problems:
            return f"✓ {result.get('counts', {}).get('ok', 0)} 个工作流都正常"
        return f"⚠ {len(problems)} 个要处理：" + "、".join(
            p["workflow"] for p in problems[:3])
    if "runs_today" in result:
        rt = result["runs_today"]
        return f"今日 {rt['total']} 次运行（✓{rt['succeeded']} ✕{rt['failed']}）· {len(result.get('schedules', []))} 个定时"
    if "status" in result:
        return f"状态 {result['status']}"
    return "完成"


def _derive_app_name(requirement: str) -> str:
    """需求首句 → 可读短名：剥常见请求前缀、砍到第一个句读、去两端标点。
    真机 E2E 曾产出「输入一段文本 text，输出 line_coun」硬截名，且两次生成同名难分辨。"""
    text = requirement.strip()
    for prefix in ("再做一个工作流：", "给我做一个工作流：", "做一个工作流：",
                   "帮我做一个工作流：", "给我做一个", "帮我做一个", "再做一个",
                   "做一个", "我要一个", "我需要一个"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    for stop in ("。", "；", ";", "\n"):
        index = text.find(stop)
        if index > 0:
            text = text[:index]
            break
    text = text.strip(" ：:，,、.！!？?")
    return text[:24] or requirement.strip()[:24] or "新工作流"
