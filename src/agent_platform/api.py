from __future__ import annotations

import asyncio
import json
import shutil
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from typing import Any, AsyncIterator
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings, get_settings
from .applications import ApplicationService
from .blocks import BlockRegistry, build_block_registry
from .builder import WorkflowBuilder
from .factory import AgentFactory
from .models import (
    GenerationRequest,
    MessageRequest,
    PermissionDecision,
    SessionCreateRequest,
)
from .permissions import PermissionBroker
from .providers import DeepSeekProvider, ModelProvider
from .runtime import AgentRuntime
from .sandbox import SandboxManager
from .scheduler import WorkflowScheduler
from .storage import Storage
from .tools import ToolRegistry, build_core_registry
from .workflow_models import (
    ApplicationCreateRequest,
    BuildRequest,
    DraftOperation,
    ResumeRunRequest,
    ManualScheduleTriggerRequest,
    WorkflowRunRequest,
)
from .workflow_runtime import WorkflowRuntime
from .workflow_storage import PublishGateError, RevisionConflict, WorkflowStorage


@dataclass(slots=True)
class Services:
    settings: Settings
    storage: Storage
    provider: ModelProvider
    tools: ToolRegistry
    sandboxes: SandboxManager
    permissions: PermissionBroker
    runtime: AgentRuntime
    factory: AgentFactory
    blocks: BlockRegistry
    workflow_store: WorkflowStorage
    applications: ApplicationService
    workflow_runtime: WorkflowRuntime
    builder: WorkflowBuilder
    scheduler: WorkflowScheduler
    background_tasks: set[asyncio.Task[Any]]


def build_services(settings: Settings, provider: ModelProvider | None = None) -> Services:
    storage = Storage(settings.data_dir)
    tools = build_core_registry()
    sandboxes = SandboxManager(settings)
    permissions = PermissionBroker()
    provider = provider or DeepSeekProvider(
        settings.deepseek_api_key,
        settings.deepseek_base_url,
        settings.deepseek_timeout_seconds,
    )
    runtime = AgentRuntime(
        settings=settings,
        storage=storage,
        provider=provider,
        tools=tools,
        sandboxes=sandboxes,
        permissions=permissions,
    )
    factory = AgentFactory(
        settings=settings,
        storage=storage,
        provider=provider,
        runtime=runtime,
        tools=tools,
        sandboxes=sandboxes,
    )
    blocks = build_block_registry()
    workflow_store = WorkflowStorage(storage)
    applications = ApplicationService(workflow_store, blocks, tools)
    workflow_runtime = WorkflowRuntime(
        storage=storage,
        workflow_store=workflow_store,
        applications=applications,
        blocks=blocks,
        provider=provider,
        agent_runtime=runtime,
        tools=tools,
        sandboxes=sandboxes,
        runtime_model=settings.deepseek_runtime_model,
    )
    builder = WorkflowBuilder(
        storage=storage,
        workflow_store=workflow_store,
        applications=applications,
        blocks=blocks,
        runtime=workflow_runtime,
        provider=provider,
        agent_runtime=runtime,
        generator_model=settings.deepseek_generator_model,
        core_tools=tools,
    )
    scheduler = WorkflowScheduler(
        storage=storage,
        workflow_store=workflow_store,
        blocks=blocks,
        runtime=workflow_runtime,
        poll_seconds=settings.scheduler_poll_seconds,
    )
    return Services(
        settings=settings,
        storage=storage,
        provider=provider,
        tools=tools,
        sandboxes=sandboxes,
        permissions=permissions,
        runtime=runtime,
        factory=factory,
        blocks=blocks,
        workflow_store=workflow_store,
        applications=applications,
        workflow_runtime=workflow_runtime,
        builder=builder,
        scheduler=scheduler,
        background_tasks=set(),
    )


