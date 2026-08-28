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

def _system_prompt() -> str:
    """带上今天的日期——不然它得靠运行记录猜「昨天」是哪天，实测会猜错。"""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return AGENT_SYSTEM + (
        f"\n当前时间：{now.strftime('%Y-%m-%d %H:%M')} UTC"
        f"（Asia/Shanghai 为 {now.astimezone().strftime('%Y-%m-%d %H:%M')}）。"
        "用户说「今天/昨天」以此为准，别从运行记录里推算。")


AGENT_SYSTEM = (
    "你是工作流平台的管家智能体，通过工具帮用户生成、运行、统筹工作流。"
    "规则：查数据必须用工具，绝不虚构结果或历史；回答给出工具返回的真实数字；"
    "问'有什么坏了/不正常'用 health_report（它已带失败原因，别再逐个查）；"
    "要修坏掉的工作流用 repair_workflow，把体检给出的失败原因原样传进 instruction；"
    "改定时时刻用 set_schedule（别拿 repair_workflow 去改定时）；"
    "列表乱了用 tidy_workflows——它只给建议，收起来前要用户点头；"
    "用户问「靠不靠谱/帮我验一下」用 acceptance_check（第一次要问他要样例）；"
    "注意 broken（跑起来出错，可以修）与 stale（压根没跑/定时没开火，"
    "该做的是手动跑一次确认再查调度器）是两回事，别给同一条建议；"
    "运行前先用 list_workflows 确认输入声明；生成工作流用 generate_workflow"
    "（提交后告知用户构建已开始，可用 build_status 跟进）；语气简洁友好。"
    "历史里的 <上下文 …/> 标签是给你解析指代用的，绝不能出现在回答里；"
    "只给结论，不要把推理过程写进回答——"
    "「让我看看」「我需要确认」「实际上」这类话是你的思考，用户不该看到；"
    "工具没有的能力就直说没有并给出替代路径，不要反复自我怀疑；"
    "工具返回的数字原样引用——不要自己推算，更不要在数字对不上时"
    "猜「可能是工具算错了」，对不上就照实说对不上。"
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
    ToolDefinition(name="generate_workflow", description="用业务需求生成新工作流（远端构建，异步）",
                   input_schema={"type": "object", "properties": {
                       "requirement": {"type": "string"},
                       "name": {"type": "string",
                                "description": "工作流短名（≤20字，名词短语，例：文本行数统计）——不给则从需求首句截取"},
                       "thinking_enabled": {"type": "boolean"},
                   }, "required": ["requirement"]}),
    ToolDefinition(name="platform_overview", description="平台统筹总览：今日运行统计、定时任务、近期失败、进行中的构建",
                   input_schema={"type": "object", "properties": {}}),
    ToolDefinition(name="tidy_workflows",
                   description="收拾工作流列表。四种用法："
                               "suggest 列出可以收起来的废弃草稿（从没发布、"
                               "从没成功跑过、放了一阵子）；list_archived 列出已经收起来的；"
                               "archive 收起一个；restore 拿回一个。"
                               "用户说'列表太乱了''把没用的收起来''收起来的有哪些'"
                               "'拿回 X'都用这个。",
                   input_schema={"type": "object", "properties": {
                       "action": {"type": "string",
                                  "enum": ["suggest", "archive", "restore", "list_archived"],
                                  "description": "suggest 看建议；list_archived 看已收起的；"
                                                 "archive/restore 需要 name_or_id"},
                       "name_or_id": {"type": "string",
                                      "description": "archive/restore 的目标；"
                                                     "配 list_archived 时当关键词过滤用"},
                       "days_idle": {"type": "integer", "description": "闲置几天算废弃，默认 3"},
                   }}),
    ToolDefinition(name="set_schedule",
                   description="改一个已发布工作流的定时时刻（几点几分、哪个时区），"
                               "改完自动重新发布。用户说'改成早上七点跑''以后别跑了'用这个。"
                               "把 hour 设为 -1 表示取消定时。",
                   input_schema={"type": "object", "properties": {
                       "name_or_id": {"type": "string"},
                       "hour": {"type": "integer",
                                "description": "0-23；-1 表示取消定时"},
                       "minute": {"type": "integer", "description": "0-59，默认 0"},
                       "timezone": {"type": "string",
                                    "description": "IANA 时区名，如 Asia/Shanghai；不给则沿用原有"},
                   }, "required": ["name_or_id", "hour"]}),
    ToolDefinition(name="acceptance_check",
                   description="请独立监理验收一个已发布工作流：按业主给的样例出卷、"
                               "逐条试运行、出一份验收单。用户说'帮我验一下''这东西靠谱吗'"
                               "'验收报告呢'用这个。监理与搭建方互不见对方的工作内容。",
                   input_schema={"type": "object", "properties": {
                       "name_or_id": {"type": "string"},
                       "examples": {"type": "string",
                                    "description": "业主给的样例：什么输入应该得到什么结果。"
                                                   "第一次验收必须给；之后再验可以不给"},
                       "action": {"type": "string", "enum": ["check", "report"],
                                  "description": "check 出卷并验收；report 只看上次的验收单"},
                   }, "required": ["name_or_id"]}),
    ToolDefinition(name="repair_workflow",
                   description="修一个已存在但跑不通的工作流：在原应用上开一次修复构建，"
                               "构建智能体从现有草稿改起（不是从零重做）。修复完会重新发布。"
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
    ToolDefinition(name="resume_build", description="续跑一个暂停/失败的构建，可附带指示或对构建方提问的回答",
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

    async def _resolve_app(self, name_or_id: str,
                           *, include_archived: bool = False) -> dict | None:
        apps = await self.services.workflow_store.list_applications()
        if include_archived:
            # 已归档的不在常规列表里，但「拿回 X」必须能按名字找到它
            apps = list(apps) + list(await self.services.workflow_store.list_archived())
        for app in apps:
            if app["id"] == name_or_id or app.get("name") == name_or_id:
                return app
        matches = [a for a in apps if name_or_id in (a.get("name") or "")]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            # 反向包含：用户说的名字更长（带了他记得的修饰），或大小写/空格有出入
            loose = name_or_id.strip().lower().replace(" ", "")
            matches = [a for a in apps
                       if loose and loose in (a.get("name") or "").lower().replace(" ", "")]
            if len(matches) == 1:
                return matches[0]
        return None

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
            hidden = len(apps) - len(items)
            result = {"workflows": items[:50], "total": len(items)}
            if hidden > 0 and args.get("only_published", True):
                # 不说的话模型会以为"总共就这些"，用户问起草稿时它只能说找不到
                result["unpublished_hidden"] = hidden
                result["note"] = (f"另有 {hidden} 个未发布的草稿没列出来；"
                                  "要看它们传 only_published=false，"
                                  "要收拾用 tidy_workflows")
            return result
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
                    "note": "已开始搭建（后台进行），用 build_status 跟进"}
        if name == "tidy_workflows":
            action = str(args.get("action") or "suggest")
            if action == "suggest":
                # 默认 3 天：7 天太长，真机上一堆 6 天前的废弃草稿被判"很干净"，
                # 用户看着满屏杂物、工具说没事，比不提供这功能还糟
                items = await services.workflow_store.list_archivable(
                    days_idle=int(args.get("days_idle") or 3))
                return {"candidates": [
                    {"name": i["name"], "id": i["id"], "runs": i["runs"],
                     "last_touched": str(i["updated_at"])[:10]} for i in items[:20]],
                    "total": len(items),
                    "note": "这些从没发布也从没成功跑过。要收起来说一声，"
                            "数据不删、随时能拿回来"}
            if action == "list_archived":
                items = await services.workflow_store.list_archived()
                # 用户多半是来找某一个的，不是要通读几十条——给个过滤比翻页管用
                keyword = str(args.get("name_or_id") or "").strip().lower()
                if keyword:
                    items = [i for i in items
                             if keyword in str(i.get("name") or "").lower()]
                shown = items[:30]
                payload = {"archived_items": [
                    {"name": i["name"], "id": i["id"],
                     "archived_at": str(i["archived_at"])[:10]} for i in shown],
                    "total": len(items),
                    "note": "说「拿回 X」就能放回列表"}
                if len(items) > len(shown):
                    # 别让模型以为「就这些」：它会据此断定某个没列出来的不存在
                    payload["truncated"] = True
                    payload["note"] += (f"。这里只列了 {len(shown)}/{len(items)} 个，"
                                        "用户报的名字即使不在上面也可能存在——"
                                        "直接按名字 restore 即可，找不到会明确报错")
                return payload
            # 拿回来时要能按名字找到已归档的——它们不在常规列表里
            app = await self._resolve_app(
                str(args.get("name_or_id") or ""), include_archived=True)
            if not app:
                return {"error": "找不到该工作流"}
            archived = action == "archive"
            result = await services.workflow_store.set_archived(app["id"], archived)
            note = ("已从列表收起（数据都在，说「拿回 X」就能恢复）"
                    if archived else "已放回列表")
            if result.get("schedule_effect"):
                note += "；" + result["schedule_effect"]
            return {**result, "note": note}
        if name == "set_schedule":
            app = await self._resolve_app(str(args.get("name_or_id") or ""))
            if not app:
                return {"error": "找不到该工作流"}
            hour = int(args.get("hour", -1))
            minute = int(args.get("minute") or 0)
            if hour != -1 and not (0 <= hour <= 23):
                return {"error": "hour 要在 0-23 之间，或用 -1 取消定时"}
            if not (0 <= minute <= 59):
                return {"error": "minute 要在 0-59 之间"}

            draft = await services.workflow_store.get_draft(app["id"])
            snapshot = draft["snapshot"].model_dump(mode="json")
            nodes = snapshot.get("workflow", {}).get("nodes", [])
            node = next((n for n in nodes if n.get("type") == "schedule_trigger"), None)
            if node is None:
                return {"error": f"「{app.get('name')}」没有定时节点——"
                                 "要加定时得重新生成或让我改造它，说一声我来做"}
            if hour == -1:
                return {"error": "取消定时需要删掉定时节点，这一步会改变工作流结构；"
                                 "说「把 X 的定时去掉」我用修复流程来做，别用这个工具"}

            config = dict(node.get("config") or {})
            before = f"{int(config.get('hour', 0)):02d}:{int(config.get('minute', 0)):02d} " \
                     f"{config.get('timezone') or 'UTC'}"
            config["hour"] = hour
            config["minute"] = minute
            if args.get("timezone"):
                config["timezone"] = str(args["timezone"])
            after = f"{hour:02d}:{minute:02d} {config.get('timezone') or 'UTC'}"

            from uuid import uuid4
            from .workflow_models import DraftOperation

            try:
                await services.applications.apply_operation(app["id"], DraftOperation(
                    expected_revision=int(draft["revision"]),
                    idempotency_key=f"set-schedule-{uuid4().hex[:12]}",
                    op="update_node",
                    data={"node_id": node["id"], "changes": {"config": config},
                          "merge_config": False},
                ))
            except Exception as error:  # noqa: BLE001 - 转成用户能懂的话
                return {"error": f"改定时没成功：{str(error)[:200]}"}

            published = None
            publish_error = ""
            try:
                result = await services.workflow_store.publish(
                    app["id"], acknowledge_warnings=True)
                published = result.get("version")
            except Exception as error:  # noqa: BLE001
                publish_error = str(error)[:200]
            return {"workflow": app.get("name"), "before": before, "after": after,
                    "published_version": published, "publish_error": publish_error,
                    "note": ("已改并重新发布，下次按新时刻开火"
                             if published else
                             "草稿已改，但没能重新发布——定时仍按旧时刻走")}
        if name == "acceptance_check":
            from . import acceptance_pm

            app = await self._resolve_app(str(args.get("name_or_id") or ""))
            if not app:
                return {"error": "找不到该工作流"}
            if app.get("active_version") is None:
                return {"error": f"「{app.get('name')}」还没有发布版——"
                                 "验收的对象是交付物，先把它搭完发布再验"}

            action = str(args.get("action") or "check")
            if action == "report":
                report = acceptance_pm.load_report(services.settings.data_dir, app["id"])
                if not report:
                    return {"error": "还没验收过——说「帮我验一下」并给几个样例即可"}
                return _acceptance_summary(app, report)

            examples = str(args.get("examples") or "").strip()
            if len(examples) < 5:
                spec = acceptance_pm.load_spec(services.settings.data_dir, app["id"])
                if spec is None:
                    return {"error": "第一次验收要给样例：什么输入应该得到什么结果。"
                                     "比如「输入三行文本，应该得到行数 3」"}
            else:
                spec = await acceptance_pm.generate_spec(services, app, examples)
                acceptance_pm.save_spec(services.settings.data_dir, app["id"], spec)
            report = await acceptance_pm.run_acceptance(services, app["id"])
            return _acceptance_summary(app, report)
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
            result = {"status": build["status"], "revision": state.revision,
                      "published_version": state.published_version,
                      "error": (build.get("error") or "")[:200]}
            # 停下来问业主时必须把问题带出来：只报 needs_attention 的话，
            # 模型说得出「需要你注意」却说不出在问什么，用户等构建、构建等用户
            if state.pending_question:
                result["pending_question"] = state.pending_question[:600]
                result["note"] = ("搭建停下来等你回话了——把问题原样转达给用户，"
                                  "拿到答复后用 resume_build 带上 message 续跑")
            elif build["status"] in ("queued", "building"):
                result["note"] = "还在搭；revision 会往上走，说明在推进"
            return result
        return {"error": f"unknown tool: {name}"}

    @staticmethod
    def _history_text(message: dict) -> str:
        """助手轮次要带上「做了什么、对谁做的」。

        只留回答文本的话，下一轮的「它」「那个」就无从解析——
        实测验收完问「看一下它的验收报告」，管家只能反问「你说的它是哪个」。
        """
        text = str(message.get("text", ""))[:8000]
        if message.get("role") != "assistant":
            return text
        marks = []
        for action in message.get("actions") or []:
            tool = action.get("tool")
            if not tool:
                continue
            target = (action.get("workflow") or action.get("name")
                      or action.get("app_id") or action.get("build_id") or "")
            marks.append(f"{tool}({str(target)[:40]})" if target else str(tool))
        if marks:
            # 用 XML 式标签而不是方括号：方括号看起来像正文的一部分，
            # 实测模型会把它原样抄进回答
            text = f"<上下文 上一轮做了=\"{'、'.join(marks[:4])}\" />\n{text}"
        return text

    async def reply(self, history: list[dict], user: dict, emit=None) -> tuple[list[dict], str]:
        async def _emit(event: dict) -> None:
            if emit is not None:
                await emit(event)

        messages = [ChatMessage(role="assistant" if m.get("role") == "assistant" else "user",
                                content=[ContentBlock(type="text",
                                                      text=self._history_text(m))])
                    for m in history[-12:]]
        actions: list[dict] = []
        for _ in range(6):
            stream = self.services.provider.stream(
                model=self.settings.deepseek_runtime_model,
                system=_system_prompt(), messages=messages, tools=TOOLS,
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


def _acceptance_summary(app: dict, report: dict) -> dict:
    """只给结论与不合格项——整份验收单太长，塞进对话没人看。"""
    cases = report.get("cases") or []
    failed = [c for c in cases if not c.get("passed")]
    return {
        "workflow": app.get("name"),
        "passed_cases": report.get("passed_cases", 0),
        "total_cases": report.get("total_cases", len(cases)),
        "verdict": "通过" if report.get("accepted") else "有不合格项",
        # 必须带实际值：只给检查项名字的话，模型看不到「实际是多少」，
        # 实测它会脑补一个数字并反过来说「疑似验收方比对出了问题」
        "failed_cases": [
            {"name": c.get("name"),
             "run_status": c.get("run_status"),
             "why": [{"检查": x.get("check"), "实际": x.get("actual")}
                     for x in (c.get("checks") or []) if not x.get("passed")][:4]}
            for c in failed[:5]],
        "note": ("全部通过" if not failed else
                 "「实际」就是这次真跑出来的值，照它说，别自己推算；"
                 "不合格的说「帮我修」就能进返修"),
    }


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
    if "verdict" in result and "passed_cases" in result:
        mark = "✓" if result["verdict"] == "通过" else "⚠"
        return f"{mark} 验收 {result['passed_cases']}/{result['total_cases']} 条通过"
    if "archived_items" in result:
        total = result.get("total", 0)
        return f"📦 已收起 {total} 个" if total else "✓ 没有收起来的东西"
    if "candidates" in result:
        total = result.get("total", 0)
        if total:
            return f"🧹 {total} 个可以收起来"
        return "✓ 按「从没发布且从没成功跑过」这个标准，没有可收的"
    if "archived" in result:
        mark = "📦 已收起 " if result["archived"] else "↩ 已放回 "
        tail = "（定时也停了）" if result.get("was_scheduled") and result["archived"] else ""
        return mark + str(result.get("name", "")) + tail
    if "before" in result and "after" in result:
        arrow = f"{result['before']} → {result['after']}"
        return (f"⏰ {arrow}" if result.get("published_version")
                else f"⚠ {arrow}（未发布）")
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
