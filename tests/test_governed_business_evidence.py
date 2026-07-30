from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from tests.test_runtime import ScriptedProvider
from tests.test_v04_13_lilies_platform_api import (
    ZERO_DIGEST,
    _create_assigned_application,
    _issue,
    _request,
    _settings,
)


def _contract() -> dict:
    capabilities = [
        {
            "id": "F.match",
            "title": "Deterministic match",
            "description": "Match one source record without guessing.",
            "required": True,
            "required_envelope": "E3",
            "kind": "F",
            "inputs": ["source", "candidates"],
            "outputs": ["match"],
        },
        {
            "id": "F.review",
            "title": "Exception review",
            "description": "Route unsafe records to a typed review decision.",
            "required": True,
            "required_envelope": "E3",
            "kind": "F",
            "inputs": ["exception"],
            "outputs": ["decision"],
        },
    ]
    return {
        "schema_version": "1.0",
        "contract_id": "generic-governed-reconciliation",
        "generation_source": "model",
        "source_requirement": "Match records and require review for unsafe outcomes.",
        "target_user": "Operations reviewer",
        "business_goal": "Prevent unsafe automated decisions.",
        "start_inputs": [
            {
                "name": "source",
                "label": "Source",
                "value_type": "object",
                "required": True,
            },
            {
                "name": "candidates",
                "label": "Candidates",
                "value_type": "array",
                "required": True,
            },
        ],
        "functional_capabilities": capabilities,
        "runtime_guarantees": [],
        "external_contracts": [],
        "required_envelope": "E3",
        "risk_level": "high",
        "risk_reasons": ["Unsafe matches require an explicit review path."],
        "carrier_decisions": [
            {
                "capability_id": "F.match",
                "carrier_type": "atomic_block",
                "resource_hint": "record_match",
                "rationale": "Explainable deterministic matching.",
                "status": "proposed",
                "implementation_refs": [],
            },
            {
                "capability_id": "F.review",
                "carrier_type": "atomic_block",
                "resource_hint": "human_input",
                "rationale": "Typed review response.",
                "status": "proposed",
                "implementation_refs": [],
            },
        ],
        "platform_coverage": [
            {
                "capability_id": item["id"],
                "owner": "workflow_runtime",
                "status": "available",
                "surface": "record_match" if item["id"] == "F.match" else "human_input",
                "notes": "",
            }
            for item in capabilities
        ],
        "evidence_plan": [
            {
                "capability_ids": ["F.match", "F.review"],
                "target_level": "H3",
                "environment": "live",
                "expected_status": "integration_verified",
                "required_evidence": [
                    "mandatory executable acceptance",
                    "typed review result",
                ],
                "claim_scope": "Frozen test environment.",
            }
        ],
        "workflow_outline": ["Match", "Route", "Review", "Return"],
        "runtime_interface": "Source and candidates in; decision out.",
        "claim_scope": {
            "ceiling": "integration_verified",
            "verified": [],
            "excluded": [],
        },
        "unresolved_decisions": [],
    }


