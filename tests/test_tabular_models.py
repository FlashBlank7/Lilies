from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.tabular_models import (
    EvaluateTabularModelRequest,
    FeatureContract,
    FineTuneTabularModelRequest,
    ImportTabularModelRequest,
    LabeledObservation,
    ModelObservation,
    PromoteTabularModelRequest,
    RollbackTabularDeploymentRequest,
    TabularDriftRequest,
    TabularInferenceRequest,
    TabularModelConflict,
    TabularModelService,
    TrainTabularModelRequest,
)
from tests.test_runtime import ScriptedProvider


def _rows(offset: float = 0) -> list[LabeledObservation]:
    return [
        LabeledObservation(features={"signal": value + offset}, units={"signal": "u"}, label=label)
        for value, label in [
            (-4.0, 0),
            (-3.0, 0),
            (-2.0, 0),
            (-1.0, 0),
            (1.0, 1),
            (2.0, 1),
            (3.0, 1),
            (4.0, 1),
        ]
    ]


async def _service(tmp_path: Path) -> TabularModelService:
    service = TabularModelService(tmp_path / "models.db")
    await service.initialize()
    return service


@pytest.mark.asyncio
async def test_train_evaluate_promote_predict_and_idempotent_replay(
    tmp_path: Path,
) -> None:
    service = await _service(tmp_path)
    request = TrainTabularModelRequest(
        model_name="generic-risk",
        features=[FeatureContract(name="signal", unit="u", minimum=-10, maximum=10)],
        rows=_rows(),
        idempotency_key="train-generic-risk-001",
        source={"kind": "customer_dataset"},
    )

    trained = await service.train(request)
    replayed = await service.train(request)

    assert trained["route"] == "train_new"
    assert trained["model_digest"].startswith("sha256:")
    assert replayed["model_id"] == trained["model_id"]
    assert replayed["replayed"] is True

    evaluated = await service.evaluate(
        trained["model_id"],
        1,
        EvaluateTabularModelRequest(
            rows=_rows(),
            idempotency_key="evaluate-generic-risk-001",
        ),
    )
    assert evaluated["metrics"]["recall"] == 1
    promoted = await service.promote(
        "production-risk",
        PromoteTabularModelRequest(
            model_id=trained["model_id"],
            version=1,
            evaluation_id=evaluated["evaluation_id"],
            approved_by="model-owner",
            approval_reason="Held-out acceptance met",
            minimum_recall=0.9,
            idempotency_key="promote-generic-risk-001",
        ),
    )
    assert promoted["revision"] == 1

    low = await service.predict(
        "production-risk",
        TabularInferenceRequest(features={"signal": -4}, units={"signal": "u"}),
    )
    high = await service.predict(
        "production-risk",
        TabularInferenceRequest(features={"signal": 4}, units={"signal": "u"}),
    )

    assert low["probability"] < high["probability"]
    assert low["predicted_label"] == 0
    assert high["predicted_label"] == 1
    assert high["model_digest"] == trained["model_digest"]
    assert high["deployment_revision"] == 1
    assert high["model_card"]["route"] == "train_new"
    assert high["evaluation_metrics"]["recall"] == 1


