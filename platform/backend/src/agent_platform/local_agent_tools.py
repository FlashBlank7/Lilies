"""Project-bound, versioned tool surface shared by local agent adapters."""
from __future__ import annotations

import asyncio
import csv
import math
from pathlib import Path
from typing import Any, Literal

from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .requirement_discussion import load_discussion, save_discussion
from .workflow_models import DraftOperation, EdgeSpec, NodeSpec, WorkflowRunRequest, WorkflowTestCase


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectFile(Arguments):
    action: Literal["list", "read", "profile", "write"]
    path: str = ""
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=200, ge=1, le=2000)
    content: str = Field(default="", max_length=500_000)


class Catalog(Arguments):
    block_type: str = ""
    tool_name: str = ""


class Draft(Arguments):
    operation: DraftOperation | None = None

    @model_validator(mode='after')
    def update_shape(self):
        if self.operation and self.operation.op == 'update_node':
            data = self.operation.data
            if not data.get('node_id') or not isinstance(data.get('changes'), dict) or not data['changes']:
                raise ValueError('update_node 需要 data={node_id:"节点ID", changes:{title:"新标题"或config:{...}}, merge_config:true}；'
                                 '修改内容放在 changes 中，不能用 patch、updates、node 或顶层 config。'
                                 '可用 block_catalog(tool_name="workflow_draft") 查看完整说明。')
        return self


class Run(Arguments):
    action: Literal["validate", "start", "inspect", "tests"]
    inputs: dict[str, Any] = Field(default_factory=dict)
    run_id: str = ""


class Question(Arguments):
    id: str = Field(min_length=1, max_length=120)
    question: str = Field(min_length=1, max_length=1000)
    why: str = Field(default="", max_length=1000)


class Requirements(Arguments):
    action: Literal['read', 'submit'] = 'submit'
    understanding: str = Field(default='', max_length=8000)
    questions: list[Question] = Field(default_factory=list, max_length=8)
    document: str = Field(default="", max_length=28_000)


TOOL_MODELS = {
    "project_file": (ProjectFile, "List/read/profile project files. Only requirement-package/, solution/, results/ and the confirmed requirements document are accessible. Write only to solution/ or results/. CSV profile scans all rows and reports actual counts; read returns a bounded slice."),
    "block_catalog": (Catalog, "List available platform blocks, or get the exact block config schema, ports, instructions and generic examples. Set tool_name to any project agent tool (including workflow_draft) for its complete description and input schema, or Read/Write/Edit/Glob/Grep/Bash for runtime tool schema, output and environment rules. Also returns node, edge and test schemas. No existing project workflows or solutions are exposed."),
    "workflow_draft": (Draft, "Read the CURRENT editable draft, or apply ONE revision-checked operation. Read the draft first. operation.data shapes: add_node={node:NodeSpec}; update_node={node_id:string,changes:{title?,config?,position?},merge_config:true}; remove_node={node_id:string}; add_edge={edge:EdgeSpec}; remove_edge={edge_id:string}; add_test={test:WorkflowTestCase}; remove_test={test_id:string}. Get NodeSpec/EdgeSpec/test schemas from block_catalog(block_type). Editing is enabled only after the user starts building. Do not write workflow state as files."),
    "workflow_run": (Run, "Validate the current draft, start a REAL platform run, inspect its outputs/events, or execute its saved tests. Runs only this project and returns actual runtime results. No model calls or external systems are enabled for this local test."),
    "requirements_submit": (Requirements, "action=read returns the current full requirement document and discussion. action=submit presents understanding/questions or a COMPLETE replacement document after discussion. document REPLACES the entire previous document; never submit only an erratum. It never confirms or starts building."),
}


def tool_specs() -> list[dict]:
    return [{"type": "function", "name": name, "description": description,
             "inputSchema": model.model_json_schema(), "deferLoading": False}
            for name, (model, description) in TOOL_MODELS.items()]


