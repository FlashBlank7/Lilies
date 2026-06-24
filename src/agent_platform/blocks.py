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
        return sorted(self._definitions.values(), key=lambda item: (item.category, item.title))

    def get(self, block_type: str) -> BlockDefinition:
        try:
            return self._definitions[block_type]
        except KeyError as error:
            raise KeyError(f"unknown block type: {block_type}") from error

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
) -> BlockDefinition:
    return BlockDefinition(
        type=block_type,
        title=title,
        description=description,
        category=category,
        config_schema=config.model_json_schema(),
        input_ports=[PortDefinition(name=name, value_type=value_type) for name, value_type in inputs],
        output_ports=[PortDefinition(name=name, value_type=value_type) for name, value_type in outputs],
        supports_retry=retry,
        supports_error_branch=error_branch,
        editor={"icon": block_type, "accent": category},
    )


def build_block_registry() -> BlockRegistry:
    registry = BlockRegistry()
    blocks: list[tuple[BlockDefinition, type[BaseModel]]] = [
        (_definition("start", "User Input", "Declare workflow inputs.", "input", StartConfig, outputs=[("output", ValueType.any)]), StartConfig),
        (_definition("schedule_trigger", "Schedule Trigger", "Start a published workflow on an IANA-timezone daily schedule.", "input", ScheduleTriggerConfig, outputs=[("output", ValueType.any)]), ScheduleTriggerConfig),
        (_definition("llm", "LLM", "Make one provider-neutral model call.", "model", LLMConfig, inputs=[("input", ValueType.any)], outputs=[("text", ValueType.string), ("structured", ValueType.object)], retry=True, error_branch=True), LLMConfig),
        (_definition("claude_agent", "Claude Agent", "Run the complete Claude-style agent loop.", "agent", ClaudeAgentConfig, inputs=[("input", ValueType.any)], outputs=[("text", ValueType.string)], retry=True, error_branch=True), ClaudeAgentConfig),
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
    for definition, model in blocks:
        registry.register(definition, model)
    return registry


def edge_by_id(workflow: WorkflowSpec, edge_id: str) -> EdgeSpec:
    try:
        return next(edge for edge in workflow.edges if edge.id == edge_id)
    except StopIteration as error:
        raise KeyError(f"edge not found: {edge_id}") from error