def _workflow_operations() -> list[tuple[str, dict]]:
    return [
        (
            "set_metadata",
            {
                "delivery_mode": "governed",
                "governed_hard_gate": True,
            },
        ),
        (
            "set_capability_build_contract",
            {"contract": _contract()},
        ),
        (
            "add_node",
            {
                "node": {
                    "id": "start",
                    "type": "start",
                    "title": "Input",
                    "config": {
                        "inputs": [
                            {"name": "source", "type": "object"},
                            {"name": "candidates", "type": "array"},
                        ]
                    },
                }
            },
        ),
        (
            "add_node",
            {
                "node": {
                    "id": "match",
                    "type": "record_match",
                    "title": "Match",
                    "config": {
                        "source": {
                            "$ref": {
                                "node_id": "$inputs",
                                "path": ["source"],
                            }
                        },
                        "candidates": {
                            "$ref": {
                                "node_id": "$inputs",
                                "path": ["candidates"],
                            }
                        },
                        "conditions": [
                            {
                                "name": "external_id",
                                "source_path": ["external_id"],
                                "candidate_path": ["external_id"],
                                "comparator": "exact",
                                "weight": 1.0,
                                "required": True,
                            }
                        ],
                        "min_score": 1.0,
                    },
                }
            },
        ),
        (
            "add_node",
            {
                "node": {
                    "id": "decision",
                    "type": "if_else",
                    "title": "Decision",
                    "config": {
                        "cases": [
                            {
                                "id": "safe",
                                "conditions": [
                                    {
                                        "value": {
                                            "$ref": {
                                                "node_id": "match",
                                                "path": ["status"],
                                            }
                                        },
                                        "operator": "equals",
                                        "expected": "matched",
                                    }
                                ],
                            }
                        ],
                        "default_branch": "review",
                    },
                }
            },
        ),
        (
            "add_node",
            {
                "node": {
                    "id": "review",
                    "type": "human_input",
                    "title": "Review",
                    "config": {
                        "title": "Review unsafe match",
                        "fields": [
                            {
                                "name": "approved",
                                "label": "Approved",
                                "type": "boolean",
                                "required": True,
                            }
                        ],
                    },
                }
            },
        ),
        (
            "add_node",
            {
                "node": {
                    "id": "end",
                    "type": "end",
                    "title": "End",
                    "config": {
                        "outputs": {
                            "decision": {
                                "$ref": {
                                    "node_id": "decision",
                                    "path": ["branch"],
                                }
                            },
                            "approved": {
                                "$ref": {
                                    "node_id": "review",
                                    "path": ["approved"],
                                },
                                "optional": True,
                            },
                        }
                    },
                }
            },
        ),
        *[
            (
                "add_edge",
                {
                    "edge": {
                        "id": edge_id,
                        "source": source,
                        "target": target,
                        "source_port": source_port,
                        "target_port": "input",
                        **({"branch": branch} if branch else {}),
                    }
                },
            )
            for edge_id, source, target, source_port, branch in [
                ("start-match", "start", "match", "output", None),
                ("match-decision", "match", "decision", "output", None),
                ("decision-safe", "decision", "end", "branch", "safe"),
                ("decision-review", "decision", "review", "branch", "review"),
                ("review-end", "review", "end", "output", None),
            ]
        ],
    ]


def _apply(
    client: TestClient,
    headers: dict[str, str],
    application_id: str,
    operations: list[tuple[str, dict]],
) -> int:
    revision = 0
    for index, (operation, data) in enumerate(operations):
        response = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/draft",
            headers,
            key=f"governed-business-operation-{index:04d}",
            json={
                "expected_revision": revision,
                "op": operation,
                "data": data,
            },
        )
        assert response.status_code == 200, response.text
        revision = response.json()["data"]["revision"]
    return revision