class ProjectTools:
    def __init__(self, services, application_id: str, manager) -> None:
        self.services = services
        self.application_id = application_id
        self.manager = manager
        self.workspace = services.settings.workspace_root.resolve() / application_id

    def tool_definitions(self) -> list[dict]:
        return tool_specs()

    def path(self, value: str, *, write: bool = False) -> Path:
        relative = Path(value)
        allowed = {"solution", "results"} if write else {"requirement-package", "solution", "results"}
        confirmed_document = value == "requirements/requirements.md" and not write
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError("只能访问本项目的相对路径")
        if relative.parts[0] not in allowed and not confirmed_document:
            raise ValueError("此文件不在本项目输入和产物范围内")
        target = self.workspace / relative
        # Reject every link component, including links to another allowed directory.
        for item in (target, *target.parents):
            if item == self.workspace:
                break
            if item.is_symlink():
                raise ValueError("项目工具不读取或写入符号链接")
        if not target.resolve().is_relative_to(self.workspace.resolve()):
            raise ValueError("文件超出本项目范围")
        return target

    def require_build(self) -> None:
        state = self.manager.load(self.application_id)
        discussion = load_discussion(self.workspace)
        if discussion['status'] != 'confirmed':
            raise ValueError('需求尚未确认，请先完成需求沟通与确认')
        if state.get('phase') != 'build':
            if state.get('conversation_enabled'):
                if state.get('active_item_id') in state.get('blocked_this_request', []):
                    raise ValueError('此事项因连续失败已暂停，需求确认仍有效。修复已记录的问题后，从原项目对话继续即可恢复。')
                raise ValueError('需求已确认；编辑前请调用 project_action(action="build", item_id="当前事项") 绑定建设事项，不需要重新确认需求。')
            raise ValueError('需求已确认，请在原会话开始或继续搭建')

    async def call(self, name: str, arguments: dict) -> Any:
        if name not in TOOL_MODELS:
            raise ValueError("未开放的项目工具")
        args = TOOL_MODELS[name][0].model_validate(arguments)
        if name == "project_file":
            return await asyncio.to_thread(self.file, args)
        if name == "block_catalog":
            blocks = await self.services.projects.blocks_for(self.application_id)
            if args.tool_name:
                definition = next((t for t in self.tool_definitions() if t['name'] == args.tool_name), None)
                if definition:
                    result = {'name': definition['name'], 'description': definition['description'],
                              'input_schema': definition['inputSchema'], 'surface': 'project_agent'}
                    if args.tool_name == 'workflow_draft':
                        result['examples'] = [{}, {'operation': {'op': 'update_node', 'expected_revision': 1,
                            'idempotency_key': 'unique-edit-key', 'data': {'node_id': 'example_node',
                            'changes': {'title': 'Updated title'}, 'merge_config': True}}}]
                    return result
                if args.tool_name not in {'Read', 'Write', 'Edit', 'Glob', 'Grep', 'Bash'}:
                    raise ValueError('未找到此工具说明；请使用已提供的项目工具名称，或 Read/Write/Edit/Glob/Grep/Bash')
                tool = self.services.tools.get(args.tool_name)
                return {'name': tool.name, 'description': tool.description,
                        'input_schema': tool.input_model.model_json_schema(),
                        'output': 'Tool node returns {output: parsed JSON when tool text is valid JSON, otherwise text}. Tool errors fail the node with the error message.',
                        'environment': 'Docker sandbox; current directory /workspace maps to the project workspace. '
                                       'requirement-package/ and requirements/ are read-only; solution/ and results/ are writable. '
                                       'No network. The default image includes NumPy, SciPy, pandas and scikit-learn. '
                                       'Use installed standard libraries for preprocessing, models and evaluation. '
                                       'Check the actual runtime with python /opt/platform/check_sandbox_ml.py; '
                                       'the pinned package manifest is /opt/platform/requirements-sandbox.txt. '
                                       'If a required dependency is missing, report the required environment change '
                                       'instead of silently reducing the requested solution.'}
            if args.block_type:
                return {"manual": blocks.manual(args.block_type),
                        "node_schema": NodeSpec.model_json_schema(),
                        "edge_schema": EdgeSpec.model_json_schema(),
                        "test_schema": WorkflowTestCase.model_json_schema()}
            return [{"type": b.type, "title": b.title, "description": b.description}
                    for b in blocks.list()]
        if name == "workflow_draft":
            if args.operation:
                self.require_build()
                if args.operation.op == "set_metadata" and "requirement" in args.operation.data:
                    raise ValueError("请通过需求沟通修改需求文档")
                return jsonable_encoder(await self.services.applications.apply_operation(
                    self.application_id, args.operation))
            return jsonable_encoder(await self.services.workflow_store.get_draft(self.application_id))
        if name == "requirements_submit":
            if args.action == 'read':
                return load_discussion(self.workspace)
            if not args.understanding.strip():
                raise ValueError('请提供需求理解')
            if self.manager.load(self.application_id).get("phase") == "build":
                raise ValueError("当前处于搭建阶段；需求有变化时请让用户切回需求沟通")
            state = load_discussion(self.workspace)
            source = "lilies" if self.manager.load(self.application_id).get("provider") == "api" else "codex"
            state.update(enabled=True, revision=state["revision"] + 1, source=source,
                         status="review" if args.document.strip() else "discussing",
                         document=args.document.strip())
            state["turns"].append({"user": self.manager.last_user_message(self.application_id),
                                   "analysis": {"detected_goal": args.understanding,
                                                "reasoning_summary": "", "questions": [
                                                    {**q.model_dump(), "choice_type": "single", "options": []}
                                                    for q in args.questions],
                                                "proposed_document": args.document}})
            save_discussion(self.workspace, state)
            return {"status": state["status"], "revision": state["revision"],
                    "message": "已展示给用户，等待回复或确认；此操作不会开始搭建"}
        if name == "workflow_run":
            self.require_build()
            self.services.sandboxes.protect_inputs(self.workspace, ["requirement-package", "requirements"])
            if args.action == "validate":
                return jsonable_encoder(await self.services.applications.validate_draft(self.application_id))
            scope = {"workspace_boundary": str(self.workspace),
                     "allowed_nested_application_ids": [],
                     "allowed_runtime_tools": ["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
                     "allowed_network_hosts": [], "model_access": self.manager.connections.enabled(self.application_id),
                     "allowed_connector_operations": []}
            if args.action == "start":
                creation = asyncio.create_task(self.services.workflow_runtime.create_run(
                    self.application_id,
                    WorkflowRunRequest(inputs=args.inputs, use_draft=True,
                                       workspace_path=str(self.workspace)),
                    origin="local_agent", triggered_by="Codex", **scope))
                try:
                    result = await asyncio.shield(creation)
                except asyncio.CancelledError:
                    result = await creation
                    self.manager.track_run(self.application_id, result["run_id"])
                    raise
                self.manager.track_run(self.application_id, result["run_id"])
                return jsonable_encoder(result)
            if args.action == "tests":
                return jsonable_encoder(await self.services.workflow_runtime.run_test_suite(
                    self.application_id, workspace_path=str(self.workspace), **scope))
            run = await self.services.workflow_store.get_run(args.run_id)
            if run["application_id"] != self.application_id:
                raise ValueError("不能查看其他项目的运行")
            events = await self.services.storage.list_events(args.run_id)
            return jsonable_encoder({"run": run, "events": events[-100:],
                                     "events_omitted": max(0, len(events) - 100)})
        raise ValueError("未实现的工具")

    def file(self, args: ProjectFile) -> dict:
        if args.action == "list":
            directories = [self.path(args.path)] if args.path else [
                self.workspace / x for x in ("requirement-package", "solution", "results")]
            files = []
            for directory in directories:
                for path in sorted(directory.rglob("*")) if directory.is_dir() else []:
                    if len(files) >= 2000:
                        return {"files": files, "truncated": True}
                    if path.is_file():
                        relative = path.relative_to(self.workspace).as_posix()
                        try:
                            self.path(relative)
                        except ValueError:
                            continue
                        files.append({"path": relative, "size": path.stat().st_size})
            return {"files": files, "truncated": False}
        path = self.path(args.path, write=args.action == "write")
        if args.action == "write":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args.content, encoding="utf-8")
            return {"path": args.path, "bytes": path.stat().st_size}
        if args.action == "profile":
            if path.suffix.lower() not in {".csv", ".tsv"}:
                raise ValueError("字段统计支持 CSV/TSV；其他文本请使用 read")
            return profile_csv(path)
        lines, size = [], 0
        with path.open(encoding="utf-8-sig") as reader:
            for index, line in enumerate(reader):
                if index < args.offset:
                    continue
                if len(lines) >= args.limit or size + len(line) > 200_000:
                    return {"path": args.path, "lines": lines, "truncated": True,
                            "next_offset": args.offset + len(lines)}
                lines.append(line.rstrip("\n"))
                size += len(line)
        return {"path": args.path, "lines": lines, "truncated": False}


def profile_csv(path: Path) -> dict:
    with path.open(encoding="utf-8-sig", newline="") as reader:
        rows = csv.reader(reader, delimiter="\t" if path.suffix.lower() == ".tsv" else ",")
        header = next(rows, [])
        columns = [{"name": name, "empty": 0, "numeric": 0, "min": None, "max": None,
                    "examples": []} for name in header]
        count, malformed = 0, 0
        for row in rows:
            count += 1
            malformed += len(row) != len(header)
            for index, column in enumerate(columns):
                value = row[index] if index < len(row) else ""
                if not value.strip():
                    column["empty"] += 1
                    continue
                if len(column["examples"]) < 3 and value not in column["examples"]:
                    column["examples"].append(value[:300])
                try:
                    number = float(value)
                except ValueError:
                    continue
                if math.isfinite(number):
                    column["numeric"] += 1
                    column["min"] = number if column["min"] is None else min(number, column["min"])
                    column["max"] = number if column["max"] is None else max(number, column["max"])
        return {"row_count": count, "malformed_rows": malformed, "columns": columns,
                "scan_complete": True, "note": "这是原始字段统计，业务解释由 Agent 根据企业材料判断。"}
