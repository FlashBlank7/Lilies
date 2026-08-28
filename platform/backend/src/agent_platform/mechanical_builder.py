"""小模型协作 builder（设计文档 §3 形态 B，引擎名 mechanical）。

协调者不是模型，是确定性状态机：阶段推进（建图 → 验收编写 → 跑测 → 修复）
全部机械可判——`draft_validate` 通过才进验收编写，`test_run` 全绿才算交付，
修复循环耗尽即失败。模型对阶段转移零控制权。

小模型是无状态的提案函数：每次调用只收到窄角色提示 + 草稿投影 + 上一步反馈
（黑板模式，不携带滚动会话历史），期望输出恰好一个工具调用提案。提案经由
`WorkflowBuilder._execute` 的同一套硬门执法（Pydantic / revision 乐观锁 /
修复预算），被拒绝就带结构化错误重试——高错误率被边界转化为廉价重试信号。

v0 与设计文档的收敛（单人最小可用）：
- scaffold 与 wire 合并为一个建图阶段，机械关卡不变（仍是 draft_validate）；
- 不开放 ask_owner / spawn_teammate / 沙盒工具——实验任务的需求必须自足；
- planning_mode=required 不支持（状态机本身就是计划）。

血缘与回流：每次提案照常走 `_record_turn` 转录与 harness 计量，actor 名即
阶段角色（graph-builder / test-author / repairer），field_report 无需改动即可
统计各角色的边界拒绝率。
"""

from __future__ import annotations

import difflib
import inspect
import json
import os
from typing import Any

from .agent_core import INVALID_TOOL_INPUT_JSON_KEY
from .build_transcript import tool_call_record
from .builder import WorkflowBuilder
from .formula import function_names as _formula_function_names
from .models import ChatMessage, ContentBlock, ToolDefinition
from .workflow_models import BuildTeamState





def _accepts_temperature(stream_fn: Any) -> bool:
    """后端能否接收 temperature：具名参数或 **kwargs 都算。

    只看具名参数会漏掉 `async def stream(self, **kwargs)` 这种转发式实现
    （测试替身与 OpenAI/Anthropic 包装器都是这一类）。
    """

    try:
        params = inspect.signature(stream_fn).parameters
    except (TypeError, ValueError):
        return False
    if "temperature" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _accepts_temperature(stream_fn: Any) -> bool:
    """后端能否接收 temperature：具名参数或 **kwargs 都算。

    只看具名参数会漏掉 `async def stream(self, **kwargs)` 这种转发式实现
    （测试替身与 OpenAI/Anthropic 包装器都是这一类）。
    """

    try:
        params = inspect.signature(stream_fn).parameters
    except (TypeError, ValueError):
        return False
    if "temperature" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _accepts_temperature(stream_fn: Any) -> bool:
    """后端能否接收 temperature：具名参数或 **kwargs 都算。

    只看具名参数会漏掉 `async def stream(self, **kwargs)` 这种转发式实现
    （测试替身与 OpenAI/Anthropic 包装器都是这一类）。
    """

    try:
        params = inspect.signature(stream_fn).parameters
    except (TypeError, ValueError):
        return False
    if "temperature" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _accepts_temperature(stream_fn: Any) -> bool:
    """后端能否接收 temperature：具名参数或 **kwargs 都算。

    只看具名参数会漏掉 `async def stream(self, **kwargs)` 这种转发式实现
    （测试替身与 OpenAI/Anthropic 包装器都是这一类）。
    """

    try:
        params = inspect.signature(stream_fn).parameters
    except (TypeError, ValueError):
        return False
    if "temperature" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


ENGINE_NAME = "mechanical"

PHASE_DONE_TOOL = ToolDefinition(
    name="phase_done",
    description="当且仅当本阶段目标已经完成时调用，用一句话汇报结论。",
    input_schema={
        "type": "object",
        "properties": {"summary": {"type": "string", "description": "一句话结论"}},
    },
)

# 各阶段开放的工具面（窄工具面 = 小模型的注意力保护）。
SCAFFOLD_TOOLS = (
    "catalog_get", "manual_get", "draft_add_node", "draft_update_node",
    "draft_remove_node", "draft_connect", "draft_remove_edge",
)
TEST_TOOLS = ("catalog_get", "test_add", "test_remove")
REPAIR_TOOLS = (
    "catalog_get", "manual_get", "draft_add_node",
    "draft_update_node", "draft_connect", "draft_remove_edge", "test_add",
)

# 提案竞争（框架重设计 L1）：同一步预算内按温度阶梯重提，验证器裁决，
# 第一个过关的落库。依据：有确定性验证器的域里 best-of-N 收益对数线性全额
# 兑现（LLM Monkeys，最陡段 N=4→16）；多温度较单温度再 +7.3（arXiv:2510.02611）。
# 我们的边界就是那个完美验证器，此前却一直在做单点采样。
PROPOSAL_TEMPERATURES: tuple[float, ...] = (0.2, 0.7, 1.0)

# 每阶段的提案预算（同时受全局 max_turns 钳制）。
SCAFFOLD_BUDGET = 18
TEST_BUDGET = 8
REPAIR_BUDGET_PER_CYCLE = 8

# 缺测试属于 test 阶段的职责，建图关卡放行这一条校验错误。
_MISSING_TEST_ERROR = "at least one mandatory acceptance test is required"

_SHARED_RULES = (
    "规则：每次回复必须恰好调用一个工具，不要输出解释文字。"
    "不确定某积木的配置字段时，先调用 catalog_get 或 manual_get 查它的确切 schema，"
    "绝不凭记忆编造字段名。上一步的错误反馈里写明了被拒绝的原因，照它改正。"
)

SCAFFOLD_SYSTEM = (
    "你是工作流建图手，负责把业务需求翻译成节点和连线。" + _SHARED_RULES +
    "只能使用积木目录里列出的类型，目录之外的类型不存在。"
    "节点要少而准：一个 start（带中文 label 和 example 的输入声明）、"
    "必要的处理节点、一个 end（暴露需求要求的输出字段）。"
    "确定性计算（求和/分组/对账）用确定性积木，不要塞进 LLM 节点。"
    "draft_add_node 的正确形状示例（node 是嵌套对象，绝不能拍平）："
    '{"node": {"id": "start", "type": "start", "title": "输入", '
    '"config": {"inputs": [{"name": "name", "label": "姓名", "type": "string", '
    '"example": "Ada"}]}}}。'
    "引用上游输出用 {\"$ref\": {\"node_id\": \"<id>\", \"path\": [\"字段\"]}}。"
    "所有节点配置完成且连线完整后，调用 phase_done。"
)

TEST_SYSTEM = (
    "你是验收测试作者。" + _SHARED_RULES +
    "用 test_add 添加 mandatory=true 的验收测试：输入用需求里的样例数据，"
    "断言锚定具体数字（equals），不要只验字段形状。"
    "期望值必须由你从样例数据一步步算出来。添加完成后调用 phase_done。"
)

REPAIR_SYSTEM = (
    "你是修理手，负责让失败的验收测试变绿。" + _SHARED_RULES +
    "失败报告与执行台账已经由平台读好放在任务里——不要再去查，直接动手修实现"
    "（改节点配置、补缺失节点、改连线）。除非断言本身算错了，否则不要改测试。"
    "修完调用 phase_done，平台会重新跑测试。"
)


# 强制调用时的瘦身 schema：完整 WorkflowTestCase.model_json_schema() 体量巨大，
# 命名 tool_choice 下的约束解码在这种复杂 schema 上会崩（实测 4B 连吐三次空参
# {}；JSONSchemaBench 亦报告复杂 schema 覆盖率从 86% 崩到 3%）。强制那一步换成
# 只含必要字段的精简定义——落库仍过完整 Pydantic 校验，硬门一分不减。
SLIM_TEST_ADD = ToolDefinition(
    name="test_add",
    description="添加一条 mandatory 验收测试（断言锚定具体数值）。",
    input_schema={
        "type": "object",
        "properties": {
            "test": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "小写下划线 id"},
                    "name": {"type": "string"},
                    "requirement": {"type": "string", "description": "这条测试验证的需求"},
                    "inputs": {"type": "object", "description": "工作流输入，如 {\"name\": \"Ada\"}"},
                    "assertions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "array", "items": {"type": "string"},
                                         "description": "输出字段路径，如 [\"greeting\"]"},
                                "operator": {"type": "string", "enum": ["equals", "contains", "not_contains"]},
                                "expected": {"description": "期望值（精确数值/字符串）"},
                            },
                            "required": ["path", "operator", "expected"],
                        },
                    },
                    "mandatory": {"type": "boolean"},
                },
                "required": ["id", "name", "requirement", "inputs", "assertions", "mandatory"],
            }
        },
        "required": ["test"],
    },
)

