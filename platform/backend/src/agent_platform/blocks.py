from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator

from .cluster_blocks import (
    ClusterAcquireConfig, ClusterDiscoverConfig, ClusterPublishConfig,
    ClusterRegisterConfig, ClusterReleaseConfig, ClusterSubscribeConfig,
    _CLUSTER_BLOCKS, _CLUSTER_EDITOR_FIELDS, _CLUSTER_MANUALS, _ZH_CLUSTER_BLOCKS,
)
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
    durable: bool = False
    max_attempts: int = Field(default=3, ge=1, le=20)
    retry_backoff_seconds: float = Field(default=5, ge=0, le=86_400)
    lease_seconds: float = Field(default=60, ge=1, le=86_400)

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


class WebCollectionConfig(BaseModel):
    sources: Any
    allowed_hosts: list[str] = Field(min_length=1, max_length=100)
    permission_basis: str = Field(min_length=3, max_length=500)
    user_agent: str = Field(default="LiliesControlledCollector/0.4", min_length=3, max_length=200)
    respect_robots: bool = True
    robots_failure_policy: Literal["deny", "allow_with_receipt"] = "deny"
    timeout_seconds: float = Field(default=20, ge=1, le=300)
    max_content_bytes: int = Field(default=1_000_000, ge=1_024, le=20_000_000)
    max_sources: int = Field(default=20, ge=1, le=200)
    fail_on_source_error: bool = False


class CollectionDigestConfig(BaseModel):
    collection: Any
    topic: Any = "Daily collection"
    include_unchanged: bool = False
    max_items: int = Field(default=20, ge=1, le=100)


