from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator

from .workflow_models import (
    BlockDefinition,
    EdgeSpec,
    NodeSpec,
    PortDefinition,
    ValueType,
    WorkflowSpec,
)


class InputField(BaseModel):
    name: str
    label: str = ""
    type: ValueType = ValueType.string
    required: bool = True
    default: Any = None


class StartConfig(BaseModel):
    inputs: list[InputField] = Field(default_factory=list)


class ScheduleTriggerConfig(BaseModel):
    timezone: str = "Asia/Tokyo"
    hour: int = Field(default=8, ge=0, le=23)
    minute: int = Field(default=0, ge=0, le=59)
    inputs: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"unknown IANA timezone: {value}") from error
        return value


class LLMConfig(BaseModel):
    system: str = "You are a helpful assistant."
    prompt: Any
    model: str | None = None
    structured_output: dict[str, Any] | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    seed: int | None = Field(default=None, ge=0)


class ClaudeAgentConfig(BaseModel):
    agent_id: str
    version: int | None = None
    task: Any


class ToolConfig(BaseModel):
    tool_name: str
    input: dict[str, Any] = Field(default_factory=dict)


class Condition(BaseModel):
    value: Any
    operator: Literal[
        "equals", "not_equals", "contains", "not_contains", "gt", "gte", "lt", "lte", "exists", "empty"
    ] = "equals"
    expected: Any = None


class IfCase(BaseModel):
    id: str
    conditions: list[Condition]
    logical_operator: Literal["and", "or"] = "and"


class IfElseConfig(BaseModel):
    cases: list[IfCase]
    default_branch: str = "else"


class ClassifierConfig(BaseModel):
    input: Any
    classes: list[str] = Field(min_length=2)
    instruction: str = "Choose exactly one class."
    model: str | None = None


class ExtractField(BaseModel):
    name: str
    type: ValueType = ValueType.string
    description: str = ""
    required: bool = True


class ParameterExtractorConfig(BaseModel):
    input: Any
    fields: list[ExtractField] = Field(min_length=1)
    instruction: str = "Extract the requested fields and return JSON."
    model: str | None = None


class TemplateConfig(BaseModel):
    template: str
    variables: dict[str, Any] = Field(default_factory=dict)


class VariableAssignerConfig(BaseModel):
    assignments: dict[str, Any] = Field(default_factory=dict)


class VariableAggregatorConfig(BaseModel):
    variables: list[Any] = Field(min_length=1)
    mode: Literal["first_non_null", "array", "merge"] = "first_non_null"


class HTTPConfig(BaseModel):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    url: Any
    headers: dict[str, Any] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    body: Any = None
    timeout_seconds: float = Field(default=30, ge=1, le=300)


class IterationConfig(BaseModel):
    items: Any
    workflow: WorkflowSpec
    item_name: str = "item"
    output_node_id: str
    output_path: list[str] = Field(default_factory=list)
    parallelism: int = Field(default=4, ge=1, le=20)


class LoopConfig(BaseModel):
    workflow: WorkflowSpec
    variables: dict[str, Any] = Field(default_factory=dict)
    break_condition: Condition
    break_value: Any
    max_iterations: int = Field(default=10, ge=1, le=100)
    output_node_id: str


class HumanField(BaseModel):
    name: str
    label: str
    type: ValueType = ValueType.string
    required: bool = True
    options: list[str] = Field(default_factory=list)


class HumanInputConfig(BaseModel):
    title: str = "Input required"
    description: str = ""
    fields: list[HumanField] = Field(min_length=1)


class EndConfig(BaseModel):
    outputs: dict[str, Any] = Field(default_factory=dict)


class AnswerConfig(BaseModel):
    answer: Any


class AgentArchitectureConfig(BaseModel):
    input: Any = None
    settings: dict[str, Any] = Field(default_factory=dict)


_ZH_CATEGORIES = {
    "input": "输入",
    "model": "模型",
    "agent": "智能体",
    "logic": "逻辑",
    "transform": "转换",
    "integration": "集成",
    "output": "输出",
}