def test_public_builder_can_simulate_human_input_without_reserved_run_keys(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        headers, _, _, _, _ = _issue(client)
        application_id = _create_assigned_application(
            client,
            headers,
            key="governed-business-create",
        )["id"]
        revision = _apply(
            client,
            headers,
            application_id,
            _workflow_operations(),
        )

        rejected = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/draft",
            headers,
            key="governed-business-reserved-input",
            json={
                "expected_revision": revision,
                "op": "add_test",
                "data": {
                    "test": {
                        "id": "review-path",
                        "name": "Review path",
                        "requirement": "Unsafe match receives review.",
                        "inputs": {
                            "source": {"external_id": "missing"},
                            "candidates": [],
                            "__human__": {"review": {"approved": True}},
                        },
                    }
                },
            },
        )
        assert rejected.status_code == 422, rejected.text

        added = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/draft",
            headers,
            key="governed-business-safe-fixture",
            json={
                "expected_revision": revision,
                "op": "add_test",
                "data": {
                    "test": {
                        "id": "review-path",
                        "name": "Review path",
                        "requirement": "Unsafe match receives review.",
                        "inputs": {
                            "source": {"external_id": "missing"},
                            "candidates": [],
                        },
                        "simulated_human_inputs": {
                            "review": {"approved": True}
                        },
                        "assertions": [
                            {
                                "path": ["decision"],
                                "operator": "equals",
                                "expected": "review",
                            },
                            {
                                "path": ["approved"],
                                "operator": "equals",
                                "expected": True,
                            },
                        ],
                        "required_node_types": [
                            "record_match",
                            "if_else",
                            "human_input",
                        ],
                        "capability_ids": ["F.match", "F.review"],
                        "evidence_target": {
                            "level": "H3",
                            "environment": "live",
                            "expected_status": "integration_verified",
                            "claim_scope": "Frozen test environment.",
                        },
                        "mandatory": True,
                        "structural_only": False,
                    }
                },
            },
        )
        assert added.status_code == 200, added.text

        inspected = _request(
            client,
            "GET",
            f"/api/v1/lilies/applications/{application_id}/draft",
            headers,
            key="governed-business-preflight",
        )
        assert inspected.status_code == 200, inspected.text
        assert inspected.json()["data"]["preflight"]["valid"] is True

        result = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/tests/run",
            headers,
            key="governed-business-tests",
            json={},
        )
        assert result.status_code == 200, result.text
        assert result.json()["data"]["passed"] is True, result.text

        published = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/versions",
            headers,
            key="governed-business-publish",
            json={},
        )
        assert published.status_code == 200, published.text

        with sqlite3.connect(settings.data_dir / "agent_platform.db") as connection:
            connection.execute(
                "UPDATE application_drafts SET validation_contract_digest='' "
                "WHERE application_id=?",
                (application_id,),
            )

        decision = client.get(
            f"/api/v1/applications/{application_id}/publication-decision",
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )
        assert decision.status_code == 200, decision.text
        assert decision.json()["allowed"] is True
        assert decision.json()["requires_confirmation"] is True
        assert decision.json()["warning_codes"] == ["stale_evidence"]
        refreshed_contract = _request(
            client,
            "GET",
            "/api/v1/lilies/platform-contract",
            {**headers, "X-Lilies-Contract-Digest": ZERO_DIGEST},
            key="governed-business-contract-refresh",
        )
        assert refreshed_contract.status_code == 200, refreshed_contract.text
        headers["X-Lilies-Contract-Digest"] = refreshed_contract.json()["data"][
            "contract_digest"
        ]

        legacy_republish = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/versions",
            headers,
            key="governed-business-legacy-republish",
            json={},
        )
        assert legacy_republish.status_code == 200, legacy_republish.text


def test_governed_business_diagnostics_do_not_replace_builder_validation(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        headers, _, _, _, _ = _issue(client)
        application_id = _create_assigned_application(
            client,
            headers,
            key="governed-shortcut-create",
        )["id"]
        operations = _workflow_operations()
        operations = [
            item
            for item in operations
            if not (
                item[0] == "add_node"
                and item[1].get("node", {}).get("id") == "review"
            )
            and not (
                item[0] == "add_edge"
                and item[1].get("edge", {}).get("id")
                in {"decision-review", "review-end"}
            )
        ]
        revision = _apply(client, headers, application_id, operations)
        shortcut = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/draft",
            headers,
            key="governed-shortcut-test",
            json={
                "expected_revision": revision,
                "op": "add_test",
                "data": {
                    "test": {
                        "id": "structural-shortcut",
                        "name": "Structural shortcut",
                        "requirement": "Only inspect node presence.",
                        "inputs": {
                            "source": {"external_id": "A"},
                            "candidates": [],
                        },
                        "required_node_types": [
                            "record_match",
                            "if_else",
                        ],
                        "capability_ids": ["F.match"],
                        "evidence_target": {
                            "level": "H3",
                            "environment": "live",
                            "expected_status": "integration_verified",
                        },
                        "mandatory": True,
                        "structural_only": True,
                    }
                },
            },
        )
        assert shortcut.status_code == 200, shortcut.text

        inspected = _request(
            client,
            "GET",
            f"/api/v1/lilies/applications/{application_id}/draft",
            headers,
            key="governed-shortcut-preflight",
        )
        assert inspected.status_code == 200, inspected.text
        preflight = inspected.json()["data"]["preflight"]
        assert not any(
            "structural-only" in item
            or "required capability ids" in item
            or "human_input node" in item
            or "governed decision branches" in item
            for item in preflight["errors"]
        )

        result = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/tests/run",
            headers,
            key="governed-shortcut-run",
            json={},
        )
        assert result.status_code == 200, result.text
        data = result.json()["data"]
        errors = data["validation"]["errors"]
        assert not any(
            "structural-only" in item
            or "required capability ids" in item
            or "human_input node" in item
            or "governed decision branches" in item
            for item in errors
        )

        published = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/versions",
            headers,
            key="governed-shortcut-publish",
            json={},
        )
        assert published.status_code == 200, published.text
        assert published.json()["data"]["publication_decision"]["blocked"] is False


