from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.capability_contracts import (
    CapabilityBuildContract,
    CapabilityCarrierDecision,
    CapabilityClaimScope,
    CapabilityCoverage,
    CapabilityEvidencePlanItem,
    CarrierStatus,
    CarrierType,
    CoverageOwner,
    CoverageStatus,
    EvidenceEnvironment,
    EvidenceLevel,
    ExecutionEnvelope,
    FunctionalCapability,
    RiskLevel,
    RuntimeGuarantee,
    StartInputContract,
    VerificationStatus,
    reference_capability_contract,
)
from agent_platform.config import Settings
from agent_platform.evaluation_harness import EvaluationPlanRequest
from agent_platform.models import ChatMessage, StreamEvent, ToolDefinition
from agent_platform.providers.base import ModelProvider, ProviderCapabilities


ROOT = Path(__file__).resolve().parents[1]
HEADERS = {"Authorization": "Bearer evaluation-test", "Content-Type": "application/json"}


class NoopProvider(ModelProvider):
    name = "evaluation-noop"

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, True, False, False, 100_000, 10_000)

    async def stream(
        self,
        *,
        model: str,
        system: str,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        max_output_tokens: int,
        thinking_enabled: bool,
        effort: str,
        tool_choice: dict[str, str] | None = None,
        user_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        yield StreamEvent(
            type="message_start",
            data={"message": {"usage": {"input_tokens": 1}}},
        )
        yield StreamEvent(
            type="content_block_start",
            data={"index": 0, "content_block": {"type": "text", "text": ""}},
        )
        yield StreamEvent(
            type="content_block_delta",
            data={"index": 0, "delta": {"type": "text_delta", "text": "done"}},
        )
        yield StreamEvent(
            type="message_delta",
            data={"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
        )


def settings(
    tmp_path: Path,
    *,
    observation_enabled: bool = False,
    observation_path: Path | None = None,
) -> Settings:
    return Settings(
        api_token="evaluation-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        scheduler_poll_seconds=3600,
        evaluation_production_observation_enabled=observation_enabled,
        evaluation_production_observation_evidence_path=observation_path,
    )


def component_contract() -> CapabilityBuildContract:
    functional = FunctionalCapability(
        id="F.echo",
        title="Echo result",
        description="Transform the declared input into one customer-readable output.",
        required_envelope=ExecutionEnvelope.E1,
        inputs=["query"],
        outputs=["greeting"],
        acceptance=["A terminal greeting exists."],
    )
    trace = RuntimeGuarantee(
        id="G.trace",
        title="Observable result",
        description="Expose the terminal result and execution trace.",
        requires=[functional.id],
        required_envelope=ExecutionEnvelope.E1,
        guarantee_type="observability",
        acceptance=["The run has a terminal status."],
    )
    return CapabilityBuildContract(
        contract_id="test.evaluation.component.v1",
        generation_source="reference",
        source_requirement="Build a deterministic greeting workflow.",
        target_user="Evaluation test user",
        business_goal="Return a deterministic greeting.",
        start_inputs=[
            StartInputContract(
                name="query",
                label="Query",
                value_type="string",
                description="Text to include in the greeting.",
            )
        ],
        functional_capabilities=[functional],
        runtime_guarantees=[trace],
        required_envelope=ExecutionEnvelope.E1,
        risk_level=RiskLevel.low,
        carrier_decisions=[
            CapabilityCarrierDecision(
                capability_id=functional.id,
                carrier_type=CarrierType.reusable_module,
                resource_hint="template transform",
                rationale="A deterministic editable transform implements the output.",
                status=CarrierStatus.bound,
                implementation_refs=["template"],
            ),
            CapabilityCarrierDecision(
                capability_id=trace.id,
                carrier_type=CarrierType.runtime_service,
                resource_hint="workflow runtime",
                rationale="Workflow Runtime records terminal state.",
                status=CarrierStatus.bound,
                implementation_refs=["end"],
            ),
        ],
        platform_coverage=[
            CapabilityCoverage(
                capability_id=functional.id,
                owner=CoverageOwner.workflow_runtime,
                status=CoverageStatus.available,
                surface="deterministic workflow",
            ),
            CapabilityCoverage(
                capability_id=trace.id,
                owner=CoverageOwner.platform_harness,
                status=CoverageStatus.available,
                surface="workflow and Platform Harness trace",
            ),
        ],
        evidence_plan=[
            CapabilityEvidencePlanItem(
                capability_ids=[functional.id, trace.id],
                target_level=EvidenceLevel.H2,
                environment=EvidenceEnvironment.sandbox,
                expected_status=VerificationStatus.component_verified,
                required_evidence=["generated component cases", "runtime test report"],
                claim_scope="Deterministic local component behavior only.",
            )
        ],
        workflow_outline=["Read query", "Format greeting", "Return greeting"],
        runtime_interface="Submit a query and receive a greeting.",
        claim_scope=CapabilityClaimScope(
            ceiling=VerificationStatus.component_verified,
            verified=["deterministic local greeting shape"],
            excluded=["live provider quality", "production reliability"],
        ),
    )


def mutate(
    client: TestClient,
    application_id: str,
    revision: int,
    operation: str,
    data: dict,
) -> int:
    response = client.post(
        f"/api/v1/applications/{application_id}/draft",
        headers=HEADERS,
        json={
            "expected_revision": revision,
            "idempotency_key": str(uuid4()),
            "op": operation,
            "data": data,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["revision"]


def create_component_application(client: TestClient) -> tuple[str, dict]:
    response = client.post(
        "/api/v1/applications",
        headers=HEADERS,
        json={
            "name": "Evaluation greeting",
            "requirement": "Build a deterministic greeting workflow.",
            "capability_build_contract": component_contract().model_dump(mode="json"),
        },
    )
    assert response.status_code == 201, response.text
    application_id = response.json()["id"]
    revision = 0
    for node in [
        {
            "id": "start",
            "type": "start",
            "title": "Input",
            "config": {"inputs": [{"name": "query", "type": "string"}]},
        },
        {
            "id": "template",
            "type": "template_transform",
            "title": "Greeting",
            "config": {
                "template": "Hello {{ query }}",
                "variables": {
                    "query": {"$ref": {"node_id": "start", "path": ["query"]}}
                },
            },
        },
        {
            "id": "end",
            "type": "end",
            "title": "Result",
            "config": {
                "outputs": {
                    "greeting": {"$ref": {"node_id": "template", "path": ["text"]}}
                }
            },
        },
    ]:
        revision = mutate(client, application_id, revision, "add_node", {"node": node})
    for edge in [
        {"id": "edge-start-template", "source": "start", "target": "template"},
        {
            "id": "edge-template-end",
            "source": "template",
            "target": "end",
            "source_port": "text",
        },
    ]:
        revision = mutate(client, application_id, revision, "add_edge", {"edge": edge})
    revision = mutate(
        client,
        application_id,
        revision,
        "add_test",
        {
            "test": {
                "id": "customer_authored_greeting",
                "name": "Customer-authored greeting",
                "requirement": "Preserve this non-generated acceptance case.",
                "inputs": {"query": "Ada"},
                "assertions": [
                    {"path": ["greeting"], "operator": "equals", "expected": "Hello Ada"}
                ],
                "mandatory": True,
            }
        },
    )
    draft = client.get(
        f"/api/v1/applications/{application_id}/draft",
        headers=HEADERS,
    ).json()
    assert draft["revision"] == revision
    return application_id, draft


def apply_plan_tests(
    client: TestClient,
    application_id: str,
    draft: dict,
    *,
    profile_id: str,
    environment_id: str,
) -> dict:
    response = client.post(
        f"/api/v1/applications/{application_id}/evaluation/tests/apply",
        headers=HEADERS,
        json={
            "profile_id": profile_id,
            "environment_id": environment_id,
            "expected_revision": draft["revision"],
            "expected_content_hash": draft["content_hash"],
            "mode": "replace_generated",
            "idempotency_key": str(uuid4()),
        },
    )
    assert response.status_code == 200, response.text
    return client.get(
        f"/api/v1/applications/{application_id}/draft",
        headers=HEADERS,
    ).json()


def test_profile_and_environment_catalog_is_exact_and_default_bounded(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path), NoopProvider())
    with TestClient(app) as client:
        profiles_response = client.get("/api/v1/evaluation/profiles", headers=HEADERS)
        environments_response = client.get("/api/v1/evaluation/environments", headers=HEADERS)

    assert profiles_response.status_code == 200
    profiles = profiles_response.json()
    assert [item["level"] for item in profiles] == ["H0", "H1", "H2", "H3", "H4", "H5"]
    assert [item["maximum_status"] for item in profiles] == [
        "design_only",
        "static_verified",
        "component_verified",
        "integration_verified",
        "live_verified",
        "production_observed",
    ]
    observation = next(item for item in profiles if item["level"] == "H5")
    assert observation["workflow_execution_allowed"] is False
    assert observation["draft_test_apply_allowed"] is False
    assert observation["external_mutation_allowed"] is False

    assert environments_response.status_code == 200
    environments = {item["id"]: item for item in environments_response.json()}
    assert environments["local_mock"]["availability"] == "available"
    assert environments["local_sandbox"]["availability"] == "available"
    assert environments["local_contract"]["availability"] == "available"
    assert environments["configured_live"]["availability"] == "unavailable"
    assert environments["production_observation"]["availability"] == "unavailable"
    assert environments["production_observation"]["workflow_execution_allowed"] is False


def test_generator_uses_distinct_capability_families_and_stable_ids(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path), NoopProvider())
    with TestClient(app) as client:
        evaluation = client.app.state.services.evaluation_harness
        request = EvaluationPlanRequest(profile_id="h1_static", environment_id="local_mock")
        daily = evaluation.plan_for_contract(
            reference_capability_contract("daily_web_collection"),
            request,
        )
        daily_repeat = evaluation.plan_for_contract(
            reference_capability_contract("daily_web_collection"),
            request,
        )
        embedded = evaluation.plan_for_contract(
            reference_capability_contract("customer_system_embedding"),
            request,
        )
        blocked_daily = evaluation.plan_for_contract(
            reference_capability_contract("daily_web_collection"),
            EvaluationPlanRequest(
                profile_id="h2_component",
                environment_id="local_sandbox",
            ),
        )

    daily_families = {item.family for item in daily.cases}
    assert {
        "functional_tool_feedback",
        "durable_schedule",
        "retry_resume",
        "audit_provenance",
        "site_access_contract",
        "storage_contract",
        "notification_contract",
    } <= daily_families
    embedded_families = {item.family for item in embedded.cases}
    assert {
        "identity_contract",
        "schema_contract",
        "writeback_contract",
        "idempotent_side_effect",
        "compensation",
        "isolation_boundary",
    } <= embedded_families
    assert [item.id for item in daily.cases] == [item.id for item in daily_repeat.cases]
    assert daily.eligibility == "ready"
    assert blocked_daily.eligibility == "blocked_by_environment"
    assert any("X.site_access" in item for item in blocked_daily.blockers)
    assert all(item.test.evidence_target is not None for item in daily.cases)


def test_plan_apply_and_h2_run_are_revision_safe_and_durable(tmp_path: Path) -> None:
    config = settings(tmp_path)
    app = create_app(config, NoopProvider())
    with TestClient(app) as client:
        application_id, draft = create_component_application(client)
        plan_response = client.post(
            f"/api/v1/applications/{application_id}/evaluation/plan",
            headers=HEADERS,
            json={"profile_id": "h2_component", "environment_id": "local_sandbox"},
        )
        assert plan_response.status_code == 200, plan_response.text
        plan = plan_response.json()
        assert plan["eligibility"] == "ready"
        assert plan["required_capability_ids"] == ["F.echo", "G.trace"]
        assert plan["covered_capability_ids"] == ["F.echo", "G.trace"]
        assert len(plan["generated_tests"]) == 2

        before_apply = client.post(
            f"/api/v1/applications/{application_id}/evaluation/runs",
            headers=HEADERS,
            json={
                "profile_id": "h2_component",
                "environment_id": "local_sandbox",
                "expected_revision": draft["revision"],
                "expected_content_hash": draft["content_hash"],
            },
        )
        assert before_apply.status_code == 200, before_apply.text
        assert before_apply.json()["outcome"] == "unsupported"
        assert "must be applied" in before_apply.json()["blockers"][0]

        applied = apply_plan_tests(
            client,
            application_id,
            draft,
            profile_id="h2_component",
            environment_id="local_sandbox",
        )
        test_ids = [item["id"] for item in applied["snapshot"]["tests"]]
        assert "customer_authored_greeting" in test_ids
        assert len([item for item in test_ids if item.startswith("eval_")]) == 2

        stale_apply = client.post(
            f"/api/v1/applications/{application_id}/evaluation/tests/apply",
            headers=HEADERS,
            json={
                "profile_id": "h2_component",
                "environment_id": "local_sandbox",
                "expected_revision": draft["revision"],
                "expected_content_hash": draft["content_hash"],
                "mode": "replace_generated",
                "idempotency_key": str(uuid4()),
            },
        )
        assert stale_apply.status_code == 409

        run_response = client.post(
            f"/api/v1/applications/{application_id}/evaluation/runs",
            headers=HEADERS,
            json={
                "profile_id": "h2_component",
                "environment_id": "local_sandbox",
                "expected_revision": applied["revision"],
                "expected_content_hash": applied["content_hash"],
            },
        )
        assert run_response.status_code == 200, run_response.text
        record = run_response.json()
        assert record["outcome"] == "completed"
        assert record["achieved_status"] == "component_verified"
        assert record["passed"] is True
        assert set(record["generated_test_ids"]) <= set(record["executed_test_ids"])
        assert {item["capability_id"] for item in record["capability_results"]} == {
            "F.echo",
            "G.trace",
        }

        task = client.get(
            f"/api/v1/platform/harness/tasks/{record['id']}",
            headers=HEADERS,
        ).json()
        assert task["kind"] == "evaluation_run"
        assert task["status"] == "succeeded"

        governance_tasks = client.get(
            "/api/v1/governance/tasks",
            headers=HEADERS,
            params={"application_id": application_id, "kind": "evaluation_run"},
        ).json()
        assert governance_tasks["total"] == 2
        assert {item["kind"] for item in governance_tasks["items"]} == {"evaluation_run"}

        evidence = client.get(
            "/api/v1/governance/capability-evidence",
            headers=HEADERS,
        ).json()
        capability = next(
            item
            for item in evidence["capabilities"]
            if item["capability_id"] == "platform.evaluation_harness_profiles"
        )
        assert capability["strongest_status"] == "integration_verified"
        assert {"implementation", "api", "test", "integration"} <= set(
            capability["artifact_categories"]
        )
        assert any(
            gap["field"] == "live_and_production_evidence"
            for gap in capability["known_gaps"]
        )
        assert task["metadata"]["achieved_status"] == "component_verified"

        governance = client.get(
            "/api/v1/governance/tasks",
            headers=HEADERS,
            params={"kind": "evaluation_run", "application_id": application_id},
        ).json()
        assert any(item["id"] == record["id"] for item in governance["items"])
        run_id = record["id"]

    restarted = create_app(config, NoopProvider())
    with TestClient(restarted) as client:
        history = client.get(
            f"/api/v1/applications/{application_id}/evaluation/runs",
            headers=HEADERS,
        )
        exact = client.get(f"/api/v1/evaluation/runs/{run_id}", headers=HEADERS)
        assert history.status_code == 200
        assert history.json()[0]["id"] == run_id
        assert exact.status_code == 200
        assert exact.json()["draft_content_hash"] == record["draft_content_hash"]


def test_h0_h1_do_not_execute_and_h3_cannot_exceed_contract_ceiling(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path), NoopProvider())
    with TestClient(app) as client:
        application_id, draft = create_component_application(client)
        h0 = client.post(
            f"/api/v1/applications/{application_id}/evaluation/runs",
            headers=HEADERS,
            json={"profile_id": "h0_design", "environment_id": "local_mock"},
        ).json()
        h1 = client.post(
            f"/api/v1/applications/{application_id}/evaluation/runs",
            headers=HEADERS,
            json={"profile_id": "h1_static", "environment_id": "local_mock"},
        ).json()
        assert h0["achieved_status"] == "design_only"
        assert h1["achieved_status"] == "static_verified"
        assert h0["executed_test_ids"] == []
        assert h1["executed_test_ids"] == []
        assert client.get(
            "/api/v1/platform/harness/tasks",
            headers=HEADERS,
            params={"kind": "workflow_run"},
        ).json() == []

        h3_draft = apply_plan_tests(
            client,
            application_id,
            draft,
            profile_id="h3_integration",
            environment_id="local_contract",
        )
        h3 = client.post(
            f"/api/v1/applications/{application_id}/evaluation/runs",
            headers=HEADERS,
            json={
                "profile_id": "h3_integration",
                "environment_id": "local_contract",
                "expected_revision": h3_draft["revision"],
                "expected_content_hash": h3_draft["content_hash"],
            },
        ).json()
        assert h3["passed"] is True
        assert h3["achieved_status"] == "component_verified"
        assert h3["profile_level"] == "H3"


def test_unavailable_and_incompatible_requests_are_scoped_not_fabricated(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path), NoopProvider())
    with TestClient(app) as client:
        application_id, _ = create_component_application(client)
        blocked = client.post(
            f"/api/v1/applications/{application_id}/evaluation/runs",
            headers=HEADERS,
            json={"profile_id": "h4_live", "environment_id": "configured_live"},
        )
        unsupported = client.post(
            f"/api/v1/applications/{application_id}/evaluation/runs",
            headers=HEADERS,
            json={"profile_id": "h4_live", "environment_id": "local_sandbox"},
        )

    assert blocked.status_code == 200
    assert blocked.json()["outcome"] == "blocked"
    assert blocked.json()["achieved_status"] == "blocked_by_environment"
    assert blocked.json()["passed"] is None
    assert unsupported.status_code == 200
    assert unsupported.json()["outcome"] == "unsupported"
    assert unsupported.json()["achieved_status"] == "unsupported"