_ZH_BLOCKS = {
    "start": ("用户输入", "声明工作流输入。"),
    "schedule_trigger": ("定时触发", "按 IANA 时区每天启动已发布工作流。"),
    "llm": ("LLM", "执行一次供应商无关的模型调用。"),
    "claude_agent": ("Claude 智能体", "运行完整的 Claude 风格 agent loop。"),
    "tool": ("工具", "调用一个已注册的核心、MCP 或工作流工具。"),
    "if_else": ("If / Else", "用确定性条件进行分支路由。"),
    "question_classifier": ("问题分类器", "把自由文本路由到指定类别。"),
    "parameter_extractor": ("参数提取器", "从文本中提取类型化 JSON 字段。"),
    "template_transform": ("模板转换", "用变量渲染模板。"),
    "variable_assigner": ("变量赋值", "创建命名工作流变量。"),
    "variable_aggregator": ("变量聚合", "合并分支或多个上游值。"),
    "http_request": ("HTTP 请求", "调用外部 HTTP 接口。"),
    "iteration": ("迭代", "对数组中的每一项运行嵌套工作流。"),
    "loop": ("循环", "重复运行嵌套工作流直到满足退出条件。"),
    "human_input": ("人工输入", "持久化暂停，并通过表单恢复。"),
    "end": ("结束", "返回命名工作流输出。"),
    "answer": ("回答", "返回聊天式答案。"),
    "context_assembler": ("上下文组装器", "把输入、节点输出和片段组装成模型上下文。"),
    "workspace_context_injector": ("工作区上下文注入", "把工作区路径、文件提示和范围注入上下文。"),
    "conversation_memory": ("对话记忆", "维护可传递的对话事实与消息摘要。"),
    "context_compactor": ("上下文压缩", "压缩长上下文并保留关键事实。"),
    "model_turn": ("模型轮次", "执行一次可观测的模型推理轮次。"),
    "tool_call_router": ("工具调用路由", "把模型产生的工具意图路由到可执行工具。"),
    "stop_continue_controller": ("停止/继续控制", "根据停止原因决定终止或进入下一轮。"),
    "retry_error_classifier": ("重试/错误分类", "把错误分类为可重试、权限、工具或致命错误。"),
    "tool_executor": ("工具执行器", "执行注册工具并返回标准化结果。"),
    "tool_result_normalizer": ("工具结果标准化", "把工具输出解析为稳定 JSON 或文本结构。"),
    "permission_gate": ("权限门", "在敏感动作前暂停、请求批准并恢复。"),
    "sandbox_boundary": ("沙箱边界", "声明工作区和网络边界。"),
    "skill_loader": ("Skill 加载器", "把 Skill 指令加载成能力上下文。"),
    "mcp_gateway": ("MCP 网关", "声明 MCP 服务器与工具能力入口。"),
    "capability_registry": ("能力注册表", "汇总工具、Skill、MCP 和子图能力。"),
    "subagent_spawn": ("子智能体启动", "为子任务创建独立上下文和预算描述。"),
    "task_dispatcher": ("任务分派", "按依赖和 owner 分派任务。"),
    "mailbox_wait_wake": ("Mailbox 等待/唤醒", "持久化等待消息并在收到消息后继续。"),
    "dependency_gate": ("依赖门", "等待上游任务完成后放行。"),
    "budget_gate": ("预算门", "根据成本或 token 预算放行/停止。"),
    "round_limit": ("轮次限制", "限制 agent loop 最大轮次。"),
    "cancellation_point": ("取消点", "提供可观测的取消检查点。"),
    "checkpoint_resume": ("检查点/恢复", "记录可恢复状态。"),
    "event_recorder": ("事件记录器", "向 Trace 写入结构化事件。"),
    "hook_point": ("钩子点", "在工作流中插入可被外部系统监听的钩子。"),
}