_FORMULA_FUNCTIONS = _formula_function_names()

# 真正会做确定性算术的积木（白名单）。黑名单挡不住：真机连着两轮被绕开——
# 先是删掉 config_sketch 里的公式、保留 variable_aggregator（310141fd），
# 再是改用真实存在的 tool 类型指向一个不存在的函数 sum_by_store（92af320c）。
# 白名单把"算术必须落在哪"变成封闭集合，绕不过去：不在集合里就得说明理由。
_COMPUTING_TYPES = {
    "variable_assigner",        # $formula：业务算术与记录聚合
    "record_match",             # 确定性对账
    "record_collection_normalize",
    "record_deduplicate",
    "collection_digest",
    "replenishment_planner",    # 补货约束计算
    "deployed_forecast",        # 预测
    "deployed_model_inference",
    "iteration",                # 逐条处理（内部再落到上面某个积木）
}

# 算术意图词：只收**动作**词，不收"总额/合计"这类名词——"生成含合计的日报文本"
# 是模板节点的正当职责，不该被拦。
_ARITHMETIC_INTENT = (
    "求和", "分组求和", "汇总", "累加", "相加", "算出", "计算", "统计",
    "sum(", "aggregate", "compute",
)

ARCHITECTURE_PLAN_TOOL = ToolDefinition(
    name="architecture_plan",
    description="给出这个工作流要用哪些积木、各自职责、数据怎么流。只调用一次。",
    input_schema={
        "type": "object",
        "properties": {
            "nodes": {
                "type": "array",
                "description": "按数据流顺序列出要建的节点",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "小写下划线 id"},
                        "type": {"type": "string", "description": "必须是积木目录里存在的类型"},
                        "purpose": {"type": "string", "description": "这个节点负责什么（一句话）"},
                        "config_sketch": {
                            "type": "string",
                            "description": (
                                "关键配置写死在这里，执行者照抄：公式积木要给出完整"
                                '中缀表达式（如 sum_by(sales, "store", "amount")）以及'
                                "每个变量绑哪个上游节点的哪个字段。不涉及计算的节点可留空。"
                            ),
                        },
                    },
                    "required": ["id", "type", "purpose"],
                },
            },
            "notes": {"type": "string", "description": "接线与配置要点"},
        },
        "required": ["nodes"],
    },
)

PLAN_SYSTEM = (
    "你是工作流架构师。读需求和积木目录，选出实现它需要的积木并说明各自职责。"
    "选型是这一步唯一重要的事：确定性计算（分组求和、对账、算术）必须落在"
    "能真正计算的积木上——只做变量合并/透传的积木不能用来求和。"
    "不确定某积木能不能算数时，先 catalog_get/manual_get 查清楚。"
    "\n\n落实这套方案的是一个更小的模型，它只会照抄不会推导："
    "凡涉及计算的节点，必须在 config_sketch 里写出**完整的表达式**和"
    "**每个变量绑哪个上游节点的哪个字段**，不要只写'对销售额分组求和'这种描述。"
    "\n想清楚后调用 architecture_plan 一次性给出完整方案。"
)

def referenceable_paths(node: dict[str, Any]) -> list[str]:
    """这个节点可以被 $ref 引用的路径（按真实输入语法给出）。

    不同积木的产出形状不一样：start 直接给 {输入名: 值}，variable_assigner 包在
    output 下，template_transform 给 {"text": ...}。这份"谁能被怎么引"的知识
    此前完全不在投影里，模型只能靠记——真机里反复写错：把 start 的字段写成
    ["output","sales"]（多一层）、把 variable_assigner 的产出写成 ["by_store"]
    （少一层）。它是机械算得出来的，不给才是平台的问题。
    """
    node_type = str(node.get("type") or "")
    config = node.get("config") or {}
    if node_type in {"start", "schedule_trigger"}:
        return [
            f'["{field.get("name")}"]'
            for field in (config.get("inputs") or [])
            if isinstance(field, dict) and field.get("name")
        ]
    if node_type == "variable_assigner":
        return [f'["output", "{key}"]' for key in (config.get("assignments") or {})]
    if node_type == "variable_aggregator":
        return ['["output"]']
    if node_type == "template_transform":
        return ['["text"]']
    if node_type in {"end", "answer"}:
        return []
    return []