def test_h5_observation_is_read_only_and_requires_matching_evidence(tmp_path: Path) -> None:
    observation_path = tmp_path / "production-observation.json"
    config = settings(
        tmp_path,
        observation_enabled=True,
        observation_path=observation_path,
    )
    initial = create_app(config, NoopProvider())
    with TestClient(initial) as client:
        application_id, draft = create_component_application(client)
        blocked = client.post(
            f"/api/v1/applications/{application_id}/evaluation/runs",
            headers=HEADERS,
            json={
                "profile_id": "h5_production_observation",
                "environment_id": "production_observation",
            },
        ).json()
        assert blocked["achieved_status"] == "blocked_by_environment"

    observation_path.write_text(
        json.dumps({
            "application_id": application_id,
            "draft_content_hash": draft["content_hash"],
            "status": "observed",
            "observed_at": "2026-07-16T00:00:00Z",
            "telemetry_refs": ["telemetry://evaluation-test"],
        }),
        encoding="utf-8",
    )
    restarted = create_app(config, NoopProvider())
    with TestClient(restarted) as client:
        before = client.get(
            f"/api/v1/applications/{application_id}/draft",
            headers=HEADERS,
        ).json()
        observed = client.post(
            f"/api/v1/applications/{application_id}/evaluation/runs",
            headers=HEADERS,
            json={
                "profile_id": "h5_production_observation",
                "environment_id": "production_observation",
                "expected_revision": before["revision"],
                "expected_content_hash": before["content_hash"],
            },
        ).json()
        after = client.get(
            f"/api/v1/applications/{application_id}/draft",
            headers=HEADERS,
        ).json()
        workflow_tasks = client.get(
            "/api/v1/platform/harness/tasks",
            headers=HEADERS,
            params={"kind": "workflow_run"},
        ).json()

    assert observed["outcome"] == "completed"
    assert observed["achieved_status"] == "component_verified"
    assert observed["execution_mode"] == "observation"
    assert observed["executed_test_ids"] == []
    assert before["revision"] == after["revision"]
    assert before["content_hash"] == after["content_hash"]
    assert workflow_tasks == []