_AGENT_ARCHITECTURE_BLOCKS: list[tuple[str, str, str, str]] = [
    ("context_assembler", "Context Assembler", "Compose inputs, prior node outputs, and fragments into model-ready context.", "Context assembly"),
    ("workspace_context_injector", "File/Workspace Context Injector", "Attach workspace scope, file hints, and repository facts to context.", "Workspace context injection"),
    ("conversation_memory", "Conversation Memory", "Carry conversation facts and compact message history between turns.", "Conversation memory"),
    ("context_compactor", "Context Compactor", "Compact long context while preserving decisions and tool evidence.", "Auto-compaction"),
    ("model_turn", "Model Turn", "Run one observable model turn without hiding the surrounding loop.", "Model sampling turn"),
    ("tool_call_router", "Tool Call Router", "Route tool-use intents to executable tool or workflow capabilities.", "Tool-use routing"),
    ("stop_continue_controller", "Stop/Continue Controller", "Decide whether an agent loop should stop or continue after a turn.", "Loop continuation control"),
    ("retry_error_classifier", "Retry / Error Classifier", "Classify errors for retry, permission, tool, or fatal handling.", "Error recovery"),
    ("tool_executor", "Tool Executor", "Execute one registered tool with workflow context and trace events.", "Tool execution"),
    ("tool_result_normalizer", "Tool Result Normalizer", "Normalize raw tool output into stable structured context.", "Tool-result feedback"),
    ("permission_gate", "Permission Gate", "Pause for approval before a sensitive step and resume with a decision.", "Permission gating"),
    ("sandbox_boundary", "Sandbox Boundary", "Declare workspace, filesystem, and network execution boundaries.", "Sandbox isolation"),
    ("skill_loader", "Skill Loader", "Load named skills and instructions into a capability context.", "Skill loading"),
    ("mcp_gateway", "MCP Gateway", "Expose MCP servers and tool surfaces as workflow capabilities.", "MCP tool bridge"),
    ("capability_registry", "Capability Registry", "Collect tools, skills, MCP servers, and workflow tools into one registry.", "Capability discovery"),
    ("subagent_spawn", "Subagent Spawn", "Create a subagent work package with independent context, tools, and budget.", "Sub-agent spawning"),
    ("task_dispatcher", "Task Dispatcher", "Assign tasks by owner and dependency state.", "Task dispatch"),
    ("mailbox_wait_wake", "Mailbox Wait/Wake", "Persist mailbox waits and wake execution when messages arrive.", "Mailbox coordination"),
    ("dependency_gate", "Dependency Gate", "Block until declared dependencies are completed.", "Task dependency gate"),
    ("budget_gate", "Budget Gate", "Stop or continue based on token/cost budgets.", "Budget control"),
    ("round_limit", "Round Limit", "Enforce maximum loop rounds.", "Round limit"),
    ("cancellation_point", "Cancellation Point", "Record a cancellable execution checkpoint.", "Cancellation handling"),
    ("checkpoint_resume", "Checkpoint / Resume", "Persist resumable state for later recovery.", "Session recovery"),
    ("event_recorder", "Event Recorder", "Write structured trace events for observability.", "Telemetry and trace"),
    ("hook_point", "Hook Point", "Expose a named hook for external systems to observe or intercept.", "External hook / plugin"),
]


def _manual(
    block_type: str,
    title: str,
    summary: str,
    mapping: str,
    *,
    legacy: bool = False,
) -> dict[str, Any]:
    if legacy:
        return {
            "summary": "Compatibility wrapper for old drafts. Prefer composing explicit agent architecture blocks.",
            "when_to_use": ["Only load old drafts or migrate an existing opaque agent node."],
            "examples": [{"description": "Legacy draft compatibility", "connection": "start -> claude_agent -> end"}],
            "anti_patterns": [
                "Do not use as the default Builder Team choice for new Claude-like agents.",
                "Do not hide search, permissions, context, tools, or budgets inside this node when explicit blocks exist.",
            ],
            "common_errors": [
                "New workflows pass tests structurally but hide behavior inside a legacy macro.",
                "The agent binding is missing from the draft agents map.",
            ],
            "claude_architecture_mapping": "Macro placeholder for the full Claude-like loop.",
            "composability_constraints": ["Long-term target is expand-to-template, not opaque execution."],
        }
    return {
        "summary": summary,
        "when_to_use": [
            f"Use {title} when a workflow needs the {mapping.lower()} mechanism as an explicit step.",
            "Use it when tests, humans, or Builder Team need to inspect or replace this runtime capability.",
        ],
        "examples": [
            {
                "description": f"Use {title} as one visible runtime step.",
                "connection": f"... -> {block_type} -> ...",
                "config": {"input": {"$ref": {"node_id": "<upstream>", "path": ["output"]}}, "settings": {}},
            }
        ],
        "anti_patterns": [
            "Do not use this block as decoration without connecting its output.",
            "Do not bypass the manual and emit a whole graph JSON in one step.",
        ],
        "common_errors": [
            "Input references point to a skipped or missing upstream node.",
            "Settings are shaped like prose instead of the config schema.",
            "The block is connected but its output is not consumed by a downstream step or test.",
        ],
        "claude_architecture_mapping": mapping,
        "composability_constraints": [
            "Keep each block responsible for one runtime mechanism.",
            "Use nested WorkflowSpec subgraphs when a loop would crowd the main canvas.",
        ],
    }


class BlockRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, BlockDefinition] = {}
        self._config_models: dict[str, type[BaseModel]] = {}

    def register(self, definition: BlockDefinition, config_model: type[BaseModel]) -> None:
        if definition.type in self._definitions:
            raise ValueError(f"duplicate block type: {definition.type}")
        self._definitions[definition.type] = definition
        self._config_models[definition.type] = config_model

    def list(self) -> list[BlockDefinition]:
        return sorted(self._definitions.values(), key=lambda item: (item.block_kind, item.category, item.title))

    def get(self, block_type: str) -> BlockDefinition:
        try:
            return self._definitions[block_type]
        except KeyError as error:
            raise KeyError(f"unknown block type: {block_type}") from error

    def manual(self, block_type: str) -> dict[str, Any]:
        definition = self.get(block_type)
        return {
            "type": definition.type,
            "title": definition.title,
            "description": definition.description,
            "category": definition.category,
            "block_kind": definition.block_kind,
            "summary": definition.manual_summary,
            "when_to_use": definition.when_to_use,
            "input_ports": [port.model_dump(mode="json") for port in definition.input_ports],
            "output_ports": [port.model_dump(mode="json") for port in definition.output_ports],
            "config_schema": definition.config_schema,
            "examples": definition.examples,
            "anti_patterns": definition.anti_patterns,
            "common_errors": definition.common_errors,
            "claude_architecture_mapping": definition.claude_architecture_mapping,
            "composability_constraints": definition.composability_constraints,
        }

    def manuals(self, query: str = "", *, block_kind: str | None = None) -> list[dict[str, Any]]:
        needle = query.casefold().strip()
        result = []
        for definition in self.list():
            if block_kind and definition.block_kind != block_kind:
                continue
            searchable = " ".join(
                [
                    definition.type,
                    definition.title,
                    definition.description,
                    definition.category,
                    definition.block_kind,
                    definition.manual_summary,
                    " ".join(definition.when_to_use),
                    definition.claude_architecture_mapping or "",
                    " ".join(definition.anti_patterns),
                    " ".join(definition.common_errors),
                    " ".join(definition.composability_constraints),
                ]
            ).casefold()
            if needle and needle not in searchable:
                continue
            result.append(self.manual(definition.type))
        return result

    def claude_architecture_blueprint(self) -> dict[str, Any]:
        groups = {
            "context": ["context_assembler", "workspace_context_injector", "conversation_memory", "context_compactor"],
            "model_loop": ["model_turn", "tool_call_router", "stop_continue_controller", "retry_error_classifier"],
            "tools": ["tool_executor", "tool_result_normalizer", "permission_gate", "sandbox_boundary"],
            "skill_mcp": ["skill_loader", "mcp_gateway", "capability_registry"],
            "multi_agent": ["subagent_spawn", "task_dispatcher", "mailbox_wait_wake", "dependency_gate"],
            "governance": ["budget_gate", "round_limit", "cancellation_point", "checkpoint_resume", "event_recorder"],
        }
        return {
            "goal": "Compose Claude Code-like agent runtime behavior from explicit executable blocks.",
            "groups": {
                group: [self.manual(block_type) for block_type in block_types]
                for group, block_types in groups.items()
            },
            "legacy_macro": self.manual("claude_agent"),
        }

    def template_names(self) -> list[str]:
        """Deprecated. Use TemplateStore.list() instead."""
        return ["claude_like_coding_agent"]

    def expand_template(
        self,
        template_name: str,
        *,
        prefix: str = "claude",
        x: float = 0,
        y: float = 0,
    ) -> WorkflowSpec:
        """Deprecated. Use TemplateStore.expand_into_workflow() instead.

        Fallback path — raises if the template is not the legacy hardcoded one.
        """
        if template_name != "claude_like_coding_agent":
            raise KeyError(
                f"unknown workflow template: {template_name}. "
                f"Use TemplateStore.expand_into_workflow() for JSON-backed templates."
            )
        raise RuntimeError(
            "Legacy hardcoded template has been removed. "
            "The claude_like_coding_agent template is now at templates/claude_like_coding_agent.json. "
            "Use TemplateStore.expand_into_workflow() instead."
        )

    def validate_node(self, node: NodeSpec) -> BaseModel:
        try:
            definition = self.get(node.type)
        except KeyError:
            known = sorted(self._blocks.keys())
            similar = [b for b in known if node.type.casefold() in b.casefold() or b.casefold() in node.type.casefold()]
            hint = f" Did you mean: {similar[:5]}?" if similar else f" Available blocks: {known[:15]}..."
            raise KeyError(f"unknown block type: {node.type}.{hint}") from None
        if not definition.available:
            raise ValueError(f"block is not available: {node.type}")
        if node.block_version != definition.version:
            raise ValueError(
                f"unsupported block version for {node.type}: {node.block_version}, expected {definition.version}"
            )
        return self._config_models[node.type].model_validate(node.config)

    def validate_workflow(self, workflow: WorkflowSpec, *, nested: bool = False) -> list[str]:
        errors: list[str] = []
        node_map = {node.id: node for node in workflow.nodes}
        for node in workflow.nodes:
            try:
                config = self.validate_node(node)
                if node.type in {"iteration", "loop"}:
                    errors.extend(
                        f"{node.id}.{item}" for item in self.validate_workflow(config.workflow, nested=True)  # type: ignore[attr-defined]
                    )
            except Exception as error:
                errors.append(f"{node.id}: {error}")

        starts = [node for node in workflow.nodes if node.type in {"start", "schedule_trigger"}]
        terminals = [node for node in workflow.nodes if node.type in {"end", "answer"}]
        if len(starts) != 1:
            errors.append("workflow must contain exactly one start or schedule_trigger node")
        if not terminals:
            errors.append("workflow must contain at least one end or answer node")

        errors.extend(self._validate_edges(workflow, node_map))
        errors.extend(self._validate_graph_shape(workflow, starts))
        return errors

    def _validate_edges(self, workflow: WorkflowSpec, node_map: dict[str, NodeSpec]) -> list[str]:
        errors: list[str] = []
        for edge in workflow.edges:
            source = node_map.get(edge.source)
            target = node_map.get(edge.target)
            if not source or not target:
                continue
            source_def, target_def = self.get(source.type), self.get(target.type)
            source_port = self._port(source_def.output_ports, edge.source_port)
            target_port = self._port(target_def.input_ports, edge.target_port)
            if source_port is None:
                errors.append(f"{edge.id}: unknown source port {source.type}.{edge.source_port}")
            if target_port is None:
                errors.append(f"{edge.id}: unknown target port {target.type}.{edge.target_port}")
            if source_port and target_port and not self._compatible(source_port.value_type, target_port.value_type):
                errors.append(
                    f"{edge.id}: incompatible ports {source_port.value_type.value} -> {target_port.value_type.value}"
                )
        return errors

    def _validate_graph_shape(self, workflow: WorkflowSpec, starts: list[NodeSpec]) -> list[str]:
        errors: list[str] = []
        outgoing: dict[str, list[str]] = defaultdict(list)
        indegree = {node.id: 0 for node in workflow.nodes}
        for edge in workflow.edges:
            if edge.source in indegree and edge.target in indegree:
                outgoing[edge.source].append(edge.target)
                indegree[edge.target] += 1
        queue = deque(node_id for node_id, degree in indegree.items() if degree == 0)
        visited: list[str] = []
        while queue:
            current = queue.popleft()
            visited.append(current)
            for target in outgoing[current]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        if len(visited) != len(workflow.nodes):
            errors.append("workflow graph contains a cycle; use an explicit loop block")
        if starts:
            reachable = {starts[0].id}
            pending = [starts[0].id]
            while pending:
                for target in outgoing[pending.pop()]:
                    if target not in reachable:
                        reachable.add(target)
                        pending.append(target)
            unreachable = set(indegree) - reachable
            if unreachable:
                errors.append(f"unreachable nodes: {sorted(unreachable)}")
        return errors

    @staticmethod
    def _port(ports: list[PortDefinition], name: str) -> PortDefinition | None:
        return next((port for port in ports if port.name == name), None)

    @staticmethod
    def _compatible(source: ValueType, target: ValueType) -> bool:
        return source == ValueType.any or target == ValueType.any or source == target


