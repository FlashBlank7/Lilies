"""Project-bound simulated services and debug runs on the existing workflow engine."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
from typing import Any, Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .db import connect
from .models import NetworkPolicy
from .project_http_connections import checked_path, scope_digest, target_url
from .project_store import ProjectConflict, encode
from .simulation_provider import PRESETS, RUNNER, SimulatorResult, SimulatorSpec, handle
from .workflow_models import WorkflowSpec, WorkflowTestCase


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Binding(Body):
    service_id: str
    scenario: str = ""


class InputSource(Body):
    kind: Literal["synthetic", "upload", "run"] = "synthetic"
    label: str = ""
    task_id: str = ""
    run_id: str = ""
    path: list[str | int] = Field(default_factory=list)
    input_name: str = ""


class DebugCase(Body):
    id: str = ""
    name: str = Field(min_length=1, max_length=160)
    requirement: str = ""
    inputs: dict[str, Any] = Field(default_factory=dict)
    bindings: dict[str, Binding] = Field(default_factory=dict)
    source: InputSource = Field(default_factory=InputSource)
    expected_revision: int


class DebugRun(Body):
    workflow_id: str
    case_id: str = ""
    inputs: dict[str, Any] = Field(default_factory=dict)
    bindings: dict[str, Binding] = Field(default_factory=dict)
    request_key: str = Field(min_length=1, max_length=200)
    recovery_task_id: str = ""
    now: str = "2026-10-23T00:00:00Z"
    seed: int = 0


class ChainStep(Body):
    workflow_id: str
    inputs: dict[str, Any] = Field(default_factory=dict)


class Chain(Body):
    expected_revision: int
    steps: list[ChainStep] = Field(min_length=1, max_length=30)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def graph_nodes(graph):
    for node in graph.get("nodes", []):
        yield node
        nested = node.get("config", {}).get("workflow")
        if isinstance(nested, dict):
            yield from graph_nodes(nested)


class SimulationTransport(httpx.AsyncBaseTransport):
    """Internal HTTP gateway: no socket/real-network fallback, same HTTP executor."""

    def __init__(self, debug, pid, sid, alias):
        self.debug, self.pid, self.sid, self.alias = debug, pid, sid, alias

    async def handle_async_request(self, request):
        prefix = "/" + self.sid + "/" + self.alias
        if request.url.host != "simulation.invalid" or not request.url.path.startswith(
            prefix + "/"
        ):
            raise ValueError("模拟请求超出本次服务范围")
        body = await request.aread()
        payload = {
            "method": request.method,
            "path": request.url.path[len(prefix) :],
            "query": dict(request.url.params),
            "headers": dict(request.headers),
            "body": json.loads(body) if body else None,
        }
        result = await self.debug.dispatch(self.pid, self.sid, self.alias, payload)
        if result.fault == "disconnect":
            raise httpx.ReadError("模拟写入完成后响应丢失", request=request)
        response = result.response
        return httpx.Response(
            response.get("status", 200),
            headers=response.get("headers", {}),
            json=response.get("body"),
            request=request,
        )


class ProjectDebug:
    def __init__(self, services):
        self.services = services
        self.db_path = services.storage.db_path
        self.locks: dict[str, asyncio.Lock] = {}

    @property
    def projects(self):
        return self.services.projects

    async def initialize(self):
        with connect(self.db_path) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS project_simulators (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    revision INTEGER NOT NULL, spec_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS project_simulation_sessions (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    config_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS project_simulation_states (
                    session_id TEXT NOT NULL, alias TEXT NOT NULL, revision INTEGER NOT NULL,
                    state_json TEXT NOT NULL, requests_json TEXT NOT NULL,
                    PRIMARY KEY(session_id, alias));
                CREATE TABLE IF NOT EXISTS project_simulation_observations (
                    task_id TEXT PRIMARY KEY, states_json TEXT NOT NULL);
            """)

    async def services_list(self, pid):
        await self.projects.store.get(pid)
        with connect(self.db_path) as db:
            return [
                {**json.loads(r["spec_json"]), "id": r["id"], "revision": r["revision"]}
                for r in db.execute(
                    "SELECT * FROM project_simulators WHERE project_id=? ORDER BY rowid", (pid,)
                )
            ]

    async def save_service(self, pid, sid, body: SimulatorSpec):
        await self.projects.store.get(pid)
        for path in body.allowed_paths:
            checked_path(path.removesuffix("/**") or "/")
            if "*" in path.removesuffix("/**"):
                raise ValueError("路径只支持精确匹配或 /** 范围")
        if body.idempotency_header and (
            not re.fullmatch(r"[A-Za-z0-9-]+", body.idempotency_header)
            or body.idempotency_header.lower()
            in {"host", "content-length", "connection", "transfer-encoding"}
        ):
            raise ValueError("幂等请求头无效")
        if body.implementation == "records" and body.idempotency_header.lower() not in {
            "",
            "idempotency-key",
        }:
            raise ValueError("记录预设仅支持 Idempotency-Key 幂等请求头")
        if body.implementation == "python":
            if not body.code.strip():
                raise ValueError("请提供 handle(request, state, context) 实现")
            try:
                compile(body.code, "provider.py", "exec")
            except SyntaxError as e:
                raise ValueError(f"模拟实现第 {e.lineno} 行语法错误：{e.msg}") from None
        updating = bool(sid)
        sid = sid or str(uuid4())
        spec = body.model_dump(exclude={"expected_revision"})
        with connect(self.db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM project_simulators WHERE id=? AND project_id=?", (sid, pid)
            ).fetchone()
            if updating and not old:
                raise KeyError("模拟服务不属于本项目")
            if (old["revision"] if old else 0) != body.expected_revision:
                raise ProjectConflict("模拟服务已修改，请刷新版本")
            revision = body.expected_revision + 1
            if old:
                db.execute(
                    "UPDATE project_simulators SET revision=?,spec_json=? WHERE id=?",
                    (revision, encode(spec), sid),
                )
            else:
                db.execute(
                    "INSERT INTO project_simulators VALUES (?,?,?,?)",
                    (sid, pid, revision, encode(spec)),
                )
        return {**spec, "id": sid, "revision": revision}

    async def cases(self, pid, wid):
        await self.projects.member(pid, wid)
        draft = await self.services.workflow_store.get_draft(wid)
        return {
            "revision": draft["revision"],
            "cases": [c.model_dump() for c in draft["snapshot"].tests],
        }

    async def suite(self, pid, wid, case_ids=None):
        cases = (await self.cases(pid, wid))["cases"]
        if case_ids is not None:
            if not case_ids or set(case_ids) - {c["id"] for c in cases}:
                raise ValueError("请选择存在的测试样例")
            cases = [c for c in cases if c["id"] in case_ids]
        if not cases:
            raise ValueError("请先保存测试样例")
        results = []
        for case in cases:
            task = await self.run(
                pid,
                DebugRun(workflow_id=wid, case_id=case["id"], request_key="suite-" + str(uuid4())),
            )
            active = self.projects.active.get(task["id"])
            if active:
                await asyncio.shield(active)
            task = await self.inspect(pid, task["id"])
            results.append(
                {
                    "test_id": case["id"],
                    "name": case["name"],
                    "mandatory": case["mandatory"],
                    "passed": task["status"] == "succeeded",
                    "project_task_id": task["id"],
                    "run_id": task["runs"][0]["id"] if task["runs"] else "",
                    "run_status": task["status"],
                    "run_error": task["error"],
                    "outputs": task["outputs"],
                }
            )
        return {
            "passed": all(r["passed"] for r in results if r["mandatory"]),
            "results": results,
            "environment": "simulation",
        }

    async def overview(self, pid):
        project = await self.projects.store.get(pid)
        workflows = []
        for member in project["members"]:
            draft = await self.services.workflow_store.get_draft(member["id"])
            definition = draft["snapshot"].workflow.model_dump()
            nodes = definition["nodes"]
            aliases = set()
            calls = []

            for value in graph_nodes(definition):
                config = value.get("config", {})
                if value.get("type") == "http_request":
                    aliases.add(
                        config.get("service_ref") or config.get("connection_id") or "(待配置)"
                    )
                if value.get("type") == "tool" and config.get("tool_name", "").startswith(
                    "workflow:"
                ):
                    calls.append(
                        {
                            "node_id": value["id"],
                            "workflow_id": config["tool_name"][9:],
                            "inputs": config.get("input", {}),
                            "nested": not any(value is node for node in nodes),
                        }
                    )
            workflows.append(
                {
                    **member,
                    "revision": draft["revision"],
                    "description": draft["snapshot"].description,
                    "inputs": next(
                        (n["config"].get("inputs", []) for n in nodes if n["type"] == "start"), []
                    ),
                    "outputs": sorted(
                        {
                            key
                            for n in nodes
                            if n["type"] == "end"
                            for key in n["config"].get("outputs", {})
                        }
                    ),
                    "services": sorted(aliases),
                    "calls": calls,
                }
            )
        return {
            "workflows": workflows,
            "progress": await self.projects.store.progress(pid),
            "tasks": [
                t for t in await self.projects.store.tasks(pid) if t["purpose"] == "simulation"
            ],
        }

    async def save_case(self, pid, wid, body: DebugCase):
        await self.projects.member(pid, wid)
        draft = await self.services.workflow_store.get_draft(wid)
        inputs = copy.deepcopy(body.inputs)
        if body.source.kind == "run":
            task = await self.projects.task(pid, body.source.task_id)
            source = task
            if body.source.run_id:
                source = next((r for r in task["runs"] if r["id"] == body.source.run_id), None)
            if not source or source["status"] != "succeeded":
                raise ValueError("请选择本项目已完成运行的实际输出")
            value = source["outputs"]
            for key in body.source.path:
                try:
                    value = value[int(key)] if isinstance(value, list) else value[str(key)]
                except (KeyError, IndexError, TypeError, ValueError):
                    raise ValueError("所选输出字段不存在") from None
            if body.source.input_name:
                inputs[body.source.input_name] = value
            elif isinstance(value, dict):
                inputs = value
            else:
                raise ValueError("请选择接收此输出的输入字段")
        case = WorkflowTestCase(
            id=body.id or str(uuid4()),
            name=body.name,
            requirement=body.requirement,
            inputs=inputs,
            debug={
                "bindings": {k: v.model_dump() for k, v in body.bindings.items()},
                "source": body.source.model_dump(),
            },
        )
        snapshot = draft["snapshot"].model_copy(deep=True)
        old = next((t for t in snapshot.tests if t.id == case.id), None)
        if old:
            case.assertions = old.assertions
            case.mandatory = old.mandatory
            if "requirement" not in body.model_fields_set:
                case.requirement = old.requirement
        snapshot.tests = [t for t in snapshot.tests if t.id != case.id] + [case]
        saved = await self.services.workflow_store.save_draft(
            wid,
            snapshot,
            expected_revision=body.expected_revision,
            idempotency_key="debug-case-" + str(uuid4()),
        )
        return {"case": case.model_dump(), "revision": saved["revision"]}

    def restrict(self, snapshots, wid):
        reached, visiting = set(), set()

        def visit(key):
            if key in visiting:
                raise ValueError("模拟调试的成员调用存在循环")
            if key in reached:
                return
            if key not in snapshots:
                raise ValueError("引用的工作流不在本项目")
            visiting.add(key)

            def graph(value):
                for node in value.get("nodes", []):
                    kind, config = node["type"], node.get("config", {})
                    if kind == "tool":
                        tool = config.get("tool_name", "")
                        if not tool.startswith("workflow:"):
                            raise ValueError("此工具尚未支持隔离模拟：" + tool)
                        visit(tool[9:])
                    elif kind not in {
                        "start",
                        "end",
                        "code",
                        "http_request",
                        "if_else",
                        "iteration",
                        "loop",
                        "template",
                        "variable_assigner",
                        "variable_aggregator",
                    }:
                        raise ValueError("此积木尚未支持隔离模拟：" + kind)
                    if kind == "http_request" and not (
                        config.get("service_ref") or config.get("connection_id")
                    ):
                        raise ValueError("模拟 HTTP 请选择逻辑服务或显式映射旧连接")
                    if isinstance(config.get("workflow"), dict):
                        graph(config["workflow"])

            graph(snapshots[key]["snapshot"]["workflow"])
            visiting.remove(key)
            reached.add(key)

        visit(wid)
        for key in list(snapshots):
            if key not in reached:
                del snapshots[key]

    async def run(self, pid, body: DebugRun):
        # One project lock covers request dedupe, session creation and task launch.
        async with self.locks.setdefault("start:" + pid, asyncio.Lock()):
            existing = await self.projects.store.task_for_request(pid, body.request_key)
            if existing:
                if existing["purpose"] != "simulation":
                    raise ProjectConflict("此请求编号已被其他任务使用")
                previous = await self.projects.store.get_task(pid, existing["id"], snapshots=True)
                previous_simulation = next(iter(previous["snapshots"].values()), {}).get(
                    "simulation", {}
                )
                if previous_simulation.get("request_digest") != digest(body.model_dump()):
                    raise ProjectConflict("同一请求编号的调试参数已变化，请使用新的请求编号")
                return await self.inspect(pid, existing["id"])
            await self.projects.member(pid, body.workflow_id)
            draft = await self.services.workflow_store.get_draft(body.workflow_id)
            case = next((c for c in draft["snapshot"].tests if c.id == body.case_id), None)
            if body.case_id and not case:
                raise ValueError("测试样例不存在")
            inputs = case.inputs if case else body.inputs
            bindings = (
                case.debug.get("bindings", {})
                if case
                else {k: v.model_dump() for k, v in body.bindings.items()}
            )
            services = {s["id"]: s for s in await self.services_list(pid)}
            frozen = {}
            for alias, binding in bindings.items():
                if not alias or "/" in alias or any(c in alias for c in "?#\\"):
                    raise ValueError("服务引用不能包含路径分隔符")
                spec = services.get(binding["service_id"])
                if not spec:
                    raise ValueError("模拟服务不属于本项目：" + binding["service_id"])
                scenario = binding.get("scenario", "")
                if scenario and scenario not in spec["scenarios"]:
                    raise ValueError("模拟场景不存在：" + scenario)
                frozen[alias] = {**spec, "scenario": scenario}
            sid = str(uuid4())
            config = {"services": frozen, "now": body.now, "seed": body.seed}
            if body.recovery_task_id:
                previous = await self.projects.store.get_task(
                    pid, body.recovery_task_id, snapshots=True
                )
                old = next(iter(previous["snapshots"].values()), {}).get("simulation")
                if not old:
                    raise ValueError("模拟修复只能引用模拟任务")
                sid = old["session_id"]
                old_config = self.session(pid, sid)
                if digest(frozen) != digest(old_config["services"]):
                    raise ValueError("修复保留原模拟服务版本；服务变化请重置并重跑")
                config = old_config
            simulation = {
                "session_id": sid,
                "case_id": body.case_id,
                "request_digest": digest(body.model_dump()),
                "case": case.model_dump() if case else None,
                "bindings": bindings,
                "configuration_digest": digest(config),
                "workflow_id": body.workflow_id,
            }

            def prepare(snapshots):
                if body.recovery_task_id:
                    self.ensure_session_idle(pid, sid)
                self.restrict(snapshots, body.workflow_id)

                for snapshot in snapshots.values():
                    for node in graph_nodes(snapshot["snapshot"]["workflow"]):
                        if node.get("type") == "http_request":
                            node_config = node.get("config", {})
                            alias = node_config.get("service_ref") or node_config.get("connection_id")
                            if alias not in frozen:
                                raise ValueError("缺少模拟服务绑定：" + str(alias))
                for item in snapshots.values():
                    item.update(
                        simulation=simulation,
                        http_connections={},
                        model_resources={},
                        knowledge_resources={},
                    )
                if not body.recovery_task_id:
                    with connect(self.db_path) as db:
                        db.execute(
                            "INSERT INTO project_simulation_sessions VALUES (?,?,?)",
                            (sid, pid, encode(config)),
                        )
                        for alias, spec in frozen.items():
                            state = copy.deepcopy(spec["initial_state"])
                            state.update(spec["scenarios"].get(spec["scenario"], {}))
                            db.execute(
                                "INSERT INTO project_simulation_states VALUES (?,?,0,?,?)",
                                (sid, alias, encode(state), "[]"),
                            )

            task = await self.projects.start(
                pid,
                request_key=body.request_key,
                workflow_id=body.workflow_id,
                inputs=inputs,
                purpose="simulation",
                message=case.name if case else "模拟调试",
                recovery_task_id=body.recovery_task_id,
                validate_snapshots=prepare,
            )
            return await self.inspect(pid, task["id"])

    def ensure_session_idle(self, pid, sid):
        # Called under the existing project launch/resume lock. Repair tasks
        # share one service state and must not race a different continuation.
        with connect(self.db_path) as db:
            rows = db.execute(
                "SELECT snapshots_json FROM project_tasks WHERE project_id=? AND status IN ('queued','running')",
                (pid,),
            )
            for row in rows:
                frozen = next(iter(json.loads(row["snapshots_json"]).values()), {})
                if (frozen.get("simulation") or {}).get("session_id") == sid:
                    raise ProjectConflict("此模拟状态正在被另一任务使用，请等待它结束再继续或修复")

    def session(self, pid, sid):
        with connect(self.db_path) as db:
            row = db.execute(
                "SELECT config_json FROM project_simulation_sessions WHERE id=? AND project_id=?",
                (sid, pid),
            ).fetchone()
        if not row:
            raise ValueError("模拟会话不属于本项目")
        return json.loads(row["config_json"])

    def connection(self, context, alias):
        sid = context["simulation"]["session_id"]
        config = self.session(context["project_id"], sid)
        spec = config["services"].get(alias)
        if not spec:
            raise ValueError("缺少模拟服务绑定：" + alias)
        item = {
            "id": alias,
            "name": spec["name"],
            "base_url": "http://simulation.invalid/" + sid + "/" + alias,
            "allowed_paths": spec["allowed_paths"],
            "allowed_methods": spec["allowed_methods"],
            "auth_header": "",
            "idempotency_header": spec["idempotency_header"],
            "enabled": True,
        }
        item["scope_digest"] = scope_digest(item)
        return item, ""

    def transport(self, context, alias):
        return SimulationTransport(
            self, context["project_id"], context["simulation"]["session_id"], alias
        )

    async def dispatch(self, pid, sid, alias, request):
        async with self.locks.setdefault("service:" + sid + ":" + alias, asyncio.Lock()):
            config = self.session(pid, sid)
            spec = config["services"][alias]
            target_url(
                self.connection({"project_id": pid, "simulation": {"session_id": sid}}, alias)[0],
                request["path"],
                request["method"],
            )
            with connect(self.db_path) as db:
                row = db.execute(
                    "SELECT * FROM project_simulation_states WHERE session_id=? AND alias=?",
                    (sid, alias),
                ).fetchone()
            state = json.loads(row["state_json"])
            context = {"now": config["now"], "seed": config["seed"], "scenario": spec["scenario"]}
            if spec["implementation"] == "records":
                value = handle(request, state, context)
            else:
                from pathlib import Path

                sandbox_id = "sim-" + sid + "-" + digest(alias)[:8]
                sandbox = self.services.sandboxes.sessions.get(sandbox_id)
                if sandbox is None:
                    workspace = (
                        Path(self.services.settings.workspace_root)
                        / ".simulators"
                        / sid
                        / str(uuid4())
                    )
                    workspace.mkdir(parents=True, exist_ok=False)
                    (workspace / "provider.py").write_text(spec["code"])
                    self.services.sandboxes.protect_inputs(workspace, ["provider.py"])
                    sandbox = await self.services.sandboxes.get_or_create(
                        sandbox_id, str(workspace), NetworkPolicy.none, []
                    )
                try:
                    result = await sandbox.run(
                        ["python", "-c", RUNNER],
                        stdin=encode({"request": request, "state": state, "context": context}),
                        timeout=20,
                        max_output=5_000_000,
                    )
                    if result.exit_code:
                        raise ValueError(
                            "模拟实现执行失败：" + (result.stderr or result.stdout)[-2000:]
                        )
                    if result.output_truncated:
                        raise ValueError("模拟响应与状态超过 5 MB")
                    value = json.loads(result.stdout)
                except BaseException:
                    await self.services.sandboxes.remove(sandbox.session_id)
                    raise
            result = SimulatorResult.model_validate(value)
            status = result.response.get("status", 200)
            if not isinstance(status, int) or not 100 <= status <= 599:
                raise ValueError("模拟实现返回了无效 HTTP 状态")
            requests = json.loads(row["requests_json"])
            requests.append(
                {"request": request, "response": result.response, "fault": result.fault}
            )
            with connect(self.db_path) as db:
                changed = db.execute(
                    "UPDATE project_simulation_states SET state_json=?,requests_json=?,revision=revision+1 WHERE session_id=? AND alias=? AND revision=?",
                    (encode(result.state), encode(requests), sid, alias, row["revision"]),
                ).rowcount
                if not changed:
                    raise ProjectConflict("模拟服务状态被另一执行更新，请继续原任务")
            return result

    async def release(self, context):
        sid = context["simulation"]["session_id"]
        for key in list(self.services.sandboxes.sessions):
            if key.startswith("sim-" + sid + "-"):
                await self.services.sandboxes.remove(key)

    def states(self, sid):
        with connect(self.db_path) as db:
            return {
                r["alias"]: {
                    "state": json.loads(r["state_json"]),
                    "requests": json.loads(r["requests_json"]),
                }
                for r in db.execute(
                    "SELECT * FROM project_simulation_states WHERE session_id=?", (sid,)
                )
            }

    def capture(self, context):
        states = self.states(context["simulation"]["session_id"])
        with connect(self.db_path) as db:
            db.execute(
                "INSERT OR REPLACE INTO project_simulation_observations VALUES (?,?)",
                (context["task_id"], encode(states)),
            )

    async def inspect(self, pid, task_id):
        task = await self.projects.task(pid, task_id)
        frozen = await self.projects.store.get_task(pid, task_id, snapshots=True)
        simulation = next(iter(frozen["snapshots"].values()), {}).get("simulation")
        if not simulation:
            raise ValueError("这不是模拟调试任务")
        sid = simulation["session_id"]
        config = self.session(pid, sid)
        with connect(self.db_path) as db:
            observation = db.execute(
                "SELECT states_json FROM project_simulation_observations WHERE task_id=?",
                (task_id,),
            ).fetchone()
        states = (
            json.loads(observation["states_json"])
            if observation and task["status"] not in {"running", "queued"}
            else self.states(sid)
        )
        current = await self.projects.freeze(pid)
        stale = any(
            key not in current or current[key]["content_hash"] != value["content_hash"]
            for key, value in frozen["snapshots"].items()
        )
        latest = {s["id"]: s for s in await self.services_list(pid)}
        stale = stale or any(
            s["id"] not in latest or latest[s["id"]]["revision"] != s["revision"]
            for s in config["services"].values()
        )
        return {
            **task,
            "environment": "simulation",
            "simulation": simulation,
            "services": states,
            "outdated": stale,
            "versions": {k: v["revision"] for k, v in frozen["snapshots"].items()},
            "service_versions": {
                alias: {
                    "service_id": s["id"],
                    "name": s["name"],
                    "revision": s["revision"],
                    "scenario": s["scenario"],
                }
                for alias, s in config["services"].items()
            },
        }

    async def finish(self, context, outputs):
        case = context["simulation"].get("case")
        failures = []
        runtime = self.services.workflow_runtime
        for assertion in (case or {}).get("assertions", []):
            try:
                value = runtime._resolve_assertion_path(outputs, assertion["path"])
                passed = runtime._assert(value, assertion["operator"], assertion.get("expected"))
            except (KeyError, IndexError, TypeError, ValueError):
                passed = False
            if not passed:
                failures.append(".".join(assertion["path"]) or "完整输出")
        if failures:
            raise ValueError("测试输出不符合预期：" + "、".join(failures))

    async def chain(self, pid, body: Chain):
        def ref(node, *path):
            return {"$ref": {"node_id": node, "path": list(path)}}

        nodes = [{"id": "start", "type": "start", "title": "开始", "config": {}}]
        for i, step in enumerate(body.steps):
            await self.projects.member(pid, step.workflow_id)
            if step.workflow_id == pid:
                raise ValueError("主流程不能调用自己")
            nodes.append(
                {
                    "id": "step_" + str(i + 1),
                    "type": "tool",
                    "title": (await self.services.workflow_store.get_application(step.workflow_id))[
                        "name"
                    ],
                    "config": {"tool_name": "workflow:" + step.workflow_id, "input": step.inputs},
                }
            )
        nodes.append(
            {
                "id": "end",
                "type": "end",
                "title": "串联结果",
                "config": {
                    "outputs": {
                        "result": ref(nodes[-1]["id"], "output"),
                        **{n["id"]: ref(n["id"], "output") for n in nodes[1:]},
                    }
                },
            }
        )
        graph = {
            "nodes": nodes,
            "edges": [
                {"id": a["id"] + "-" + b["id"], "source": a["id"], "target": b["id"]}
                for a, b in zip(nodes, nodes[1:])
            ],
        }
        draft = await self.services.workflow_store.get_draft(pid)
        snapshot = draft["snapshot"].model_copy(deep=True)
        snapshot.workflow = WorkflowSpec.model_validate(graph)
        return await self.services.workflow_store.save_draft(
            pid,
            snapshot,
            expected_revision=body.expected_revision,
            idempotency_key="debug-chain-" + str(uuid4()),
        )