def test_api_auth_unknown_selection_and_body_validation(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path), NoopProvider())
    with TestClient(app) as client:
        assert client.get("/api/v1/evaluation/profiles").status_code in {401, 403}
        application_id, _ = create_component_application(client)
        unknown = client.post(
            f"/api/v1/applications/{application_id}/evaluation/plan",
            headers=HEADERS,
            json={"profile_id": "h9_magic", "environment_id": "local_mock"},
        )
        malformed = client.post(
            f"/api/v1/applications/{application_id}/evaluation/runs",
            headers=HEADERS,
            json={
                "profile_id": "h2_component",
                "environment_id": "local_sandbox",
                "expected_revision": 4,
            },
        )

    assert unknown.status_code == 404
    assert malformed.status_code == 422


def test_v048_source_contract_and_frontend_markers() -> None:
    contract = json.loads(
        (ROOT / "docs/evolution-control/stage-contracts/v0.4.8.json").read_text(
            encoding="utf-8"
        )
    )
    backend = (ROOT / "platform/backend/src/agent_platform/evaluation_harness.py").read_text(
        encoding="utf-8"
    )
    api_source = (ROOT / "platform/backend/src/agent_platform/api.py").read_text(
        encoding="utf-8"
    )
    studio_source = (
        ROOT / "platform/frontend/app/applications/[id]/page.tsx"
    ).read_text(encoding="utf-8")
    evaluation_source = (
        ROOT / "platform/frontend/app/applications/[id]/evaluation-harness-panel.tsx"
    ).read_text(encoding="utf-8")
    runtime_source = (
        ROOT / "platform/frontend/app/runtime/[id]/page.tsx"
    ).read_text(encoding="utf-8")
    platform_types = (ROOT / "platform/frontend/lib/platform.ts").read_text(
        encoding="utf-8"
    )

    assert len(contract["mandatory_tasks"]) == 7
    assert {item["task_id"] for item in contract["mandatory_tasks"]} == {
        f"V04-08-T01{suffix}" for suffix in "ABCDEFG"
    }
    for marker in (
        "h0_design",
        "h1_static",
        "h2_component",
        "h3_integration",
        "h4_live",
        "h5_production_observation",
        "blocked_by_environment",
        "production_observation",
        "evaluation_run",
    ):
        assert marker in backend
    for route in (
        "/api/v1/evaluation/profiles",
        "/api/v1/evaluation/environments",
        "/evaluation/plan",
        "/evaluation/tests/apply",
        "/evaluation/runs",
    ):
        assert route in api_source
    assert "<EvaluationHarnessPanel" in studio_source
    for marker in (
        'data-evaluation-harness="studio"',
        'data-evaluation-profile-controls="h0-h5"',
        "role=\"radiogroup\"",
        "evaluation/tests/apply",
        "evaluation/runs",
        "generatedTestsApplied",
        "runtimeNeedsAppliedCases",
        "verified_claims",
        "excluded_claims",
        "capability_results",
    ):
        assert marker in evaluation_source
    assert "'evaluation_run'" in platform_types
    assert "EvaluationHarnessPanel" not in runtime_source
    assert "/evaluation/" not in runtime_source