def _definition(
    block_type: str,
    title: str,
    description: str,
    category: Literal["input", "model", "agent", "logic", "transform", "integration", "output"],
    config: type[BaseModel],
    *,
    inputs: list[tuple[str, ValueType]] = [],
    outputs: list[tuple[str, ValueType]] = [("output", ValueType.any)],
    retry: bool = False,
    error_branch: bool = False,
    block_kind: Literal["business_workflow", "agent_architecture", "legacy_compatibility"] = "business_workflow",
    manual: dict[str, Any] | None = None,
    available: bool = True,
    family: str | None = None,
) -> BlockDefinition:
    manual = manual or _manual(block_type, title, description, "Business workflow primitive")
    editor: dict[str, Any] = {
        "icon": block_type,
        "accent": category,
        "hidden_by_default": block_kind == "legacy_compatibility",
        "block_kind": block_kind,
        "i18n": {
            "zh": {
                "title": _ZH_BLOCKS.get(block_type, (block_type, description))[0],
                "description": _ZH_BLOCKS.get(block_type, (block_type, description))[1],
                "category": _ZH_CATEGORIES[category],
            },
            "en": {
                "title": title,
                "description": description,
                "category": category,
            },
        },
    }
    if family is not None:
        editor["family"] = family
    return BlockDefinition(
        type=block_type,
        title=title,
        description=description,
        category=category,
        block_kind=block_kind,
        config_schema=config.model_json_schema(),
        input_ports=[PortDefinition(name=name, value_type=value_type) for name, value_type in inputs],
        output_ports=[PortDefinition(name=name, value_type=value_type) for name, value_type in outputs],
        supports_retry=retry,
        supports_error_branch=error_branch,
        available=available,
        manual_summary=str(manual["summary"]),
        when_to_use=list(manual["when_to_use"]),
        examples=list(manual["examples"]),
        anti_patterns=list(manual["anti_patterns"]),
        common_errors=list(manual["common_errors"]),
        claude_architecture_mapping=str(manual["claude_architecture_mapping"]),
        composability_constraints=list(manual["composability_constraints"]),
        editor=editor,
    )


