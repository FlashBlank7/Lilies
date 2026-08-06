from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.blocks import build_block_registry
from agent_platform.capability_evidence import (
    EvidenceEnvironment,
    VerificationStatus,
    CapabilityEvidenceCreateRequest,
    CapabilityEvidenceRegistry,
    EvidenceArtifact,
    EvidenceGap,
    ModuleCapabilityClaim,
    ModuleKnownBoundary,
    ModulePort,
    ReusableModuleContract,
)
from agent_platform.config import Settings
from agent_platform.models import ChatMessage, StreamEvent, ToolDefinition
from agent_platform.providers.base import ModelProvider, ProviderCapabilities
from agent_platform.reference_modules import CODEX_MODULE_ID, ensure_codex_reference_module
from agent_platform.template_store import TemplateStore
from agent_platform.workflow_models import (
    ApplicationCreateRequest,
    BuildPlan,
    BuildPlanModule,
    BuildTeamState,
    EdgeSpec,
    NodeSpec,
    WorkflowSpec,
)


ROOT = Path(__file__).resolve().parents[1]
HEADERS = {"Authorization": "Bearer workflow-test", "Content-Type": "application/json"}


class PassiveProvider(ModelProvider):
    name = "v046-passive"

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
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        yield StreamEvent(type="content_block_start", data={
            "index": 0,
            "content_block": {"type": "text", "text": "done"},
        })
        yield StreamEvent(type="content_block_delta", data={
            "index": 0,
            "delta": {"type": "text_delta", "text": "done"},
        })
        yield StreamEvent(type="message_delta", data={
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 1},
        })


def settings(tmp_path: Path) -> Settings:
    return Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        scheduler_poll_seconds=3600,
    )


def simple_workflow(label: str = "ok") -> WorkflowSpec:
    return WorkflowSpec(
        nodes=[
            NodeSpec(
                id="start",
                type="start",
                title="Start",
                config={"inputs": [{"name": "request", "type": "string"}]},
            ),
            NodeSpec(
                id="end",
                type="end",
                title="End",
                config={"outputs": {"answer": label}},
            ),
        ],
        edges=[
            EdgeSpec(
                id="start-end",
                source="start",
                target="end",
                source_port="output",
                target_port="input",
            )
        ],
    )


def echo_contract(
    capability_id: str = "F.echo",
    *,
    required_envelope: str = "E1",
) -> ReusableModuleContract:
    return ReusableModuleContract(
        capability_ids=[capability_id],
        inputs=[
            ModulePort(
                name="request",
                value_type="string",
                description="One request to process.",
            )
        ],
        outputs=[
            ModulePort(
                name="answer",
                value_type="string",
                description="Processed customer-facing answer.",
            )
        ],
        dependencies=[],
        required_envelope=required_envelope,
        risk_level="low",
        known_boundaries=[
            ModuleKnownBoundary(
                id="local_only",
                title="Local evidence only",
                description="No live-provider or production reliability claim is made.",
                effect="blocked_by_environment",
                capability_ids=[capability_id],
            )
        ],
        claims=[
            ModuleCapabilityClaim(
                capability_id=capability_id,
                statement="The module carries one deterministic echo capability.",
                requested_status="component_verified",
                claim_scope="Local deterministic component behavior only.",
            )
        ],
    )


def component_evidence(
    capability_id: str = "F.echo",
    *,
    record_id: str | None = None,
) -> CapabilityEvidenceCreateRequest:
    return CapabilityEvidenceCreateRequest(
        record_id=record_id,
        capability_id=capability_id,
        claim="The module carries a deterministic component capability.",
        claim_scope="Local component only; live and production are excluded.",
        requested_status="component_verified",
        environment="sandbox",
        artifacts=[
            EvidenceArtifact(
                category="implementation",
                path="platform/backend/src/agent_platform/template_store.py",
                locator="TemplateStore.verify",
            ),
            EvidenceArtifact(
                category="test",
                path="tests/test_v04_06_versioned_module_evidence_registry.py",
            ),
        ],
    )


