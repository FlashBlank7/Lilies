from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class ForecastModelConflict(ValueError):
    """A deployment revision or idempotency precondition did not match."""


class TimeSeriesPoint(BaseModel):
    timestamp: date
    value: float = Field(ge=0)


class ForecastSeries(BaseModel):
    series_id: str = Field(min_length=1, max_length=200)
    points: list[TimeSeriesPoint] = Field(min_length=2, max_length=100_000)

    @field_validator("points")
    @classmethod
    def chronological_points(cls, value: list[TimeSeriesPoint]) -> list[TimeSeriesPoint]:
        timestamps = [point.timestamp for point in value]
        if any(right <= left for left, right in zip(timestamps, timestamps[1:])):
            raise ValueError("time-series points must be unique and strictly chronological")
        return value


class TrainForecastModelRequest(BaseModel):
    model_name: str = Field(min_length=2, max_length=120)
    unit: str = Field(min_length=1, max_length=64)
    series: list[ForecastSeries] = Field(min_length=1, max_length=1_000)
    algorithm: Literal["seasonal_naive"] = "seasonal_naive"
    seasonal_period: int = Field(default=7, ge=1, le=365)
    interval_coverage: float = Field(default=0.9, gt=0.5, lt=1)
    retraining_wape_threshold: float = Field(default=0.35, gt=0, le=10)
    source: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=200)

    @model_validator(mode="after")
    def sufficient_history(self) -> TrainForecastModelRequest:
        if any(len(item.points) < self.seasonal_period * 2 for item in self.series):
            raise ValueError("each training series needs at least two seasonal periods")
        if len({item.series_id for item in self.series}) != len(self.series):
            raise ValueError("training series ids must be unique")
        return self


class ImportForecastModelRequest(BaseModel):
    model_name: str = Field(min_length=2, max_length=120)
    unit: str = Field(min_length=1, max_length=64)
    algorithm: Literal["seasonal_naive"] = "seasonal_naive"
    seasonal_period: int = Field(default=7, ge=1, le=365)
    interval_coverage: float = Field(default=0.9, gt=0.5, lt=1)
    interval_radius: float = Field(default=0, ge=0)
    retraining_wape_threshold: float = Field(default=0.35, gt=0, le=10)
    source: dict[str, Any]
    idempotency_key: str = Field(min_length=8, max_length=200)


class FineTuneForecastModelRequest(BaseModel):
    series: list[ForecastSeries] = Field(min_length=1, max_length=1_000)
    source: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=200)

    @field_validator("series")
    @classmethod
    def unique_series(cls, value: list[ForecastSeries]) -> list[ForecastSeries]:
        if len({item.series_id for item in value}) != len(value):
            raise ValueError("fine-tuning series ids must be unique")
        return value


class ForecastEvaluationSeries(BaseModel):
    series_id: str = Field(min_length=1, max_length=200)
    history: list[TimeSeriesPoint] = Field(min_length=2, max_length=100_000)
    actual: list[TimeSeriesPoint] = Field(min_length=1, max_length=365)

    @model_validator(mode="after")
    def chronological_split(self) -> ForecastEvaluationSeries:
        ForecastSeries(series_id=self.series_id, points=self.history)
        ForecastSeries(series_id=self.series_id, points=self.actual)
        if self.actual[0].timestamp <= self.history[-1].timestamp:
            raise ValueError("evaluation actuals must occur after history")
        return self


class EvaluateForecastModelRequest(BaseModel):
    series: list[ForecastEvaluationSeries] = Field(min_length=1, max_length=1_000)
    idempotency_key: str = Field(min_length=8, max_length=200)

    @field_validator("series")
    @classmethod
    def unique_series(cls, value: list[ForecastEvaluationSeries]) -> list[ForecastEvaluationSeries]:
        if len({item.series_id for item in value}) != len(value):
            raise ValueError("evaluation series ids must be unique")
        return value


class PromoteForecastModelRequest(BaseModel):
    model_id: str
    version: int = Field(ge=1)
    evaluation_id: str
    approved_by: str = Field(min_length=2, max_length=200)
    approval_reason: str = Field(min_length=3, max_length=1_000)
    expected_revision: int = Field(default=0, ge=0)
    maximum_wape: float = Field(default=1, ge=0, le=10)
    maximum_mase: float = Field(default=1, ge=0, le=10)
    minimum_interval_coverage: float = Field(default=0, ge=0, le=1)
    idempotency_key: str = Field(min_length=8, max_length=200)


class RollbackForecastDeploymentRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    target_revision: int | None = Field(default=None, ge=1)
    approved_by: str = Field(min_length=2, max_length=200)
    approval_reason: str = Field(min_length=3, max_length=1_000)
    idempotency_key: str = Field(min_length=8, max_length=200)


class ForecastInferenceRequest(BaseModel):
    series: list[ForecastSeries] = Field(min_length=1, max_length=1_000)
    unit: str = Field(min_length=1, max_length=64)
    horizon: int = Field(ge=1, le=365)

    @field_validator("series")
    @classmethod
    def unique_series(cls, value: list[ForecastSeries]) -> list[ForecastSeries]:
        if len({item.series_id for item in value}) != len(value):
            raise ValueError("inference series ids must be unique")
        return value


class ForecastModelService:
    """Deterministic registry for governed time-series forecasting models."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS forecast_models (
                  model_id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS forecast_model_versions (
                  model_id TEXT NOT NULL,
                  version INTEGER NOT NULL,
                  spec_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(model_id, version)
                );
                CREATE TABLE IF NOT EXISTS forecast_model_evaluations (
                  evaluation_id TEXT PRIMARY KEY,
                  model_id TEXT NOT NULL,
                  version INTEGER NOT NULL,
                  dataset_digest TEXT NOT NULL,
                  metrics_json TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS forecast_model_deployments (
                  name TEXT PRIMARY KEY,
                  record_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS forecast_model_deployment_history (
                  name TEXT NOT NULL,
                  revision INTEGER NOT NULL,
                  record_json TEXT NOT NULL,
                  PRIMARY KEY(name, revision)
                );
                CREATE TABLE IF NOT EXISTS forecast_model_idempotency (
                  scope TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  request_digest TEXT NOT NULL,
                  response_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(scope, idempotency_key)
                );
                """
            )

    async def train(self, request: TrainForecastModelRequest) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._train_sync, request)

    def _train_sync(self, request: TrainForecastModelRequest) -> dict[str, Any]:
        payload = request.model_dump(mode="json")
        with self._connect() as conn:
            replay = self._replay(conn, "train", request.idempotency_key, _sha256(payload))
            if replay is not None:
                return {**replay, "replayed": True}
            residuals = self._seasonal_residuals(request.series, request.seasonal_period)
            model_id = str(uuid4())
            now = _utc_now()
            spec = self._build_spec(
                model_id=model_id,
                version=1,
                model_name=request.model_name,
                route="train_new",
                unit=request.unit,
                algorithm=request.algorithm,
                seasonal_period=request.seasonal_period,
                interval_coverage=request.interval_coverage,
                interval_radius=self._quantile(residuals, request.interval_coverage),
                retraining_wape_threshold=request.retraining_wape_threshold,
                dataset_digest=_sha256(payload["series"]),
                source=request.source,
                lineage={"base_model": None},
                training_metrics={
                    "rolling_origin_mae": sum(residuals) / len(residuals),
                    "rolling_origin_observations": len(residuals),
                },
            )
            conn.execute(
                "INSERT INTO forecast_models VALUES(?,?,?,?)",
                (model_id, request.model_name, now, now),
            )
            conn.execute(
                "INSERT INTO forecast_model_versions VALUES(?,?,?,?)",
                (model_id, 1, _canonical_json(spec), now),
            )
            response = self._public_version(spec)
            self._save_replay(conn, "train", request.idempotency_key, _sha256(payload), response)
            return {**response, "replayed": False}

    async def import_model(self, request: ImportForecastModelRequest) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._import_sync, request)

    def _import_sync(self, request: ImportForecastModelRequest) -> dict[str, Any]:
        payload = request.model_dump(mode="json")
        with self._connect() as conn:
            replay = self._replay(conn, "import", request.idempotency_key, _sha256(payload))
            if replay is not None:
                return {**replay, "replayed": True}
            model_id = str(uuid4())
            now = _utc_now()
            spec = self._build_spec(
                model_id=model_id,
                version=1,
                model_name=request.model_name,
                route="import",
                unit=request.unit,
                algorithm=request.algorithm,
                seasonal_period=request.seasonal_period,
                interval_coverage=request.interval_coverage,
                interval_radius=request.interval_radius,
                retraining_wape_threshold=request.retraining_wape_threshold,
                dataset_digest=str(request.source.get("dataset_digest", "")),
                source=request.source,
                lineage={"base_model": request.source.get("base_model")},
                training_metrics=None,
            )
            conn.execute(
                "INSERT INTO forecast_models VALUES(?,?,?,?)",
                (model_id, request.model_name, now, now),
            )
            conn.execute(
                "INSERT INTO forecast_model_versions VALUES(?,?,?,?)",
                (model_id, 1, _canonical_json(spec), now),
            )
            response = self._public_version(spec)
            self._save_replay(conn, "import", request.idempotency_key, _sha256(payload), response)
            return {**response, "replayed": False}

    async def fine_tune(
        self,
        model_id: str,
        version: int,
        request: FineTuneForecastModelRequest,
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._fine_tune_sync, model_id, version, request)

    def _fine_tune_sync(
        self,
        model_id: str,
        version: int,
        request: FineTuneForecastModelRequest,
    ) -> dict[str, Any]:
        payload = request.model_dump(mode="json")
        scope = f"fine_tune:{model_id}:{version}"
        with self._connect() as conn:
            replay = self._replay(conn, scope, request.idempotency_key, _sha256(payload))
            if replay is not None:
                return {**replay, "replayed": True}
            base = self._load_version(conn, model_id, version)
            period = int(base["seasonal_period"])
            if any(len(item.points) < period * 2 for item in request.series):
                raise ValueError("each fine-tuning series needs at least two seasonal periods")
            residuals = self._seasonal_residuals(request.series, period)
            next_version = int(
                conn.execute(
                    "SELECT COALESCE(MAX(version),0)+1 AS value "
                    "FROM forecast_model_versions WHERE model_id=?",
                    (model_id,),
                ).fetchone()["value"]
            )
            now = _utc_now()
            spec = self._build_spec(
                model_id=model_id,
                version=next_version,
                model_name=str(base["model_name"]),
                route="fine_tune",
                unit=str(base["unit"]),
                algorithm=str(base["algorithm"]),
                seasonal_period=period,
                interval_coverage=float(base["interval_coverage"]),
                interval_radius=self._quantile(residuals, float(base["interval_coverage"])),
                retraining_wape_threshold=float(base["retraining_wape_threshold"]),
                dataset_digest=_sha256(payload["series"]),
                source=request.source,
                lineage={
                    "base_model": {
                        "model_id": model_id,
                        "version": version,
                        "model_digest": base["model_digest"],
                    }
                },
                training_metrics={
                    "rolling_origin_mae": sum(residuals) / len(residuals),
                    "rolling_origin_observations": len(residuals),
                },
            )
            conn.execute(
                "INSERT INTO forecast_model_versions VALUES(?,?,?,?)",
                (model_id, next_version, _canonical_json(spec), now),
            )
            conn.execute(
                "UPDATE forecast_models SET updated_at=? WHERE model_id=?",
                (now, model_id),
            )
            response = self._public_version(spec)
            self._save_replay(conn, scope, request.idempotency_key, _sha256(payload), response)
            return {**response, "replayed": False}

    async def evaluate(
        self,
        model_id: str,
        version: int,
        request: EvaluateForecastModelRequest,
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._evaluate_sync, model_id, version, request)

    def _evaluate_sync(
        self,
        model_id: str,
        version: int,
        request: EvaluateForecastModelRequest,
    ) -> dict[str, Any]:
        payload = request.model_dump(mode="json")
        scope = f"evaluate:{model_id}:{version}"
        with self._connect() as conn:
            replay = self._replay(conn, scope, request.idempotency_key, _sha256(payload))
            if replay is not None:
                return {**replay, "replayed": True}
            spec = self._load_version(conn, model_id, version)
            actuals: list[float] = []
            predictions: list[float] = []
            covered = 0
            scale_errors: list[float] = []
            per_series: list[dict[str, Any]] = []
            period = int(spec["seasonal_period"])
            radius = float(spec["interval_radius"])
            for item in request.series:
                if len(item.history) < period:
                    raise ValueError("evaluation history is shorter than the seasonal period")
                predicted = self._forecast_values(
                    [point.value for point in item.history],
                    len(item.actual),
                    period,
                )
                item_actual = [point.value for point in item.actual]
                errors = [
                    abs(actual - prediction)
                    for actual, prediction in zip(item_actual, predicted, strict=True)
                ]
                denominators = [
                    abs(item.history[index].value - item.history[index - period].value)
                    for index in range(period, len(item.history))
                ]
                scale = sum(denominators) / len(denominators) if denominators else 0
                mase = (sum(errors) / len(errors)) / max(scale, 1e-12)
                item_covered = sum(
                    max(0, prediction - radius) <= actual <= prediction + radius
                    for actual, prediction in zip(item_actual, predicted, strict=True)
                )
                actuals.extend(item_actual)
                predictions.extend(predicted)
                covered += item_covered
                scale_errors.extend([max(scale, 1e-12)] * len(errors))
                per_series.append(
                    {
                        "series_id": item.series_id,
                        "wape": sum(errors) / max(sum(item_actual), 1e-12),
                        "mase": mase,
                        "interval_coverage": item_covered / len(item_actual),
                        "observations": len(item.actual),
                    }
                )
            errors = [
                abs(actual - prediction)
                for actual, prediction in zip(actuals, predictions, strict=True)
            ]
            metrics = {
                "wape": sum(errors) / max(sum(actuals), 1e-12),
                "mae": sum(errors) / len(errors),
                "rmse": math.sqrt(
                    sum((a - p) ** 2 for a, p in zip(actuals, predictions, strict=True))
                    / len(errors)
                ),
                "mase": sum(
                    error / scale for error, scale in zip(errors, scale_errors, strict=True)
                )
                / len(errors),
                "interval_coverage": covered / len(errors),
                "observations": len(errors),
                "per_series": per_series,
            }
            evaluation_id = str(uuid4())
            now = _utc_now()
            dataset_digest = _sha256(payload["series"])
            response = {
                "evaluation_id": evaluation_id,
                "model_id": model_id,
                "version": version,
                "model_digest": spec["model_digest"],
                "dataset_digest": dataset_digest,
                "metrics": metrics,
                "created_at": now,
            }
            conn.execute(
                "INSERT INTO forecast_model_evaluations VALUES(?,?,?,?,?,?)",
                (
                    evaluation_id,
                    model_id,
                    version,
                    dataset_digest,
                    _canonical_json(metrics),
                    now,
                ),
            )
            self._save_replay(conn, scope, request.idempotency_key, _sha256(payload), response)
            return {**response, "replayed": False}

    async def promote(
        self, deployment_name: str, request: PromoteForecastModelRequest
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._promote_sync, deployment_name, request)

    def _promote_sync(
        self, deployment_name: str, request: PromoteForecastModelRequest
    ) -> dict[str, Any]:
        payload = request.model_dump(mode="json")
        scope = f"promote:{deployment_name}"
        with self._connect() as conn:
            replay = self._replay(conn, scope, request.idempotency_key, _sha256(payload))
            if replay is not None:
                return {**replay, "replayed": True}
            spec = self._load_version(conn, request.model_id, request.version)
            row = conn.execute(
                "SELECT * FROM forecast_model_evaluations WHERE evaluation_id=?",
                (request.evaluation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"evaluation not found: {request.evaluation_id}")
            if row["model_id"] != request.model_id or int(row["version"]) != request.version:
                raise ValueError("evaluation does not belong to the promoted model version")
            metrics = json.loads(str(row["metrics_json"]))
            if float(metrics["wape"]) > request.maximum_wape:
                raise ValueError("evaluation WAPE exceeds the promotion limit")
            if float(metrics["mase"]) > request.maximum_mase:
                raise ValueError("evaluation MASE exceeds the promotion limit")
            if float(metrics["interval_coverage"]) < request.minimum_interval_coverage:
                raise ValueError("evaluation interval coverage is below the promotion limit")
            current = self._load_deployment_optional(conn, deployment_name)
            current_revision = int(current["revision"]) if current else 0
            if current_revision != request.expected_revision:
                raise ForecastModelConflict(
                    f"deployment revision conflict: expected {request.expected_revision}, "
                    f"current {current_revision}"
                )
            now = _utc_now()
            record = {
                "name": deployment_name,
                "revision": current_revision + 1,
                "model_id": request.model_id,
                "version": request.version,
                "model_digest": spec["model_digest"],
                "evaluation_id": request.evaluation_id,
                "metrics": metrics,
                "approved_by": request.approved_by,
                "approval_reason": request.approval_reason,
                "action": "promote",
                "created_at": now,
                "updated_at": now,
            }
            self._save_deployment(conn, record)
            self._save_replay(conn, scope, request.idempotency_key, _sha256(payload), record)
            return {**record, "replayed": False}

    async def rollback(
        self, deployment_name: str, request: RollbackForecastDeploymentRequest
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._rollback_sync, deployment_name, request)

    def _rollback_sync(
        self, deployment_name: str, request: RollbackForecastDeploymentRequest
    ) -> dict[str, Any]:
        payload = request.model_dump(mode="json")
        scope = f"rollback:{deployment_name}"
        with self._connect() as conn:
            replay = self._replay(conn, scope, request.idempotency_key, _sha256(payload))
            if replay is not None:
                return {**replay, "replayed": True}
            current = self._load_deployment(conn, deployment_name)
            if int(current["revision"]) != request.expected_revision:
                raise ForecastModelConflict("deployment revision conflict")
            target_revision = request.target_revision or request.expected_revision - 1
            if target_revision < 1 or target_revision >= request.expected_revision:
                raise ValueError("rollback target must be an earlier deployment revision")
            target_row = conn.execute(
                "SELECT record_json FROM forecast_model_deployment_history "
                "WHERE name=? AND revision=?",
                (deployment_name, target_revision),
            ).fetchone()
            if target_row is None:
                raise KeyError(f"deployment history not found: {deployment_name}@{target_revision}")
            target = json.loads(str(target_row["record_json"]))
            now = _utc_now()
            record = {
                **target,
                "revision": request.expected_revision + 1,
                "approved_by": request.approved_by,
                "approval_reason": request.approval_reason,
                "action": "rollback",
                "rollback_from_revision": request.expected_revision,
                "rollback_target_revision": target_revision,
                "created_at": now,
                "updated_at": now,
            }
            self._save_deployment(conn, record)
            self._save_replay(conn, scope, request.idempotency_key, _sha256(payload), record)
            return {**record, "replayed": False}

    async def predict(
        self, deployment_name: str, request: ForecastInferenceRequest
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._predict_sync, deployment_name, request)

    def _predict_sync(
        self, deployment_name: str, request: ForecastInferenceRequest
    ) -> dict[str, Any]:
        with self._connect() as conn:
            deployment = self._load_deployment(conn, deployment_name)
            spec = self._load_version(conn, str(deployment["model_id"]), int(deployment["version"]))
        if request.unit != spec["unit"]:
            raise ValueError(f"forecast unit mismatch: expected {spec['unit']}, got {request.unit}")
        period = int(spec["seasonal_period"])
        radius = float(spec["interval_radius"])
        forecasts: list[dict[str, Any]] = []
        recent_errors: list[float] = []
        recent_actuals: list[float] = []
        for item in request.series:
            if len(item.points) < period * 2:
                raise ValueError("inference series needs at least two seasonal periods")
            history_values = [point.value for point in item.points]
            values = self._forecast_values(history_values, request.horizon, period)
            start_ordinal = item.points[-1].timestamp.toordinal()
            points = [
                {
                    "timestamp": date.fromordinal(start_ordinal + index + 1).isoformat(),
                    "point": value,
                    "lower": max(0, value - radius),
                    "upper": value + radius,
                }
                for index, value in enumerate(values)
            ]
            recent_actual = history_values[-period:]
            previous = history_values[-period * 2 : -period]
            recent_errors.extend(
                abs(actual - predicted)
                for actual, predicted in zip(recent_actual, previous, strict=True)
            )
            recent_actuals.extend(recent_actual)
            forecasts.append(
                {
                    "series_id": item.series_id,
                    "unit": request.unit,
                    "horizon": request.horizon,
                    "forecast_total": sum(values),
                    "points": points,
                }
            )
        recent_wape = sum(recent_errors) / max(sum(recent_actuals), 1e-12)
        threshold = float(spec["retraining_wape_threshold"])
        return {
            "deployment_name": deployment_name,
            "deployment_revision": int(deployment["revision"]),
            "model_id": spec["model_id"],
            "version": int(spec["version"]),
            "model_digest": spec["model_digest"],
            "evaluation_id": deployment["evaluation_id"],
            "evaluation_metrics": deployment["metrics"],
            "unit": request.unit,
            "forecasts": forecasts,
            "monitoring": {
                "recent_backtest_wape": recent_wape,
                "retraining_wape_threshold": threshold,
                "status": "retraining_recommended" if recent_wape > threshold else "stable",
                "retraining_recommended": recent_wape > threshold,
                "automatic_training_triggered": False,
            },
            "lineage": spec["lineage"],
            "model_card": {
                "model_name": spec["model_name"],
                "task_type": "time_series_forecasting",
                "route": spec["route"],
                "unit": spec["unit"],
                "algorithm": spec["algorithm"],
                "seasonal_period": period,
                "training_dataset_digest": spec["dataset_digest"],
                "source": spec["source"],
                "lineage": spec["lineage"],
                "training_metrics": spec["training_metrics"],
            },
        }

    async def list_models(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_models_sync)

    def _list_models_sync(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT m.*, COUNT(v.version) AS version_count, "
                "MAX(v.version) AS latest_version "
                "FROM forecast_models m LEFT JOIN forecast_model_versions v "
                "ON m.model_id=v.model_id GROUP BY m.model_id ORDER BY m.created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    async def get_version(self, model_id: str, version: int) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_version_sync, model_id, version)

    def _get_version_sync(self, model_id: str, version: int) -> dict[str, Any]:
        with self._connect() as conn:
            return self._public_version(self._load_version(conn, model_id, version))

    async def get_deployment(self, name: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_deployment_sync, name)

    def _get_deployment_sync(self, name: str) -> dict[str, Any]:
        with self._connect() as conn:
            return self._load_deployment(conn, name)

    @staticmethod
    def _seasonal_residuals(series: list[ForecastSeries], period: int) -> list[float]:
        residuals = [
            abs(item.points[index].value - item.points[index - period].value)
            for item in series
            for index in range(period, len(item.points))
        ]
        if not residuals:
            raise ValueError("training data does not contain seasonal comparisons")
        return residuals

    @staticmethod
    def _quantile(values: list[float], coverage: float) -> float:
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, math.ceil(coverage * len(ordered)) - 1))
        return float(ordered[index])

    @staticmethod
    def _forecast_values(history: list[float], horizon: int, period: int) -> list[float]:
        values = list(history)
        result: list[float] = []
        for _ in range(horizon):
            prediction = float(values[-period])
            values.append(prediction)
            result.append(prediction)
        return result

    @staticmethod
    def _build_spec(**values: Any) -> dict[str, Any]:
        core = {
            **values,
            "task_type": "time_series_forecasting",
        }
        return {
            **core,
            "model_digest": _sha256(core),
            "created_at": _utc_now(),
        }

    @staticmethod
    def _public_version(spec: dict[str, Any]) -> dict[str, Any]:
        return dict(spec)

    @staticmethod
    def _load_version(conn: sqlite3.Connection, model_id: str, version: int) -> dict[str, Any]:
        row = conn.execute(
            "SELECT spec_json FROM forecast_model_versions WHERE model_id=? AND version=?",
            (model_id, version),
        ).fetchone()
        if row is None:
            raise KeyError(f"forecast model version not found: {model_id}@{version}")
        return json.loads(str(row["spec_json"]))

    @staticmethod
    def _load_deployment_optional(conn: sqlite3.Connection, name: str) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT record_json FROM forecast_model_deployments WHERE name=?", (name,)
        ).fetchone()
        return json.loads(str(row["record_json"])) if row else None

    @classmethod
    def _load_deployment(cls, conn: sqlite3.Connection, name: str) -> dict[str, Any]:
        value = cls._load_deployment_optional(conn, name)
        if value is None:
            raise KeyError(f"forecast deployment not found: {name}")
        return value

    @staticmethod
    def _save_deployment(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
        payload = _canonical_json(record)
        conn.execute(
            "INSERT INTO forecast_model_deployments(name,record_json) VALUES(?,?) "
            "ON CONFLICT(name) DO UPDATE SET record_json=excluded.record_json",
            (record["name"], payload),
        )
        conn.execute(
            "INSERT INTO forecast_model_deployment_history VALUES(?,?,?)",
            (record["name"], record["revision"], payload),
        )

    @staticmethod
    def _replay(
        conn: sqlite3.Connection,
        scope: str,
        key: str,
        request_digest: str,
    ) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT request_digest,response_json FROM forecast_model_idempotency "
            "WHERE scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_digest"] != request_digest:
            raise ForecastModelConflict("idempotency key reused with a different request")
        return json.loads(str(row["response_json"]))

    @staticmethod
    def _save_replay(
        conn: sqlite3.Connection,
        scope: str,
        key: str,
        request_digest: str,
        response: dict[str, Any],
    ) -> None:
        conn.execute(
            "INSERT INTO forecast_model_idempotency VALUES(?,?,?,?,?)",
            (scope, key, request_digest, _canonical_json(response), _utc_now()),
        )
