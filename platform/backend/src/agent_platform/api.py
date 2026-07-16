from __future__ import annotations

import asyncio
import json
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
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
from .governed_task import GovernedTask
from .models import (
    GenerationRequest,
    MessageRequest,
    PermissionDecision,
    SessionCreateRequest,
)
from .permissions import PermissionBroker
from .providers import ModelProvider
from .providers.multi import MultiProvider
from .runtime import AgentRuntime
from .sandbox import SandboxManager
from .scheduler import WorkflowScheduler
from .storage import Storage
from .template_models import TemplateCreateRequest
from .template_store import TemplateStore
from .tools import ToolRegistry, build_core_registry
from .workflow_models import (
    ApplicationCreateRequest,
    BuildRequest,
    ClyinsRunRequest,
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
    templates: TemplateStore
    governed_tasks: dict[str, GovernedTask]
    background_tasks: set[asyncio.Task[Any]]


def build_services(settings: Settings, provider: ModelProvider | None = None) -> Services:
    storage = Storage(settings.data_dir)
    # Templates must be created before tools so EvolutionGateTool gets the store
    templates = TemplateStore()
    templates_dir = settings.templates_dir
    if templates_dir and templates_dir.is_dir():
        loaded = templates.load_builtins(templates_dir)
        print(f"[api] Loaded {loaded} built-in templates from {templates_dir}")
    tools = build_core_registry(template_store=templates)
    sandboxes = SandboxManager(settings)
    permissions = PermissionBroker()
    provider = provider or MultiProvider(
        deepseek_api_key=settings.deepseek_api_key,
        deepseek_base_url=settings.deepseek_base_url,
        openai_api_key=settings.openai_api_key,
        openai_base_url=settings.openai_base_url,
        anthropic_api_key=settings.anthropic_api_key,
        anthropic_base_url=settings.anthropic_base_url,
        timeout_seconds=settings.deepseek_timeout_seconds,
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
        template_store=templates,
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
        templates=templates,
        governed_tasks={},
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
        await services.workflow_store.fail_interrupted_runs()
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
            "docker_available": shutil.which("docker") is not None,
            "provider": services.provider.name,
            "tools": services.tools.names(),
            "configured_providers": getattr(services.provider, "configured_providers", []),
            "providers": {
                "deepseek": bool(settings.deepseek_api_key),
                "openai": bool(settings.openai_api_key),
                "anthropic": bool(settings.anthropic_api_key),
            },
        }

    @app.get("/v1/models", dependencies=[Depends(require_token)])
    async def models() -> dict[str, Any]:
        return {
            "provider": services.provider.name,
            "type": services.provider.name,  # new key, backward compat
            "configured_providers": getattr(services.provider, "configured_providers", ["deepseek"]),
            "configured_models": getattr(services.provider, "configured_models", []),
            "generator_model": settings.deepseek_generator_model,
            "runtime_model": settings.deepseek_runtime_model,
            "capabilities": asdict(services.provider.capabilities(settings.deepseek_runtime_model)),
        }

    @app.get("/api/v1/blocks", dependencies=[Depends(require_token)])
    async def list_blocks() -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in services.blocks.list()]

    @app.get("/api/v1/block-manuals", dependencies=[Depends(require_token)])
    async def list_block_manuals(
        query: str = "",
        block_kind: str | None = None,
    ) -> list[dict[str, Any]]:
        return services.blocks.manuals(query=query, block_kind=block_kind)

    @app.get("/api/v1/claude-architecture-blueprint", dependencies=[Depends(require_token)])
    async def claude_architecture_blueprint() -> dict[str, Any]:
        return services.blocks.claude_architecture_blueprint()

    @app.get("/api/v1/blocks/{block_type}", dependencies=[Depends(require_token)])
    async def get_block(block_type: str) -> dict[str, Any]:
        try:
            return services.blocks.get(block_type).model_dump(mode="json")
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.get("/api/v1/blocks/{block_type}/manual", dependencies=[Depends(require_token)])
    async def get_block_manual(block_type: str) -> dict[str, Any]:
        try:
            return services.blocks.manual(block_type)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    # ── Templates ───────────────────────────────────────────

    @app.get("/api/v1/templates", dependencies=[Depends(require_token)])
    async def list_templates(
        category: str | None = None,
        query: str = "",
    ) -> list[dict[str, Any]]:
        return [meta.model_dump(mode="json") for meta in services.templates.list(category=category, query=query)]

    @app.get("/api/v1/templates/categories", dependencies=[Depends(require_token)])
    async def template_categories() -> list[str]:
        return services.templates.categories()

    @app.post(
        "/api/v1/templates/{name}/rate",
        dependencies=[Depends(require_token)],
    )
    async def rate_template(name: str, body: dict[str, Any] = {}) -> dict[str, Any]:
        """Rate a template 1-5. Affects quality_score and ranking."""
        rating = int(body.get("rating", 3))
        if not 1 <= rating <= 5:
            raise HTTPException(422, "rating must be 1-5")
        try:
            template = services.templates.get(name)
        except KeyError:
            raise HTTPException(404, f"template not found: {name}")
        template.meta.rating_sum += rating
        template.meta.rating_count += 1
        return {
            "name": name,
            "rating": template.meta.rating,
            "rating_count": template.meta.rating_count,
            "quality_score": template.meta.quality_score,
        }

    @app.get("/api/v1/templates/suggestions", dependencies=[Depends(require_token)])
    async def suggest_templates(requirement: str = "") -> list[dict[str, Any]]:
        """Suggest matching templates for a requirement, sorted by relevance."""
        if not requirement:
            return []

        query = requirement.casefold()
        scored: list[tuple[float, Any]] = []

        for meta in services.templates.list():
            searchable = f"{meta.name} {meta.title} {meta.description} {' '.join(meta.tags)}".casefold()
            tag_matches = sum(
                1 for tag in meta.tags
                if tag.casefold() in query or any(
                    word in tag.casefold() for word in query.split()
                )
            )
            name_match = 1.0 if any(
                word in searchable for word in query.split() if len(word) > 3
            ) else 0.0
            score = meta.confidence * (0.5 * tag_matches + 0.5 * name_match)
            if score > 0.1:
                scored.append((score, meta))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {**meta.model_dump(mode="json"), "relevance_score": round(score, 3)}
            for score, meta in scored[:5]
        ]

    @app.get("/api/v1/templates/{name}", dependencies=[Depends(require_token)])
    async def get_template(name: str) -> dict[str, Any]:
        try:
            return services.templates.get(name).model_dump(mode="json")
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post(
        "/api/v1/templates/{name}/expand",
        dependencies=[Depends(require_token)],
    )
    async def expand_template(
        name: str,
        prefix: str = "",
        x: float = 0,
        y: float = 0,
    ) -> dict[str, Any]:
        try:
            wf = services.templates.expand_into_workflow(name, prefix=prefix, x=x, y=y)
            return wf.model_dump(mode="json")
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post(
        "/api/v1/applications/{application_id}/publish-template",
        status_code=201,
        dependencies=[Depends(require_token)],
    )
    async def publish_template(
        application_id: str, body: TemplateCreateRequest
    ) -> dict[str, Any]:
        try:
            draft = await services.workflow_store.get_draft(application_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        app = await services.workflow_store.get_application(application_id)
        name = app["name"].lower().replace(" ", "_").replace("-", "_")
        template = services.templates.register(
            name,
            draft["snapshot"].workflow,
            meta_overrides={
                "title": body.title or app["name"],
                "description": body.description or app["description"],
                "category": body.category,
                "tags": body.tags,
                "icon": body.icon,
                "author": "user",
            },
        )
        return template.model_dump(mode="json")

    # ── Meta-Cognition (session extraction) ──────────────────

    @app.post(
        "/api/v1/sessions/{session_id}/extract-template",
        dependencies=[Depends(require_token)],
    )
    async def extract_template_from_session(session_id: str) -> dict[str, Any]:
        """Try to extract a workflow template from a session's decision history."""
        try:
            record = await services.storage.get_session(session_id)
        except KeyError:
            raise HTTPException(404, f"session not found: {session_id}")

        from .meta_cognition import DecisionTracker
        from .extraction_gate import ExtractionGate

        # Build a DecisionTracker from session messages
        tracker = DecisionTracker(f"Session {session_id[:8]}")
        messages = record.get("messages", [])
        # Extract decision points from user/assistant message pairs
        decision_count = 0
        for i, msg in enumerate(messages):
            if msg.get("role") == "user" and i + 1 < len(messages):
                if messages[i + 1].get("role") == "assistant":
                    question = "".join(
                        b.get("text", "") for b in msg.get("content", [])
                        if b.get("type") == "text"
                    )[:200]
                    answer = "".join(
                        b.get("text", "") for b in messages[i + 1].get("content", [])
                        if b.get("type") == "text"
                    )[:200]
                    if question and answer and len(question) > 20:
                        tracker._current = tracker.ask(question, f"Session {session_id[:8]}")
                        tracker.answer("continue", answer)
                        decision_count += 1

        # Gate check
        gate = ExtractionGate(services.templates)
        should, reason = gate.should_propose(tracker.roots)

        if not should:
            return {"proposed": False, "reason": reason, "decision_points": decision_count}

        wf = tracker.extract_workflow()
        return {
            "proposed": True,
            "workflow": wf.model_dump(mode="json") if wf else None,
            "summary": tracker.summary(),
            "decision_points": decision_count,
            "similar_templates": [],
        }

    @app.post(
        "/api/v1/templates/{name}/merge-check",
        dependencies=[Depends(require_token)],
    )
    async def check_template_merge(
        name: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Check if a candidate workflow should be merged into an existing template."""
        from .merge_engine import MergeEngine
        from .workflow_models import WorkflowSpec

        try:
            services.templates.get(name)
        except KeyError:
            raise HTTPException(404, f"template not found: {name}")

        try:
            candidate = WorkflowSpec.model_validate(body.get("candidate", {}))
        except Exception as e:
            raise HTTPException(422, f"invalid candidate workflow: {e}")

        engine = MergeEngine(services.templates)
        result = engine.check_similarity(candidate)

        return {
            "should_merge": result.should_merge,
            "target_template": result.target_template,
            "similarity_score": result.similarity_score,
            "confidence_after": result.confidence_after,
            "diff_summary": result.diff_summary,
        }

    @app.post(
        "/api/v1/templates/{name}/merge",
        dependencies=[Depends(require_token)],
    )
    async def merge_template(
        name: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Merge a candidate workflow into an existing template."""
        from .merge_engine import MergeEngine
        from .workflow_models import WorkflowSpec
        from .template_models import ProvenanceSource
        from datetime import datetime, timezone

        try:
            candidate = WorkflowSpec.model_validate(body.get("candidate", {}))
        except Exception as e:
            raise HTTPException(422, f"invalid candidate workflow: {e}")

        source = ProvenanceSource(
            source_type=body.get("source_type", "session_extract"),
            identifier=body.get("identifier", ""),
            created_at=body.get("created_at", datetime.now(timezone.utc).isoformat()),
            user_id=body.get("user_id"),
        )

        engine = MergeEngine(services.templates)
        confirm = body.get("confirm", False)
        if not confirm:
            # Return what would happen without executing
            sim = engine.check_similarity(candidate)
            return {
                "dry_run": True,
                "should_merge": sim.should_merge,
                "confidence_after": sim.confidence_after,
                "diff_summary": sim.diff_summary,
            }

        merged = engine.merge(candidate, name, source)
        if merged is None:
            raise HTTPException(404, f"merge failed for template: {name}")

        return merged.model_dump(mode="json")

    # ── Orchestration Advisor ──────────────────────────────────

    @app.get(
        "/api/v1/orchestration/advise",
        dependencies=[Depends(require_token)],
    )
    async def orchestration_advise(
        requirement: str = "",
    ) -> dict[str, Any]:
        """Recommend block sequences, blocks, and templates for a requirement."""
        from .orchestration_advisor import OrchestrationAdvisor
        advisor = OrchestrationAdvisor(services.blocks, services.templates)
        return advisor.recommend_all(requirement)

    # ── Observability ─────────────────────────────────────────

    @app.get(
        "/api/v1/runs/{run_id}/metrics",
        dependencies=[Depends(require_token)],
    )
    async def run_metrics(run_id: str) -> dict[str, Any]:
        """Get detailed metrics for a completed workflow run."""
        from .observability import RunAnalyzer, render_metrics_summary
        analyzer = RunAnalyzer(services.storage)
        metrics = await analyzer.analyze(run_id)
        if metrics is None:
            raise HTTPException(404, f"no events found for run: {run_id}")
        return {
            "metrics": {
                "run_id": metrics.run_id,
                "status": metrics.status,
                "total_elapsed_ms": metrics.total_elapsed_ms,
                "total_input_tokens": metrics.total_input_tokens,
                "total_output_tokens": metrics.total_output_tokens,
                "total_cost_usd": metrics.total_cost_usd,
                "node_count": metrics.node_count,
                "tool_call_count": metrics.tool_call_count,
                "error_count": metrics.error_count,
                "failure_pattern": metrics.failure_pattern,
            },
            "node_breakdown": [
                {
                    "node_id": n.node_id,
                    "node_type": n.node_type,
                    "title": n.title,
                    "elapsed_ms": n.elapsed_ms,
                    "input_tokens": n.input_tokens,
                    "output_tokens": n.output_tokens,
                    "cost_usd": n.cost_usd,
                    "failed": n.failed,
                    "retry_count": n.retry_count,
                }
                for n in (metrics.nodes or [])[:20]
            ],
            "summary_markdown": render_metrics_summary(metrics),
        }

    @app.get(
        "/api/v1/applications/{application_id}/failure-patterns",
        dependencies=[Depends(require_token)],
    )
    async def application_failure_patterns(
        application_id: str,
    ) -> list[dict[str, Any]]:
        """Get failure pattern clusters for an application."""
        from .observability import RunAnalyzer
        analyzer = RunAnalyzer(services.storage)
        patterns = await analyzer.failure_patterns(application_id)
        return [
            {
                "pattern_name": p.pattern_name,
                "count": p.count,
                "example_run_ids": p.example_run_ids,
            }
            for p in patterns
        ]

    # ── Module Protocol Validation ────────────────────────────

    @app.get(
        "/api/v1/module-protocol/validate",
        dependencies=[Depends(require_token)],
    )
    async def validate_module_output(
        data: str = "",
    ) -> dict[str, Any]:
        """Check if a JSON value conforms to the ModuleOutput envelope."""
        from .module_protocol import is_envelope
        import json as _json
        try:
            parsed = _json.loads(data) if data else {}
        except _json.JSONDecodeError:
            return {"valid": False, "reason": "invalid JSON"}
        return {
            "valid": is_envelope(parsed),
            "has_result": "result" in parsed,
            "has_structured": "structured" in parsed,
            "has_module_name": "module_name" in parsed,
        }

    # ── Block Families (agent architecture grouping) ───────────

    @app.get(
        "/api/v1/blocks/families",
        dependencies=[Depends(require_token)],
    )
    async def list_block_families(
        family: str | None = None,
    ) -> dict[str, Any]:
        """List agent-architecture block families and their member blocks.

        Families are a *property of blocks*, not blocks themselves. Each
        family groups discrete, independently testable runtime blocks that
        share a common architectural concern (context, model loop, tools,
        governance, multi-agent, skill/MCP).
        """
        from .block_families import (
            FAMILY_MAP, list_families, strategy_help, get_discrete_block_type,
        )
        if family and family in FAMILY_MAP:
            members = {
                s: {
                    "help": strategy_help(s),
                    "block_type": get_discrete_block_type(s),
                }
                for s in FAMILY_MAP[family]
            }
            return {"family": family, "members": members}

        result = {}
        for fam, strategies in FAMILY_MAP.items():
            result[fam] = {
                s: {
                    "help": strategy_help(s),
                    "block_type": get_discrete_block_type(s),
                }
                for s in strategies
            }
        return {"families": list_families(), "members_by_family": result}

    # ── Tools ────────────────────────────────────────────────

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

    # ── Auto meta-cognition hook: Builder → template evolution ──

    async def _auto_extract_from_build(build_id: str) -> None:
        """After a build completes, evolve templates from the ACTUAL built workflow.

        Uses GovernedTask for timeout, cancellation, state machine, and audit events,
        closing the governance gap identified in the Platform Harness asset.

        Runs as a fire-and-forget governed task — never blocks the build response.
        """
        gov = GovernedTask(
            name=f"auto-evolve:{build_id[:8]}",
            max_timeout_seconds=120,
            emit=services.storage.append_event,
        )
        services.governed_tasks[build_id] = gov

        async def _evolve() -> None:
            build = await services.workflow_store.get_build(build_id)
            if build.get("status") not in ("published", "ready"):
                return

            from .evolution_engine import EvolutionEngine

            # Use the ACTUAL built workflow, not DecisionTracker's derived version
            draft = await services.workflow_store.get_draft(build["application_id"])
            candidate_wf = draft["snapshot"].workflow
            requirement = build.get("requirement", "")

            await services.storage.append_event(build_id, "template.evolution.started", {
                "build_id": build_id,
                "node_count": len(candidate_wf.nodes),
                "edge_count": len(candidate_wf.edges),
                "requirement": requirement[:200],
            })

            engine = EvolutionEngine(services.templates)
            result = engine.evolve_or_create(candidate_wf, requirement, build_id)

            if result.evolved:
                await services.storage.append_event(build_id, "template.evolved", {
                    "build_id": build_id,
                    "mode": result.mode,
                    "template_name": result.template_name,
                    "similarity_score": result.similarity_score,
                    "confidence_after": result.confidence_after,
                    "nodes_added": result.nodes_added,
                    "edges_added": result.edges_added,
                })
                print(
                    f"[auto-evolve] Build {build_id[:8]}: {result.mode} → "
                    f"{result.template_name} "
                    f"(sim={result.similarity_score:.2f}, "
                    f"conf={result.confidence_after:.2f}, "
                    f"+{result.nodes_added}n/+{result.edges_added}e)"
                )
            else:
                await services.storage.append_event(build_id, "template.evolution.skipped", {
                    "build_id": build_id,
                    "reason": result.gate_reason,
                    "mode": result.mode,
                })
                print(
                    f"[auto-evolve] Build {build_id[:8]}: skipped "
                    f"({result.mode}: {result.gate_reason})"
                )

        bg_task = gov.run(build_id, _evolve())
        # Cleanup completed tasks from the registry (keeps memory bounded)
        async def _cleanup(task: asyncio.Task[Any]) -> None:
            try:
                await task
            except Exception:
                pass
            services.governed_tasks.pop(build_id, None)

        services.background_tasks.add(
            asyncio.create_task(_cleanup(bg_task))
        )

    # Wire the evolution hook: every completed build triggers template evolution
    services.builder.on_build_complete = _auto_extract_from_build

    # ── PWA DingTalk App ─────────────────────────────────────

    _pwa_dir = Path(__file__).resolve().parent.parent.parent.parent.parent / "mobile_app"

    @app.get("/dingtalk.html", response_class=HTMLResponse)
    async def dingtalk_pwa() -> str:
        p = _pwa_dir / "index.html"
        return p.read_text(encoding="utf-8") if p.exists() else "PWA not found"

    @app.get("/dingtalk-icon-192.png")
    async def dingtalk_icon_192():
        from fastapi.responses import FileResponse
        p = _pwa_dir / "dingtalk-icon-192.png"
        return FileResponse(p, media_type="image/png") if p.exists() else HTMLResponse("", status_code=404)

    @app.get("/dingtalk-icon-512.png")
    async def dingtalk_icon_512():
        from fastapi.responses import FileResponse
        p = _pwa_dir / "dingtalk-icon-512.png"
        return FileResponse(p, media_type="image/png") if p.exists() else HTMLResponse("", status_code=404)

    @app.get("/manifest.json")
    async def dingtalk_manifest():
        from fastapi.responses import FileResponse
        p = _pwa_dir / "manifest.json"
        return FileResponse(p, media_type="application/json") if p.exists() else HTMLResponse("", status_code=404)

    @app.get("/sw.js")
    async def dingtalk_sw():
        from fastapi.responses import FileResponse
        p = _pwa_dir / "sw.js"
        return FileResponse(p, media_type="application/javascript") if p.exists() else HTMLResponse("", status_code=404)

    # ── Dashboard ────────────────────────────────────────────

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard() -> str:
        p = _pwa_dir / "dashboard.html"
        return p.read_text(encoding="utf-8") if p.exists() else "<h1>Dashboard not found</h1>"

    # ── BlockFlow Canvas ──────────────────────────────────────

    @app.get("/canvas.html", response_class=HTMLResponse)
    async def canvas_page() -> str:
        p = _pwa_dir / "canvas.html"
        return p.read_text(encoding="utf-8") if p.exists() else "<h1>Canvas not found</h1>"

    # ── Template Marketplace ──────────────────────────────────

    @app.get("/marketplace.html", response_class=HTMLResponse)
    async def marketplace_page() -> str:
        p = _pwa_dir / "marketplace.html"
        return p.read_text(encoding="utf-8") if p.exists() else "<h1>Marketplace not found</h1>"

    # ── Clyins: Upload meeting → generate schedule ─────────

    @app.post(
        "/api/v1/clyins/run",
        status_code=202,
        dependencies=[Depends(require_token)],
    )
    async def clyins_run(body: ClyinsRunRequest) -> dict[str, Any]:
        """Upload a meeting transcript, run the Clyins workflow, return a run_id.

        The frontend polls GET /api/v1/runs/{run_id} and auto-resumes at the
        human_input verification step to produce the final schedule.
        """
        # 1. Create application
        app = await services.workflow_store.create_application(
            ApplicationCreateRequest(
                name=f"Clyins {body.meeting_date or '会议'}",
                description="Clyins 自动生成的会议日程",
                requirement="从上传的会议记录中提取行动项并生成日程表",
            )
        )
        application_id = app["id"]

        # 2. Expand Clyins template
        try:
            expanded = services.templates.expand_into_workflow(
                "clyins", prefix="cy", x=0, y=0,
            )
        except KeyError:
            raise HTTPException(500, "Clyins template not found in template store")

        # 3. Populate draft with all nodes
        draft = await services.workflow_store.get_draft(application_id)
        revision = int(draft["revision"])
        draft_key_prefix = f"clyins-api-{application_id[:8]}"

        for node in expanded.nodes:
            result = await services.applications.apply_operation(
                application_id,
                DraftOperation(
                    expected_revision=revision,
                    idempotency_key=f"{draft_key_prefix}-n-{node.id}",
                    op="add_node",
                    data={"node": node.model_dump(mode="json")},
                ),
            )
            revision = int(result["revision"])

        for edge in expanded.edges:
            result = await services.applications.apply_operation(
                application_id,
                DraftOperation(
                    expected_revision=revision,
                    idempotency_key=f"{draft_key_prefix}-e-{edge.id}",
                    op="add_edge",
                    data={"edge": edge.model_dump(mode="json")},
                ),
            )
            revision = int(result["revision"])

        # 4. Create workflow run
        run_result = await services.workflow_runtime.create_run(
            application_id,
            WorkflowRunRequest(
                inputs={
                    "meeting_transcript": body.meeting_transcript,
                    "team_context": body.team_context,
                    "meeting_date": body.meeting_date,
                },
                use_draft=True,
                workspace_path=".",
            ),
        )
        run_id = run_result["run_id"]

        # 5. Auto-resume when workflow pauses at human_input
        async def _auto_resume() -> None:
            """Background task: wait for the human_input pause, then auto-approve."""
            await asyncio.sleep(2)
            for _ in range(120):
                try:
                    run_record = await services.workflow_store.get_run(run_id)
                except KeyError:
                    break
                if run_record["status"] == "paused":
                    try:
                        await services.workflow_runtime.resume(
                            run_id,
                            {"approved": True, "corrections": "", "assign_to_lilies": True},
                        )
                    except Exception:
                        pass
                    break
                if run_record["status"] in ("succeeded", "failed", "cancelled"):
                    break
                await asyncio.sleep(1)

        bg = asyncio.create_task(_auto_resume())
        services.background_tasks.add(bg)
        bg.add_done_callback(services.background_tasks.discard)

        return {
            "run_id": run_id,
            "application_id": application_id,
            "status": "queued",
        }

    # ── Workflow Quality Analysis ────────────────────────

    @app.post(
        "/api/v1/workflows/analyze",
        dependencies=[Depends(require_token)],
    )
    async def analyze_workflow_quality(body: dict[str, Any]) -> dict[str, Any]:
        """Static analysis of a WorkflowSpec: quality score, issues, suggestions.

        Accepts a full WorkflowSpec JSON. Returns a WorkflowQualityReport
        with graph-theoretic metrics, Petri-net checks, and suggestions.
        """
        from .workflow_models import WorkflowSpec as WS
        from .workflow_quality import analyze_workflow

        try:
            wf = WS.model_validate(body.get("workflow", body))
        except Exception as e:
            raise HTTPException(422, f"invalid workflow: {e}")

        report = analyze_workflow(wf)
        return {
            "score": report.score,
            "grade": report.grade,
            "structural": {
                "dead_code": report.structural.dead_code,
                "orphan_inputs": report.structural.orphan_inputs,
                "redundant_chain": report.structural.redundant_chain,
            },
            "robustness": {
                "missing_error_handling": report.robustness.missing_error_handling,
                "unguarded_tool": report.robustness.unguarded_tool,
            },
            "complexity": {
                "node_count": report.complexity.node_count,
                "edge_count": report.complexity.edge_count,
                "cyclomatic": report.complexity.cyclomatic,
                "max_depth": report.complexity.max_depth,
                "max_width": report.complexity.max_width,
                "dependency_density": report.complexity.dependency_density,
            },
            "petrinet": {
                "siphon_cycle": report.petrinet.siphon_cycle,
                "unbound_parallelism": report.petrinet.unbound_parallelism,
                "missing_break": report.petrinet.missing_break,
            },
            "coend": {
                "ambiguous_refs": report.coend.ambiguous_refs,
                "missing_optional": report.coend.missing_optional,
                "aggregator_mode_mismatch": report.coend.aggregator_mode_mismatch,
            },
            "suggestions": report.suggestions,
        }

    # ── Clyins: Dispatch tasks to Lilies Builder ────────

    @app.post(
        "/api/v1/clyins/dispatch",
        status_code=202,
        dependencies=[Depends(require_token)],
    )
    async def clyins_dispatch(body: dict[str, Any]) -> dict[str, Any]:
        """Human-reviewed: dispatch selected tasks to Lilies Builder Team.

        Each task becomes a Lilies Application + Builder Team build.
        Deduplication: if a task with the same name was already dispatched,
        returns the existing build instead of creating a duplicate.
        """
        tasks = body.get("tasks", [])
        if not tasks:
            raise HTTPException(422, "at least one task is required")

        # Pre-load all existing Clyins-dispatched apps for dedup
        all_apps = await services.workflow_store.list_applications()
        clyins_apps = {
            app["name"]: app
            for app in all_apps
            if app["name"].startswith("[Clyins] ")
        }

        dispatched = []
        skipped = []
        for task in tasks:
            task_name = task.get("name", "Unnamed task")
            app_name = f"[Clyins] {task_name[:80]}"

            # ── Dedup: check if already dispatched ──
            if app_name in clyins_apps:
                existing_app = clyins_apps[app_name]
                existing_builds = await services.workflow_store.list_builds(
                    existing_app["id"]
                )
                if existing_builds:
                    latest = existing_builds[0]
                    skipped.append({
                        "task_name": task_name,
                        "application_id": existing_app["id"],
                        "build_id": latest["id"],
                        "status": latest["status"],
                        "duplicate": True,
                        "message": "任务已下达过，跳过重复创建",
                    })
                    continue

            task_owner = task.get("owner", "")
            task_deadline = task.get("deadline", "")
            task_desc = task.get("description", task_name)

            requirement = f"实现任务: {task_name}"
            if task_owner:
                requirement += f"\n负责人: {task_owner}"
            if task_deadline:
                requirement += f"\n截止日期: {task_deadline}"
            requirement += f"\n\n{task_desc}"

            app = await services.workflow_store.create_application(
                ApplicationCreateRequest(
                    name=app_name,
                    description=f"由 Clyins 下达。负责人: {task_owner or '待分配'}。截止: {task_deadline or '待定'}",
                    requirement=requirement,
                )
            )
            app_id = app["id"]
            clyins_apps[app_name] = app  # track for dedup within this batch too

            build_id = str(uuid4())
            await services.workflow_store.create_build(
                build_id, app_id, requirement,
                auto_publish=True,
                max_turns=60,
                max_repair_cycles=4,
            )
            services.builder.start(build_id)

            dispatched.append({
                "task_name": task_name,
                "application_id": app_id,
                "build_id": build_id,
                "status": "queued",
                "duplicate": False,
            })

        all_tasks = dispatched + skipped

        # ── Auto-retry background task ──
        if dispatched:
            async def _monitor_and_retry() -> None:
                """Monitor builds, auto-resume on failure up to 3 times."""
                retry_counts: dict[str, int] = {}
                for _ in range(60):  # monitor for up to 30 min
                    await asyncio.sleep(30)
                    for task_info in dispatched:
                        bid = task_info["build_id"]
                        try:
                            build = await services.workflow_store.get_build(bid)
                        except KeyError:
                            continue
                        status = build.get("status", "")
                        if status == "published":
                            continue  # success, stop monitoring
                        if status == "needs_attention":
                            retry_counts[bid] = retry_counts.get(bid, 0) + 1
                            if retry_counts[bid] <= 3:
                                try:
                                    await services.workflow_store.update_build(
                                        bid, status="queued", error="",
                                    )
                                    services.builder.start(bid)
                                    print(f"[auto-retry] Build {bid[:12]}... "
                                          f"retry {retry_counts[bid]}/3")
                                except Exception:
                                    pass
                        # If building/queued, keep waiting
                    # Stop if all published or exhausted retries
                    all_done = True
                    for task_info in dispatched:
                        try:
                            b = await services.workflow_store.get_build(task_info["build_id"])
                            if b["status"] not in ("published",):
                                if b["status"] == "needs_attention" and retry_counts.get(task_info["build_id"], 0) >= 3:
                                    continue  # exhausted retries
                                all_done = False
                        except KeyError:
                            pass
                    if all_done:
                        break

            bg = asyncio.create_task(_monitor_and_retry())
            services.background_tasks.add(bg)
            bg.add_done_callback(services.background_tasks.discard)

        return {
            "dispatched": len(dispatched),
            "skipped": len(skipped),
            "tasks": all_tasks,
        }

    # ── Clyins: Dispatch status ──────────────────────────

    @app.get(
        "/api/v1/clyins/status",
        dependencies=[Depends(require_token)],
    )
    async def clyins_status() -> list[dict[str, Any]]:
        """Return status of all Clyins-dispatched builds."""
        all_apps = await services.workflow_store.list_applications()
        result = []
        for app in all_apps:
            if not app["name"].startswith("[Clyins] "):
                continue
            builds = await services.workflow_store.list_builds(app["id"])
            if not builds:
                continue
            latest = builds[0]
            # Count events for progress
            event_count = 0
            try:
                events = await services.storage.list_events(latest["id"])
                event_count = len(events)
            except Exception:
                pass
            result.append({
                "task_name": app["name"].replace("[Clyins] ", ""),
                "application_id": app["id"],
                "build_id": latest["id"],
                "status": latest["status"],
                "error": latest.get("error", ""),
                "active_version": app.get("active_version"),
                "event_count": event_count,
                "created_at": latest.get("created_at", ""),
            })
        return result

    # ── Clyins: AI planning ──────────────────────────────

    @app.post(
        "/api/v1/clyins/plan",
        dependencies=[Depends(require_token)],
    )
    async def clyins_plan(body: dict[str, Any]) -> dict[str, Any]:
        """Generate AI-powered planning suggestions from current task board."""
        tasks = body.get("tasks", [])
        today = body.get("today", "")

        # Build a compact summary of the current state
        lines = [f"当前日期: {today}", f"总任务数: {len(tasks)}", ""]
        cats = {}
        for t in tasks:
            c = t.get("cat", "其他")
            cats.setdefault(c, [])
            cats[c].append(t)

        for cat, items in cats.items():
            lines.append(f"## {cat} ({len(items)}项)")
            for t in items:
                s = t.get("status", "?")
                p = t.get("priority", "?")
                lines.append(f"  [{s}] {t.get('name','?')} | {t.get('owner','?')} | {t.get('start','?')}→{t.get('end','?')} | 优先级:{p}")

        prompt = "\n".join(lines)
        prompt += "\n\n请基于以上任务看板，分析并提出：\n"
        prompt += "1. 未来一周的关键行动项（按优先级排序）\n"
        prompt += "2. 潜在的风险和阻塞点\n"
        prompt += "3. 资源优化建议（谁的工作量过大/过少，任务是否可以并行）\n"
        prompt += "4. 紧凑度评估和改进方案\n"
        prompt += "请用简洁的中文回答，适合决策者快速阅读。"

        try:
            from .models import ChatMessage, ContentBlock
            stream = services.provider.stream(
                model=services.settings.deepseek_runtime_model,
                system="你是 Clyins，一个 AI 项目经理和战略顾问。你的分析应简洁、有洞察力，适合决策者快速阅读。每次建议不超过 500 字。",
                messages=[ChatMessage(role="user", content=[ContentBlock(type="text", text=prompt)])],
                tools=[],
                max_output_tokens=2048,
                thinking_enabled=True,
                effort="xhigh",
                user_id="clyins-plan",
            )
            response = await services.agent_runtime._collect_stream(
                "clyins-plan", stream, "plan.model", services.settings.deepseek_runtime_model
            )
            plan_text = "".join(block.text or "" for block in response.blocks if block.type == "text")
        except Exception as e:
            # Fallback: deterministic analysis without LLM
            plan_text = _deterministic_plan(tasks, today)

        # Parse out risks and suggestions
        risks = []
        suggestions = []
        for line in plan_text.split("\n"):
            line = line.strip()
            if any(kw in line for kw in ["风险", "阻塞", "延迟", "逾期", "瓶颈"]):
                risks.append(line.lstrip("•-* 1234567890."))
            if any(kw in line for kw in ["建议", "优化", "改进", "调整", "可以"]):
                suggestions.append(line.lstrip("•-* 1234567890."))

        return {
            "plan": plan_text,
            "risks": risks[:5] if risks else [
                f"任务「{t['name']}」已逾期" for t in tasks if t.get("status") != "completed" and t.get("end", "") < today
            ][:3],
            "suggestions": suggestions[:5] if suggestions else [
                "优先处理高优先级逾期任务",
                "检查资源分配是否均衡",
            ],
        }


    # ── Run Result Viewer (Clyins 输出可读页面) ──────────────

    @app.get("/run-view.html", response_class=HTMLResponse)
    async def run_view_page() -> str:
        p = _pwa_dir / "run-view.html"
        return p.read_text(encoding="utf-8") if p.exists() else "<h1>Run Viewer not found</h1>"

    # ── Debug ────────────────────────────────────────────────

    @app.get("/debug", response_class=HTMLResponse)
    async def debug_page() -> str:
        return DEBUG_HTML

    return app


def _deterministic_plan(tasks: list, today: str) -> str:
    """Deterministic fallback planner when LLM is unavailable."""
    pending = [t for t in tasks if t.get("status") != "completed"]
    overdue = [t for t in pending if t.get("end", "") < today]
    critical = [t for t in pending if t.get("priority") == "critical"]

    lines = ["## Clyins 计划分析（确定性模式）", ""]

    if overdue:
        lines.append(f"⚠️ {len(overdue)} 项任务已逾期，需立即关注：")
        for t in overdue[:5]:
            lines.append(f"  • {t.get('name')} ({t.get('owner')}) — 截止 {t.get('end')}")

    if critical:
        lines.append(f"\n🔥 {len(critical)} 项紧急任务进行中：")
        for t in critical[:5]:
            lines.append(f"  • {t.get('name')} ({t.get('owner')})")

    lines.append(f"\n📊 统计：待完成 {len(pending)}/{len(tasks)}，逾期 {len(overdue)}，紧急 {len(critical)}")
    lines.append(f"\n💡 建议：")
    lines.append("  1. 优先处理所有逾期任务")
    lines.append("  2. 对高优先级任务进行每日站会跟踪")
    lines.append("  3. 评估是否有任务可以并行或委派")

    return "\n".join(lines)


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