class ConnectorActionConfig(BaseModel):
    connector_id: str = Field(min_length=2, max_length=120)
    connector_version: int = Field(default=1, ge=1)
    operation_id: str = Field(min_length=2, max_length=120)
    tenant_id: Any
    actor_id: Any
    actor_roles: Any
    profile_id: Any
    payload: Any
    idempotency_key: Any
    authorization_id: Any = ""
    execution_mode: Any = "dry_run"


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
    initial_state: Any = None
    state_input_name: str = Field(default="loop_state", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    state_update: Any = None
    feedback_input_name: str = Field(default="tool_feedback", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    feedback_value: Any = None
    break_condition: Condition
    break_value: Any
    cancel_condition: Condition | None = None
    cancel_value: Any = None
    max_iterations: int = Field(default=10, ge=1, le=100)
    output_node_id: str
    checkpoint_each_iteration: bool = False


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


class ModelTurnConfig(AgentArchitectureConfig):
    @field_validator("settings")
    @classmethod
    def validate_model_turn_settings(cls, value: dict[str, Any]) -> dict[str, Any]:
        for key in ("system", "system_prompt", "model", "output_format"):
            if key in value and not isinstance(value[key], str):
                raise ValueError(f"model_turn.settings.{key} must be a string")
        if value.get("output_format") not in {None, "", "text", "json"}:
            raise ValueError("model_turn.settings.output_format must be text or json")
        tools = value.get("tools")
        if tools is not None and (
            not isinstance(tools, list) or any(not isinstance(item, str) for item in tools)
        ):
            raise ValueError("model_turn.settings.tools must be an array of tool names")
        return value


class ToolExecutorConfig(AgentArchitectureConfig):
    @field_validator("settings")
    @classmethod
    def validate_tool_executor_settings(cls, value: dict[str, Any]) -> dict[str, Any]:
        if "tool_name" in value and value["tool_name"] is not None and not isinstance(value["tool_name"], str):
            raise ValueError("tool_executor.settings.tool_name must be a string")
        if "tool_input" in value and not isinstance(value["tool_input"], dict):
            raise ValueError("tool_executor.settings.tool_input must be an object")
        workspace_path = value.get("workspace_path")
        if workspace_path is not None and not (
            isinstance(workspace_path, str)
            or (
                isinstance(workspace_path, dict)
                and isinstance(workspace_path.get("$ref"), dict)
            )
        ):
            raise ValueError("tool_executor.settings.workspace_path must be a string or workflow reference")
        return value


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
    "web_collection": ("受控网页采集", "按允许来源和 robots 策略采集内容并保存来源证据。"),
    "collection_digest": ("采集摘要", "把采集结果整理为带来源和状态的可读摘要。"),
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
# Merge cluster block ZH names
_ZH_BLOCKS.update(_ZH_CLUSTER_BLOCKS)


_EDITOR_FIELDS: dict[str, list[dict[str, Any]]] = {
    "schedule_trigger": [
        {"path": "timezone", "label": "Timezone", "label_zh": "时区", "control": "text", "description": "IANA timezone such as Asia/Tokyo.", "required": True},
        {"path": "hour", "label": "Hour", "label_zh": "小时", "control": "number", "minimum": 0, "maximum": 23, "step": 1, "required": True},
        {"path": "minute", "label": "Minute", "label_zh": "分钟", "control": "number", "minimum": 0, "maximum": 59, "step": 1, "required": True},
        {"path": "inputs", "label": "Scheduled inputs", "label_zh": "定时输入", "control": "json"},
        {"path": "durable", "label": "Durable execution", "label_zh": "耐久执行", "control": "boolean", "description": "Persist fire identity, attempts, recovery, cancellation, and history."},
        {"path": "max_attempts", "label": "Maximum attempts", "label_zh": "最大尝试次数", "control": "number", "minimum": 1, "maximum": 20, "step": 1},
        {"path": "retry_backoff_seconds", "label": "Retry backoff", "label_zh": "重试退避秒数", "control": "number", "minimum": 0, "maximum": 86400, "step": 1},
        {"path": "lease_seconds", "label": "Worker lease", "label_zh": "工作租约秒数", "control": "number", "minimum": 1, "maximum": 86400, "step": 1},
    ],
    "llm": [
        {"path": "system", "label": "System instruction", "label_zh": "系统指令", "control": "textarea", "description": "Persistent instruction for this model call.", "required": True},
        {"path": "prompt", "label": "Prompt", "label_zh": "用户提示", "control": "reference_or_text", "description": "Text or a workflow value reference.", "required": True},
        {"path": "model", "label": "Model override", "label_zh": "模型覆盖", "control": "text", "description": "Leave empty to use the runtime default."},
        {"path": "temperature", "label": "Temperature", "label_zh": "温度", "control": "number", "minimum": 0, "maximum": 2, "step": 0.1},
        {"path": "seed", "label": "Seed", "label_zh": "随机种子", "control": "number", "minimum": 0, "step": 1},
        {"path": "structured_output", "label": "Structured output schema", "label_zh": "结构化输出 Schema", "control": "json", "description": "Optional JSON schema for structured output."},
    ],
    "model_turn": [
        {"path": "settings.system", "label": "System instruction", "label_zh": "系统指令", "control": "textarea", "description": "Instruction applied to this observable model turn."},
        {"path": "settings.prompt", "label": "Prompt", "label_zh": "用户提示", "control": "reference_or_text", "description": "Text or a workflow value reference."},
        {"path": "settings.model", "label": "Model override", "label_zh": "模型覆盖", "control": "text"},
        {"path": "settings.tools", "label": "Available tools", "label_zh": "可用工具", "control": "string_list", "description": "One registered tool name per line."},
        {"path": "settings.output_format", "label": "Output format", "label_zh": "输出格式", "control": "enum", "options": ["text", "json"]},
    ],
    "http_request": [
        {"path": "method", "label": "HTTP method", "label_zh": "HTTP 方法", "control": "enum", "options": ["GET", "POST", "PUT", "PATCH", "DELETE"], "required": True},
        {"path": "url", "label": "URL", "label_zh": "请求 URL", "control": "reference_or_text", "description": "Literal URL or a workflow value reference.", "required": True},
        {"path": "headers", "label": "Headers", "label_zh": "请求头", "control": "json"},
        {"path": "query", "label": "Query parameters", "label_zh": "查询参数", "control": "json"},
        {"path": "body", "label": "Request body", "label_zh": "请求体", "control": "json"},
        {"path": "timeout_seconds", "label": "Timeout (seconds)", "label_zh": "超时秒数", "control": "number", "minimum": 1, "maximum": 300, "step": 1, "required": True},
    ],
    "web_collection": [
        {"path": "sources", "label": "Sources", "label_zh": "来源", "control": "reference_or_text", "description": "Array of URL strings or source objects; workflow references are supported.", "required": True},
        {"path": "allowed_hosts", "label": "Allowed hosts", "label_zh": "允许主机", "control": "string_list", "description": "Exact hostnames that this block may request.", "required": True},
        {"path": "permission_basis", "label": "Permission basis", "label_zh": "访问依据", "control": "textarea", "description": "Operator-declared reason this access pattern is allowed.", "required": True},
        {"path": "respect_robots", "label": "Respect robots.txt", "label_zh": "遵守 robots.txt", "control": "boolean"},
        {"path": "robots_failure_policy", "label": "Robots failure policy", "label_zh": "robots 失败策略", "control": "enum", "options": ["deny", "allow_with_receipt"]},
        {"path": "timeout_seconds", "label": "Timeout", "label_zh": "超时秒数", "control": "number", "minimum": 1, "maximum": 300, "step": 1},
        {"path": "max_content_bytes", "label": "Maximum response bytes", "label_zh": "最大响应字节", "control": "number", "minimum": 1024, "maximum": 20000000, "step": 1024},
        {"path": "max_sources", "label": "Maximum sources", "label_zh": "最大来源数", "control": "number", "minimum": 1, "maximum": 200, "step": 1},
        {"path": "fail_on_source_error", "label": "Fail job on source error", "label_zh": "来源错误时任务失败", "control": "boolean"},
    ],
    "collection_digest": [
        {"path": "collection", "label": "Collection result", "label_zh": "采集结果", "control": "reference_or_text", "required": True},
        {"path": "topic", "label": "Digest topic", "label_zh": "摘要主题", "control": "reference_or_text"},
        {"path": "include_unchanged", "label": "Include unchanged sources", "label_zh": "包含未变化来源", "control": "boolean"},
        {"path": "max_items", "label": "Maximum digest items", "label_zh": "最大摘要条目", "control": "number", "minimum": 1, "maximum": 100, "step": 1},
    ],
    "tool": [
        {"path": "tool_name", "label": "Tool name", "label_zh": "工具名称", "control": "text", "description": "Registered core, MCP, or workflow tool name.", "required": True},
        {"path": "input", "label": "Tool input", "label_zh": "工具输入", "control": "json", "description": "JSON values and workflow references passed to the tool."},
    ],
    "tool_executor": [
        {"path": "settings.tool_name", "label": "Tool name", "label_zh": "工具名称", "control": "text", "description": "Leave empty when a Tool Call Router supplies the tool dynamically."},
        {"path": "settings.tool_input", "label": "Tool input", "label_zh": "工具输入", "control": "json"},
        {"path": "settings.workspace_path", "label": "Workspace path", "label_zh": "工作区路径", "control": "text"},
    ],
    "loop": [
        {"path": "initial_state", "label": "Initial loop state", "label_zh": "初始循环状态", "control": "json", "description": "State supplied to the first nested iteration."},
        {"path": "state_input_name", "label": "State input name", "label_zh": "状态输入名", "control": "text", "required": True},
        {"path": "state_update", "label": "Next state reference", "label_zh": "下一轮状态引用", "control": "reference_or_text", "description": "Nested output reference used as the next iteration state."},
        {"path": "feedback_input_name", "label": "Feedback input name", "label_zh": "反馈输入名", "control": "text", "required": True},
        {"path": "feedback_value", "label": "Tool feedback reference", "label_zh": "工具反馈引用", "control": "reference_or_text", "description": "Nested output reference fed into the next model decision."},
        {"path": "max_iterations", "label": "Maximum iterations", "label_zh": "最大循环次数", "control": "number", "minimum": 1, "maximum": 100, "step": 1, "required": True},
        {"path": "output_node_id", "label": "Output node", "label_zh": "输出积木 ID", "control": "text", "required": True},
        {"path": "break_condition.operator", "label": "Break operator", "label_zh": "退出判断", "control": "enum", "options": ["equals", "not_equals", "contains", "not_contains", "gt", "gte", "lt", "lte", "exists", "empty"], "required": True},
        {"path": "break_condition.expected", "label": "Expected break value", "label_zh": "预期退出值", "control": "reference_or_text"},
        {"path": "break_value", "label": "Observed break value", "label_zh": "实际判断值", "control": "reference_or_text", "required": True},
        {"path": "cancel_condition.operator", "label": "Cancel operator", "label_zh": "取消判断", "control": "enum", "options": ["equals", "not_equals", "contains", "not_contains", "gt", "gte", "lt", "lte", "exists", "empty"]},
        {"path": "cancel_condition.expected", "label": "Expected cancel value", "label_zh": "预期取消值", "control": "reference_or_text"},
        {"path": "cancel_value", "label": "Observed cancel value", "label_zh": "实际取消判断值", "control": "reference_or_text"},
        {"path": "variables", "label": "Loop variables", "label_zh": "循环变量", "control": "json"},
        {"path": "checkpoint_each_iteration", "label": "Checkpoint every iteration", "label_zh": "每轮保存检查点", "control": "boolean", "description": "Persist iteration state for inspection and recovery."},
    ],
}
# Merge cluster block editor fields
_EDITOR_FIELDS.update(_CLUSTER_EDITOR_FIELDS)


_EDITOR_NOTICES: dict[str, list[dict[str, str]]] = {
    "loop": [
        {
            "kind": "boundary",
            "text": "The Loop cancel condition stops at an iteration boundary; the run-level cancel action remains available at async node boundaries.",
            "text_zh": "Loop 取消条件在一轮结束时生效；运行级停止仍可在异步积木边界取消整次运行。",
        },
        {
            "kind": "expert",
            "text": "Edit the nested workflow in Expert JSON until nested-canvas editing is available.",
            "text_zh": "嵌套工作流暂时在专家 JSON 中编辑，后续再接入嵌套画布。",
        },
    ],
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
        return [
            "codex_like_workspace_agent",
            "claude_like_coding_agent",
            "daily_web_collection",
            "customer_system_embedding",
        ]

    def expand_template(
        self,
        template_name: str,
        *,
        prefix: str = "claude",
        x: float = 0,
        y: float = 0,
    ) -> WorkflowSpec:
        if template_name == "codex_like_workspace_agent":
            return _codex_like_workspace_agent_template(prefix=prefix, x=x, y=y)
        if template_name == "claude_like_coding_agent":
            return _claude_like_coding_agent_template(prefix=prefix, x=x, y=y)
        if template_name == "daily_web_collection":
            return _daily_web_collection_template(prefix=prefix, x=x, y=y)
        if template_name == "customer_system_embedding":
            return _customer_system_embedding_template(prefix=prefix, x=x, y=y)
        raise KeyError(f"unknown workflow template: {template_name}")

    def validate_node(self, node: NodeSpec) -> BaseModel:
        definition = self.get(node.type)
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
) -> BlockDefinition:
    manual = manual or _manual(block_type, title, description, "Business workflow primitive")
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
        editor={
            "icon": block_type,
            "accent": category,
            "hidden_by_default": block_kind == "legacy_compatibility",
            "block_kind": block_kind,
            "fields": _EDITOR_FIELDS.get(block_type, []),
            "notices": _EDITOR_NOTICES.get(block_type, []),
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
        },
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
        (_definition("web_collection", "Controlled Web Collection", "Collect approved Web sources with durable provenance and access receipts.", "integration", WebCollectionConfig, inputs=[("input", ValueType.any)], outputs=[("output", ValueType.object), ("items", ValueType.array), ("receipts", ValueType.array)], retry=True, error_branch=True), WebCollectionConfig),
        (_definition("collection_digest", "Collection Digest", "Render collected source results as a customer-readable Markdown digest.", "transform", CollectionDigestConfig, inputs=[("input", ValueType.any)], outputs=[("text", ValueType.string), ("summary", ValueType.object)]), CollectionDigestConfig),
        (_definition("connector_action", "Connector Action", "Execute a versioned tenant-scoped Connector operation through platform policy.", "integration", ConnectorActionConfig, inputs=[("input", ValueType.any)], outputs=[("output", ValueType.object), ("receipt", ValueType.object), ("response", ValueType.object)], retry=True, error_branch=True), ConnectorActionConfig),
        # ── Cluster coordination blocks ──────────────────────────
        (_definition("cluster_publish", "Cluster Publish", "Publish a structured message to a named topic with persistence, ordering, and idempotent delivery for multi-agent coordination.", "integration", ClusterPublishConfig, inputs=[("input", ValueType.any)], outputs=[("output", ValueType.object)], retry=True, error_branch=True, manual=_CLUSTER_MANUALS["cluster_publish"]), ClusterPublishConfig),
        (_definition("cluster_subscribe", "Cluster Subscribe", "Subscribe to a topic and receive new messages with ordered delivery. Supports blocking wait and non-blocking poll. Each subscriber tracks an independent cursor.", "integration", ClusterSubscribeConfig, inputs=[("input", ValueType.any)], outputs=[("output", ValueType.object), ("messages", ValueType.array)], retry=True, manual=_CLUSTER_MANUALS["cluster_subscribe"]), ClusterSubscribeConfig),
        (_definition("cluster_register", "Cluster Register", "Register agent capabilities (e.g. 'image_analysis','database_write') in the cluster registry so other agents can discover and coordinate.", "integration", ClusterRegisterConfig, inputs=[("input", ValueType.any)], outputs=[("output", ValueType.object)], manual=_CLUSTER_MANUALS["cluster_register"]), ClusterRegisterConfig),
        (_definition("cluster_discover", "Cluster Discover", "Query the cluster registry for active agents matching a capability keyword. Returns agent list for dynamic coordination (who can handle this task?).", "integration", ClusterDiscoverConfig, inputs=[("input", ValueType.any)], outputs=[("output", ValueType.object), ("agents", ValueType.array)], manual=_CLUSTER_MANUALS["cluster_discover"]), ClusterDiscoverConfig),
        (_definition("cluster_acquire", "Cluster Acquire", "Acquire a distributed lock (read or write) on a shared resource before modification. Auto-expires after TTL to prevent dead agents holding locks forever.", "integration", ClusterAcquireConfig, inputs=[("input", ValueType.any)], outputs=[("output", ValueType.object), ("acquired", ValueType.boolean)], retry=True, manual=_CLUSTER_MANUALS["cluster_acquire"]), ClusterAcquireConfig),
        (_definition("cluster_release", "Cluster Release", "Release a previously acquired resource lock. Only the original lock owner can release. Always pair with cluster_acquire in the same workflow path.", "integration", ClusterReleaseConfig, inputs=[("input", ValueType.any)], outputs=[("output", ValueType.object)], manual=_CLUSTER_MANUALS["cluster_release"]), ClusterReleaseConfig),
        (_definition("iteration", "Iteration", "Run a nested workflow for each array item.", "logic", IterationConfig, inputs=[("input", ValueType.array)], outputs=[("items", ValueType.array)], retry=True, error_branch=True), IterationConfig),
        (_definition("loop", "Loop", "Run a nested workflow until a condition matches.", "logic", LoopConfig, inputs=[("input", ValueType.any)], outputs=[("output", ValueType.object)], retry=True, error_branch=True), LoopConfig),
        (_definition("human_input", "Human Input", "Pause and resume with a typed form.", "input", HumanInputConfig, inputs=[("input", ValueType.any)], outputs=[("output", ValueType.object)]), HumanInputConfig),
        (_definition("end", "End", "Return named workflow outputs.", "output", EndConfig, inputs=[("input", ValueType.any)], outputs=[]), EndConfig),
        (_definition("answer", "Answer", "Return a chat answer.", "output", AnswerConfig, inputs=[("input", ValueType.any)], outputs=[]), AnswerConfig),
    ]
    for block_type, title, description, mapping in _AGENT_ARCHITECTURE_BLOCKS:
        config_model: type[BaseModel]
        if block_type == "model_turn":
            config_model = ModelTurnConfig
        elif block_type == "tool_executor":
            config_model = ToolExecutorConfig
        else:
            config_model = AgentArchitectureConfig
        blocks.append((
            _definition(
                block_type, title, description, "agent", config_model,
                inputs=[("input", ValueType.any)],
                outputs=[("output", ValueType.any), ("state", ValueType.object)],
                retry=block_type in {"model_turn", "tool_executor", "mcp_gateway"},
                error_branch=True,
                block_kind="agent_architecture",
                manual=_manual(block_type, title, description, mapping),
            ),
            config_model,
        ))
    for definition, model in blocks:
        registry.register(definition, model)
    return registry


def _arch_config(input_value: Any = None, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    config: dict[str, Any] = {"settings": settings or {}}
    if input_value is not None:
        config["input"] = input_value
    return config


def _ref(node_id: str, *path: str) -> dict[str, Any]:
    return {"$ref": {"node_id": node_id, "path": list(path)}}


def _optional_ref(node_id: str, *path: str) -> dict[str, Any]:
    return {"$ref": {"node_id": node_id, "path": list(path), "optional": True}}


def _daily_web_collection_template(*, prefix: str, x: float, y: float) -> WorkflowSpec:
    schedule_id = f"{prefix}_schedule"
    collect_id = f"{prefix}_collect"
    digest_id = f"{prefix}_digest"
    answer_id = f"{prefix}_answer"
    return WorkflowSpec(
        nodes=[
            NodeSpec(
                id=schedule_id,
                type="schedule_trigger",
                title="Daily Collection Schedule",
                description="Create one durable local-date job and preserve attempts and recovery history.",
                config={
                    "timezone": "Asia/Tokyo",
                    "hour": 8,
                    "minute": 0,
                    "inputs": {"topic": "Daily source digest", "sources": []},
                    "durable": True,
                    "max_attempts": 3,
                    "retry_backoff_seconds": 5,
                    "lease_seconds": 60,
                },
                position={"x": x, "y": y},
            ),
            NodeSpec(
                id=collect_id,
                type="web_collection",
                title="Collect Approved Sources",
                description="Enforce source policy and persist one provenance receipt per source.",
                config={
                    "sources": _ref(schedule_id, "sources"),
                    "allowed_hosts": ["127.0.0.1", "localhost"],
                    "permission_basis": (
                        "Controlled local contract fixture; replace with documented source permission."
                    ),
                    "respect_robots": True,
                    "robots_failure_policy": "deny",
                    "timeout_seconds": 20,
                    "max_content_bytes": 1_000_000,
                    "max_sources": 20,
                    "fail_on_source_error": False,
                },
                position={"x": x + 280, "y": y},
                retry={"enabled": True, "max_attempts": 2, "delay_seconds": 1},
                error_strategy="fail",
            ),
            NodeSpec(
                id=digest_id,
                type="collection_digest",
                title="Build Traceable Digest",
                description="Create readable Markdown with source citations and collection status.",
                config={
                    "collection": _ref(collect_id, "output"),
                    "topic": _ref(schedule_id, "topic"),
                    "include_unchanged": False,
                    "max_items": 20,
                },
                position={"x": x + 560, "y": y},
            ),
            NodeSpec(
                id=answer_id,
                type="answer",
                title="Daily Digest",
                description="Deliver the current digest in Customer Runtime.",
                config={"answer": _ref(digest_id, "text")},
                position={"x": x + 840, "y": y},
            ),
        ],
        edges=[
            EdgeSpec(
                id=f"{prefix}_schedule_to_collect",
                source=schedule_id,
                target=collect_id,
            ),
            EdgeSpec(
                id=f"{prefix}_collect_to_digest",
                source=collect_id,
                target=digest_id,
            ),
            EdgeSpec(
                id=f"{prefix}_digest_to_answer",
                source=digest_id,
                target=answer_id,
                source_port="text",
            ),
        ],
    )


def _customer_system_embedding_template(*, prefix: str, x: float, y: float) -> WorkflowSpec:
    start_id = f"{prefix}_request"
    read_payload_id = f"{prefix}_read_payload"
    read_id = f"{prefix}_read"
    decision_id = f"{prefix}_decision"
    write_payload_id = f"{prefix}_write_payload"
    write_id = f"{prefix}_writeback"
    answer_id = f"{prefix}_answer"
    return WorkflowSpec(
        nodes=[
            NodeSpec(
                id=start_id,
                type="start",
                title="Embedded Customer Request",
                description="Receive a tenant-scoped request resolved by signed embedding ingress.",
                config={
                    "inputs": [
                        {"name": "tenant_id", "label": "Tenant", "type": "string"},
                        {"name": "actor_id", "label": "Actor", "type": "string"},
                        {"name": "actor_roles", "label": "Roles", "type": "array"},
                        {"name": "request", "label": "业务请求", "type": "object"},
                        {
                            "name": "connector_profile_id",
                            "label": "Deployment profile",
                            "type": "string",
                            "default": "test",
                        },
                        {
                            "name": "connector_authorization_id",
                            "label": "Preauthorization",
                            "type": "string",
                            "required": False,
                            "default": "",
                        },
                        {
                            "name": "connector_idempotency_key",
                            "label": "Idempotency key",
                            "type": "string",
                        },
                        {
                            "name": "write_mode",
                            "label": "Write mode",
                            "type": "string",
                            "default": "dry_run",
                        },
                    ]
                },
                position={"x": x, "y": y},
            ),
            NodeSpec(
                id=read_payload_id,
                type="variable_assigner",
                title="Map Read Contract",
                description="Map the customer request into the Connector read schema.",
                config={
                    "assignments": {"case_id": _ref(start_id, "request", "case_id")}
                },
                position={"x": x + 260, "y": y},
            ),
            NodeSpec(
                id=read_id,
                type="connector_action",
                title="Read Tenant Context",
                description="Read through the versioned customer-system Connector contract.",
                config={
                    "connector_id": "customer_system",
                    "connector_version": 1,
                    "operation_id": "get_case",
                    "tenant_id": _ref(start_id, "tenant_id"),
                    "actor_id": _ref(start_id, "actor_id"),
                    "actor_roles": _ref(start_id, "actor_roles"),
                    "profile_id": _ref(start_id, "connector_profile_id"),
                    "payload": _ref(read_payload_id, "output"),
                    "idempotency_key": _ref(start_id, "connector_idempotency_key"),
                    "execution_mode": "execute",
                },
                position={"x": x + 520, "y": y},
                retry={"enabled": True, "max_attempts": 2, "delay_seconds": 1},
                error_strategy="fail",
            ),
            NodeSpec(
                id=decision_id,
                type="llm",
                title="Decide Tenant Update",
                description="Use authorized context to propose one bounded customer update.",
                config={
                    "system": (
                        "Produce a concise customer-system decision. Never invent another tenant, "
                        "credential, authorization, or side effect."
                    ),
                    "prompt": {
                        "request": _ref(start_id, "request"),
                        "customer_context": _ref(read_id, "response"),
                    },
                    "temperature": 0,
                },
                position={"x": x + 780, "y": y},
                retry={"enabled": True, "max_attempts": 2, "delay_seconds": 1},
                error_strategy="fail",
            ),
            NodeSpec(
                id=write_payload_id,
                type="variable_assigner",
                title="Map Writeback Contract",
                description="Map the decision into the declared writeback schema.",
                config={
                    "assignments": {
                        "case_id": _ref(start_id, "request", "case_id"),
                        "decision": _ref(decision_id, "text"),
                    }
                },
                position={"x": x + 1040, "y": y},
            ),
            NodeSpec(
                id=write_id,
                type="connector_action",
                title="Governed Customer Writeback",
                description="Request an idempotent writeback with compensation evidence.",
                config={
                    "connector_id": "customer_system",
                    "connector_version": 1,
                    "operation_id": "update_case",
                    "tenant_id": _ref(start_id, "tenant_id"),
                    "actor_id": _ref(start_id, "actor_id"),
                    "actor_roles": _ref(start_id, "actor_roles"),
                    "profile_id": _ref(start_id, "connector_profile_id"),
                    "payload": _ref(write_payload_id, "output"),
                    "idempotency_key": _ref(start_id, "connector_idempotency_key"),
                    "authorization_id": _ref(start_id, "connector_authorization_id"),
                    "execution_mode": _ref(start_id, "write_mode"),
                },
                position={"x": x + 1300, "y": y},
                retry={"enabled": False, "max_attempts": 1, "delay_seconds": 0},
                error_strategy="fail",
            ),
            NodeSpec(
                id=answer_id,
                type="answer",
                title="Customer Writeback Receipt",
                description="Return tenant-safe writeback, callback, and compensation state.",
                config={"answer": _ref(write_id, "receipt")},
                position={"x": x + 1560, "y": y},
            ),
        ],
        edges=[
            EdgeSpec(id=f"{prefix}_e1", source=start_id, target=read_payload_id),
            EdgeSpec(id=f"{prefix}_e2", source=read_payload_id, target=read_id),
            EdgeSpec(id=f"{prefix}_e3", source=read_id, target=decision_id),
            EdgeSpec(
                id=f"{prefix}_e4",
                source=decision_id,
                source_port="text",
                target=write_payload_id,
            ),
            EdgeSpec(id=f"{prefix}_e5", source=write_payload_id, target=write_id),
            EdgeSpec(
                id=f"{prefix}_e6",
                source=write_id,
                source_port="receipt",
                target=answer_id,
            ),
        ],
    )


def _codex_like_workspace_agent_template(*, prefix: str, x: float, y: float) -> WorkflowSpec:
    def node(
        suffix: str,
        block_type: str,
        title: str,
        config: dict[str, Any],
        column: int,
        row: int = 0,
    ) -> NodeSpec:
        return NodeSpec(
            id=f"{prefix}_{suffix}",
            type=block_type,
            title=title,
            config=config,
            position={"x": x + column * 260, "y": y + row * 150},
        )

    def edge(
        source: str,
        target: str,
        source_port: str = "output",
        target_port: str = "input",
        branch: str | None = None,
    ) -> EdgeSpec:
        branch_suffix = f"_{branch}" if branch else ""
        return EdgeSpec(
            id=f"{prefix}_{source}_to_{target}{branch_suffix}",
            source=f"{prefix}_{source}",
            target=f"{prefix}_{target}",
            source_port=source_port,
            target_port=target_port,
            branch=branch,
        )

    nested = WorkflowSpec(
        nodes=[
            NodeSpec(
                id="loop_start",
                type="start",
                title="Iteration Context",
                config={"inputs": [
                    {"name": "iteration", "label": "Iteration", "type": "number"},
                    {"name": "task", "label": "Task", "type": "string"},
                    {"name": "workspace_path", "label": "Workspace", "type": "string"},
                    {"name": "plan", "label": "Plan", "type": "object"},
                    {"name": "agent_context", "label": "Agent context", "type": "object"},
                    {"name": "loop_state", "label": "Loop state", "type": "object"},
                    {"name": "tool_feedback", "label": "Prior tool feedback", "type": "any", "required": False},
                    {"name": "previous", "label": "Prior iteration", "type": "object", "required": False},
                    {"name": "cancel_requested", "label": "Cancel requested", "type": "boolean", "required": False, "default": False},
                ]},
            ),
            NodeSpec(
                id="loop_model_turn",
                type="model_turn",
                title="Decide Next Action",
                config=_arch_config(_ref("loop_start", "output"), {
                    "system": (
                        "You are one observable turn in a workspace coding agent. Follow the approved plan, "
                        "inspect prior tool feedback, choose at most one registered tool when more evidence or "
                        "an edit is needed, and otherwise return the final customer-readable answer."
                    ),
                    "prompt": _ref("loop_start", "output"),
                    "tools": ["Read", "Glob", "Grep", "Write", "Edit", "Bash", "WebSearch"],
                }),
            ),
            NodeSpec(
                id="loop_tool_router",
                type="tool_call_router",
                title="Route Tool Call",
                config=_arch_config(_ref("loop_model_turn", "output")),
            ),
            NodeSpec(
                id="loop_route_decision",
                type="if_else",
                title="Tool Or Final Answer",
                config={
                    "cases": [{
                        "id": "use_tool",
                        "conditions": [{
                            "value": _ref("loop_tool_router", "output", "no_tool_calls"),
                            "operator": "equals",
                            "expected": False,
                        }],
                    }],
                    "default_branch": "done",
                },
            ),
            NodeSpec(
                id="loop_tool_executor",
                type="tool_executor",
                title="Execute Routed Tool",
                config=_arch_config(_ref("loop_tool_router", "output"), {
                    "workspace_path": _ref("loop_start", "workspace_path"),
                }),
            ),
            NodeSpec(
                id="loop_tool_result",
                type="tool_result_normalizer",
                title="Normalize Tool Result",
                config=_arch_config(_ref("loop_tool_executor", "output")),
            ),
            NodeSpec(
                id="loop_no_tool_result",
                type="variable_assigner",
                title="Use Final Model Result",
                config={"assignments": {"model_result": _ref("loop_model_turn", "output")}},
            ),
            NodeSpec(
                id="loop_feedback_join",
                type="variable_aggregator",
                title="Join Iteration Feedback",
                config={
                    "variables": [
                        _optional_ref("loop_tool_result", "output"),
                        _optional_ref("loop_no_tool_result", "output"),
                    ],
                    "mode": "first_non_null",
                },
            ),
            NodeSpec(
                id="loop_stop",
                type="stop_continue_controller",
                title="Stop Or Continue",
                config=_arch_config(_ref("loop_model_turn", "output")),
            ),
            NodeSpec(
                id="loop_state_builder",
                type="variable_assigner",
                title="Update Loop State",
                config={"assignments": {
                    "iteration": _ref("loop_start", "iteration"),
                    "task": _ref("loop_start", "task"),
                    "plan": _ref("loop_start", "plan"),
                    "model_result": _ref("loop_model_turn", "output"),
                    "tool_feedback": _ref("loop_feedback_join", "output"),
                    "continue": _ref("loop_stop", "output", "continue"),
                    "stop_reason": _ref("loop_stop", "output", "stop_reason"),
                    "cancel_requested": _ref("loop_start", "cancel_requested"),
                }},
            ),
            NodeSpec(
                id="loop_end",
                type="end",
                title="Iteration Output",
                config={"outputs": {
                    "answer": _ref("loop_model_turn", "text"),
                    "model_result": _ref("loop_model_turn", "output"),
                    "state": _ref("loop_state_builder", "output"),
                    "feedback": _ref("loop_feedback_join", "output"),
                    "continue": _ref("loop_stop", "output", "continue"),
                    "stop_reason": _ref("loop_stop", "output", "stop_reason"),
                    "cancel_requested": _ref("loop_state_builder", "output", "cancel_requested"),
                }},
            ),
        ],
        edges=[
            EdgeSpec(id="loop_start_model", source="loop_start", target="loop_model_turn"),
            EdgeSpec(id="loop_model_router", source="loop_model_turn", target="loop_tool_router"),
            EdgeSpec(id="loop_router_decision", source="loop_tool_router", target="loop_route_decision"),
            EdgeSpec(
                id="loop_decision_tool",
                source="loop_route_decision",
                target="loop_tool_executor",
                source_port="branch",
                branch="use_tool",
            ),
            EdgeSpec(
                id="loop_decision_done",
                source="loop_route_decision",
                target="loop_no_tool_result",
                source_port="branch",
                branch="done",
            ),
            EdgeSpec(id="loop_tool_normalize", source="loop_tool_executor", target="loop_tool_result"),
            EdgeSpec(id="loop_tool_join", source="loop_tool_result", target="loop_feedback_join"),
            EdgeSpec(id="loop_done_join", source="loop_no_tool_result", target="loop_feedback_join"),
            EdgeSpec(id="loop_join_stop", source="loop_feedback_join", target="loop_stop"),
            EdgeSpec(id="loop_stop_state", source="loop_stop", target="loop_state_builder"),
            EdgeSpec(id="loop_state_end", source="loop_state_builder", target="loop_end"),
        ],
    )

    nodes = [
        node("start", "start", "Workspace Task", {"inputs": [
            {"name": "task", "label": "What should the agent do?", "type": "string"},
            {"name": "workspace_path", "label": "Workspace path", "type": "string", "required": False, "default": "."},
            {"name": "network_policy", "label": "Network policy", "type": "string", "required": False, "default": "none"},
            {"name": "cancel_requested", "label": "Cancel after this iteration", "type": "boolean", "required": False, "default": False},
        ]}, 0),
        node("context", "context_assembler", "Assemble Task Context", _arch_config(
            _ref(f"{prefix}_start", "output"),
            {"fragments": [_ref(f"{prefix}_start", "task")]},
        ), 1),
        node("workspace", "workspace_context_injector", "Inject Workspace Context", _arch_config(
            _ref(f"{prefix}_context", "output"),
            {"scope": "selected_workspace", "files": ["README.md", "AGENTS.md", "tests/"]},
        ), 2),
        node("compact", "context_compactor", "Compact Context", _arch_config(
            _ref(f"{prefix}_workspace", "output"),
            {"max_chars": 8000, "preserved_facts": ["task", "plan", "tool evidence", "permission decisions", "failed tests"]},
        ), 3),
        node("capabilities", "capability_registry", "Discover Capabilities", _arch_config(
            _ref(f"{prefix}_compact", "output"),
            {"tools": ["Read", "Glob", "Grep", "Write", "Edit", "Bash", "WebSearch"]},
        ), 4),
        node("plan", "model_turn", "Plan Workspace Task", _arch_config(
            _ref(f"{prefix}_capabilities", "output"),
            {
                "system": (
                    "Plan the workspace task before any mutating action. Return JSON with goal, steps, "
                    "read_only_first, likely_tools, risks, and done_when. Do not execute tools in this block."
                ),
                "prompt": {
                    "task": _ref(f"{prefix}_start", "task"),
                    "context": _ref(f"{prefix}_capabilities", "output"),
                },
                "output_format": "json",
            },
        ), 5),
        node("budget", "budget_gate", "Budget Gate", _arch_config(
            _ref(f"{prefix}_plan", "output"),
            {"max_cost_usd": 2.0, "spent_cost_usd": 0},
        ), 6),
        node("rounds", "round_limit", "Round Limit", _arch_config(
            _ref(f"{prefix}_budget", "output"),
            {"current_round": 0, "max_rounds": 8},
        ), 7),
        node("permission", "permission_gate", "Approve Plan", _arch_config(
            _ref(f"{prefix}_plan", "output"),
            {"mode": "plan_first", "reason": "Approve the displayed plan and workspace tool boundary."},
        ), 8),
        node("sandbox", "sandbox_boundary", "Workspace Boundary", _arch_config(
            _ref(f"{prefix}_permission", "output"),
            {
                "workspace": _ref(f"{prefix}_start", "workspace_path"),
                "network_policy": _ref(f"{prefix}_start", "network_policy"),
            },
        ), 9),
        node("loop", "loop", "Plan-Act-Observe Loop", {
            "workflow": nested.model_dump(mode="json"),
            "variables": {
                "task": _ref(f"{prefix}_start", "task"),
                "workspace_path": _ref(f"{prefix}_start", "workspace_path"),
                "plan": _ref(f"{prefix}_plan", "output"),
                "agent_context": _ref(f"{prefix}_sandbox", "output"),
                "cancel_requested": _ref(f"{prefix}_start", "cancel_requested"),
            },
            "initial_state": {
                "task": _ref(f"{prefix}_start", "task"),
                "plan": _ref(f"{prefix}_plan", "output"),
                "completed_steps": [],
            },
            "state_input_name": "loop_state",
            "state_update": _ref("loop_end", "state"),
            "feedback_input_name": "tool_feedback",
            "feedback_value": _ref("loop_end", "feedback"),
            "break_condition": {"value": False, "operator": "equals", "expected": False},
            "break_value": _ref("loop_end", "continue"),
            "cancel_condition": {"value": False, "operator": "equals", "expected": True},
            "cancel_value": _ref("loop_end", "cancel_requested"),
            "max_iterations": 8,
            "output_node_id": "loop_end",
            "checkpoint_each_iteration": True,
        }, 10),
        node("trace", "event_recorder", "Record Agent Trace", _arch_config(
            _ref(f"{prefix}_loop", "output"),
            {"label": "codex_like_workspace_agent"},
        ), 11),
        node("answer", "answer", "Workspace Result", {
            "answer": _ref(f"{prefix}_loop", "output", "answer"),
        }, 12),
    ]
    edges = [
        edge("start", "context"),
        edge("context", "workspace"),
        edge("workspace", "compact"),
        edge("compact", "capabilities"),
        edge("capabilities", "plan"),
        edge("plan", "budget"),
        edge("budget", "rounds"),
        edge("rounds", "permission"),
        edge("permission", "sandbox"),
        edge("sandbox", "loop"),
        edge("loop", "trace"),
        edge("trace", "answer"),
    ]
    return WorkflowSpec(nodes=nodes, edges=edges)


def _claude_like_coding_agent_template(*, prefix: str, x: float, y: float) -> WorkflowSpec:
    def node(
        suffix: str,
        block_type: str,
        title: str,
        config: dict[str, Any],
        column: int,
        row: int = 0,
    ) -> NodeSpec:
        return NodeSpec(
            id=f"{prefix}_{suffix}",
            type=block_type,
            title=title,
            config=config,
            position={"x": x + column * 260, "y": y + row * 120},
        )

    def edge(source: str, target: str, source_port: str = "output", target_port: str = "input") -> EdgeSpec:
        return EdgeSpec(
            id=f"{prefix}_{source}_to_{target}",
            source=f"{prefix}_{source}",
            target=f"{prefix}_{target}",
            source_port=source_port,
            target_port=target_port,
        )

    nested = WorkflowSpec(
        nodes=[
            NodeSpec(
                id="loop_start",
                type="start",
                title="Loop State",
                config={"inputs": [{"name": "iteration", "type": "number"}, {"name": "previous", "type": "object", "required": False}]},
            ),
            NodeSpec(
                id="loop_model_turn",
                type="model_turn",
                title="Model Turn",
                config=_arch_config(_ref("loop_start", "output"), {"prompt": _ref("loop_start", "output")}),
            ),
            NodeSpec(
                id="loop_tool_router",
                type="tool_call_router",
                title="Tool Call Router",
                config=_arch_config(_ref("loop_model_turn", "output")),
            ),
            NodeSpec(
                id="loop_tool_executor",
                type="tool_executor",
                title="Tool Executor",
                config=_arch_config(_ref("loop_tool_router", "output"), {
                    "tool_name": "Read",
                    "tool_input": {"path": "README.md"},
                }),
            ),
            NodeSpec(
                id="loop_tool_result",
                type="tool_result_normalizer",
                title="Tool Result Normalizer",
                config=_arch_config(_ref("loop_tool_executor", "output")),
            ),
            NodeSpec(
                id="loop_continue",
                type="stop_continue_controller",
                title="Stop / Continue",
                config=_arch_config(_ref("loop_tool_result", "output"), {
                    "stop_reason": "tool_use",
                }),
            ),
            NodeSpec(
                id="loop_end",
                type="end",
                title="Loop Output",
                config={"outputs": {"state": _ref("loop_continue", "state"), "tool_result": _ref("loop_tool_result", "output")}},
            ),
        ],
        edges=[
            EdgeSpec(id="loop_start_model", source="loop_start", target="loop_model_turn", source_port="output", target_port="input"),
            EdgeSpec(id="loop_model_router", source="loop_model_turn", target="loop_tool_router", source_port="output", target_port="input"),
            EdgeSpec(id="loop_router_tool", source="loop_tool_router", target="loop_tool_executor", source_port="output", target_port="input"),
            EdgeSpec(id="loop_tool_result", source="loop_tool_executor", target="loop_tool_result", source_port="output", target_port="input"),
            EdgeSpec(id="loop_result_continue", source="loop_tool_result", target="loop_continue", source_port="output", target_port="input"),
            EdgeSpec(id="loop_continue_end", source="loop_continue", target="loop_end", source_port="output", target_port="input"),
        ],
    )

    nodes = [
        node("start", "start", "Agent Input", {"inputs": [
            {"name": "task", "label": "Task", "type": "string"},
            {"name": "workspace_path", "label": "Workspace path", "type": "string", "required": False, "default": "."},
        ]}, 0),
        node("context", "context_assembler", "Context Assembler", _arch_config(_ref(f"{prefix}_start", "output"), {
            "fragments": [_ref(f"{prefix}_start", "task")],
        }), 1),
        node("workspace", "workspace_context_injector", "Workspace Context", _arch_config(_ref(f"{prefix}_context", "output"), {
            "scope": "current_workspace",
            "files": ["README.md", "tests/"],
        }), 2),
        node("skills", "skill_loader", "Skill Loader", _arch_config(_ref(f"{prefix}_workspace", "output"), {
            "skills": ["code-repair", "test-triage"],
        }), 3),
        node("mcp", "mcp_gateway", "MCP Gateway", _arch_config(_ref(f"{prefix}_skills", "output"), {
            "servers": [],
        }), 4),
        node("capabilities", "capability_registry", "Capability Registry", _arch_config(_ref(f"{prefix}_mcp", "output"), {
            "tools": ["Read", "Write", "Bash"],
        }), 5),
        node("memory", "conversation_memory", "Conversation Memory", _arch_config(_ref(f"{prefix}_capabilities", "output"), {
            "facts": ["Preserve tool evidence and user instructions across turns."],
        }), 6),
        node("compact", "context_compactor", "Context Compactor", _arch_config(_ref(f"{prefix}_memory", "output"), {
            "max_chars": 6000,
            "preserved_facts": ["task", "tool evidence", "failed tests", "permission decisions"],
        }), 7),
        node("budget", "budget_gate", "Budget Gate", _arch_config(_ref(f"{prefix}_compact", "output"), {
            "max_cost_usd": 1.0,
            "spent_cost_usd": 0,
        }), 8),
        node("rounds", "round_limit", "Round Limit", _arch_config(_ref(f"{prefix}_budget", "output"), {
            "current_round": 0,
            "max_rounds": 8,
        }), 9),
        node("permission", "permission_gate", "Permission Gate", _arch_config(_ref(f"{prefix}_rounds", "output"), {
            "reason": "Allow workspace reads/writes and test execution for this coding task.",
            "auto_approve": True,
        }), 10),
        node("sandbox", "sandbox_boundary", "Sandbox Boundary", _arch_config(_ref(f"{prefix}_permission", "output"), {
            "network_policy": "none",
            "workspace": _ref(f"{prefix}_start", "workspace_path"),
        }), 11),
        node("loop", "loop", "Multi-round Agent Loop", {
            "workflow": nested.model_dump(mode="json"),
            "variables": {"agent_context": _ref(f"{prefix}_sandbox", "output")},
            "break_condition": {"value": False, "operator": "equals", "expected": True},
            "break_value": _ref("loop_continue", "state", "continue"),
            "max_iterations": 2,
            "output_node_id": "loop_end",
        }, 12),
        node("retry", "retry_error_classifier", "Retry / Error Classifier", _arch_config(_ref(f"{prefix}_loop", "output"), {
            "error": "",
        }), 13),
        node("subagent", "subagent_spawn", "Subagent Spawn", _arch_config(_ref(f"{prefix}_retry", "output"), {
            "name": "test-triage",
            "task": "Inspect failing tests and return evidence.",
            "budget": {"max_rounds": 3},
        }), 14),
        node("dispatch", "task_dispatcher", "Task Dispatcher", _arch_config(_ref(f"{prefix}_subagent", "output"), {
            "tasks": ["read files", "run tests", "patch code", "rerun tests"],
        }), 15),
        node("deps", "dependency_gate", "Dependency Gate", _arch_config(_ref(f"{prefix}_dispatch", "output"), {
            "dependencies": ["read files", "run tests"],
            "completed": ["read files", "run tests"],
        }), 16),
        node("mailbox", "mailbox_wait_wake", "Mailbox Wait / Wake", _arch_config(_ref(f"{prefix}_deps", "output"), {
            "messages": ["triage complete"],
        }), 17),
        node("checkpoint", "checkpoint_resume", "Checkpoint / Resume", _arch_config(_ref(f"{prefix}_mailbox", "output"), {
            "checkpoint_id": "coding-agent-after-triage",
        }), 18),
        node("cancel", "cancellation_point", "Cancellation Point", _arch_config(_ref(f"{prefix}_checkpoint", "output"), {
            "cancelled": False,
        }), 19),
        node("trace", "event_recorder", "Event Recorder", _arch_config(_ref(f"{prefix}_cancel", "output"), {
            "label": "claude_like_coding_agent_trace",
        }), 20),
        node("end", "end", "Agent Output", {"outputs": {
            "trace": _ref(f"{prefix}_trace", "state"),
            "loop": _ref(f"{prefix}_loop", "output"),
            "checkpoint": _ref(f"{prefix}_checkpoint", "state"),
        }}, 21),
    ]
    edges = [
        edge("start", "context"),
        edge("context", "workspace"),
        edge("workspace", "skills"),
        edge("skills", "mcp"),
        edge("mcp", "capabilities"),
        edge("capabilities", "memory"),
        edge("memory", "compact"),
        edge("compact", "budget"),
        edge("budget", "rounds"),
        edge("rounds", "permission"),
        edge("permission", "sandbox"),
        edge("sandbox", "loop"),
        edge("loop", "retry"),
        edge("retry", "subagent"),
        edge("subagent", "dispatch"),
        edge("dispatch", "deps"),
        edge("deps", "mailbox"),
        edge("mailbox", "checkpoint"),
        edge("checkpoint", "cancel"),
        edge("cancel", "trace"),
        edge("trace", "end"),
    ]
    return WorkflowSpec(nodes=nodes, edges=edges)


def edge_by_id(workflow: WorkflowSpec, edge_id: str) -> EdgeSpec:
    try:
        return next(edge for edge in workflow.edges if edge.id == edge_id)
    except StopIteration as error:
        raise KeyError(f"edge not found: {edge_id}") from error