@pytest.mark.asyncio
async def test_import_fine_tune_promote_and_rollback_preserve_lineage(
    tmp_path: Path,
) -> None:
    service = await _service(tmp_path)
    imported = await service.import_model(
        ImportTabularModelRequest(
            model_name="external-candidate",
            features=[FeatureContract(name="signal", unit="u", minimum=-10, maximum=10)],
            weights={"signal": 1.0},
            intercept=0,
            source={
                "registry": "customer-model-library",
                "artifact_digest": "sha256:external",
                "license": "customer-owned",
            },
            idempotency_key="import-external-candidate-001",
        )
    )
    base_evaluation = await service.evaluate(
        imported["model_id"],
        1,
        EvaluateTabularModelRequest(
            rows=_rows(),
            idempotency_key="evaluate-external-candidate-001",
        ),
    )
    await service.promote(
        "production-risk",
        PromoteTabularModelRequest(
            model_id=imported["model_id"],
            version=1,
            evaluation_id=base_evaluation["evaluation_id"],
            approved_by="model-owner",
            approval_reason="Approve imported baseline",
            idempotency_key="promote-external-candidate-001",
        ),
    )

    tuned = await service.fine_tune(
        imported["model_id"],
        1,
        FineTuneTabularModelRequest(
            rows=_rows(),
            source={"kind": "customer_authorized_fine_tune"},
            idempotency_key="fine-tune-external-candidate-001",
        ),
    )
    assert tuned["version"] == 2
    assert tuned["route"] == "fine_tune"
    assert tuned["lineage"]["base_model"]["version"] == 1
    assert tuned["lineage"]["base_model"]["model_digest"] == imported["model_digest"]

    tuned_evaluation = await service.evaluate(
        imported["model_id"],
        2,
        EvaluateTabularModelRequest(
            rows=_rows(),
            idempotency_key="evaluate-fine-tuned-candidate-001",
        ),
    )
    promoted = await service.promote(
        "production-risk",
        PromoteTabularModelRequest(
            model_id=imported["model_id"],
            version=2,
            evaluation_id=tuned_evaluation["evaluation_id"],
            approved_by="model-owner",
            approval_reason="Approve fine-tuned candidate",
            expected_revision=1,
            idempotency_key="promote-fine-tuned-candidate-001",
        ),
    )
    assert promoted["revision"] == 2

    rolled_back = await service.rollback(
        "production-risk",
        RollbackTabularDeploymentRequest(
            expected_revision=2,
            approved_by="model-owner",
            approval_reason="Observed regression; restore approved baseline",
            idempotency_key="rollback-fine-tuned-candidate-001",
        ),
    )
    assert rolled_back["revision"] == 3
    assert rolled_back["version"] == 1
    assert rolled_back["rollback_target_revision"] == 1


@pytest.mark.asyncio
async def test_units_drift_and_idempotency_conflicts_are_governed(
    tmp_path: Path,
) -> None:
    service = await _service(tmp_path)
    trained = await service.train(
        TrainTabularModelRequest(
            model_name="drift-model",
            features=[FeatureContract(name="signal", unit="u", minimum=-20, maximum=20)],
            rows=_rows(),
            idempotency_key="train-drift-model-001",
        )
    )
    evaluated = await service.evaluate(
        trained["model_id"],
        1,
        EvaluateTabularModelRequest(
            rows=_rows(),
            idempotency_key="evaluate-drift-model-001",
        ),
    )
    await service.promote(
        "drift-production",
        PromoteTabularModelRequest(
            model_id=trained["model_id"],
            version=1,
            evaluation_id=evaluated["evaluation_id"],
            approved_by="model-owner",
            approval_reason="Approve for drift test",
            idempotency_key="promote-drift-model-001",
        ),
    )

    with pytest.raises(ValueError, match="expected unit"):
        await service.predict(
            "drift-production",
            TabularInferenceRequest(features={"signal": 1}, units={"signal": "wrong"}),
        )

    drift = await service.drift(
        "drift-production",
        TabularDriftRequest(
            observations=[
                ModelObservation(features={"signal": 8}, units={"signal": "u"}),
                ModelObservation(features={"signal": 9}, units={"signal": "u"}),
            ],
            warning_threshold=1,
            critical_threshold=2,
        ),
    )
    assert drift["status"] == "critical"
    assert drift["automatic_training_triggered"] is False

    with pytest.raises(TabularModelConflict, match="different request"):
        await service.train(
            TrainTabularModelRequest(
                model_name="different-name",
                features=[
                    FeatureContract(name="signal", unit="u", minimum=-20, maximum=20)
                ],
                rows=_rows(),
                idempotency_key="train-drift-model-001",
            )
        )


