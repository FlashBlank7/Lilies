from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.forecast_models import (
    EvaluateForecastModelRequest,
    FineTuneForecastModelRequest,
    ForecastEvaluationSeries,
    ForecastInferenceRequest,
    ForecastModelConflict,
    ForecastModelService,
    ForecastSeries,
    ImportForecastModelRequest,
    PromoteForecastModelRequest,
    RollbackForecastDeploymentRequest,
    TimeSeriesPoint,
    TrainForecastModelRequest,
)
from agent_platform.replenishment import ReplenishmentPlanRequest, solve_replenishment
from tests.test_runtime import ScriptedProvider


def _points(values: list[float], start: date = date(2026, 1, 1)) -> list[TimeSeriesPoint]:
    return [
        TimeSeriesPoint(timestamp=start + timedelta(days=index), value=value)
        for index, value in enumerate(values)
    ]


def _series(series_id: str = "A") -> ForecastSeries:
    return ForecastSeries(
        series_id=series_id,
        points=_points([8, 10, 12, 9, 11, 6, 4] * 4),
    )


@pytest.mark.asyncio
async def test_forecast_lifecycle_is_versioned_evaluated_and_governed(
    tmp_path: Path,
) -> None:
    service = ForecastModelService(tmp_path / "forecast.db")
    await service.initialize()
    training = _series()
    request = TrainForecastModelRequest(
        model_name="weekly-demand",
        unit="ea/day",
        series=[ForecastSeries(series_id="A", points=training.points[:21])],
        idempotency_key="train-weekly-demand-001",
        source={"kind": "customer_history"},
    )
    trained = await service.train(request)
    replayed = await service.train(request)
    assert trained["route"] == "train_new"
    assert replayed["model_id"] == trained["model_id"]
    assert replayed["replayed"] is True

    evaluated = await service.evaluate(
        trained["model_id"],
        1,
        EvaluateForecastModelRequest(
            series=[
                ForecastEvaluationSeries(
                    series_id="A",
                    history=training.points[:21],
                    actual=training.points[21:],
                )
            ],
            idempotency_key="evaluate-weekly-demand-001",
        ),
    )
    assert evaluated["metrics"]["wape"] == 0
    assert evaluated["metrics"]["mase"] == 0
    assert evaluated["metrics"]["interval_coverage"] == 1

    promoted = await service.promote(
        "weekly-production",
        PromoteForecastModelRequest(
            model_id=trained["model_id"],
            version=1,
            evaluation_id=evaluated["evaluation_id"],
            approved_by="model-owner",
            approval_reason="Chronological holdout passed",
            maximum_wape=0.1,
            maximum_mase=0.5,
            minimum_interval_coverage=0.9,
            idempotency_key="promote-weekly-demand-001",
        ),
    )
    assert promoted["revision"] == 1
    prediction = await service.predict(
        "weekly-production",
        ForecastInferenceRequest(series=[training], unit="ea/day", horizon=7),
    )
    assert prediction["forecasts"][0]["forecast_total"] == 60
    assert prediction["monitoring"]["status"] == "stable"
    assert prediction["monitoring"]["automatic_training_triggered"] is False
    assert prediction["model_digest"] == trained["model_digest"]

    with pytest.raises(ValueError, match="unit mismatch"):
        await service.predict(
            "weekly-production",
            ForecastInferenceRequest(series=[training], unit="kg/day", horizon=7),
        )
    with pytest.raises(ForecastModelConflict, match="different request"):
        await service.train(
            TrainForecastModelRequest(
                model_name="different",
                unit="ea/day",
                series=[ForecastSeries(series_id="A", points=training.points[:21])],
                idempotency_key="train-weekly-demand-001",
            )
        )


@pytest.mark.asyncio
async def test_forecast_candidate_import_fine_tune_and_rollback(
    tmp_path: Path,
) -> None:
    service = ForecastModelService(tmp_path / "forecast.db")
    await service.initialize()
    series = _series()
    imported = await service.import_model(
        ImportForecastModelRequest(
            model_name="catalog-candidate",
            unit="ea/day",
            source={
                "registry": "customer-model-catalog",
                "artifact_digest": "sha256:" + "a" * 64,
            },
            idempotency_key="import-catalog-candidate-001",
        )
    )
    assert imported["route"] == "import"
    tuned = await service.fine_tune(
        imported["model_id"],
        1,
        FineTuneForecastModelRequest(
            series=[ForecastSeries(series_id="A", points=series.points[:21])],
            source={"kind": "customer_authorized_fine_tuning_data"},
            idempotency_key="fine-tune-catalog-candidate-001",
        ),
    )
    assert tuned["version"] == 2
    assert tuned["lineage"]["base_model"]["version"] == 1
    evaluated_v1 = await service.evaluate(
        imported["model_id"],
        1,
        EvaluateForecastModelRequest(
            series=[
                ForecastEvaluationSeries(
                    series_id="A",
                    history=series.points[:21],
                    actual=series.points[21:],
                )
            ],
            idempotency_key="evaluate-catalog-candidate-v1",
        ),
    )
    deployed_v1 = await service.promote(
        "catalog-production",
        PromoteForecastModelRequest(
            model_id=imported["model_id"],
            version=1,
            evaluation_id=evaluated_v1["evaluation_id"],
            approved_by="model-owner",
            approval_reason="Imported candidate baseline approved",
            maximum_wape=0.1,
            maximum_mase=0.5,
            minimum_interval_coverage=0.9,
            idempotency_key="promote-catalog-candidate-v1",
        ),
    )
    evaluated_v2 = await service.evaluate(
        imported["model_id"],
        2,
        EvaluateForecastModelRequest(
            series=[
                ForecastEvaluationSeries(
                    series_id="A",
                    history=series.points[:21],
                    actual=series.points[21:],
                )
            ],
            idempotency_key="evaluate-catalog-candidate-v2",
        ),
    )
    deployed_v2 = await service.promote(
        "catalog-production",
        PromoteForecastModelRequest(
            model_id=imported["model_id"],
            version=2,
            evaluation_id=evaluated_v2["evaluation_id"],
            approved_by="model-owner",
            approval_reason="Fine-tuned candidate approved",
            expected_revision=deployed_v1["revision"],
            maximum_wape=0.1,
            maximum_mase=0.5,
            minimum_interval_coverage=0.9,
            idempotency_key="promote-catalog-candidate-v2",
        ),
    )
    rolled_back = await service.rollback(
        "catalog-production",
        RollbackForecastDeploymentRequest(
            expected_revision=deployed_v2["revision"],
            target_revision=deployed_v1["revision"],
            approved_by="model-owner",
            approval_reason="Restore approved imported baseline",
            idempotency_key="rollback-catalog-candidate-to-v1",
        ),
    )
    assert rolled_back["revision"] == 3
    assert rolled_back["version"] == 1
    assert rolled_back["rollback_target_revision"] == 1