def create_app(settings: Settings | None = None, provider: ModelProvider | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.prepare()
    services = build_services(settings, provider)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await services.storage.initialize()
        await services.workflow_store.initialize()
        services.scheduler.start()
        yield
        await services.scheduler.stop()
        for task in services.background_tasks:
            task.cancel()
        await services.sandboxes.close()

    app = FastAPI(title=settings.app_name, version="0.2.0", lifespan=lifespan)
    app.state.services = services
    bearer = HTTPBearer(auto_error=False)

    async def require_token(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> None:
        supplied = credentials.credentials if credentials else request.query_params.get("token")
        if supplied != settings.api_token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API token")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "deepseek_configured": bool(settings.deepseek_api_key),
            "docker_available": shutil.which("docker") is not None,
            "provider": services.provider.name,
            "tools": services.tools.names(),
        }

    @app.get("/v1/models", dependencies=[Depends(require_token)])
    async def models() -> dict[str, Any]:
        return {
            "provider": "deepseek",
            "generator_model": settings.deepseek_generator_model,
            "runtime_model": settings.deepseek_runtime_model,
            "capabilities": asdict(services.provider.capabilities(settings.deepseek_runtime_model)),
        }

    @app.get("/api/v1/blocks", dependencies=[Depends(require_token)])
    async def list_blocks() -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in services.blocks.list()]

    @app.get("/api/v1/blocks/{block_type}", dependencies=[Depends(require_token)])
    async def get_block(block_type: str) -> dict[str, Any]:
        try:
            return services.blocks.get(block_type).model_dump(mode="json")
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.get("/api/v1/tools", dependencies=[Depends(require_token)])
    async def list_platform_tools() -> list[dict[str, Any]]:
        result = [
            {"name": name, "type": "core", "published": True}
            for name in services.tools.names()
        ]
        for application in await services.workflow_store.list_applications():
            if application["active_version"] is not None:
                result.append({
                    "name": f"workflow:{application['id']}",
                    "type": "workflow",
                    "title": application["name"],
                    "version": application["active_version"],
                    "published": True,
                })
        return result

    @app.get("/api/v1/schedules", dependencies=[Depends(require_token)])
    async def list_schedules() -> list[dict[str, Any]]:
        return await services.scheduler.list_schedules()

    @app.post("/api/v1/applications", status_code=201, dependencies=[Depends(require_token)])
    async def create_application(body: ApplicationCreateRequest) -> dict[str, Any]:
        return await services.workflow_store.create_application(body)

    @app.get("/api/v1/applications", dependencies=[Depends(require_token)])
    async def list_applications() -> list[dict[str, Any]]:
        return await services.workflow_store.list_applications()

    @app.get("/api/v1/applications/{application_id}", dependencies=[Depends(require_token)])
    async def get_application(application_id: str) -> dict[str, Any]:
        try:
            return await services.workflow_store.get_application(application_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.get("/api/v1/applications/{application_id}/draft", dependencies=[Depends(require_token)])
    async def get_application_draft(application_id: str) -> dict[str, Any]:
        try:
            draft = await services.workflow_store.get_draft(application_id)
            draft["snapshot"] = draft["snapshot"].model_dump(mode="json")
            return draft
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post("/api/v1/applications/{application_id}/draft", dependencies=[Depends(require_token)])
    async def mutate_application_draft(
        application_id: str, body: DraftOperation
    ) -> dict[str, Any]:
        try:
            return await services.applications.apply_operation(application_id, body)
        except RevisionConflict as error:
            raise HTTPException(409, str(error)) from error
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post(
        "/api/v1/applications/{application_id}/draft/validate",
        dependencies=[Depends(require_token)],
    )
    async def validate_application_draft(application_id: str) -> dict[str, Any]:
        try:
            return await services.applications.validate_draft(application_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post(
        "/api/v1/applications/{application_id}/builds",
        status_code=202,
        dependencies=[Depends(require_token)],
    )
    async def create_build(application_id: str, body: BuildRequest) -> dict[str, Any]:
        try:
            await services.workflow_store.get_application(application_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        build_id = str(uuid4())
        await services.workflow_store.create_build(
            build_id,
            application_id,
            body.requirement,
            body.auto_publish,
            body.max_turns,
            body.max_repair_cycles,
        )
        services.builder.start(build_id)
        return {"build_id": build_id, "application_id": application_id, "status": "queued"}

    @app.get(
        "/api/v1/applications/{application_id}/builds",
        dependencies=[Depends(require_token)],
    )
    async def list_application_builds(application_id: str) -> list[dict[str, Any]]:
        builds = await services.workflow_store.list_builds(application_id)
        for build in builds:
            build["team_state"] = build["team_state"].model_dump(mode="json")
        return builds

    @app.get("/api/v1/builds/{build_id}", dependencies=[Depends(require_token)])
    async def get_build(build_id: str) -> dict[str, Any]:
        try:
            build = await services.workflow_store.get_build(build_id)
            build["team_state"] = build["team_state"].model_dump(mode="json")
            return build
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post("/api/v1/builds/{build_id}/resume", dependencies=[Depends(require_token)])
    async def resume_build(build_id: str) -> dict[str, Any]:
        try:
            build = await services.workflow_store.get_build(build_id)
            if build["status"] not in {"needs_attention", "cancelled"}:
                raise HTTPException(409, f"build cannot resume from {build['status']}")
            await services.workflow_store.update_build(build_id, status="queued", error="")
            services.builder.start(build_id)
            return {"build_id": build_id, "status": "queued"}
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post("/api/v1/builds/{build_id}/cancel", dependencies=[Depends(require_token)])
    async def cancel_build(build_id: str) -> dict[str, Any]:
        try:
            services.builder.cancel(build_id)
            return {"build_id": build_id, "status": "cancelling"}
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post(
        "/api/v1/applications/{application_id}/tests/run",
        dependencies=[Depends(require_token)],
    )
    async def run_application_tests(application_id: str) -> dict[str, Any]:
        try:
            return await services.workflow_runtime.run_test_suite(application_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.get(
        "/api/v1/applications/{application_id}/versions",
        dependencies=[Depends(require_token)],
    )
    async def list_application_versions(application_id: str) -> list[dict[str, Any]]:
        return await services.workflow_store.list_versions(application_id)

    @app.post(
        "/api/v1/applications/{application_id}/versions",
        dependencies=[Depends(require_token)],
    )
    async def publish_application(application_id: str) -> dict[str, Any]:
        try:
            return await services.workflow_store.publish(application_id)
        except PublishGateError as error:
            raise HTTPException(409, str(error)) from error
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post(
        "/api/v1/applications/{application_id}/versions/{version}/restore",
        dependencies=[Depends(require_token)],
    )
    async def restore_application_version(application_id: str, version: int) -> dict[str, Any]:
        try:
            return await services.workflow_store.restore_version(application_id, version)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post(
        "/api/v1/applications/{application_id}/runs",
        status_code=202,
        dependencies=[Depends(require_token)],
    )
    async def create_workflow_run(
        application_id: str, body: WorkflowRunRequest
    ) -> dict[str, Any]:
        try:
            return await services.workflow_runtime.create_run(application_id, body)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except (ValueError, RuntimeError) as error:
            raise HTTPException(422, str(error)) from error

    @app.post(
        "/api/v1/applications/{application_id}/schedules/trigger",
        status_code=202,
        dependencies=[Depends(require_token)],
    )
    async def trigger_application_schedule(
        application_id: str, body: ManualScheduleTriggerRequest
    ) -> dict[str, Any]:
        try:
            return await services.scheduler.trigger_now(application_id, body.inputs)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.get("/api/v1/runs/{run_id}", dependencies=[Depends(require_token)])
    async def get_workflow_run(run_id: str) -> dict[str, Any]:
        try:
            run = await services.workflow_store.get_run(run_id)
            run["state"] = run["state"].model_dump(mode="json")
            return run
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post("/api/v1/runs/{run_id}/resume", dependencies=[Depends(require_token)])
    async def resume_workflow_run(run_id: str, body: ResumeRunRequest) -> dict[str, Any]:
        try:
            return await services.workflow_runtime.resume(run_id, body.values)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except RuntimeError as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/v1/runs/{run_id}/cancel", dependencies=[Depends(require_token)])
    async def cancel_workflow_run(run_id: str) -> dict[str, Any]:
        try:
            services.workflow_runtime.cancel(run_id)
            return {"run_id": run_id, "status": "cancelling"}
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post("/v1/agent-generations", status_code=202, dependencies=[Depends(require_token)])
    async def generate_agent(body: GenerationRequest) -> dict[str, str]:
        generation_id = str(uuid4())
        if body.workspace_path:
            services.sandboxes.resolve_workspace(body.workspace_path)
        await services.storage.create_generation(generation_id, body.requirement, body.workspace_path)
        task = asyncio.create_task(services.factory.generate(generation_id, body))
        services.background_tasks.add(task)
        task.add_done_callback(services.background_tasks.discard)
        return {"generation_id": generation_id, "status": "queued"}

    @app.get("/v1/agent-generations/{generation_id}", dependencies=[Depends(require_token)])
    async def get_generation(generation_id: str) -> dict[str, Any]:
        try:
            return await services.storage.get_generation(generation_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.get("/v1/agents", dependencies=[Depends(require_token)])
    async def list_agents() -> list[dict[str, Any]]:
        return await services.storage.list_agents()

    @app.get("/v1/agents/{agent_id}", dependencies=[Depends(require_token)])
    async def get_agent(agent_id: str, version: int | None = None) -> dict[str, Any]:
        try:
            spec, resolved_version, version_status = await services.storage.get_agent(agent_id, version)
            return {
                "version": resolved_version,
                "status": version_status,
                "spec": spec.model_dump(mode="json"),
            }
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post("/v1/agents/{agent_id}/versions/{version}/publish", dependencies=[Depends(require_token)])
    async def publish_agent(agent_id: str, version: int) -> dict[str, Any]:
        try:
            await services.storage.publish_agent(agent_id, version)
            return {"agent_id": agent_id, "version": version, "status": "published"}
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post("/v1/sessions", status_code=201, dependencies=[Depends(require_token)])
    async def create_session(body: SessionCreateRequest) -> dict[str, Any]:
        try:
            spec, version, version_status = await services.storage.get_agent(
                body.agent_id, body.agent_version
            )
            if version_status != "published" and body.agent_version is None:
                raise HTTPException(409, "agent version is not published")
            services.sandboxes.resolve_workspace(body.workspace_path)
            session = await services.runtime.create_session(spec, version, body.workspace_path)
            return {"session_id": session.id, "status": "ready"}
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.get("/v1/sessions/{session_id}", dependencies=[Depends(require_token)])
    async def get_session(session_id: str) -> dict[str, Any]:
        try:
            record = await services.storage.get_session(session_id)
            record["messages"] = [item.model_dump(mode="json") for item in record["messages"]]
            return record
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post("/v1/sessions/{session_id}/messages", status_code=202, dependencies=[Depends(require_token)])
    async def send_message(session_id: str, body: MessageRequest) -> dict[str, str]:
        try:
            turn_id = await services.runtime.start_turn(session_id, body.content)
            return {"session_id": session_id, "turn_id": turn_id, "status": "running"}
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except RuntimeError as error:
            raise HTTPException(409, str(error)) from error

    @app.post(
        "/v1/sessions/{session_id}/permissions/{request_id}",
        dependencies=[Depends(require_token)],
    )
    async def resolve_permission(
        session_id: str, request_id: str, body: PermissionDecision
    ) -> dict[str, str]:
        try:
            services.permissions.resolve(request_id, body, session_id)
            return {"request_id": request_id, "status": "resolved"}
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post("/v1/sessions/{session_id}/cancel", dependencies=[Depends(require_token)])
    async def cancel(session_id: str) -> dict[str, str]:
        try:
            services.runtime.cancel(session_id)
            return {"session_id": session_id, "status": "cancelling"}
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    async def sse_stream(stream_id: str, after: int) -> AsyncIterator[str]:
        iterator = services.storage.subscribe(stream_id, after).__aiter__()
        pending = asyncio.create_task(iterator.__anext__())
        try:
            while True:
                done, _ = await asyncio.wait({pending}, timeout=15)
                if not done:
                    yield ": keep-alive\n\n"
                    continue
                try:
                    event = pending.result()
                except StopAsyncIteration:
                    return
                payload = json.dumps(event.data, ensure_ascii=False, separators=(",", ":"))
                yield f"id: {event.id}\nevent: {event.type}\ndata: {payload}\n\n"
                pending = asyncio.create_task(iterator.__anext__())
        finally:
            pending.cancel()

    @app.get("/v1/streams/{stream_id}/events", dependencies=[Depends(require_token)])
    async def events(stream_id: str, request: Request, after: int = 0) -> StreamingResponse:
        header = request.headers.get("last-event-id")
        if header and header.isdigit():
            after = max(after, int(header))
        return StreamingResponse(
            sse_stream(stream_id, after),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/v1/builds/{build_id}/events", dependencies=[Depends(require_token)])
    async def build_events(build_id: str, request: Request, after: int = 0) -> StreamingResponse:
        header = request.headers.get("last-event-id")
        if header and header.isdigit():
            after = max(after, int(header))
        return StreamingResponse(
            sse_stream(build_id, after),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/v1/runs/{run_id}/events", dependencies=[Depends(require_token)])
    async def run_events(run_id: str, request: Request, after: int = 0) -> StreamingResponse:
        header = request.headers.get("last-event-id")
        if header and header.isdigit():
            after = max(after, int(header))
        return StreamingResponse(
            sse_stream(run_id, after),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/v1/streams/{stream_id}", dependencies=[Depends(require_token)])
    async def list_stream_events(stream_id: str, after: int = 0) -> list[dict[str, Any]]:
        events = await services.storage.list_events(stream_id, after)
        return [event.model_dump(mode="json") for event in events]

    @app.get("/debug", response_class=HTMLResponse)
    async def debug_page() -> str:
        return DEBUG_HTML

    return app


app = create_app()


DEBUG_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Agent Platform Debug</title>
<style>body{font:14px system-ui;margin:2rem;max-width:1100px;background:#101215;color:#e8e8e8}
input,textarea,button{font:inherit;padding:.6rem;background:#1b1f24;color:#eee;border:1px solid #444;border-radius:6px}
textarea{width:100%;min-height:100px}button{cursor:pointer;margin:.3rem}pre{white-space:pre-wrap;background:#08090a;padding:1rem;max-height:480px;overflow:auto}.row{display:flex;gap:.6rem}.row input{flex:1}</style></head>
<body><h1>Agent Platform</h1><div class="row"><input id="token" type="password" placeholder="API token"><input id="workspace" value="demo" placeholder="workspace path"></div>
<h2>1. 根据需求生成</h2><textarea id="requirement">生成一个能够分析并修复 Python 项目测试失败的智能体。它必须先运行测试、定位根因、最小化修改并重新验证。</textarea><button onclick="generate()">生成并验证</button>
<h2>2. 运行已发布智能体</h2><div class="row"><input id="agent" placeholder="agent id"><button onclick="session()">创建会话</button><input id="session" placeholder="session id"></div>
<textarea id="message">检查项目，修复失败的测试，并运行测试确认。</textarea><button onclick="send()">发送任务</button><button onclick="approve()">批准最新权限</button>
<h2>事件</h2><pre id="events"></pre>
<script>
let pending=null; const out=document.querySelector('#events');
const token=()=>document.querySelector('#token').value; const headers=()=>({'Authorization':'Bearer '+token(),'Content-Type':'application/json'});
function log(x){out.textContent+=x+'\n';out.scrollTop=out.scrollHeight}
function watch(id){const es=new EventSource('/v1/streams/'+id+'/events?token='+encodeURIComponent(token()));es.onmessage=e=>log(e.data);['generation.started','generation.model.thinking.delta','generation.model.text.delta','generation.spec.created','generation.validation.started','generation.validation.completed','generation.published','generation.failed','model.thinking.delta','model.text.delta','tool.started','tool.completed','tool.failed','permission.requested','turn.completed','turn.failed'].forEach(t=>es.addEventListener(t,e=>{log(t+' '+e.data);const d=JSON.parse(e.data);if(t==='generation.spec.created')document.querySelector('#agent').value=d.agent_id;if(t==='permission.requested')pending=d.request_id}))}
async function generate(){const r=await fetch('/v1/agent-generations',{method:'POST',headers:headers(),body:JSON.stringify({requirement:document.querySelector('#requirement').value,workspace_path:document.querySelector('#workspace').value,auto_publish:true})});const d=await r.json();log(JSON.stringify(d));if(d.generation_id)watch(d.generation_id)}
async function session(){const r=await fetch('/v1/sessions',{method:'POST',headers:headers(),body:JSON.stringify({agent_id:document.querySelector('#agent').value,workspace_path:document.querySelector('#workspace').value})});const d=await r.json();log(JSON.stringify(d));if(d.session_id){document.querySelector('#session').value=d.session_id;watch(d.session_id)}}
async function send(){const id=document.querySelector('#session').value;const r=await fetch('/v1/sessions/'+id+'/messages',{method:'POST',headers:headers(),body:JSON.stringify({content:document.querySelector('#message').value})});log(await r.text())}
async function approve(){if(!pending)return;const id=document.querySelector('#session').value;const r=await fetch('/v1/sessions/'+id+'/permissions/'+pending,{method:'POST',headers:headers(),body:JSON.stringify({behavior:'allow'})});log(await r.text());pending=null}
</script></body></html>"""