def test_public_api_and_workflow_block_call_the_approved_deployment(
    tmp_path: Path,
) -> None:
    settings = Settings(
        api_token="model-test-token",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    headers = {
        "Authorization": "Bearer model-test-token",
        "Content-Type": "application/json",
    }
    serialized_rows = [row.model_dump(mode="json") for row in _rows()]

    with TestClient(app) as client:
        trained_response = client.post(
            "/api/v1/tabular-models/train",
            headers=headers,
            json={
                "model_name": "workflow-risk",
                "features": [
                    {"name": "signal", "unit": "u", "minimum": -10, "maximum": 10}
                ],
                "rows": serialized_rows,
                "idempotency_key": "api-train-workflow-risk-001",
            },
        )
        assert trained_response.status_code == 200, trained_response.text
        trained = trained_response.json()
        evaluated_response = client.post(
            f"/api/v1/tabular-models/{trained['model_id']}/versions/1/evaluate",
            headers=headers,
            json={
                "rows": serialized_rows,
                "idempotency_key": "api-evaluate-workflow-risk-001",
            },
        )
        assert evaluated_response.status_code == 200, evaluated_response.text
        evaluated = evaluated_response.json()
        promoted_response = client.post(
            "/api/v1/model-deployments/workflow-production/promote",
            headers=headers,
            json={
                "model_id": trained["model_id"],
                "version": 1,
                "evaluation_id": evaluated["evaluation_id"],
                "approved_by": "model-owner",
                "approval_reason": "Public API evaluation passed",
                "minimum_recall": 0.9,
                "idempotency_key": "api-promote-workflow-risk-001",
            },
        )
        assert promoted_response.status_code == 200, promoted_response.text

        block_response = client.get("/api/v1/blocks", headers=headers)
        assert block_response.status_code == 200
        blocks = {item["type"]: item for item in block_response.json()}
        assert "deployed_model_inference" in blocks
        assert "model_drift_monitor" in blocks

        created = client.post(
            "/api/v1/applications",
            headers=headers,
            json={
                "name": "Approved model inference",
                "requirement": "Call one approved deployment and preserve model lineage.",
            },
        )
        assert created.status_code == 201, created.text
        application_id = created.json()["id"]
        revision = 0

        def mutate(op: str, data: dict) -> None:
            nonlocal revision
            response = client.post(
                f"/api/v1/applications/{application_id}/draft",
                headers=headers,
                json={
                    "expected_revision": revision,
                    "idempotency_key": str(uuid4()),
                    "op": op,
                    "data": data,
                },
            )
            assert response.status_code == 200, response.text
            revision = response.json()["revision"]

        mutate(
            "add_node",
            {
                "node": {
                    "id": "start",
                    "type": "start",
                    "title": "Telemetry",
                    "config": {
                        "inputs": [
                            {"name": "signal", "type": "number"},
                            {"name": "unit", "type": "string"},
                        ]
                    },
                }
            },
        )
        mutate(
            "add_node",
            {
                "node": {
                    "id": "infer",
                    "type": "deployed_model_inference",
                    "title": "Approved risk inference",
                    "config": {
                        "deployment_name": "workflow-production",
                        "features": {
                            "signal": {
                                "$ref": {"node_id": "start", "path": ["signal"]}
                            }
                        },
                        "units": {
                            "signal": {"$ref": {"node_id": "start", "path": ["unit"]}}
                        },
                    },
                }
            },
        )
        mutate(
            "add_node",
            {
                "node": {
                    "id": "end",
                    "type": "end",
                    "title": "Result",
                    "config": {
                        "outputs": {
                            "label": {
                                "$ref": {
                                    "node_id": "infer",
                                    "path": ["predicted_label"],
                                }
                            },
                            "model_digest": {
                                "$ref": {"node_id": "infer", "path": ["model_digest"]}
                            },
                            "deployment_revision": {
                                "$ref": {
                                    "node_id": "infer",
                                    "path": ["deployment_revision"],
                                }
                            },
                        }
                    },
                }
            },
        )
        mutate(
            "add_edge",
            {"edge": {"id": "a", "source": "start", "target": "infer"}},
        )
        mutate(
            "add_edge",
            {"edge": {"id": "b", "source": "infer", "target": "end"}},
        )
        mutate(
            "add_test",
            {
                "test": {
                    "name": "Approved high-risk inference",
                    "requirement": "High signal uses the approved deployment.",
                    "inputs": {"signal": 4, "unit": "u"},
                    "assertions": [
                        {"path": ["label"], "operator": "equals", "expected": 1},
                        {
                            "path": ["deployment_revision"],
                            "operator": "equals",
                            "expected": 1,
                        },
                    ],
                    "required_node_types": ["deployed_model_inference"],
                }
            },
        )

        validation = client.post(
            f"/api/v1/applications/{application_id}/draft/validate",
            headers=headers,
        )
        assert validation.status_code == 200
        assert validation.json()["valid"] is True, validation.text
        tests = client.post(
            f"/api/v1/applications/{application_id}/tests/run",
            headers=headers,
        )
        assert tests.status_code == 200, tests.text
        assert tests.json()["passed"] is True, tests.text