def mutate(
    client: TestClient,
    application_id: str,
    revision: int,
    operation: str,
    data: dict[str, object],
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


def test_evidence_claim_ceiling_rejects_inflation_and_approximation() -> None:
    registry = CapabilityEvidenceRegistry(evidence_root=ROOT)
    component = registry.register(component_evidence())
    assert component.verification_status == VerificationStatus.component_verified
    assert component.claim_ceiling == VerificationStatus.component_verified
    assert component.evidence_level.value == "H2"
    assert component.artifact_categories == ["implementation", "test"]
    assert all(item.sha256 for item in component.artifacts)

    with pytest.raises(ValueError, match="exceeds evidence ceiling"):
        registry.register(
            component_evidence().model_copy(update={
                "record_id": "inflated-integration",
                "requested_status": VerificationStatus.integration_verified,
            })
        )

    with pytest.raises(ValueError, match="evidence ceiling is unsupported"):
        registry.register(CapabilityEvidenceCreateRequest(
            capability_id="G.token_limit",
            claim="Token limit is implemented from model call count.",
            claim_scope="Call-count approximation only.",
            requested_status="static_verified",
            environment="contract",
            artifacts=[
                EvidenceArtifact(
                    category="implementation",
                    path="platform/backend/src/agent_platform/platform_harness.py",
                    method="approximation",
                )
            ],
        ))

    blocked = registry.register(CapabilityEvidenceCreateRequest(
        capability_id="X.customer_tenant",
        claim="A customer tenant is available.",
        claim_scope="No tenant is attached to the local environment.",
        requested_status="blocked_by_environment",
        environment=EvidenceEnvironment.contract,
        gaps=[EvidenceGap(
            field="customer_tenant",
            reason="No governed test tenant is configured.",
            impact="Identity and writeback cannot be tested live.",
        )],
    ))
    assert blocked.verification_status == VerificationStatus.blocked_by_environment
    assert blocked.evidence_level.value == "H0"


def test_atomic_module_state_write_uses_unique_concurrent_temporary_files(
    tmp_path: Path,
) -> None:
    path = tmp_path / "registry" / "module.state.json"
    payloads = [f'{{"writer": {index}}}' for index in range(16)]
    barrier = threading.Barrier(len(payloads))

    def write(payload: str) -> None:
        barrier.wait()
        TemplateStore._atomic_write(path, payload)

    with ThreadPoolExecutor(max_workers=len(payloads)) as executor:
        futures = [executor.submit(write, payload) for payload in payloads]
        for future in futures:
            future.result()

    assert path.read_text(encoding="utf-8") in payloads
    assert not list(path.parent.glob("*.tmp"))
    assert not list(path.parent.glob(".*.tmp"))


def test_module_versions_are_immutable_restart_safe_and_tamper_detected(
    tmp_path: Path,
) -> None:
    registry_dir = tmp_path / "module-registry"
    store = TemplateStore(registry_dir, evidence_root=ROOT)
    first = store.register(
        "echo_module",
        simple_workflow("v1"),
        meta_overrides={
            "title": "Echo Module",
            "description": "Versioned echo module.",
            "min_blocks_required": ["start", "end"],
        },
        module_contract=echo_contract(),
    )
    assert first.meta.version == 1
    evidence = store.add_evidence("echo_module", 1, component_evidence(record_id="echo-v1"))
    verified = store.verify("echo_module", 1)
    assert verified.state.status == "verified"
    assert verified.state.evidence_record_ids == [evidence.record_id]

    second = store.register(
        "echo_module",
        simple_workflow("v2"),
        meta_overrides={"title": "Echo Module v2", "min_blocks_required": ["start", "end"]},
        module_contract=echo_contract(),
    )
    assert second.meta.version == 2
    assert store.get("echo_module", 1).workflow.nodes[-1].config["outputs"]["answer"] == "v1"
    assert store.get_record("echo_module", 2).state.status == "draft"
    assert not list(registry_dir.rglob("*.tmp"))

    restarted = TemplateStore(registry_dir, evidence_root=ROOT)
    assert restarted.versions("echo_module") == [1, 2]
    assert restarted.get_record("echo_module", 1).state.status == "verified"
    assert restarted.expand_into_workflow(
        "echo_module", version=1, prefix="old"
    ).nodes[-1].config["outputs"]["answer"] == "v1"

    artifact_root = tmp_path / "rotating-evidence"
    artifact_root.mkdir()
    implementation = artifact_root / "implementation.py"
    test_artifact = artifact_root / "test_result.txt"
    implementation.write_text("def echo(value): return value\n", encoding="utf-8")
    test_artifact.write_text("passed\n", encoding="utf-8")
    rotating = TemplateStore(tmp_path / "rotating-registry", evidence_root=artifact_root)
    rotating.register(
        "rotating_module",
        simple_workflow(),
        meta_overrides={"min_blocks_required": ["start", "end"]},
        module_contract=echo_contract(),
    )
    rotating_request = CapabilityEvidenceCreateRequest(
        capability_id="F.echo",
        claim="The rotating module carries a deterministic component capability.",
        claim_scope="Local component evidence with append-only history.",
        requested_status="component_verified",
        environment="sandbox",
        artifacts=[
            EvidenceArtifact(category="implementation", path="implementation.py"),
            EvidenceArtifact(category="test", path="test_result.txt"),
        ],
    )
    old_evidence = rotating.add_evidence("rotating_module", 1, rotating_request)
    assert rotating.add_evidence("rotating_module", 1, rotating_request).record_id == (
        old_evidence.record_id
    )
    rotating.verify("rotating_module", 1)
    test_artifact.write_text("passed after evidence refresh\n", encoding="utf-8")
    new_evidence = rotating.add_evidence("rotating_module", 1, rotating_request)
    assert new_evidence.record_id != old_evidence.record_id
    refreshed = rotating.verify("rotating_module", 1)
    assert refreshed.state.status == "verified"
    assert refreshed.state.evidence_record_ids == [new_evidence.record_id]
    assert rotating.evidence.integrity_errors(old_evidence)
    assert len(rotating.evidence) == 2

    content_path, _ = restarted._paths("echo_module", 1)
    payload = json.loads(content_path.read_text(encoding="utf-8"))
    payload["meta"]["title"] = "tampered"
    content_path.write_text(json.dumps(payload), encoding="utf-8")
    quarantined = TemplateStore(registry_dir, evidence_root=ROOT)
    record = quarantined.get_record("echo_module", 1)
    assert record.state.status == "quarantined"
    assert "content hash" in record.state.verification_errors[0]
    with pytest.raises(ValueError, match="content hash"):
        quarantined.verify("echo_module", 1)
    assert quarantined.get_record("echo_module", 1).state.status == "quarantined"

    unavailable_root = tmp_path / "packaged-runtime-without-source-evidence"
    unavailable_root.mkdir()
    degraded_store = TemplateStore(
        tmp_path / "degraded-registry",
        evidence_root=unavailable_root,
    )
    degraded_reference = ensure_codex_reference_module(
        degraded_store,
        build_block_registry(),
    )
    assert degraded_reference.state.status == "draft"
    assert degraded_reference.state.verification_errors

    invalid_store = TemplateStore(
        tmp_path / "invalid-registry",
        evidence_root=ROOT,
        workflow_validator=build_block_registry().validate_workflow,
    )
    invalid_workflow = simple_workflow()
    invalid_workflow.nodes[-1].config = {"outputs": []}
    invalid_store.register(
        "invalid_module",
        invalid_workflow,
        meta_overrides={"min_blocks_required": ["start", "end"]},
        module_contract=echo_contract(),
    )
    invalid_store.add_evidence("invalid_module", 1, component_evidence())
    with pytest.raises(ValueError, match="workflow validation"):
        invalid_store.verify("invalid_module", 1)




def test_capability_module_api_publishes_queries_verifies_and_reloads(
    tmp_path: Path,
) -> None:
    app = create_app(settings(tmp_path), PassiveProvider())
    with TestClient(app) as client:
        modules = client.get("/api/v1/capability-modules", headers=HEADERS)
        assert modules.status_code == 200, modules.text
        reference = next(
            item for item in modules.json() if item["module_id"] == CODEX_MODULE_ID
        )
        assert reference["status"] == "verified"
        assert reference["module_ref"].endswith("@1")
        assert reference["contract"]["required_envelope"] == "E2"
        assert len(reference["evidence_record_ids"]) == 6

        evidence = client.get(
            "/api/v1/capability-evidence",
            headers=HEADERS,
            params={"module_id": CODEX_MODULE_ID, "category": "test"},
        )
        assert evidence.status_code == 200, evidence.text
        assert len(evidence.json()) == 6
        assert all("test" in item["artifact_categories"] for item in evidence.json())

        application_id = client.post(
            "/api/v1/applications",
            headers=HEADERS,
            json={"name": "Echo Module", "requirement": "Publish an echo module."},
        ).json()["id"]
        revision = 0
        for node in simple_workflow().nodes:
            revision = mutate(
                client,
                application_id,
                revision,
                "add_node",
                {"node": node.model_dump(mode="json")},
            )
        mutate(
            client,
            application_id,
            revision,
            "add_edge",
            {"edge": simple_workflow().edges[0].model_dump(mode="json")},
        )
        published = client.post(
            f"/api/v1/applications/{application_id}/publish-template",
            headers=HEADERS,
            json={
                "title": "Echo Module",
                "description": "Versioned API module.",
                "module_contract": echo_contract().model_dump(mode="json"),
            },
        )
        assert published.status_code == 201, published.text
        published_body = published.json()
        assert published_body["registry"]["version"] == 1
        assert published_body["registry"]["status"] == "draft"

        failed = client.post(
            "/api/v1/capability-modules/echo_module/versions/1/verify",
            headers=HEADERS,
        )
        assert failed.status_code == 422
        assert "no intact evidence" in failed.text

        registered = client.post(
            "/api/v1/capability-modules/echo_module/versions/1/evidence",
            headers=HEADERS,
            json=component_evidence(record_id="api-echo-v1").model_dump(mode="json"),
        )
        assert registered.status_code == 201, registered.text
        verified = client.post(
            "/api/v1/capability-modules/echo_module/versions/1/verify",
            headers=HEADERS,
        )
        assert verified.status_code == 200, verified.text
        assert verified.json()["status"] == "verified"
        current_draft = client.get(
            f"/api/v1/applications/{application_id}/draft",
            headers=HEADERS,
        ).json()
        inserted = client.post(
            f"/api/v1/applications/{application_id}/capability-modules/echo_module/versions/1/insert",
            headers=HEADERS,
            json={
                "expected_revision": current_draft["revision"],
                "expected_content_hash": current_draft["content_hash"],
                "prefix": "module_v1",
                "x": 600,
                "y": 100,
            },
        )
        assert inserted.status_code == 200, inserted.text
        assert inserted.json()["module"]["module_ref"] == "module:echo_module@1"
        assert set(inserted.json()["inserted_node_ids"]) == {
            "module_v1_start",
            "module_v1_end",
        }
        assert inserted.json()["draft"]["snapshot"]["workflow"]["nodes"][-1]["id"] == (
            "module_v1_end"
        )

        second = client.post(
            f"/api/v1/applications/{application_id}/publish-template",
            headers=HEADERS,
            json={
                "title": "Echo Module v2",
                "module_contract": echo_contract().model_dump(mode="json"),
            },
        )
        assert second.status_code == 201, second.text
        assert second.json()["registry"]["version"] == 2
        current_draft = client.get(
            f"/api/v1/applications/{application_id}/draft",
            headers=HEADERS,
        ).json()
        refused = client.post(
            f"/api/v1/applications/{application_id}/capability-modules/echo_module/versions/2/insert",
            headers=HEADERS,
            json={
                "expected_revision": current_draft["revision"],
                "expected_content_hash": current_draft["content_hash"],
                "prefix": "module_v2",
            },
        )
        assert refused.status_code == 409
        assert "only verified exact module versions" in refused.text
        versions = client.get(
            "/api/v1/capability-modules/echo_module/versions",
            headers=HEADERS,
        ).json()
        assert [item["version"] for item in versions] == [1, 2]
        assert [item["status"] for item in versions] == ["verified", "draft"]

        legacy_get = client.get(
            "/api/v1/templates/echo_module?version=1",
            headers=HEADERS,
        )
        assert legacy_get.status_code == 200
        assert legacy_get.json()["registry"]["module_ref"] == "module:echo_module@1"

    restarted = create_app(settings(tmp_path), PassiveProvider())
    with TestClient(restarted) as client:
        first = client.get(
            "/api/v1/capability-modules/echo_module/versions/1",
            headers=HEADERS,
        )
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "verified"
        assert first.json()["workflow"]["nodes"][-1]["type"] == "end"