class MechanicalBuilder(WorkflowBuilder):
    """状态机协调 + 小模型提案的 builder 引擎。

    继承 WorkflowBuilder 的全部生命周期（start/cancel/_run 的时限、转录、
    收尾发布）与 `_execute` 执法逻辑，只替换大脑 `_agent_loop`。
    """

    async def _agent_loop(
        self,
        build_id: str,
        application_id: str,
        state: BuildTeamState,
        messages: list[ChatMessage],
        *,
        max_turns: int,
        max_repair_cycles: int,
        auto_publish: bool,
        teammate: str | None,
        tracker: Any | None = None,
        build_started_at: float | None = None,
        max_elapsed_seconds: float | None = None,
        model: str | None = None,
    ) -> str:
        if teammate is not None:
            raise RuntimeError("mechanical builder has no teammate loop")
        if state.planning_mode == "required":
            raise RuntimeError(
                "mechanical builder does not support planning_mode=required — "
                "the state machine itself is the plan"
            )
        actor_model = model or self.generator_model
        # 温度只在后端支持时才传（测试替身与老 provider 的签名里没有它）
        supports_temperature = _accepts_temperature(self.provider.stream)
        build = await self.workflow_store.get_build(build_id)
        requirement = str(build.get("requirement") or "").strip()
        catalog = self._catalog_overview()

        turn = 0
        architecture_plan = ""
        # 方案批准后允许出现的积木类型。方案文本里写着"按此实现，不要另选积木"，
        # 但那只是**建议**——真机构建 e28708d3 里方案校验两次把 variable_aggregator
        # 顶了回去、架构师改对了，建图手转头照样加了 variable_aggregator，
        # 整单在修复阶段空转 48 轮（那个积木不做算术，配置怎么改都算不出东西）。
        # 形态 B 的分工是"强模型定选型、小模型照单填表"，选型偏离必须是硬门。
        planned_types: set[str] = set()
        # 角色→模型映射：与经典引擎同一套语义——统筹模型是大脑（这里做架构选型
        # 与卡死后的升级接手），队友模型是执行者（这里做逐步提案）。
        #
        # 此处原先是反的：coordinator 当提案者、teammate[0] 当升级模型。同样两个
        # API 字段在两个引擎里含义相反，操作者按经典引擎的直觉传参（统筹=32B、
        # 队友=4B）就会得到**完全颠倒**的分工——实测构建 0826a87a 把 4B 派去做
        # 架构选型、32B 去填表，正好和 B v2 的设计意图（强模型定选型、小模型
        # 照单填表）相反。字段含义必须跨引擎一致，否则测到的是配置事故。
        teammates = state.teammate_models or []
        proposer_model = teammates[0] if teammates else actor_model
        # 升级阶梯对全阶段可用：任何阶段的小模型卡死都先升级再判停
        #（恢复手段优先于放弃手段——建图阶段此前没有这条，被守卫直接判死）。
        escalation_model = actor_model if teammates else None
        # 分工写进事件流：角色搞反过一次，链条上看不见就查不出来。
        self._record_event(
            build_id, "phase",
            f"分工：提案者={proposer_model}；"
            + (f"架构选型/升级接手={escalation_model}" if escalation_model
               else "无队友模型，架构规划阶段跳过，全程由提案者独立完成"),
        )
        phase_escalated: dict[str, bool] = {}
        # 反刍守卫：同一个被拒提案重复出现即升级反馈，三次判停——
        # 4B 真实构建曾对不存在的积木类型连续原地重试 17 轮直到预算耗尽。
        rejection_counts: dict[str, int] = {}
        # 黑板记忆：查阅过的积木 schema 常驻后续提案的上下文。没有它，
        # 无状态调用的查阅结果一轮后蒸发，4B 真实构建曾 start/end 交替
        # 重查 18 轮零进展（成功调用，反刍守卫管不着）。
        lookups: dict[str, str] = {}
        inspected_types: set[str] = set()
        seen_success: set[str] = set()
        # 未解决拒绝的黑板：单步反馈会被下一步冲掉（真实 4B 曾在被拒后先插一次
        # 查询，回头重试时早已看不到错误原因，同一残 JSON 连犯三次）。被拒记录
        # 常驻上下文，直到同名工具成功执行才清除。
        pending_rejections: dict[str, str] = {}
        # 成功型循环守卫：连续只读提案不落一笔草稿变更，4 次警告、8 次判停
        #（经典版全局探索守卫 10 轮的按阶段细化版）。
        consecutive_reads = 0
        redundant_reads = 0
        READ_ONLY_TOOLS = {"catalog_get", "manual_get", "run_inspect"}

        def rejection_guard(outcome: dict[str, Any]) -> str:
            signature = outcome.get("signature")
            if not signature:
                return ""
            count = rejection_counts.get(signature, 0) + 1
            rejection_counts[signature] = count
            if count >= 3:
                raise RuntimeError(
                    f"model perseverating: identical rejected proposal {count}x — {signature[:200]}"
                )
            if count == 2:
                return (
                    "警告：你已经第二次重复完全相同的被拒绝提案，它永远不会通过。"
                    "禁止再发这个提案；重新读积木目录和错误原因，换一个不同的动作。"
                )
            return ""
        defs = {
            definition.name: definition
            for definition in self._definitions(allow_team=False, planning_mode="disabled")
        }

        def toolset(names: tuple[str, ...]) -> list[ToolDefinition]:
            return [defs[name] for name in names if name in defs] + [PHASE_DONE_TOOL]

        def force_action(tools: list[ToolDefinition]) -> tuple[list[ToolDefinition], str]:
            """连续只读达阈值就收走只读工具——查资料不是进展。

            实测两个角色犯同一种病：建图手连查 18 轮零变更、验收作者 8 轮全在
            catalog_get 一个测试没写。警告与判停都只是"记账"，机械收窄工具面
            才真正把控制流拿回代码手里。
            """

            # 只对"没带来新信息"的只读收窄：修复阶段读执行台账是正当诊断，
            # 复测 13 因一刀切收走 run_inspect 把刚进入修复的构建判死。
            if redundant_reads < 1 and consecutive_reads < 3:
                return tools, ""
            narrowed = [t for t in tools if t.name not in READ_ONLY_TOOLS]
            reason = (
                "你在重复查询已经看过的同一份资料"
                if redundant_reads >= 1 else
                f"你连续 {consecutive_reads} 次只查不做"
            )
            return narrowed, (
                f"\n【已收走查询类工具】{reason}。本轮只能执行真正的变更（或 phase_done）。"
            )

        async def machine_execute(tool: str, data: dict[str, Any]) -> Any:
            # 状态机自己的调用记 platform_tool_call：工具预算约束的是模型的行为，
            # 平台的自查自纠（每步 draft_inspect + draft_validate、硬门代劳查手册）
            # 不该从模型额度里扣——否则机械阶梯越完善越容易把自己撑死
            # （真机 7d5ffa06 就是这么撞上 201/200 的）。账照记，只是不占额度。
            await self.harness.record_usage(
                build_id, "platform_tool_call",
                metadata={"actor": "state-machine", "tool": tool},
            )
            return await self._execute(
                build_id, application_id, state, tool, data,
                max_repair_cycles=max_repair_cycles,
                auto_publish=auto_publish,
                tracker=tracker,
                build_started_at=build_started_at,
                max_elapsed_seconds=max_elapsed_seconds,
            )

        async def projection() -> str:
            info = await machine_execute("draft_inspect", {})
            snapshot = info["snapshot"]
            # 草稿快照的图在 snapshot.workflow 下——早期误读成 snapshot 顶层，
            # 导致投影长期谎报"草稿是空的"，模型据此反复重加已存在节点。
            # 这是"先排除管道故障再归因模型"那条纪律的又一个实例（这次是我们自己的代码）。
            workflow = snapshot.get("workflow") or {}
            nodes = workflow.get("nodes") or []
            edges = workflow.get("edges") or []
            tests = snapshot.get("tests") or workflow.get("tests") or []
            lines = [
                f"当前草稿 revision={info['revision']}："
                f"节点 {len(nodes)} 个，边 {len(edges)} 条，测试 {len(tests)} 条。"
            ]
            for node in nodes:
                config = json.dumps(node.get("config") or {}, ensure_ascii=False)
                if len(config) > 500:
                    config = config[:500] + "…"
                refs = referenceable_paths(node)
                lines.append(
                    f"- 节点 id={node.get('id')} type={node.get('type')} "
                    f"title={node.get('title') or ''} config={config}"
                    + (f"  ← 可这样引用它：{'、'.join(refs)}" if refs else "")
                )
            for edge in edges:
                brief = json.dumps(edge, ensure_ascii=False)
                lines.append(f"- 边 {brief[:300]}")
            for test in tests:
                lines.append(
                    f"- 测试 id={test.get('id')} name={test.get('name') or ''} "
                    f"mandatory={test.get('mandatory')}"
                )
            # 无状态提案的盲区：小模型看不见"自己刚做成了什么"，实测会把刚加成功的
            # 节点反复再加（被"已存在"拒绝直到判停）。把已完成事实做成显式禁令。
            node_ids = [str(node.get("id")) for node in nodes]
            if node_ids:
                lines.append(
                    "⚠️ 已存在的节点 id：" + "、".join(node_ids)
                    + " —— 禁止对这些 id 再调用 draft_add_node（要改配置用 draft_update_node）。"
                )
            # 每一轮都从确定性校验器现算待办：只在"成功变更后"给的话，一被拒
            # 就丢——4B 实测恰恰在被拒后最需要指路时看不到它，于是回头重加节点。
            check = await machine_execute("draft_validate", {})
            todo = [
                error for error in check.get("errors", [])
                if _MISSING_TEST_ERROR not in error
            ]
            # 警告同样是机械算出的线索，此前只收 errors 白白浪费：日报基准里
            # "sales 输入未被下游使用"这条正指向真问题（图里根本没消费输入），
            # 模型却从未看到它。
            todo.extend(f"（警告）{w}" for w in (check.get("warnings") or [])[:3])
            # 机械可判的语义检查：终端节点的输出必须引用上游产出（$ref），
            # 字面量是占位符不是值。实测 4B 反复把 greeting 设成 "string"
            # 并因 no-op 被拒到判停——它不知道该引用上游，也没人告诉它。
            placeholder_hits: list[str] = []
            for node in nodes:
                if str(node.get("type")) not in {"end", "answer"}:
                    continue
                outputs = (node.get("config") or {}).get("outputs") or {}
                for field, value in outputs.items():
                    if not (isinstance(value, dict) and "$ref" in value):
                        placeholder_hits.append(f"{node.get('id')}.{field}={value!r}")
            # 同类机械检查：模板变量必须绑定 $ref 引用，字符串（含 Jinja 写法）
            # 是绑不上的。复测 17 的最后一寸就死在 variables={"name": "{{input.name}}"}。
            for node in nodes:
                config = node.get("config") or {}
                variables = config.get("variables")
                if not isinstance(variables, dict):
                    continue
                for var_name, value in variables.items():
                    if not (isinstance(value, dict) and "$ref" in value):
                        placeholder_hits.append(
                            f"{node.get('id')}.variables.{var_name}={value!r}"
                        )
            if placeholder_hits:
                todo.append(
                    "以下配置项仍是字面量而非引用（" + "、".join(placeholder_hits[:3])
                    + "），必须改成引用上游节点的产出，形如 "
                    '{"$ref": {"node_id": "<上游节点id>", "path": ["text"]}}；'
                    "若还没有能产出该值的节点（例如把 Hello 和姓名拼起来），"
                    "先用 draft_add_node 加一个 template_transform 节点并连线。"
                )
            if todo:
                lines.append("📋 当前校验未通过（下一步就解决其中一条）：" + "；".join(todo[:5]))
            else:
                lines.append(
                    "📋 结构校验已全部通过。若需求已完整实现（输出字段齐全、"
                    "上下游接线正确），立即调用 phase_done 收工。"
                )
            return "\n".join(lines)

        def lookup_board() -> str:
            if not lookups:
                return ""
            parts, total = [], 0
            for key, text in reversed(list(lookups.items())):  # 最新优先，总量封顶
                if total + len(text) > 6_000:
                    break
                parts.append(f"· {key}：{text}")
                total += len(text)
            return "你已查阅过的积木 schema（不要重复查询，直接使用）：\n" + "\n".join(reversed(parts))

        async def propose(
            *, actor: str, system: str, task_text: str,
            tools: list[ToolDefinition], feedback: str, budget_left: int,
            force_tool: str | None = None,
            model_override: str | None = None,
            temperature: float | None = None,
        ) -> dict[str, Any]:
            allowed = {definition.name for definition in tools}
            """一次无状态提案：调模型 → 执行首个工具调用 → 记录与反馈。"""

            nonlocal turn
            turn += 1
            if turn > max_turns:
                raise RuntimeError(f"turn budget exhausted after {max_turns} proposals")
            step_model = model_override or proposer_model
            await self.harness.record_usage(
                build_id, "model_call",
                metadata={"actor": actor, "turn": turn, "model": step_model},
            )
            board = lookup_board()
            pending = ""
            if pending_rejections:
                pending = "尚未解决的被拒提案（重试同名工具前必须先改正这些错误）：\n" + "\n".join(
                    f"· {tool}：{note}" for tool, note in list(pending_rejections.items())[-4:]
                )
            user_text = (
                f"业务需求：\n{requirement}\n\n"
                + (architecture_plan + "\n\n" if architecture_plan else "")
                + f"本阶段任务：{task_text}\n"
                f"本阶段剩余提案次数：{budget_left}。\n\n"
                + f"{catalog}\n\n"
                + (f"{board}\n\n" if board else "")
                + (f"{pending}\n\n" if pending else "")
                + await projection()
                + (f"\n\n上一步反馈：{feedback}" if feedback else "")
            )
            stream = self.provider.stream(
                model=step_model,
                system=system,
                messages=[ChatMessage(role="user", content=[ContentBlock(type="text", text=user_text)])],
                tools=tools,
                max_output_tokens=3_072,
                thinking_enabled=False,
                effort="low",
                tool_choice=(
                    {"type": "tool", "name": force_tool} if force_tool else {"type": "auto"}
                ),
                user_id=f"{build_id}-{actor}",
                **({"temperature": temperature} if temperature is not None else {}),
            )
            response = await self.agent_runtime._collect_stream(
                build_id, stream, f"build.{actor}.model", step_model
            )
            await self.harness.record_model_usage(
                build_id, response.usage, model=step_model,
                provider=self.provider.provider_name_for(step_model),
                metadata={
                    "application_id": application_id,
                    "workflow_id": application_id,
                    "actor": actor,
                    "turn": turn,
                    "phase": "mechanical",
                },
            )
            calls = [block for block in response.blocks if block.type == "tool_use"]
            outcome: dict[str, Any] = {"executed": None, "error": None, "done": False}
            records: list[dict[str, Any]] = []
            if not calls:
                outcome["error"] = "你没有调用任何工具。必须恰好调用一个工具（或 phase_done）。"
            else:
                call = calls[0]
                arguments = call.input or {}
                if call.name not in allowed:
                    # 阶段工具面必须是硬门而非建议：系统提示里出现过的工具名
                    # 小模型照样会叫（实测 4B 在工具被收走后仍持续调用
                    # draft_add_node），执法只能在执行边界做。
                    message = (
                        f"工具 {call.name} 在本阶段/本轮不可用。当前可用："
                        + "、".join(sorted(allowed))
                        + "。改用其中之一。"
                    )
                    records.append(tool_call_record(
                        name=str(call.name), arguments=arguments,
                        result=message, is_error=True,
                    ))
                    outcome["error"] = message
                    outcome["signature"] = f"{call.name}:blocked"
                elif (
                    call.name == "draft_add_node"
                    and isinstance(arguments.get("node"), dict)
                    and planned_types
                    and not phase_escalated.get("scaffold")
                    and str((arguments["node"] or {}).get("type") or "") not in planned_types
                ):
                    # 选型偏离硬门：方案是强模型定的、且已过机械校验，执行者只负责
                    # 照单填表。升级接手后放开——那时提案者本身就是架构师那一档，
                    # 有资格改方案（恢复手段优先于放弃手段）。
                    stray_type = str((arguments["node"] or {}).get("type") or "")
                    message = (
                        f"积木 {stray_type} 不在架构方案里。方案选定的是："
                        + "、".join(sorted(planned_types))
                        + "。按方案实现；方案确实不可行就说明理由，不要自己换积木"
                        "——选型是架构师那一步的判断，不是这一步的。"
                    )
                    records.append(tool_call_record(
                        name=str(call.name), arguments=arguments,
                        result=message, is_error=True,
                    ))
                    outcome["error"] = message
                    outcome["signature"] = f"offplan:{stray_type}"
                elif (
                    call.name == "draft_add_node"
                    and isinstance(arguments.get("node"), dict)
                    and str((arguments["node"] or {}).get("type") or "") not in inspected_types
                ):
                    # 查过手册才准用（经典 builder 对架构积木本就有此门，这里推广到
                    # 全部积木）。日报基准连三跑都栽在同一处：4B 没读手册就用
                    # variable_aggregator 做分组求和——那个积木只合并变量、不算数。
                    # 选错积木是无硬门保护的软失败，能机械做的就是保证说明书
                    # 在它做决定时确实在上下文里（下面由机器代劳查阅，不是叫它自己去查）。
                    node_type = str((arguments["node"] or {}).get("type") or "")
                    # 机械阶梯第 9 级（机器代劳确定性步骤）：查手册这一步是确定性的，
                    # 让机器自己去查，把说明书原文塞回反馈里，同时记为已查。
                    #
                    # 为什么不是"让模型自己去查"：那条路径可能不可满足——force_action
                    # 收走查询工具时，硬门仍在喊"先 catalog_get"，模型只能原地重提，
                    # 被反刍守卫判死（实测 0826a87a：建图 9 轮全卡在 unread:start，
                    # 60 秒判停）。教学式拒绝的前提是**它做得到**，做不到的指令
                    # 不是教学，是死锁。
                    manual_text = ""
                    try:
                        manual_text = json.dumps(
                            await machine_execute("catalog_get", {"type": node_type}),
                            ensure_ascii=False, default=str,
                        )[:1200]
                    except Exception as lookup_error:  # noqa: BLE001
                        manual_text = f"（平台查不到该积木说明：{lookup_error}）"
                    inspected_types.add(node_type)
                    lookups[f"catalog_get:{node_type}"] = manual_text
                    message = (
                        f"你还没读过积木 {node_type} 的说明就用它——已替你查好，"
                        f"读完再决定用不用（选错积木没有任何硬门会拦你）：\n{manual_text}\n"
                        "如果它确实能做你要做的事，原样重提即可；如果不能，换一个。"
                    )
                    records.append(tool_call_record(
                        name=str(call.name), arguments=arguments,
                        result=message, is_error=True,
                    ))
                    outcome["error"] = message
                    # 签名带类型即可：同一类型的门最多触发一次（已记为已查），
                    # 不会再被反刍守卫累加。
                    outcome["signature"] = f"unread:{node_type}"
                elif call.name == ARCHITECTURE_PLAN_TOOL.name:
                    nodes_plan = arguments.get("nodes") or []
                    # 方案先过机械校验再下发。方案是自由文本，此前完全不检查：
                    # 架构师若写了不存在的积木类型，错误要等执行者一个个 add 失败
                    # 才暴露，且执行者被要求"照方案做"，会反复重试同一个不存在的
                    # 类型直到判停——把一次可立即纠正的错误摊成一整阶段的空转。
                    known = {item.type for item in self.blocks.list()}
                    plan_errors: list[str] = []
                    seen_ids: set[str] = set()
                    for entry in nodes_plan:
                        if not isinstance(entry, dict):
                            continue
                        node_id = str(entry.get("id") or "")
                        node_type = str(entry.get("type") or "")
                        if node_type not in known:
                            near = difflib.get_close_matches(node_type, sorted(known), n=3)
                            plan_errors.append(
                                f"积木类型 {node_type!r} 不存在"
                                + (f"（最接近的是：{'、'.join(near)}）" if near else "")
                            )
                        if node_id in seen_ids:
                            plan_errors.append(f"节点 id {node_id!r} 重复")
                        seen_ids.add(node_id)
                        # 选型与配置必须自洽：只有 variable_assigner 会求值
                        # $formula/赋值操作符，把公式写给别的积木等于让执行者去
                        # 实现一个不存在的能力。真机构建 b44d3594 里 32B 架构师
                        # 正是把 sum_by(...) 挂在 variable_aggregator 上——那个
                        # 积木只合并分支值，公式会被原样当字符串吞掉。
                        sketch = str(entry.get("config_sketch") or "").replace(" ", "")
                        purpose = str(entry.get("purpose") or "")
                        wants_formula = "$formula" in sketch or any(
                            f"{fn}(" in sketch for fn in _FORMULA_FUNCTIONS
                        )
                        if wants_formula and node_type != "variable_assigner":
                            plan_errors.append(
                                f"节点 {node_id!r} 的配置里写了公式，但类型是 "
                                f"{node_type!r}——只有 variable_assigner 会真正求值 "
                                "$formula/赋值操作符，其它积木会把公式当普通字符串吞掉。"
                                "把这个节点改成 variable_assigner"
                            )
                        # 判据落在"这个节点要干什么"上，且用白名单——只查字面写法
                        # 会被删字绕过，黑名单会被换一个没列进去的类型绕过，
                        # 两种规避真机上都发生过（310141fd / 92af320c）。
                        elif node_type not in _COMPUTING_TYPES and any(
                            word in purpose or word in sketch
                            for word in _ARITHMETIC_INTENT
                        ):
                            plan_errors.append(
                                f"节点 {node_id!r} 的职责是做算术（{purpose[:40]}），"
                                f"但类型 {node_type!r} 不会做任何计算——配置里写什么都不会"
                                "被求值。会做确定性算术的积木只有这些："
                                + "、".join(sorted(_COMPUTING_TYPES))
                                + "。分组求和/合计这类请用 variable_assigner，并在 "
                                "config_sketch 里写出完整公式（删掉公式、或换个不计算的"
                                "积木都不算改对，问题在选型）"
                            )
                    types_in_plan = {
                        str(e.get("type")) for e in nodes_plan if isinstance(e, dict)
                    }
                    if not types_in_plan & {"start", "schedule_trigger"}:
                        plan_errors.append("方案里没有起点节点（start 或 schedule_trigger）")
                    if not types_in_plan & {"end", "answer"}:
                        plan_errors.append("方案里没有终点节点（end 或 answer）")
                    if plan_errors:
                        message = (
                            "方案没通过机械校验，重新调用 architecture_plan 给出修正版："
                            + "；".join(plan_errors)
                        )
                        records.append(tool_call_record(
                            name=call.name, arguments=arguments,
                            result=message, is_error=True,
                        ))
                        outcome["error"] = message
                        outcome["signature"] = f"plan_invalid:{'；'.join(sorted(plan_errors))[:200]}"
                    else:
                        # 方案已过机械校验（类型存在、算术落在会计算的积木上），
                        # 就把这些积木的说明书**直接预载进黑板**并记为已查：
                        # 执行者要的是"做决定时手册在上下文里"，机器能直接保证，
                        # 没必要让它每个类型花一步去撞查手册硬门（5 个类型就吃掉
                        # 建图预算的四分之一，真机 93430e0b 正是这么撞到上限的）。
                        # 硬门保留给**方案之外**的积木——那才是需要额外审视的偏离。
                        for entry in nodes_plan:
                            if not isinstance(entry, dict):
                                continue
                            planned_type = str(entry.get("type") or "")
                            if not planned_type or planned_type in inspected_types:
                                continue
                            try:
                                lookups[f"catalog_get:{planned_type}"] = json.dumps(
                                    await machine_execute(
                                        "catalog_get", {"type": planned_type}
                                    ),
                                    ensure_ascii=False, default=str,
                                )[:1200]
                                inspected_types.add(planned_type)
                            except Exception:  # noqa: BLE001 — 查不到就让硬门照常拦
                                pass
                        lines = []
                        for n in nodes_plan:
                            if not isinstance(n, dict):
                                continue
                            lines.append(f"- {n.get('id')}（{n.get('type')}）：{n.get('purpose')}")
                            # 配置骨架照抄下发：实测 4B 知道函数名却写不对参数
                            # （把 sum_by(sales,"store","amount") 写成
                            # sum_by(pluck(...),pluck(...)) 并发明 $ref 内联语法），
                            # 32B 一次就对。判断力留给架构师，执行者只填表。
                            if str(n.get("config_sketch") or "").strip():
                                lines.append(
                                    f"  关键配置（照抄，不要自己推导）：{n['config_sketch']}"
                                )
                        planned_types.clear()
                        planned_types.update(
                            str(entry.get("type"))
                            for entry in nodes_plan if isinstance(entry, dict)
                        )
                        outcome["executed"] = call.name
                        outcome["plan_text"] = (
                            "【架构方案（由架构师选定，按此实现，不要另选积木）】\n"
                            + "\n".join(lines)
                            + (f"\n要点：{arguments.get('notes')}"
                               if arguments.get("notes") else "")
                        )
                        records.append(tool_call_record(
                            name=call.name, arguments=arguments,
                            result=outcome["plan_text"][:900], is_error=False,
                        ))
                elif call.name == PHASE_DONE_TOOL.name:
                    outcome["done"] = True
                    records.append(tool_call_record(
                        name=call.name, arguments=arguments,
                        result=str(arguments.get("summary") or "done"), is_error=False,
                    ))
                else:
                    await self.harness.record_usage(
                        build_id, "tool_call",
                        metadata={"actor": actor, "tool": call.name or ""},
                    )
                    try:
                        invalid_json = arguments.get(INVALID_TOOL_INPUT_JSON_KEY)
                        if invalid_json is not None:
                            detail = (
                                invalid_json.get("error", "unknown parse error")
                                if isinstance(invalid_json, dict) else "unknown parse error"
                            )
                            raw_preview = (
                                str(invalid_json.get("raw_preview") or "")
                                if isinstance(invalid_json, dict) else ""
                            )
                            raise RuntimeError(
                                f"invalid tool input JSON for {call.name or ''}: {detail}。"
                                f"你上次输出的参数原文（含语法错误）：{raw_preview[:400]} "
                                "—— 重新输出完整、每个括号都正确配对的合法 JSON。"
                            )
                        value = await machine_execute(str(call.name), dict(arguments))
                        result_text = json.dumps(value, ensure_ascii=False, default=str)
                        records.append(tool_call_record(
                            name=str(call.name), arguments=arguments,
                            result=result_text, is_error=False,
                        ))
                        outcome["executed"] = call.name
                        # 参数留一份给阶段循环判进展用（例如"这次 test_add 是新测试
                        # 还是把同一条又重写了一遍"）。
                        outcome["arguments"] = arguments
                        outcome["result"] = result_text[:800]
                        outcome["signature"] = f"{call.name}:" + json.dumps(
                            arguments, ensure_ascii=False, sort_keys=True, default=str
                        )[:500]
                        pending_rejections.pop(str(call.name), None)
                        if call.name not in READ_ONLY_TOOLS:
                            # 有真实进展就清空反刍计数——守卫要抓的是"卡死"，
                            # 不是"偶尔重提旧提案但整体仍在推进"。实测 4B 在
                            # 加完第三个节点后仍被全阶段累计的守卫判停。
                            rejection_counts.clear()
                        if call.name in ("catalog_get", "manual_get"):
                            key = f"{call.name}:{arguments.get('type', '')}"
                            lookups[key] = result_text[:900]
                            inspected_types.add(str(arguments.get("type") or ""))
                    except Exception as error:  # noqa: BLE001 — 边界拒绝就是实验信号
                        message = f"{type(error).__name__}: {error}"
                        records.append(tool_call_record(
                            name=str(call.name), arguments=arguments,
                            result=message, is_error=True,
                        ))
                        outcome["error"] = message
                        outcome["signature"] = f"{call.name}:" + json.dumps(
                            arguments, ensure_ascii=False, sort_keys=True, default=str
                        )[:500]
                        pending_rejections[str(call.name)] = message[:500]
                if len(calls) > 1:
                    outcome["ignored_calls"] = [c.name for c in calls[1:]]
            self._record_turn(
                build_id, turn, actor, response, records, state, model=step_model,
                prompt=({"system": system, "user": user_text}
                        if os.getenv("LILIES_TRACE_PROMPTS") == "1" else None),
            )
            await self._emit(build_id, "build.mechanical.step", {
                "actor": actor, "turn": turn,
                "executed": outcome.get("executed"),
                "error": outcome.get("error"),
                "done": outcome["done"],
            })
            return outcome

        _propose_single = propose

        async def propose(**kwargs: Any) -> dict[str, Any]:  # type: ignore[no-redef]
            """提案竞争：一步之内最多 N 个候选，第一个过验证器的落库。

            与"下一步再重试"的区别：重试是同温度同提案（4B 常逐字重复），
            竞争是不同温度的不同提案；且失败候选不消耗阶段步数，只消耗回合。
            """

            temperatures = PROPOSAL_TEMPERATURES if supports_temperature else (None,)
            outcome: dict[str, Any] = {}
            for index, temperature in enumerate(temperatures):
                outcome = await _propose_single(temperature=temperature, **kwargs)
                if outcome.get("done") or not outcome.get("error"):
                    if index:
                        await self._emit(build_id, "build.mechanical.competition", {
                            "actor": kwargs.get("actor"), "winner_index": index,
                            "candidates_tried": index + 1,
                        })
                    return outcome
            await self._emit(build_id, "build.mechanical.competition", {
                "actor": kwargs.get("actor"), "winner_index": None,
                "candidates_tried": len(temperatures),
            })
            return outcome

        def step_feedback(outcome: dict[str, Any]) -> str:
            nonlocal consecutive_reads, redundant_reads
            if outcome.get("error"):
                guard_note = rejection_guard(outcome)
                error = str(outcome["error"])
                hint = ""
                if "unknown block type" in error:
                    hint = "该积木类型不存在——只能使用上面积木目录里列出的类型。"
                elif "Extra inputs are not permitted" in error:
                    hint = ('节点配置必须包在 config 里：'
                            '{"node_id": "<id>", "changes": {"config": {...}}}，'
                            "不要把 outputs/inputs/template 等直接放在 changes 下。")
                elif "would not change the workflow" in error:
                    hint = ("你提交的配置和现状一模一样（零变化）。要么给出真正不同的配置，"
                            "要么这一步已经做完了——直接做下一件事或 phase_done。")
                elif "already exists" in error:
                    hint = ("这个节点你已经加好了，别再加——马上做下一件没做的事："
                            "添加还缺的节点、连线，或全部就绪就 phase_done。")
                elif "revision" in error.lower():
                    hint = "草稿已被更新，先看上面的最新草稿状态再提案。"
                return f"上一个提案被拒绝：{error}。{hint}{guard_note}"
            executed = outcome.get("executed")
            if executed:
                if executed in READ_ONLY_TOOLS:
                    consecutive_reads += 1
                    if consecutive_reads >= 8:
                        raise RuntimeError(
                            f"discovery loop: {consecutive_reads} consecutive read-only "
                            "proposals without mutating the draft"
                        )
                    repeat_note = ""
                    signature = outcome.get("signature")
                    if signature in seen_success:
                        redundant_reads += 1
                        repeat_note = (
                            "这是重复查询——该结果已经在上面的'已查阅'区，"
                            "不要再查，立即动手改草稿。"
                        )
                    if signature:
                        seen_success.add(str(signature))
                    urgency = (
                        f"警告：你已经连续 {consecutive_reads} 次只查不建。"
                        "下一步必须调用改动草稿的工具（如 draft_add_node）。"
                        if consecutive_reads >= 4 else ""
                    )
                    return (
                        f"上一个提案 {executed} 已成功执行，"
                        f"结果摘要：{outcome.get('result', '')[:400]}。{repeat_note}{urgency}"
                    )
                consecutive_reads = 0
                redundant_reads = 0
                return (
                    f"上一个提案 {executed} 已成功执行，"
                    f"结果摘要：{outcome.get('result', '')[:400]}"
                )
            return ""

        # ── 阶段零：架构规划（形态 B v2 / MinionS 式分解）──
        # 7 次真机复测证明：机械阶梯能把小模型的格式错误压到接近零，但压不出
        # "该用哪个积木"的判断力（连三跑用只做合并的积木实现分组求和）。
        # 由更强的模型一次性定选型，小模型照单填表——这正是调研里 MinionS
        # "强模型把任务切碎到机械粒度"的落点。
        if escalation_model:
            await self._emit(build_id, "build.mechanical.phase", {"phase": "plan"})
            self._record_event(build_id, "phase", f"架构规划：{escalation_model} 选型")
            plan_feedback = ""
            for _ in range(4):
                outcome = await propose(
                    actor="architect", system=PLAN_SYSTEM,
                    task_text="读需求与积木目录，选出要用的积木并说明职责，然后调用 architecture_plan。",
                    tools=[defs[n] for n in ("catalog_get", "manual_get") if n in defs]
                    + [ARCHITECTURE_PLAN_TOOL],
                    feedback=plan_feedback, budget_left=4,
                    model_override=escalation_model,
                )
                if outcome.get("executed") == "architecture_plan":
                    architecture_plan = outcome.get("plan_text") or ""
                    break
                plan_feedback = step_feedback(outcome)
            if architecture_plan:
                self._record_event(build_id, "phase", "架构方案已定，交给小模型逐个落实")

        # ── 阶段一：建图（关卡 = draft_validate，缺测试的错误留给下一阶段）──
        await self._emit(build_id, "build.mechanical.phase", {"phase": "scaffold"})
        self._record_event(build_id, "phase", "状态机进入建图阶段：小模型逐个提案节点与连线")
        feedback = ""
        validation: dict[str, Any] = {}
        scaffold_ok = False
        consecutive_reads = 0
        # redundant_reads 必须一起清：架构师阶段重复查过同一份手册（实测它会
        # catalog_get 同一类型两次）会让 force_action 在建图第一步就收走查询工具，
        # 而查手册硬门又要求"先 catalog_get"——两条机制互相抵消，模型无路可走。
        redundant_reads = 0
        duplicate_adds = 0
        same_tool_rejects = 0
        stalled_after_green = 0
        empty_turns = 0
        for step_index in range(SCAFFOLD_BUDGET):
            # 下一步由确定性校验器算出，不靠模型回忆自己做过什么：
            # 4B 实测会在"已加好的节点"和"新节点"之间来回震荡（加 start→加 end
            # →又加 start），显式禁令只压住一轮。校验错误是机械的、精确的、
            # 每次都新鲜的——把它当作待办清单下发。
            # 机械子阶段（形态 B 的论点落地）：该做什么由代码按草稿状态判定，
            # 不问模型。4B 实测在无状态调用里永远锚定"加 start 节点"，三个节点
            # 都加好后仍从不调 draft_connect——靠提示无法让它前进到接线。
            scaffold_force: str | None = None
            draft_now = await machine_execute("draft_inspect", {})
            snap_wf = (draft_now["snapshot"].get("workflow") or {})
            node_count = len(snap_wf.get("nodes") or [])
            edge_count = len(snap_wf.get("edges") or [])
            snap = {"nodes": snap_wf.get("nodes") or []}
            node_types = {str(n.get("type")) for n in (snap_wf.get("nodes") or [])}
            # 只有"骨架齐了"才强制接线：起点和终点都在，且边数不足以串起所有节点。
            # 早期用 node_count>=2 判定过于激进——刚加完两个节点就锁死加节点能力，
            # 第三个节点永远加不进来（happy-path 脚本测试当场抓到该回归）。
            skeleton_ready = bool(node_types & {"start", "schedule_trigger"}) and bool(
                node_types & {"end", "answer"}
            )
            # 接线是否做完，按**拓扑**判，不按边数。edge_count < node_count - 1 是个
            # 弱代理：真机构建 82a730cd 有 5 个节点 4 条边（数量达标），但
            # generate_report 一条入边都没有，图是断的——子阶段没被激活，缺口提示
            # 也就没发出去，模型对着别处的零变化更新空转到判停。
            existing_edges = snap_wf.get("edges") or []
            has_out = {str(e.get("source")) for e in existing_edges}
            has_in = {str(e.get("target")) for e in existing_edges}
            need_out = [
                str(n.get("id")) for n in snap.get("nodes") or []
                if str(n.get("type")) not in {"end", "answer"}
                and str(n.get("id")) not in has_out
            ]
            need_in = [
                str(n.get("id")) for n in snap.get("nodes") or []
                if str(n.get("type")) not in {"start", "schedule_trigger"}
                and str(n.get("id")) not in has_in
            ]
            wiring_phase = skeleton_ready and bool(need_out or need_in)
            if wiring_phase:
                # 接线子阶段：收走一切"加节点"能力，唯一合法动作就是连线
                tools_this_step = toolset((
                    "catalog_get", "draft_connect", "draft_remove_edge", "draft_update_node",
                ))
                ids = "、".join(str(n.get("id")) for n in snap.get("nodes") or [])
                # 只报"节点有几个、边有几条"不够：模型看不到**已有哪些边**，
                # 就只能猜下一条该连哪，猜中同一条已存在的边就死循环（真机构建
                # 310141fd：同一条边连提 9 次被判停）。缺口是机械算得出来的。
                wired = "；".join(
                    f"{e.get('source')}→{e.get('target')}" for e in existing_edges
                ) or "（一条都没有）"
                feedback += (
                    f"\n【接线子阶段】草稿已有 {node_count} 个节点（{ids}），"
                    f"已连的边：{wired}。"
                    + (f"还缺出边的节点：{'、'.join(need_out)}。" if need_out else "")
                    + (f"还缺入边的节点：{'、'.join(need_in)}。" if need_in else "")
                    + "本轮只能用 draft_connect 补上还缺的那条，不要重提已有的边。"
                )
                if consecutive_reads >= 1:
                    scaffold_force = "draft_connect"
            else:
                tools_this_step = toolset(SCAFFOLD_TOOLS)
            # 机械阶梯第 6 级（强制指定必调工具）推广到建图阶段：连续空轮
            # 说明模型在输出散文而不是提案，靠提示劝不动（实测 b44d3594 连续
            # 3 轮零调用）。该做什么状态机本来就算得出来，直接命名 tool_choice
            # 走约束解码。
            if empty_turns >= 2 and scaffold_force is None:
                scaffold_force = "draft_connect" if wiring_phase else "draft_add_node"
                feedback += (
                    f"\n【已强制调用 {scaffold_force}】你连续 {empty_turns} 轮没有调用任何工具。"
                    "本轮必须提交这个工具的调用。"
                )
            tools_this_step, force_note = force_action(tools_this_step)
            feedback += force_note
            if duplicate_adds >= 2 and not wiring_phase:
                tools_this_step = [t for t in tools_this_step if t.name != "draft_add_node"]
                feedback += "（本轮已禁用 draft_add_node——你反复重加已有节点，改做连线或 phase_done。）"
                duplicate_adds = 0
            try:
                outcome = await propose(
                    actor="graph-builder", system=SCAFFOLD_SYSTEM,
                task_text=(
                    "根据需求搭建工作流图。逐个添加/修正节点与连线；"
                    "全部就绪后调用 phase_done。"
                ),
                    tools=tools_this_step, feedback=feedback,
                    budget_left=SCAFFOLD_BUDGET - step_index, force_tool=scaffold_force,
                    model_override=(escalation_model if phase_escalated.get("scaffold") else None),
                )
            except RuntimeError as guard_error:
                # 判停之前先看关卡：守卫是用来止损的，关卡已经达成就没有损可止。
                # 真机构建 82a730cd 图早就建全了，模型对着 end 反复提交零变化的
                # 更新，反刍守卫（第 3 次）比"连续无进展即自动推进"（也是第 3 次）
                # 早一轮开火，把一单已经合格的建图判死。
                gate = await machine_execute("draft_validate", {})
                if not [
                    error for error in gate.get("errors", [])
                    if _MISSING_TEST_ERROR not in error
                ]:
                    validation = gate
                    scaffold_ok = True
                    self._record_event(
                        build_id, "phase",
                        f"建图守卫触发（{str(guard_error)[:40]}）但结构校验已全绿——"
                        "关卡达成即判，继续推进",
                    )
                    break
                if escalation_model and not phase_escalated.get("scaffold"):
                    phase_escalated["scaffold"] = True
                    rejection_counts.clear()
                    self._record_event(
                        build_id, "phase",
                        f"建图卡住（{str(guard_error)[:60]}），升级到 {escalation_model} 继续",
                    )
                    feedback = "（已升级到更强模型接手；重新审视需求与草稿，给出下一个正确提案。）"
                    continue
                raise
            if "没有调用任何工具" in str(outcome.get("error") or ""):
                empty_turns += 1
            elif outcome.get("executed") or outcome["done"]:
                empty_turns = 0
            if outcome.get("executed") and outcome["executed"] not in READ_ONLY_TOOLS:
                stalled_after_green = 0
            elif not outcome["done"]:
                stalled_after_green += 1
            if "already exists" in str(outcome.get("error") or ""):
                duplicate_adds += 1
                same_tool_rejects += 1
                if same_tool_rejects >= 5:
                    raise RuntimeError(
                        "graph-builder stuck re-adding existing nodes "
                        f"({same_tool_rejects}x) despite validator guidance"
                    )
            elif outcome.get("executed"):
                same_tool_rejects = 0
            if not outcome["done"] and stalled_after_green >= 3:
                # 关卡已绿却连续 3 步无耐久进展：状态机替它推进，别在绿灯前空转。
                # 语义缺陷（如输出没接上游）交给验收阶段的测试去暴露——那是有
                # 硬门的地方，比在建图阶段空耗预算强。
                check = await machine_execute("draft_validate", {})
                if not [e for e in check.get("errors", []) if _MISSING_TEST_ERROR not in e]:
                    await self._emit(build_id, "build.mechanical.phase", {
                        "phase": "scaffold", "auto_advanced": True,
                    })
                    self._record_event(
                        build_id, "phase",
                        "结构校验已通过且连续无进展，状态机自动推进到验收编写阶段",
                    )
                    validation = check
                    scaffold_ok = True
                    break
            if outcome["done"]:
                validation = await machine_execute("draft_validate", {})
                blocking = [
                    error for error in validation.get("errors", [])
                    if _MISSING_TEST_ERROR not in error
                ]
                if not blocking:
                    scaffold_ok = True
                    break
                # 宣布完成但校验没过、且错误一字未变 = 空转。这一路原先没有任何
                # 守卫：phase_done 每次都"执行成功"，反刍计数管不着，模型可以
                # 一路宣布到预算耗尽（脚本复现：16 次 phase_done 烧光建图预算）。
                # 复用同一套记账——3 次即触发升级阶梯，升级过了才判停，
                # 遵守"恢复手段优先于放弃手段"。
                outcome["signature"] = "phase_done_unfixed:" + "；".join(sorted(blocking))[:300]
                try:
                    stall_note = rejection_guard(outcome)
                except RuntimeError as guard_error:
                    if escalation_model and not phase_escalated.get("scaffold"):
                        phase_escalated["scaffold"] = True
                        rejection_counts.clear()
                        self._record_event(
                            build_id, "phase",
                            f"建图空转（{str(guard_error)[:60]}），升级到 {escalation_model} 继续",
                        )
                        feedback = (
                            "（已升级到更强模型接手；草稿仍未通过结构校验，"
                            "先把这些错误逐条修掉再谈完成：" + "；".join(blocking) + "）"
                        )
                        continue
                    raise
                feedback = (
                    "你宣布了完成，但草稿校验未通过，继续修正：" + "；".join(blocking) + stall_note
                )
            else:
                feedback = step_feedback(outcome)
        if not scaffold_ok:
            # 关卡是 draft_validate，不是"模型宣布完成"。预算用尽时先把关卡再判
            # 一次：真机构建 93430e0b 把图建全、连全，最后一条边刚落库就撞上
            # 预算上限，被当成"建图失败"整单判死——关卡明明已经达成了。
            # （机械阶梯第 5 级"关卡达成即判"此前只落在验收阶段。）
            final_check = await machine_execute("draft_validate", {})
            blocking = [
                error for error in final_check.get("errors", [])
                if _MISSING_TEST_ERROR not in error
            ]
            if not blocking:
                validation = final_check
                scaffold_ok = True
                self._record_event(
                    build_id, "phase",
                    "建图预算用尽，但结构校验已全绿——关卡达成即判，继续推进",
                )
            else:
                raise RuntimeError(
                    "scaffold budget exhausted with invalid draft: "
                    + "；".join(blocking)
                )
        self._record_event(build_id, "phase", "建图关卡通过：草稿结构校验全绿")

        async def anchored_mandatory_test() -> tuple[bool, str]:
            """关卡判据：至少一条 mandatory 测试断言了**具体值**。

            只验形状（exists/type/length，或 structural=True）的验收是假关卡：
            工作流可以什么都算错而测试全绿。判据放宽到 equals/contains 是为了
            让含模型节点的工作流也能过——但纯结构断言一律不算数。
            """
            info = await machine_execute("draft_inspect", {})
            snapshot = info.get("snapshot") or {}
            tests = snapshot.get("tests") or (snapshot.get("workflow") or {}).get("tests") or []
            mandatory = [t for t in tests if t.get("mandatory")]
            if not mandatory:
                return False, "草稿里还没有任何 mandatory 验收测试。"
            for test in mandatory:
                for assertion in test.get("assertions") or []:
                    if (
                        assertion.get("operator") in {"equals", "contains"}
                        and not assertion.get("structural")
                        and assertion.get("expected") is not None
                    ):
                        return True, ""
            return False, (
                "已有的 mandatory 测试只验了形状（exists/type/长度），没有一条断言具体值——"
                "这种验收工作流算错了也会全绿。"
            )

        # ── 阶段二：验收编写（关卡 = 至少一条锚定具体值的 mandatory 测试）──
        await self._emit(build_id, "build.mechanical.phase", {"phase": "test"})
        self._record_event(build_id, "phase", "状态机进入验收编写阶段")
        feedback = ""
        tests_ok = False
        has_test_yet = False
        idle_after_test = 0
        seen_test_ids: set[str] = set()
        consecutive_reads = 0
        redundant_reads = 0
        for step_index in range(TEST_BUDGET):
            test_tools, force_note = force_action(toolset(TEST_TOOLS))
            feedback += force_note
            # 收走查询工具后 4B 会干脆不调任何工具（复测 11：末 6 轮纯文本）。
            # 最后一道机械手段——状态机直接指定必须调用的工具，vLLM 侧对命名
            # tool_choice 走约束解码，参数合法性也一并保证。
            force_tool = None
            if not has_test_yet and consecutive_reads >= 1:
                force_tool = "test_add"
                test_tools = [SLIM_TEST_ADD] + [
                    t for t in test_tools if t.name != "test_add"
                ]
                feedback += "\n【本轮强制】只能调用 test_add 写出验收测试。"
            try:
                outcome = await propose(
                    actor="test-author", system=TEST_SYSTEM,
                task_text=(
                    "为该工作流编写验收测试（mandatory=true），输入用需求里的样例数据，"
                    "断言锚定你算出的具体数字。完成后调用 phase_done。"
                ),
                    tools=test_tools, feedback=feedback,
                    budget_left=TEST_BUDGET - step_index, force_tool=force_tool,
                    model_override=(escalation_model if phase_escalated.get("test") else None),
                )
            except RuntimeError as guard_error:
                # 同建图阶段：守卫开火前先判关卡
                anchored_now, _ = await anchored_mandatory_test()
                if anchored_now:
                    tests_ok = True
                    self._record_event(
                        build_id, "phase",
                        f"验收编写守卫触发（{str(guard_error)[:40]}）但锚定具体值的"
                        "mandatory 测试已就位——关卡达成即判，继续推进",
                    )
                    break
                if escalation_model and not phase_escalated.get("test"):
                    phase_escalated["test"] = True
                    rejection_counts.clear()
                    self._record_event(
                        build_id, "phase",
                        f"验收编写卡住（{str(guard_error)[:60]}），升级到 {escalation_model} 继续",
                    )
                    feedback = "（已升级到更强模型接手；直接写出锚定数值的验收测试。）"
                    continue
                raise
            validation = await machine_execute("draft_validate", {})
            # 关卡不能只数"有没有测试"：只验形状的断言照样全绿，发布出去的是
            # 形状合法的垃圾——本项目已经因此出过一次假成功（55434ea9）。
            # 关卡改为"至少一条 mandatory 测试锚定了具体值"。
            anchored, weak_reason = await anchored_mandatory_test()
            has_test = anchored
            # 强制 test_add 的条件跟着关卡走：写了一条只验形状的弱测试也不算数，
            # 否则模型加一条 exists 断言就把强制解除了，关卡永远达不成。
            has_test_yet = anchored
            if outcome["done"]:
                if has_test:
                    tests_ok = True
                    break
                feedback = (
                    "你宣布了完成，但验收还不成立：" + weak_reason
                    + " 用 test_add 补一条锚定具体值的 mandatory 测试"
                    "（operator=equals，expected 写你从样例数据算出来的那个值）。"
                )
            else:
                if outcome.get("executed") == "test_add":
                    # 反复重写**同一条**测试不算进展：真机构建 400a231e 关卡早已
                    # 达成（mandatory + equals 2000），模型却在最后几步一遍遍重写
                    # 那条测试、从不宣布完成，被报成"没有 mandatory 验收测试"。
                    # 只有新增了一条**新 id** 的测试才算把事情往前推了一格。
                    added_id = str(
                        ((outcome.get("arguments") or {}).get("test") or {}).get("id")
                        or ""
                    )
                    if added_id and added_id not in seen_test_ids:
                        seen_test_ids.add(added_id)
                        idle_after_test = 0
                    elif has_test:
                        idle_after_test += 1
                    else:
                        idle_after_test = 0
                elif has_test:
                    idle_after_test += 1
                # 关卡已达成（存在 mandatory 测试）却继续空转：状态机替它推进。
                # 复测 9 实况：测试早已加好（equals "Hello Ada"），模型却接着查
                # 目录直到预算耗尽，被判"没有测试"——关卡该在达成时就判过。
                if has_test and idle_after_test >= 2:
                    await self._emit(build_id, "build.mechanical.phase", {
                        "phase": "test", "auto_advanced": True,
                    })
                    self._record_event(
                        build_id, "phase", "验收测试已就位且连续无进展，状态机自动推进到跑测阶段",
                    )
                    tests_ok = True
                    break
                feedback = step_feedback(outcome)
        if not tests_ok:
            # 判死之前把关卡再判一次——与建图阶段的预算出口同源。真机构建
            # 400a231e 的验收测试其实早就写好了（mandatory + equals 2000），
            # 只是模型在预算最后几步反复重写同一条测试，never 宣布完成，
            # 于是被报成"没有 mandatory 验收测试"。关卡达成即判。
            anchored_final, weak_reason = await anchored_mandatory_test()
            if anchored_final:
                tests_ok = True
                self._record_event(
                    build_id, "phase",
                    "验收编写预算用尽，但锚定具体值的 mandatory 测试已就位——"
                    "关卡达成即判，继续推进",
                )
            else:
                raise RuntimeError(
                    "test-author budget exhausted without a mandatory acceptance test: "
                    + weak_reason
                )
        self._record_event(build_id, "phase", "验收编写关卡通过：mandatory 测试已就位")

        # ── 阶段三：跑测 + 修复循环（关卡 = test_run 全绿；通过时 _execute 自动发布）──
        # 升级阶梯（设计文档 §3 形态 A"修复诊断走升级阶梯"的落地）：
        # 修复是全流程认知最难的一步，实测 4B 能改配置但想不到"图缺一个节点"。
        # 第一轮用小模型（便宜），未过即把修理手升级到 teammate_models 里声明的
        # 更强模型——结局仍由 test_run 硬验证，升级不放松任何关卡。
        for cycle in range(max_repair_cycles + 1):
            await self._emit(build_id, "build.mechanical.phase", {"phase": "verify", "cycle": cycle})
            report = await machine_execute("test_run", {})
            if report.get("passed"):
                # 业主等的就是这句
                self._record_event(build_id, "phase", "验收测试全绿，交付成立",
                                   for_owner=True)
                return "mechanical build passed acceptance"
            failures = json.dumps(
                report.get("results") or report, ensure_ascii=False, default=str
            )[:1_800]
            # 诊断由状态机机械完成：修理手读完台账却不动手是实测常态
            # （复测 13/14 都死在这），与其逼它调 run_inspect，不如把执行台账
            # 直接读好压成摘要交给它——它只需要提出修改方案。
            ledger_digest = ""
            run_ids = [
                str(item.get("run_id")) for item in (report.get("results") or [])
                if isinstance(item, dict) and item.get("run_id") and not item.get("passed")
            ]
            if run_ids:
                try:
                    ledger = await machine_execute("run_inspect", {"run_id": run_ids[0]})
                    ledger_digest = (
                        "\n执行台账（状态机已代你读取）：\n"
                        + json.dumps(ledger, ensure_ascii=False, default=str)[:1_500]
                    )
                except Exception as error:  # noqa: BLE001
                    ledger_digest = f"\n（执行台账读取失败：{error}）"
            if cycle >= max_repair_cycles:
                raise RuntimeError(
                    f"acceptance still failing after {max_repair_cycles} repair cycles"
                )
            await self._emit(build_id, "build.mechanical.phase", {"phase": "repair", "cycle": cycle + 1})
            # 返修是业主该知道的：东西还没好，正在改
            self._record_event(
                build_id, "phase", f"验收未过，进入第 {cycle + 1} 轮修复",
                for_owner=True,
            )
            feedback = ""
            consecutive_reads = 0
            redundant_reads = 0
            for step_index in range(REPAIR_BUDGET_PER_CYCLE):
                repair_model = escalation_model if (cycle >= 1 and escalation_model) else None
                if repair_model and step_index == 0:
                    self._record_event(
                        build_id, "phase",
                        f"第 {cycle + 1} 轮修复升级到更强模型：{repair_model}",
                    )
                repair_tools, force_note = force_action(toolset(REPAIR_TOOLS))
                feedback += force_note
                try:
                    outcome = await propose(
                        actor="repairer", system=REPAIR_SYSTEM,
                    task_text=(
                        "验收测试未通过。失败报告（截断）：\n" + failures + ledger_digest +
                        "\n\n据此直接修实现（改节点配置/加缺失节点/改连线）；修完调用 phase_done。"
                    ),
                        tools=repair_tools, feedback=feedback,
                        budget_left=REPAIR_BUDGET_PER_CYCLE - step_index,
                        model_override=repair_model,
                    )
                    if outcome["done"]:
                        break
                    feedback = step_feedback(outcome)
                except RuntimeError as guard_error:
                    # 守卫判停前先把升级阶梯用完：小模型卡住不等于全局失败，
                    # 下一轮修复本来就该换更强的模型。注意反刍守卫是在
                    # step_feedback 里抛的——try 必须同时包住它，否则升级永远
                    # 等不到（实测 4B 在能升级之前就被判死两次）。
                    if escalation_model and not repair_model:
                        self._record_event(
                            build_id, "phase",
                            f"小模型修复卡住（{str(guard_error)[:60]}），提前升级到 {escalation_model}",
                        )
                        rejection_counts.clear()
                        break
                    raise
        raise RuntimeError("unreachable: repair loop exit without verdict")
