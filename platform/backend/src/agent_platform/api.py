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
from .builder_benchmark import BuilderBenchmark, BuilderBenchmarkCase, BuilderBenchmarkSuiteCase
from .factory import AgentFactory
from .draft_patch_preview import DraftPatchPreviewer, DraftPatchPreviewRequest
from .models import (
    GenerationRequest,
    MessageRequest,
    PermissionDecision,
    SessionCreateRequest,
)
from .permissions import PermissionBroker
from .platform_harness import PlatformHarness
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
    harness: PlatformHarness
    applications: ApplicationService
    workflow_runtime: WorkflowRuntime
    builder: WorkflowBuilder
    scheduler: WorkflowScheduler
    templates: TemplateStore
    benchmark: BuilderBenchmark
    draft_patcher: DraftPatchPreviewer
    background_tasks: set[asyncio.Task[Any]]


def build_services(settings: Settings, provider: ModelProvider | None = None) -> Services:
    storage = Storage(settings.data_dir)
    tools = build_core_registry()
    sandboxes = SandboxManager(settings)
    permissions = PermissionBroker()
    provider = provider or MultiProvider(
        deepseek_api_key=settings.deepseek_api_key,
        deepseek_base_url=settings.deepseek_base_url,
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
    harness = PlatformHarness(
        storage=storage,
        max_active_tasks=settings.platform_harness_max_active_tasks,
        max_model_calls_per_task=settings.platform_harness_max_model_calls_per_task,
        max_tool_calls_per_task=settings.platform_harness_max_tool_calls_per_task,
        max_node_executions_per_task=settings.platform_harness_max_node_executions_per_task,
        max_model_calls_per_owner=settings.platform_harness_max_model_calls_per_owner,
        max_tool_calls_per_owner=settings.platform_harness_max_tool_calls_per_owner,
        max_node_executions_per_owner=settings.platform_harness_max_node_executions_per_owner,
        stale_active_task_seconds=settings.platform_harness_stale_active_task_seconds,
    )
    applications = ApplicationService(workflow_store, blocks, tools)
    workflow_runtime = WorkflowRuntime(
        storage=storage,
        workflow_store=workflow_store,
        harness=harness,
        applications=applications,
        blocks=blocks,
        provider=provider,
        agent_runtime=runtime,
        tools=tools,
        sandboxes=sandboxes,
        runtime_model=settings.deepseek_runtime_model,
    )
    templates = TemplateStore()
    benchmark = BuilderBenchmark()
    draft_patcher = DraftPatchPreviewer()
    templates_dir = settings.templates_dir
    if templates_dir and templates_dir.is_dir():
        loaded = templates.load_builtins(templates_dir)
        print(f"[api] Loaded {loaded} built-in templates from {templates_dir}")

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
        harness=harness,
        template_store=templates,
    )
    scheduler = WorkflowScheduler(
        storage=storage,
        workflow_store=workflow_store,
        blocks=blocks,
        runtime=workflow_runtime,
        harness=harness,
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
        harness=harness,
        applications=applications,
        workflow_runtime=workflow_runtime,
        builder=builder,
        scheduler=scheduler,
        templates=templates,
        benchmark=benchmark,
        draft_patcher=draft_patcher,
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
            "deepseek_configured": bool(settings.deepseek_api_key),
            "docker_available": shutil.which("docker") is not None,
            "provider": services.provider.name,
            "tools": services.tools.names(),
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

    @app.get("/api/v1/platform/harness/tasks", dependencies=[Depends(require_token)])
    async def list_platform_harness_tasks(
        kind: str | None = None,
        status: str | None = None,
        owner_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        tasks = await services.harness.list_tasks(
            kind=kind,
            status=status,
            owner_id=owner_id,
            limit=max(1, min(limit, 500)),
        )
        return [task.model_dump(mode="json") for task in tasks]

    @app.get("/api/v1/platform/harness/tasks/{task_id}", dependencies=[Depends(require_token)])
    async def get_platform_harness_task(task_id: str) -> dict[str, Any]:
        try:
            task = await services.harness.get_task(task_id)
            return task.model_dump(mode="json")
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post("/api/v1/builder-benchmark/evaluate", dependencies=[Depends(require_token)])
    async def evaluate_builder_benchmark(body: BuilderBenchmarkCase) -> dict[str, Any]:
        task_id = str(uuid4())
        await services.harness.start_task(
            task_id,
            kind="benchmark",
            owner_id="builder-benchmark",
            resource_id=body.name,
            metadata={"case": body.name},
        )
        try:
            report = services.benchmark.evaluate(body)
            await services.harness.finish_task(
                task_id,
                status="succeeded" if report.passed else "failed",
                metadata={"score": report.score},
            )
            return {"task_id": task_id, "report": report.model_dump(mode="json")}
        except Exception as error:
            await services.harness.finish_task(task_id, status="failed", error=str(error))
            raise

    @app.post("/api/v1/builder-benchmark/suites/evaluate", dependencies=[Depends(require_token)])
    async def evaluate_builder_benchmark_suite(body: BuilderBenchmarkSuiteCase) -> dict[str, Any]:
        task_id = str(uuid4())
        await services.harness.start_task(
            task_id,
            kind="benchmark",
            owner_id="builder-benchmark-suite",
            resource_id=body.name,
            metadata={
                "suite": body.name,
                "case_count": len(body.cases),
                "minimum_score": body.minimum_score,
                "minimum_pass_rate": body.minimum_pass_rate,
            },
        )
        try:
            await services.harness.record_usage(
                task_id,
                "node_execution",
                amount=max(1, len(body.cases)),
                metadata={
                    "operation": "builder_benchmark_suite",
                    "case_count": len(body.cases),
                },
            )
            report = services.benchmark.evaluate_suite(body)
            await services.harness.finish_task(
                task_id,
                status="succeeded" if report.passed else "failed",
                metadata={
                    "score": report.score,
                    "pass_rate": report.pass_rate,
                    "failed_cases": report.failed_cases,
                },
            )
            return {"task_id": task_id, "report": report.model_dump(mode="json")}
        except Exception as error:
            await services.harness.finish_task(task_id, status="failed", error=str(error))
            raise

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
    async def suggest_templates(requirement: str = "", reuse_depth: str = "shallow") -> list[dict[str, Any]]:
        """Suggest matching templates for a requirement, sorted by relevance."""
        if not requirement:
            return []
        if reuse_depth not in {"none", "shallow", "deep"}:
            raise HTTPException(422, "reuse_depth must be one of: none, shallow, deep")
        if reuse_depth == "none":
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
        recommended_action = "compose_modules" if reuse_depth == "deep" else "expand_template"
        return [
            {
                **meta.model_dump(mode="json"),
                "relevance_score": round(score, 3),
                "reuse_depth": reuse_depth,
                "recommended_action": recommended_action,
            }
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

    # ── Soft Block Strategies ──────────────────────────────────

    @app.get(
        "/api/v1/soft-block/strategies",
        dependencies=[Depends(require_token)],
    )
    async def list_soft_block_strategies(
        family: str | None = None,
    ) -> dict[str, Any]:
        """List available soft-block strategies, grouped by family."""
        from .soft_block import (
            FAMILY_MAP, strategy_help, get_discrete_block_type,
        )
        if family and family in FAMILY_MAP:
            strategies = {
                s: {
                    "help": strategy_help(s),
                    "maps_to": get_discrete_block_type(s),
                }
                for s in FAMILY_MAP[family]
            }
            return {"family": family, "strategies": strategies}

        result = {}
        for fam, strategies in FAMILY_MAP.items():
            result[fam] = {
                s: {
                    "help": strategy_help(s),
                    "maps_to": get_discrete_block_type(s),
                }
                for s in strategies
            }
        return {"families": list(FAMILY_MAP.keys()), "strategies": result}

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
        "/api/v1/applications/{application_id}/draft/preview-patch",
        dependencies=[Depends(require_token)],
    )
    async def preview_application_draft_patch(
        application_id: str, body: DraftPatchPreviewRequest
    ) -> dict[str, Any]:
        task_id = str(uuid4())
        await services.harness.start_task(
            task_id,
            kind="draft_patch_preview",
            owner_id=application_id,
            resource_id=application_id,
            metadata={"instruction": body.instruction[:200]},
        )
        try:
            draft = await services.workflow_store.get_draft(application_id)
            response = services.draft_patcher.preview(
                draft["snapshot"], int(draft["revision"]), body.instruction
            )
            await services.harness.finish_task(
                task_id,
                status="succeeded" if response.supported else "failed",
                metadata={"intent": response.intent},
            )
            return {"task_id": task_id, **response.model_dump(mode="json")}
        except KeyError as error:
            await services.harness.finish_task(task_id, status="failed", error=str(error))
            raise HTTPException(404, str(error)) from error

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

    # ── Auto meta-cognition hook: Builder → template extraction ──

    async def _auto_extract_from_build(build_id: str) -> None:
        """After a build publishes, try to extract a reusable template from it.
        Runs as a fire-and-forget background task — never blocks the build response.
        """
        try:
            build = await services.workflow_store.get_build(build_id)
            if build.get("status") not in ("published", "ready"):
                return

            from .extraction_gate import ExtractionGate
            from .merge_engine import MergeEngine
            from .template_models import ProvenanceSource
            from datetime import datetime, timezone

            # Use the real DecisionTracker from Builder — each draft_add_node,
            # draft_connect, template_expand, and draft_publish call was recorded.
            tracker = services.builder._trackers.pop(build_id, None)
            if tracker is None or len(tracker.roots) == 0:
                print(f"[auto-extract] Build {build_id[:8]}: no decision data")
                return

            requirement = build.get("requirement", "")
            draft = await services.workflow_store.get_draft(build["application_id"])
            node_types = [node.type for node in draft["snapshot"].workflow.nodes]

            gate = ExtractionGate(services.templates)
            should, reason = gate.should_propose(tracker.roots)
            if not should:
                print(f"[auto-extract] Build {build_id[:8]}: not proposed ({reason})")
                return

            wf = tracker.extract_workflow()
            if wf is None:
                print(f"[auto-extract] Build {build_id[:8]}: no extractable workflow")
                return
            engine = MergeEngine(services.templates)
            sim = engine.check_similarity(wf)

            if sim.should_merge and sim.target_template:
                source = ProvenanceSource(
                    source_type="session_extract",
                    identifier=build_id,
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
                merged = engine.merge(wf, sim.target_template, source)
                if merged:
                    print(f"[auto-extract] Build {build_id[:8]}: merged into {sim.target_template} v{merged.meta.version} conf={merged.meta.confidence}")
            elif all(not t.name.startswith("build-") for t in services.templates.list()):
                # Register as new template if no obvious match
                tname = f"build-extracted-{build_id[:8]}"
                services.templates.register(tname, wf, meta_overrides={
                    "title": f"Extracted: {requirement[:50]}",
                    "category": "task_management",
                    "tags": node_types[:5],
                    "author": "auto-extract",
                })
                print(f"[auto-extract] Build {build_id[:8]}: registered as {tname}")
        except Exception as exc:
            print(f"[auto-extract] Build {build_id[:8]} failed: {exc}")

    # Wire the meta-cognition hook: every completed build triggers extraction
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

    # ── Debug ────────────────────────────────────────────────

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