def test_public_orchestration_manual_exposes_agent_fillable_templates(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        headers, _, _, _, _ = _issue(client)
        manuals = []
        for index, block_type in enumerate(
            ("if_else", "iteration", "human_input", "connector_action")
        ):
            response = _request(
                client,
                "GET",
                f"/api/v1/lilies/blocks/{block_type}",
                headers,
                key=f"orchestration-manual-{index:04d}",
            )
            assert response.status_code == 200, response.text
            manuals.append(response.json()["data"]["manual"])
        serialized = json.dumps(manuals, ensure_ascii=False)
        assert "simulated_human_inputs" in serialized
        assert "record_collection_normalize" in serialized
        assert "$inputs.record" in serialized
        assert "<registered connector>" in serialized
        assert "Paperless" not in serialized
        assert "InvenTree" not in serialized


def test_required_reference_failure_reports_exact_cardinality_context(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        headers, _, _, _, _ = _issue(client)
        application_id = _create_assigned_application(
            client,
            headers,
            key="reference-diagnostic-create",
        )["id"]
        operations = [
            (
                "add_node",
                {
                    "node": {
                        "id": "start",
                        "type": "start",
                        "title": "Input",
                        "config": {
                            "inputs": [{"name": "records", "type": "array"}]
                        },
                    }
                },
            ),
            (
                "add_node",
                {
                    "node": {
                        "id": "deduplicate",
                        "type": "record_deduplicate",
                        "title": "Deduplicate",
                        "config": {
                            "records": {
                                "$ref": {
                                    "node_id": "$inputs",
                                    "path": ["records"],
                                }
                            },
                            "key_paths": [["id"]],
                        },
                    }
                },
            ),
            (
                "add_node",
                {
                    "node": {
                        "id": "end",
                        "type": "end",
                        "title": "End",
                        "config": {
                            "outputs": {
                                "first_id": {
                                    "$ref": {
                                        "node_id": "deduplicate",
                                        "path": ["unique", 0, "id"],
                                    }
                                }
                            }
                        },
                    }
                },
            ),
            (
                "add_edge",
                {
                    "edge": {
                        "id": "start-deduplicate",
                        "source": "start",
                        "target": "deduplicate",
                        "source_port": "output",
                        "target_port": "input",
                    }
                },
            ),
            (
                "add_edge",
                {
                    "edge": {
                        "id": "deduplicate-end",
                        "source": "deduplicate",
                        "target": "end",
                        "source_port": "output",
                        "target_port": "input",
                    }
                },
            ),
            (
                "add_test",
                {
                    "test": {
                        "id": "empty-cardinality",
                        "name": "Empty cardinality",
                        "requirement": "Required first record must fail with a precise path.",
                        "inputs": {"records": []},
                        "mandatory": True,
                    }
                },
            ),
        ]
        _apply(client, headers, application_id, operations)
        result = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/tests/run",
            headers,
            key="reference-diagnostic-run",
            json={},
        )
        assert result.status_code == 200, result.text
        test = result.json()["data"]["tests"][0]
        assert test["passed"] is False
        assert "node='deduplicate'" in test["run_error"]
        assert "path=['unique', 0, 'id']" in test["run_error"]
        assert "failed_segment=0" in test["run_error"]
        assert "container_type=list" in test["run_error"]
        assert "container_length=0" in test["run_error"]