def register_debug_routes(router, services, invoke):
    debug = services.projects.debug

    @router.get("/debug/overview")
    async def overview(project_id: str):
        return await invoke(debug.overview, project_id)

    @router.get("/simulators/presets")
    async def presets(project_id: str):
        await services.projects.store.get(project_id)
        return PRESETS

    @router.get("/simulators")
    async def listing(project_id: str):
        return await invoke(debug.services_list, project_id)

    @router.post("/simulators", status_code=201)
    async def create(project_id: str, body: SimulatorSpec):
        return await invoke(debug.save_service, project_id, "", body)

    @router.put("/simulators/{service_id}")
    async def update(project_id: str, service_id: str, body: SimulatorSpec):
        return await invoke(debug.save_service, project_id, service_id, body)

    @router.get("/members/{workflow_id}/debug-cases")
    async def cases(project_id: str, workflow_id: str):
        return await invoke(debug.cases, project_id, workflow_id)

    @router.put("/members/{workflow_id}/debug-cases")
    async def save_case(project_id: str, workflow_id: str, body: DebugCase):
        return await invoke(debug.save_case, project_id, workflow_id, body)

    @router.post("/debug/runs", status_code=202)
    async def run(project_id: str, body: DebugRun):
        return await invoke(debug.run, project_id, body)

    @router.get("/debug/runs/{task_id}")
    async def inspect(project_id: str, task_id: str):
        return await invoke(debug.inspect, project_id, task_id)

    @router.post("/debug/runs/{task_id}/resume", status_code=202)
    async def resume(project_id: str, task_id: str):
        await invoke(debug.inspect, project_id, task_id)
        await invoke(services.projects.resume, project_id, task_id)
        return await invoke(debug.inspect, project_id, task_id)

    @router.put("/debug/chain")
    async def chain(project_id: str, body: Chain):
        return await invoke(debug.chain, project_id, body)
