"""服务端工作流管家：CLI/客户端对话背后的智能体循环（工具在服务端执行）。

bench 北极星的招牌特性——终端里与智能体对话来生成/运行/统筹工作流。
本地永远只是薄 REPL：语言理解、工具选择、工具执行、结果核对全部发生在
服务端，动作与结果可审计（events），不依赖客户端诚实。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from .agent_core import collect_model_stream
from .models import ChatMessage, ContentBlock, ToolDefinition
from .workflow_storage import TERMINAL_BUILD_STATUSES

logger = logging.getLogger(__name__)

# 塞进历史给模型解析指代的标记。它**只应**出现在输入里；
# 实测模型会把它抄进回答（方括号版、XML 版都抄过），所以出口再剪一道。
_CONTEXT_MARK = re.compile(r"<上下文[^>]*/>\s*")


def _without_context_marks(text: str) -> str:
    """把内部上下文标记从回答里剪掉。

    提示词里已经写了「绝不能出现在回答里」，但那是约束不是保证——
    真机上它照样出现在回答的第一行。能机械保证的就别只靠嘱咐。
    """
    return _CONTEXT_MARK.sub("", str(text or "")).strip()

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
    "用户说「这个不要了/别修了」用 abandon_build，别一味劝他续跑；"
    "注意 broken（跑起来出错，可以修）与 stale（压根没跑/定时没开火，"
    "该做的是手动跑一次确认再查调度器）是两回事，别给同一条建议；"
    "运行前先用 list_workflows 确认输入声明；生成工作流用 generate_workflow"
    "（提交后告知用户构建已开始，可用 build_status 跟进）；语气简洁友好。"
    "历史里的 <上下文 …/> 标签是给你解析指代用的，绝不能出现在回答里；"
    "工具返回里下划线开头的字段（如 _怎么做）同样是给你看的操作指引，"
    "照做但绝不复述，回答里不出现工具名、状态码、字段名；"
    "只给结论，不要把推理过程写进回答——"
    "「让我看看」「我需要确认」「实际上」这类话是你的思考，用户不该看到；"
    "「有哪些/现在怎么样/还剩几个」这类问清单与现状的问题，每次都要现查工具，"
    "不能拿上一轮的回答当答案——列表随时在变，照着旧的说等于报错数；"
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
    ToolDefinition(name="explain_workflow",
                   description="讲清楚一个工作流是怎么做的：要什么输入、分几步、"
                               "每步干什么、出什么结果、有没有定时。"
                               "用户问'它是怎么做的''这个工作流长什么样'"
                               "'它到底干了啥'用这个。",
                   input_schema={"type": "object", "properties": {
                       "name_or_id": {"type": "string"}}, "required": ["name_or_id"]}),
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
                       "days_idle": {"type": "integer",
                                     "description": "配 suggest 时是「闲置几天算废弃」（默认 3）；"
                                                    "配 list_archived 时是「只看最近几天收起来的」"
                                                    "——用户说「上周收的那些」传 7"},
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
                                                   "第一次验收必须给；之后再验可以不给。"
                                                   "业主要改样例时，把他说的原话照传即可"
                                                   "（只说变化的那一处也行，"
                                                   "系统会带上他上次的原话）"},
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
    ToolDefinition(name="abandon_build",
                   description="放弃一个不要了的构建。用户说'这个不要了''别修了'"
                               "'取消吧''方向错了'用这个。已经搭完的不用取消。",
                   input_schema={"type": "object", "properties": {
                       "build_id": {"type": "string"}}, "required": ["build_id"]}),
    ToolDefinition(name="build_status", description="查询生成任务（构建）的状态",
                   input_schema={"type": "object", "properties": {"build_id": {"type": "string"}},
                                 "required": ["build_id"]}),
]


class WorkflowConcierge:
    def __init__(self, services: Any, settings: Any):
        self.services = services
        self.settings = settings
        # 业主最近一句原话，机械留存：凡是要「照业主说的」核对的地方，
        # 都不能用模型转述的版本，否则等于自己跟自己对
        self._owner_words = ""

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

    async def _declared_inputs(self, app_id: str) -> list[dict[str, Any]]:
        """发布版 start 节点声明的输入。取不到就回空——宁可不拦，不可误拦。"""
        try:
            version = await self.services.workflow_store.get_version(app_id)
            nodes = version["snapshot"].workflow.nodes
        except Exception:  # noqa: BLE001 - 没发布版/结构异常都当作「说不清」
            return []
        fields: list[dict[str, Any]] = []
        for node in nodes:
            if getattr(node, "type", None) != "start":
                continue
            config = getattr(node, "config", None) or {}
            for item in config.get("inputs") or []:
                if isinstance(item, dict) and item.get("name"):
                    fields.append({
                        "名字": str(item["name"]),
                        "类型": str(item.get("type") or "string"),
                        "必填": bool(item.get("required", True)),
                    })
        return fields

    async def _get_build_or_error(self, build_id: str) -> tuple[dict | None, dict | None]:
        """回 (build, error)。构建号是模型填的，填错很常见——不能让它掀翻整轮。"""
        if not build_id:
            return None, {"error": "没给构建号。用 recent_builds 找到那一个再来。"}
        try:
            return await self.services.workflow_store.get_build(build_id), None
        except KeyError:
            return None, {"error": "找不到这个构建号。用 recent_builds 列一下最近的，"
                                   "对上再重试；也可能是它属于别的工作流。"}

    async def _exec(self, name: str, args: dict, user: dict) -> dict:
        services = self.services
        if name == "list_workflows":
            apps = await services.workflow_store.list_applications()
            items = []
            for app in apps:
                if args.get("only_published", True) and not app.get("active_version"):
                    continue
                item = {"name": app.get("name"), "id": app["id"],
                        "published_version": app.get("active_version")}
                if app.get("active_version"):
                    # 工具说明一直写着「输入声明」，却从没真的给过——
                    # 模型于是不知道该问业主要什么，直接空跑，白造一条失败记录
                    declared = await self._declared_inputs(app["id"])
                    if declared:
                        item["要给的输入"] = declared
                items.append(item)
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
            given = dict(args.get("inputs") or {})
            declared = await self._declared_inputs(app["id"])
            missing = [f["名字"] for f in declared
                       if f["必填"] and not str(given.get(f["名字"], "")).strip()]
            if missing:
                # 注定失败的运行不该被创建：那条失败记录会永久留在业主的历史里，
                # 还会喂给体检和「近期失败」面板，看起来像是工作流坏了
                return {"error": f"还缺这些输入：{'、'.join(missing)}",
                        "这个工作流要什么": declared,
                        "接下来": "问业主要这几项，拿到再跑；别自己编"}
            created = await services.workflow_runtime.create_run(
                app["id"], WorkflowRunRequest(inputs=given),
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
                    from .overview import _human_error

                    result = {"run_id": run_id, "情况": _RUN_WORDS.get(
                        current["status"], current["status"]), "outputs": outputs}
                    # 报错也要说人话：_human_error 本来就有，
                    # 之前只给 today 面板和体检用，管家自己跑出来的错反倒是英文原文
                    reason = _human_error(current.get("error") or "")
                    if reason:
                        result["没成的原因"] = reason
                    return result
                await asyncio.sleep(1.5)
            return {"run_id": run_id, "情况": "还在跑",
                    "note": "超过等待时间了，可稍后用 recent_runs 看结果"}
        if name == "explain_workflow":
            app = await self._resolve_app(str(args.get("name_or_id") or ""))
            if not app:
                return {"error": "找不到该工作流"}
            if app.get("active_version") is None:
                return {"error": f"「{app.get('name')}」还没有发布版，"
                                 "现在只有草稿——搭完发布了才好讲它做什么"}
            try:
                version = await services.workflow_store.get_version(app["id"])
                nodes = list(version["snapshot"].workflow.nodes)
            except Exception:  # noqa: BLE001 - 读不到就直说，别编
                return {"error": "读不到它的结构，稍后再试"}

            def title(node_type: str) -> str:
                try:
                    return services.blocks.get(node_type).title
                except Exception:  # noqa: BLE001 - 目录里没有就用原名
                    return node_type

            steps, inputs, outputs, schedule = [], [], [], None
            for node in nodes:
                config = getattr(node, "config", None) or {}
                node_type = getattr(node, "type", "")
                if node_type == "start":
                    inputs = [f"{i.get('name')}（{i.get('type') or 'string'}）"
                              for i in config.get("inputs") or [] if i.get("name")]
                    continue
                if node_type == "schedule_trigger":
                    schedule = (f"每天 {config.get('hour', 0):02d}:"
                                f"{config.get('minute', 0):02d}"
                                f"（{config.get('timezone') or 'UTC'}）")
                    continue
                if node_type in ("end", "answer"):
                    outputs = list((config.get("outputs") or {}).keys())
                    continue
                steps.append(title(node_type))
            return {"工作流": app.get("name"),
                    "要给它什么": inputs or ["（不用给，自己跑）"],
                    "它做几步": steps or ["（只有取值与出结果，没有中间步骤）"],
                    "会得到什么": outputs or ["（没有声明输出）"],
                    "定时": schedule or "没有定时，要手动跑",
                    "note": "用业主的话复述，别照抄节点名；他问细节再展开"}
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
            # 把提交的需求回给模型并要求念出来：需求是模型自己组织的措辞，
            # 它有可能跟业主的本意走样，而一次构建要跑好几分钟。
            # 早一句确认，胜过让业主等到最后才发现建的不是他要的东西。
            return {"build_id": build_id, "app_id": app["id"],
                    "工作流名": app_name, "要做的事": requirement,
                    "note": "已开始搭建（后台进行）。把「要做的事」原样念给业主听，"
                            "让他确认这就是他要的——不对就说一声，现在放弃重来最省事"}
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
                # 「上周收的那些」——按名字过滤答不了这种问法
                days = args.get("days_idle")
                if days:
                    from datetime import datetime, timedelta, timezone

                    edge = (datetime.now(timezone.utc)
                            - timedelta(days=int(days))).isoformat()
                    items = [i for i in items if str(i.get("archived_at") or "") >= edge]
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
                    "published_version": published,
                    # 下划线键 = 给模型的内部细节，约定过不许复述：
                    # 这是原始异常文本，多半是英文，念出来业主也看不懂
                    "_发布失败详情": publish_error,
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
            if examples:
                # 业主多半只说变化的那一处（「净字数应该是 11」），
                # 把上一次的原话接上，他就不必整段重说
                previous = acceptance_pm.load_spec(services.settings.data_dir, app["id"])
                earlier = getattr(previous, "owner_words", "") if previous else ""
                if earlier and earlier.strip() not in examples:
                    examples = (f"业主先前说的：\n{earlier.strip()}\n\n"
                                f"业主现在的更正（**以这句为准**）：\n{examples}")
            if len(examples) < 5:
                spec = acceptance_pm.load_spec(services.settings.data_dir, app["id"])
                if spec is None:
                    return {"error": "第一次验收要给样例：什么输入应该得到什么结果。"
                                     "比如「输入三行文本，应该得到行数 3」"}
            else:
                spec = await acceptance_pm.generate_spec(
                    services, app, examples, owner_words=self._owner_words)
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
            if instruction:
                # 同 resume_build：修什么这件事被转述走样，就会修错东西。
                # 只在模型确实给了指示时才并原话——否则下面那道闸就形同虚设：
                # 空指示会被业主的上一句话填满，径直闯过体检门去花钱。
                instruction = _keep_owner_words(instruction, self._owner_words)
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
                    return {"error": "这个工作流没有可归因的失败原因"
                                     f"（{_HEALTH_WORDS.get(state, _HEALTH_WORDS['unknown'])}）。"
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
            # 跟 build_status 走同一层翻译：状态码不能从这条路漏出去
            # （真机上就是从这里漏的：「4 个需要关注（needs_attention）」）
            rows = []
            for b in builds:
                question = (b["team_state"].pending_question or "")[:120]
                situation, _ = _build_situation(
                    b["status"], question or None, b.get("error") or "")
                row = {"build_id": b["id"], "情况": situation,
                       "要做的事": (b.get("requirement") or "")[:60]}
                if question:
                    row["搭建方在问"] = question
                rows.append(row)
            return {"builds": rows}
        if name == "resume_build":
            build_id = str(args.get("build_id") or "")
            build, failure = await self._get_build_or_error(build_id)
            if failure:
                return failure
            if build["status"] in ("queued", "building"):
                return {"error": "该构建正在进行中，无需续跑"}
            # 搭建方那边会把这条当「业主的答复」并标为最高优先级——
            # 转述走样的话，它就拿着走样的指令干活
            message = _keep_owner_words(str(args.get("message") or ""),
                                        self._owner_words)
            engine = services.builders.for_build(build)
            if message:
                engine.queue_resume_message(build_id, message)
            await services.workflow_store.update_build(build_id, status="queued", error="")
            engine.start(build_id)
            return {"build_id": build_id, "情况": "已经让它接着跑了",
                    "note": "过一会儿再查一次即可"}
        if name == "abandon_build":
            build_id = str(args.get("build_id") or "")
            build, failure = await self._get_build_or_error(build_id)
            if failure:
                return failure
            if build["status"] in TERMINAL_BUILD_STATUSES:
                return {"情况": "这个构建早就结束了，没什么可放弃的",
                        "接下来": "不用做什么"}
            try:
                services.builders.for_build(build).cancel(build_id)
            except KeyError:
                # 没在跑也照样能放弃——见 api.cancel_build 里的同一条理由
                await services.workflow_store.update_build(
                    build_id, status="cancelled", error="")
            return {"情况": "已经放弃这个构建了", "接下来": "不用做什么"}
        if name == "build_status":
            build, failure = await self._get_build_or_error(
                str(args.get("build_id") or ""))
            if failure:
                return failure
            state = build["team_state"]
            situation, what_to_do = _build_situation(
                build["status"], state.pending_question, build.get("error") or "")
            result = {"情况": situation, "接下来": what_to_do,
                      "已发布版本": state.published_version}
            if build["status"] not in ("queued", "building", "published"):
                # 下划线键是给模型的操作指引，约定过不许出现在回答里
                result["_怎么做"] = ("用户点头后调 resume_build；"
                                     "有 搭建方在问 时把答复放进 message")
            # 停下来问业主时必须把问题带出来：只报「需要你处理」的话，
            # 模型说得出「要你处理」却说不出在问什么，用户等构建、构建等用户
            if state.pending_question:
                result["搭建方在问"] = state.pending_question[:600]
            return result
        return {"error": f"没有「{name}」这个工具——换个方式，或如实告诉用户做不到"}

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

        self._owner_words = next(
            (str(m.get("text", ""))[:4_000] for m in reversed(history)
             if m.get("role") != "assistant" and str(m.get("text", "")).strip()), "")
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
                text = _without_context_marks(
                    " ".join(b.text or "" for b in response.blocks if b.type == "text"))
                await _emit({"type": "final", "text": text or "（无回复）"})
                return actions, text or "（无回复）"
            messages.append(ChatMessage(role="assistant", content=response.blocks))
            result_blocks = []
            for call in calls:
                try:
                    result = await self._exec(call.name or "", call.input or {}, user)
                except Exception:  # noqa: BLE001 - 工具报错是数据，不是崩溃
                    # 十几个工具、参数全由模型填，总会有填错的。
                    # 让模型收到一句可读的错误自己纠正，胜过业主收到一个 500。
                    logger.exception("concierge tool failed: %s", call.name)
                    result = {"error": f"「{call.name}」这一步没执行成功。"
                                       "换个方式试试，或把情况如实告诉用户。"}
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


# 运行状态 → 人话。跟构建状态那套并列：模型手里有什么词就说什么词。
_HEALTH_WORDS = {
    "broken": "确实在反复失败",
    "stale": "有定时却一直没跑起来",
    "waiting": "还在跑，结果没出来",
    "ok": "看起来是正常的",
    "unknown": "查不到它的近况",
}

_RUN_WORDS = {
    "succeeded": "跑成了",
    "failed": "没跑成",
    "running": "还在跑",
    "queued": "排着队还没开始",
    "paused": "停下来等人填东西",
    "cancelled": "被取消了",
}

# 内部报错 → 业主听得懂的话。模型只会照抄手里的词，所以别把英文递给它。
_BUILD_ERROR_WORDS = (
    ("stream timed out", "搭建方想得太久，中途断了"),
    # 下面几条是线上真实统计出来的常客，不是想象的错误码
    ("perseverating", "搭建方反复提同一个被否掉的方案，自己绕不出来"),
    ("restarted while building", "平台重启，把搭建打断了"),
    ("repair cycles", "反复返修多次仍没通过验收"),
    ("budget exhausted", "搭建预算用完了还没达标"),
    ("before mandatory tests passed", "必测项还没跑过就停了"),
    ("budget exceeded", "搭建预算用完了还没达标"),
    ("invalid draft", "搭出来的图不成立，搭建方自己停了"),
    ("returned 400", "模型服务拒绝了这次请求"),
    ("returned 500", "模型服务自己出错了"),
    ("task is not running", "后台任务已经不在了，多半是平台重启过"),
    ("unique constraint", "平台内部记事出了岔子"),
    ("timed out", "等太久超时了"),
    ("timeout", "等太久超时了"),
    ("rate limit", "模型服务这会儿太忙"),
    ("429", "模型服务这会儿太忙"),
    ("connection", "连不上模型服务"),
    ("unauthorized", "模型服务的凭据不对"),
    ("401", "模型服务的凭据不对"),
)


def _build_situation(status: str, pending_question: str | None,
                     error: str) -> tuple[str, str]:
    """把构建状态翻成一句人话，外加一句「所以你能做什么」。

    revision（第几版修订）不外传：那是搭建方的内部计数，
    业主看了只会以为自己该关心版本号。
    """
    if pending_question:
        return ("搭建停下来了，在等业主回话",
                "把上面的问题原样转达给业主，等他回答")
    if status in ("queued", "building"):
        return "还在搭，正常进行中", "过一会儿再查一次即可，不用做什么"
    if status == "published":
        return "搭完了，已经发布可以用", "不用做什么"
    if status == "cancelled":
        # 别落到「状态未知 → 让它接着跑」：业主明确不要了的东西，
        # 再劝他续跑是把已经做完的决定又翻出来
        return "这个构建已经放弃了", "不用做什么；真要重来就当成新需求重新提"
    lowered = str(error).lower()
    reason = next((word for key, word in _BUILD_ERROR_WORDS if key in lowered), "")
    if status == "needs_attention":
        return (f"搭建中途卡住了（{reason}）" if reason else "搭建中途卡住了",
                "多半是一时的，让它接着跑就行")
    if status in ("failed", "error"):
        return (f"这次没搭成（{reason}）" if reason else "这次没搭成",
                "可以让它接着跑；连着不成就把需求说得更具体些")
    return "搭建状态未知", "让它接着跑试试"


def _keep_owner_words(rendition: str, owner_words: str) -> str:
    """模型转述的指示 + 业主原话。两者实质相同就只留一份。

    不用原话直接替换：模型有时确实要把好几轮对话拼成一条指示。
    但只留转述是不行的——已经实测过它会改写业主给的具体内容。
    """
    rendition = (rendition or "").strip()
    owner_words = (owner_words or "").strip()
    if not owner_words:
        return rendition
    if not rendition:
        return owner_words
    squeeze = lambda text: "".join(text.split())  # noqa: E731 - 只为比对，不留形态
    if squeeze(owner_words) in squeeze(rendition):
        return rendition
    return f"{rendition}\n\n业主原话：{owner_words}"


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
        # 判不合格时把业主逐字原话一并给出：监理有可能把例子翻错，
        # 那种情况下该改的是卷子，不是工作流（真机上发生过一次，烧了一个构建）。
        # 这句是机械取来的，不是你转述的版本——照它核对才有意义。
        "业主逐字说的话": report.get("owner_examples") or "",
        "note": ("全部通过" if not failed else
                 "「实际」就是这次真跑出来的值，照它说，别自己推算。"
                 "先拿「业主逐字说的话」跟用例对一遍："
                 "考的跟他说的不是一回事，就是卷子错了，请他换个自洽的例子重验；"
                 "对得上才是工作流的问题，那时才提「帮我修」"),
    }


def _summarize(result: dict) -> str:
    if result.get("error"):
        return "✕ " + str(result["error"])[:60]
    if "workflows" in result:
        return f"{result['total']} 个工作流"
    if "outputs" in result:
        pairs = [f"{k}={str(v)[:30]}" for k, v in list(result["outputs"].items())[:3]]
        mark = "✓ " if result.get("情况") == "跑成了" else "⚠ "
        return mark + (" · ".join(pairs) or str(result.get("情况", "")))
    if "build_id" in result:
        return "⚙ 构建已提交"
    if "runs" in result:
        return f"{len(result['runs'])} 条历史"
    if "builds" in result:
        return f"{len(result['builds'])} 个构建"
    if "它做几步" in result:
        return f"{result.get('工作流')}：{len(result['它做几步'])} 步"
    if "情况" in result:
        return str(result["情况"])
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