def build_block_registry() -> BlockRegistry:
    registry = BlockRegistry()
    blocks: list[tuple[BlockDefinition, type[BaseModel]]] = [
        (_definition("start", "User Input", "Declare workflow inputs.", "input", StartConfig, outputs=[("output", ValueType.any)]), StartConfig),
        (_definition("schedule_trigger", "Schedule Trigger", "Start a published workflow on an IANA-timezone daily schedule.", "input", ScheduleTriggerConfig, outputs=[("output", ValueType.any)]), ScheduleTriggerConfig),
        (_definition("llm", "LLM", "Make one provider-neutral model call.", "model", LLMConfig, inputs=[("input", ValueType.any)], outputs=[("text", ValueType.string), ("structured", ValueType.object)], retry=True, error_branch=True), LLMConfig),
        (_definition(
            "claude_agent",
            "Claude Agent (Legacy)",
            "Compatibility wrapper for old drafts that run the complete Claude-style agent loop.",
            "agent",
            ClaudeAgentConfig,
            inputs=[("input", ValueType.any)],
            outputs=[("text", ValueType.string)],
            retry=True,
            error_branch=True,
            block_kind="legacy_compatibility",
            manual=_manual("claude_agent", "Claude Agent (Legacy)", "", "", legacy=True),
        ), ClaudeAgentConfig),
        (_definition("tool", "Tool", "Call one registered core, MCP, or workflow tool.", "integration", ToolConfig, inputs=[("input", ValueType.any)], retry=True, error_branch=True), ToolConfig),
        (_definition("if_else", "If / Else", "Route with deterministic conditions.", "logic", IfElseConfig, inputs=[("input", ValueType.any)], outputs=[("branch", ValueType.string)]), IfElseConfig),
        (_definition("question_classifier", "Question Classifier", "Route free text into a named class.", "logic", ClassifierConfig, inputs=[("input", ValueType.any)], outputs=[("branch", ValueType.string), ("text", ValueType.string)], retry=True, error_branch=True), ClassifierConfig),
        (_definition("parameter_extractor", "Parameter Extractor", "Extract typed JSON fields from text.", "transform", ParameterExtractorConfig, inputs=[("input", ValueType.any)], outputs=[("structured", ValueType.object)], retry=True, error_branch=True), ParameterExtractorConfig),
        (_definition("template_transform", "Template Transform", "Render a template from variables.", "transform", TemplateConfig, inputs=[("input", ValueType.any)], outputs=[("text", ValueType.string)]), TemplateConfig),
        (_definition("variable_assigner", "Variable Assigner", "Create named workflow values.", "transform", VariableAssignerConfig, inputs=[("input", ValueType.any)], outputs=[("output", ValueType.object)]), VariableAssignerConfig),
        (_definition("variable_aggregator", "Variable Aggregator", "Join branch values.", "transform", VariableAggregatorConfig, inputs=[("input", ValueType.any)], outputs=[("output", ValueType.any)]), VariableAggregatorConfig),
        (_definition("http_request", "HTTP Request", "Call an external HTTP endpoint.", "integration", HTTPConfig, inputs=[("input", ValueType.any)], outputs=[("output", ValueType.object)], retry=True, error_branch=True), HTTPConfig),
        (_definition("iteration", "Iteration", "Run a nested workflow for each array item.", "logic", IterationConfig, inputs=[("input", ValueType.array)], outputs=[("items", ValueType.array)], retry=True, error_branch=True), IterationConfig),
        (_definition("loop", "Loop", "Run a nested workflow until a condition matches.", "logic", LoopConfig, inputs=[("input", ValueType.any)], outputs=[("output", ValueType.object)], retry=True, error_branch=True), LoopConfig),
        (_definition("human_input", "Human Input", "Pause and resume with a typed form.", "input", HumanInputConfig, inputs=[("input", ValueType.any)], outputs=[("output", ValueType.object)]), HumanInputConfig),
        (_definition("end", "End", "Return named workflow outputs.", "output", EndConfig, inputs=[("input", ValueType.any)], outputs=[]), EndConfig),
        (_definition("answer", "Answer", "Return a chat answer.", "output", AnswerConfig, inputs=[("input", ValueType.any)], outputs=[]), AnswerConfig),
    ]
    from .block_families import get_family
    for block_type, title, description, mapping in _AGENT_ARCHITECTURE_BLOCKS:
        blocks.append((
            _definition(
                block_type, title, description, "agent", AgentArchitectureConfig,
                inputs=[("input", ValueType.any)],
                outputs=[("output", ValueType.any), ("state", ValueType.object)],
                retry=block_type in {"model_turn", "tool_executor", "mcp_gateway"},
                error_branch=True,
                block_kind="agent_architecture",
                manual=_manual(block_type, title, description, mapping),
                family=get_family(block_type),
            ),
            AgentArchitectureConfig,
        ))
    for definition, model in blocks:
        registry.register(definition, model)
    return registry


# ── Previously: _arch_config(), _ref(), _claude_like_coding_agent_template() ──
# These 206 lines were migrated to templates/claude_like_coding_agent.json
# (2026-07-14). The data-driven JSON template is loaded by TemplateStore and
# expanded via TemplateStore.expand_into_workflow().
#
# expand_template() and template_names() below are kept as deprecated fallbacks
# for when TemplateStore is not available.


def edge_by_id(workflow: WorkflowSpec, edge_id: str) -> EdgeSpec:
    try:
        return next(edge for edge in workflow.edges if edge.id == edge_id)
    except StopIteration as error:
        raise KeyError(f"edge not found: {edge_id}") from error