def test_replenishment_solver_returns_auditable_feasible_and_infeasible_results() -> None:
    request = ReplenishmentPlanRequest(
        forecasts=[
            {"series_id": "A", "forecast_total": 60},
            {"series_id": "B", "forecast_total": 29},
            {"series_id": "C", "forecast_total": 95},
        ],
        items=[
            {
                "item_code": "A",
                "inventory": 30,
                "inbound": 10,
                "safety_stock": 10,
                "moq": 20,
                "lot_size": 10,
                "unit_cost": 250,
                "minimum_fulfillment": 0.8,
            },
            {
                "item_code": "B",
                "inventory": 20,
                "inbound": 0,
                "safety_stock": 6,
                "moq": 10,
                "lot_size": 10,
                "unit_cost": 400,
                "minimum_fulfillment": 0.8,
            },
            {
                "item_code": "C",
                "inventory": 50,
                "inbound": 10,
                "safety_stock": 15,
                "moq": 25,
                "lot_size": 25,
                "unit_cost": 100,
                "minimum_fulfillment": 0.8,
            },
        ],
        capacity=100,
        budget=22_000,
    )
    feasible = solve_replenishment(request)
    assert feasible["status"] == "feasible"
    assert [line["order_quantity"] for line in feasible["lines"]] == [30, 20, 50]
    assert feasible["capacity"]["used"] == 100
    assert feasible["budget"]["used"] == 20_500
    assert feasible["plan_digest"].startswith("sha256:")

    infeasible = solve_replenishment(request.model_copy(update={"capacity": 10, "budget": 1_000}))
    assert infeasible["status"] == "infeasible"
    assert set(infeasible["binding_constraints"]) == {"budget", "capacity"}
    assert infeasible["lines"] == []
    assert infeasible["infeasibility"]["deficits"]["capacity"] > 0


def test_public_forecast_api_and_two_atomic_blocks_are_discoverable(
    tmp_path: Path,
) -> None:
    app = create_app(
        Settings(
            api_token="forecast-test-token",
            data_dir=tmp_path / "data",
            workspace_root=tmp_path / "workspaces",
        ),
        ScriptedProvider(),
    )
    headers = {"Authorization": "Bearer forecast-test-token"}
    series = _series()
    with TestClient(app) as client:
        trained = client.post(
            "/api/v1/forecast-models/train",
            headers=headers,
            json={
                "model_name": "api-weekly",
                "unit": "ea/day",
                "series": [
                    ForecastSeries(series_id="A", points=series.points[:21]).model_dump(mode="json")
                ],
                "idempotency_key": "api-train-weekly-001",
            },
        )
        assert trained.status_code == 200, trained.text
        model = trained.json()
        evaluated = client.post(
            f"/api/v1/forecast-models/{model['model_id']}/versions/1/evaluate",
            headers=headers,
            json={
                "series": [
                    ForecastEvaluationSeries(
                        series_id="A",
                        history=series.points[:21],
                        actual=series.points[21:],
                    ).model_dump(mode="json")
                ],
                "idempotency_key": "api-evaluate-weekly-001",
            },
        )
        assert evaluated.status_code == 200, evaluated.text
        promoted = client.post(
            "/api/v1/forecast-deployments/api-production/promote",
            headers=headers,
            json={
                "model_id": model["model_id"],
                "version": 1,
                "evaluation_id": evaluated.json()["evaluation_id"],
                "approved_by": "model-owner",
                "approval_reason": "Chronological holdout passed",
                "maximum_wape": 0.1,
                "maximum_mase": 0.5,
                "minimum_interval_coverage": 0.9,
                "idempotency_key": "api-promote-weekly-001",
            },
        )
        assert promoted.status_code == 200, promoted.text
        predicted = client.post(
            "/api/v1/forecast-deployments/api-production/predict",
            headers=headers,
            json={
                "series": [series.model_dump(mode="json")],
                "unit": "ea/day",
                "horizon": 7,
            },
        )
        assert predicted.status_code == 200, predicted.text
        assert predicted.json()["forecasts"][0]["forecast_total"] == 60
        blocks = {
            item["type"]: item for item in client.get("/api/v1/blocks", headers=headers).json()
        }
        assert "deployed_forecast" in blocks
        assert "replenishment_planner" in blocks
        assert "ERPNext" not in json_dump(blocks["deployed_forecast"])
        assert "ERPNext" not in json_dump(blocks["replenishment_planner"])


def json_dump(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True)
