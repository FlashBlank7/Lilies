from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
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


class TabularModelConflict(ValueError):
    """A revision or idempotency precondition did not match current state."""


class FeatureContract(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
    unit: str = Field(min_length=1, max_length=64)
    minimum: float | None = None
    maximum: float | None = None

    @model_validator(mode="after")
    def valid_bounds(self) -> FeatureContract:
        if self.minimum is not None and self.maximum is not None and self.minimum >= self.maximum:
            raise ValueError("feature minimum must be less than maximum")
        return self


class LabeledObservation(BaseModel):
    features: dict[str, float]
    units: dict[str, str]
    label: Literal[0, 1]


class ModelObservation(BaseModel):
    features: dict[str, float]
    units: dict[str, str]


class TrainTabularModelRequest(BaseModel):
    model_name: str = Field(min_length=2, max_length=120)
    features: list[FeatureContract] = Field(min_length=1, max_length=100)
    rows: list[LabeledObservation] = Field(min_length=4, max_length=100_000)
    threshold: float = Field(default=0.65, gt=0, lt=1)
    learning_rate: float = Field(default=0.08, gt=0, le=1)
    epochs: int = Field(default=400, ge=1, le=10_000)
    source: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=200)

    @field_validator("features")
    @classmethod
    def unique_features(cls, value: list[FeatureContract]) -> list[FeatureContract]:
        names = [item.name for item in value]
        if len(set(names)) != len(names):
            raise ValueError("feature names must be unique")
        return value


class ImportTabularModelRequest(BaseModel):
    model_name: str = Field(min_length=2, max_length=120)
    features: list[FeatureContract] = Field(min_length=1, max_length=100)
    weights: dict[str, float]
    intercept: float
    means: dict[str, float] = Field(default_factory=dict)
    scales: dict[str, float] = Field(default_factory=dict)
    drift_baseline: dict[str, dict[str, float]] = Field(default_factory=dict)
    threshold: float = Field(default=0.65, gt=0, lt=1)
    source: dict[str, Any]
    idempotency_key: str = Field(min_length=8, max_length=200)

    @field_validator("features")
    @classmethod
    def unique_features(cls, value: list[FeatureContract]) -> list[FeatureContract]:
        names = [item.name for item in value]
        if len(set(names)) != len(names):
            raise ValueError("feature names must be unique")
        return value


class FineTuneTabularModelRequest(BaseModel):
    rows: list[LabeledObservation] = Field(min_length=4, max_length=100_000)
    learning_rate: float = Field(default=0.03, gt=0, le=1)
    epochs: int = Field(default=200, ge=1, le=10_000)
    source: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=200)


class EvaluateTabularModelRequest(BaseModel):
    rows: list[LabeledObservation] = Field(min_length=2, max_length=100_000)
    idempotency_key: str = Field(min_length=8, max_length=200)


class PromoteTabularModelRequest(BaseModel):
    model_id: str
    version: int = Field(ge=1)
    evaluation_id: str
    approved_by: str = Field(min_length=2, max_length=200)
    approval_reason: str = Field(min_length=3, max_length=1000)
    expected_revision: int = Field(default=0, ge=0)
    minimum_recall: float = Field(default=0, ge=0, le=1)
    idempotency_key: str = Field(min_length=8, max_length=200)


class RollbackTabularDeploymentRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    target_revision: int | None = Field(default=None, ge=1)
    approved_by: str = Field(min_length=2, max_length=200)
    approval_reason: str = Field(min_length=3, max_length=1000)
    idempotency_key: str = Field(min_length=8, max_length=200)


class TabularInferenceRequest(BaseModel):
    features: dict[str, float]
    units: dict[str, str]


class TabularDriftRequest(BaseModel):
    observations: list[ModelObservation] = Field(min_length=2, max_length=100_000)
    warning_threshold: float = Field(default=1.0, gt=0)
    critical_threshold: float = Field(default=2.0, gt=0)

    @model_validator(mode="after")
    def ordered_thresholds(self) -> TabularDriftRequest:
        if self.critical_threshold <= self.warning_threshold:
            raise ValueError("critical threshold must be greater than warning threshold")
        return self


class TabularModelService:
    """Small deterministic model registry for governed tabular binary classifiers."""

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
                CREATE TABLE IF NOT EXISTS tabular_models (
                  model_id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  task_type TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tabular_model_versions (
                  model_id TEXT NOT NULL,
                  version INTEGER NOT NULL,
                  route TEXT NOT NULL,
                  spec_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(model_id, version)
                );
                CREATE TABLE IF NOT EXISTS tabular_model_evaluations (
                  evaluation_id TEXT PRIMARY KEY,
                  model_id TEXT NOT NULL,
                  version INTEGER NOT NULL,
                  dataset_digest TEXT NOT NULL,
                  metrics_json TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tabular_model_deployments (
                  name TEXT PRIMARY KEY,
                  revision INTEGER NOT NULL,
                  model_id TEXT NOT NULL,
                  version INTEGER NOT NULL,
                  evaluation_id TEXT NOT NULL,
                  approved_by TEXT NOT NULL,
                  approval_reason TEXT NOT NULL,
                  action TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tabular_model_deployment_history (
                  name TEXT NOT NULL,
                  revision INTEGER NOT NULL,
                  model_id TEXT NOT NULL,
                  version INTEGER NOT NULL,
                  evaluation_id TEXT NOT NULL,
                  approved_by TEXT NOT NULL,
                  approval_reason TEXT NOT NULL,
                  action TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(name, revision)
                );
                CREATE TABLE IF NOT EXISTS tabular_model_idempotency (
                  scope TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  request_digest TEXT NOT NULL,
                  response_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(scope, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_tabular_versions_created
                  ON tabular_model_versions(model_id, version DESC);
                CREATE INDEX IF NOT EXISTS idx_tabular_evaluations_model
                  ON tabular_model_evaluations(model_id, version, created_at DESC);
                """
            )

    async def train(self, request: TrainTabularModelRequest) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._train_sync, request)

    def _train_sync(self, request: TrainTabularModelRequest) -> dict[str, Any]:
        payload = request.model_dump(mode="json")
        with self._connect() as conn:
            replay = self._idempotent_replay(
                conn, "train", request.idempotency_key, _sha256(payload)
            )
            if replay is not None:
                return {**replay, "replayed": True}
            feature_names = self._validate_rows(request.features, request.rows)
            means, scales = self._normalization(feature_names, request.rows)
            weights = [0.0] * len(feature_names)
            intercept = 0.0
            weights, intercept = self._fit(
                feature_names,
                request.rows,
                means,
                scales,
                weights,
                intercept,
                request.learning_rate,
                request.epochs,
            )
            training_predictions = self._predict_rows(
                feature_names, request.rows, means, scales, weights, intercept
            )
            training_metrics = self._metrics(
                [row.label for row in request.rows],
                training_predictions,
                request.threshold,
            )
            model_id = str(uuid4())
            created_at = _utc_now()
            spec = self._version_spec(
                model_id=model_id,
                version=1,
                model_name=request.model_name,
                route="train_new",
                features=request.features,
                weights=dict(zip(feature_names, weights, strict=True)),
                intercept=intercept,
                means=means,
                scales=scales,
                drift_baseline=self._drift_baseline(feature_names, request.rows),
                threshold=request.threshold,
                dataset_digest=_sha256([row.model_dump(mode="json") for row in request.rows]),
                source=request.source,
                lineage={"base_model": None},
                training_metrics=training_metrics,
            )
            conn.execute(
                "INSERT INTO tabular_models VALUES(?,?,?,?,?)",
                (
                    model_id,
                    request.model_name,
                    "binary_classification",
                    created_at,
                    created_at,
                ),
            )
            conn.execute(
                "INSERT INTO tabular_model_versions VALUES(?,?,?,?,?)",
                (model_id, 1, "train_new", _canonical_json(spec), created_at),
            )
            response = self._public_version(spec)
            self._save_idempotency(
                conn, "train", request.idempotency_key, _sha256(payload), response
            )
            return {**response, "replayed": False}

    async def import_model(self, request: ImportTabularModelRequest) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._import_sync, request)

    def _import_sync(self, request: ImportTabularModelRequest) -> dict[str, Any]:
        payload = request.model_dump(mode="json")
        with self._connect() as conn:
            replay = self._idempotent_replay(
                conn, "import", request.idempotency_key, _sha256(payload)
            )
            if replay is not None:
                return {**replay, "replayed": True}
            names = [feature.name for feature in request.features]
            if set(request.weights) != set(names):
                raise ValueError("imported weights must exactly match feature names")
            if request.means and set(request.means) != set(names):
                raise ValueError("imported means must exactly match feature names")
            if request.scales and set(request.scales) != set(names):
                raise ValueError("imported scales must exactly match feature names")
            means = request.means or {name: 0.0 for name in names}
            scales = request.scales or {name: 1.0 for name in names}
            if any(value <= 0 for value in scales.values()):
                raise ValueError("imported feature scales must be positive")
            baseline = request.drift_baseline or {
                name: {"mean": means[name], "scale": scales[name]} for name in names
            }
            model_id = str(uuid4())
            created_at = _utc_now()
            spec = self._version_spec(
                model_id=model_id,
                version=1,
                model_name=request.model_name,
                route="import",
                features=request.features,
                weights=request.weights,
                intercept=request.intercept,
                means=means,
                scales=scales,
                drift_baseline=baseline,
                threshold=request.threshold,
                dataset_digest=str(request.source.get("dataset_digest", "")),
                source=request.source,
                lineage={"base_model": request.source.get("base_model")},
                training_metrics=None,
            )
            conn.execute(
                "INSERT INTO tabular_models VALUES(?,?,?,?,?)",
                (
                    model_id,
                    request.model_name,
                    "binary_classification",
                    created_at,
                    created_at,
                ),
            )
            conn.execute(
                "INSERT INTO tabular_model_versions VALUES(?,?,?,?,?)",
                (model_id, 1, "import", _canonical_json(spec), created_at),
            )
            response = self._public_version(spec)
            self._save_idempotency(
                conn, "import", request.idempotency_key, _sha256(payload), response
            )
            return {**response, "replayed": False}

    async def fine_tune(
        self,
        model_id: str,
        version: int,
        request: FineTuneTabularModelRequest,
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._fine_tune_sync, model_id, version, request)

    def _fine_tune_sync(
        self,
        model_id: str,
        version: int,
        request: FineTuneTabularModelRequest,
    ) -> dict[str, Any]:
        payload = request.model_dump(mode="json")
        scope = f"fine_tune:{model_id}:{version}"
        with self._connect() as conn:
            replay = self._idempotent_replay(conn, scope, request.idempotency_key, _sha256(payload))
            if replay is not None:
                return {**replay, "replayed": True}
            base = self._load_version(conn, model_id, version)
            contracts = [FeatureContract.model_validate(item) for item in base["features"]]
            names = self._validate_rows(contracts, request.rows)
            means = {name: float(base["normalization"]["means"][name]) for name in names}
            scales = {name: float(base["normalization"]["scales"][name]) for name in names}
            weights = [float(base["parameters"]["weights"][name]) for name in names]
            weights, intercept = self._fit(
                names,
                request.rows,
                means,
                scales,
                weights,
                float(base["parameters"]["intercept"]),
                request.learning_rate,
                request.epochs,
            )
            predictions = self._predict_rows(names, request.rows, means, scales, weights, intercept)
            training_metrics = self._metrics(
                [row.label for row in request.rows],
                predictions,
                float(base["threshold"]),
            )
            row = conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 AS next_version "
                "FROM tabular_model_versions WHERE model_id=?",
                (model_id,),
            ).fetchone()
            next_version = int(row["next_version"])
            created_at = _utc_now()
            spec = self._version_spec(
                model_id=model_id,
                version=next_version,
                model_name=str(base["model_name"]),
                route="fine_tune",
                features=contracts,
                weights=dict(zip(names, weights, strict=True)),
                intercept=intercept,
                means=means,
                scales=scales,
                drift_baseline=self._drift_baseline(names, request.rows),
                threshold=float(base["threshold"]),
                dataset_digest=_sha256([row.model_dump(mode="json") for row in request.rows]),
                source=request.source,
                lineage={
                    "base_model": {
                        "model_id": model_id,
                        "version": version,
                        "model_digest": base["model_digest"],
                    }
                },
                training_metrics=training_metrics,
            )
            conn.execute(
                "INSERT INTO tabular_model_versions VALUES(?,?,?,?,?)",
                (
                    model_id,
                    next_version,
                    "fine_tune",
                    _canonical_json(spec),
                    created_at,
                ),
            )
            conn.execute(
                "UPDATE tabular_models SET updated_at=? WHERE model_id=?",
                (created_at, model_id),
            )
            response = self._public_version(spec)
            self._save_idempotency(conn, scope, request.idempotency_key, _sha256(payload), response)
            return {**response, "replayed": False}

    async def evaluate(
        self,
        model_id: str,
        version: int,
        request: EvaluateTabularModelRequest,
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._evaluate_sync, model_id, version, request)

    def _evaluate_sync(
        self,
        model_id: str,
        version: int,
        request: EvaluateTabularModelRequest,
    ) -> dict[str, Any]:
        payload = request.model_dump(mode="json")
        scope = f"evaluate:{model_id}:{version}"
        with self._connect() as conn:
            replay = self._idempotent_replay(conn, scope, request.idempotency_key, _sha256(payload))
            if replay is not None:
                return {**replay, "replayed": True}
            spec = self._load_version(conn, model_id, version)
            contracts = [FeatureContract.model_validate(item) for item in spec["features"]]
            names = self._validate_rows(contracts, request.rows)
            predictions = self._predict_rows(
                names,
                request.rows,
                spec["normalization"]["means"],
                spec["normalization"]["scales"],
                [spec["parameters"]["weights"][name] for name in names],
                spec["parameters"]["intercept"],
            )
            metrics = self._metrics(
                [row.label for row in request.rows],
                predictions,
                float(spec["threshold"]),
            )
            evaluation_id = str(uuid4())
            dataset_digest = _sha256([row.model_dump(mode="json") for row in request.rows])
            created_at = _utc_now()
            response = {
                "evaluation_id": evaluation_id,
                "model_id": model_id,
                "version": version,
                "model_digest": spec["model_digest"],
                "dataset_digest": dataset_digest,
                "metrics": metrics,
                "created_at": created_at,
            }
            conn.execute(
                "INSERT INTO tabular_model_evaluations VALUES(?,?,?,?,?,?)",
                (
                    evaluation_id,
                    model_id,
                    version,
                    dataset_digest,
                    _canonical_json(metrics),
                    created_at,
                ),
            )
            self._save_idempotency(conn, scope, request.idempotency_key, _sha256(payload), response)
            return {**response, "replayed": False}

    async def promote(
        self, deployment_name: str, request: PromoteTabularModelRequest
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._promote_sync, deployment_name, request)

    def _promote_sync(
        self, deployment_name: str, request: PromoteTabularModelRequest
    ) -> dict[str, Any]:
        payload = request.model_dump(mode="json")
        scope = f"promote:{deployment_name}"
        with self._connect() as conn:
            replay = self._idempotent_replay(conn, scope, request.idempotency_key, _sha256(payload))
            if replay is not None:
                return {**replay, "replayed": True}
            spec = self._load_version(conn, request.model_id, request.version)
            evaluation = conn.execute(
                "SELECT * FROM tabular_model_evaluations WHERE evaluation_id=?",
                (request.evaluation_id,),
            ).fetchone()
            if evaluation is None:
                raise KeyError(f"evaluation not found: {request.evaluation_id}")
            if (
                evaluation["model_id"] != request.model_id
                or int(evaluation["version"]) != request.version
            ):
                raise ValueError("evaluation does not belong to the promoted model version")
            metrics = json.loads(str(evaluation["metrics_json"]))
            if float(metrics["recall"]) < request.minimum_recall:
                raise ValueError(
                    f"evaluation recall {metrics['recall']} is below "
                    f"required {request.minimum_recall}"
                )
            current = conn.execute(
                "SELECT revision FROM tabular_model_deployments WHERE name=?",
                (deployment_name,),
            ).fetchone()
            current_revision = int(current["revision"]) if current else 0
            if current_revision != request.expected_revision:
                raise TabularModelConflict(
                    f"deployment revision conflict: expected {request.expected_revision}, "
                    f"current {current_revision}"
                )
            revision = current_revision + 1
            now = _utc_now()
            record = {
                "name": deployment_name,
                "revision": revision,
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
            self._save_idempotency(conn, scope, request.idempotency_key, _sha256(payload), record)
            return {**record, "replayed": False}

    async def rollback(
        self, deployment_name: str, request: RollbackTabularDeploymentRequest
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._rollback_sync, deployment_name, request)

    def _rollback_sync(
        self, deployment_name: str, request: RollbackTabularDeploymentRequest
    ) -> dict[str, Any]:
        payload = request.model_dump(mode="json")
        scope = f"rollback:{deployment_name}"
        with self._connect() as conn:
            replay = self._idempotent_replay(conn, scope, request.idempotency_key, _sha256(payload))
            if replay is not None:
                return {**replay, "replayed": True}
            current = conn.execute(
                "SELECT * FROM tabular_model_deployments WHERE name=?",
                (deployment_name,),
            ).fetchone()
            if current is None:
                raise KeyError(f"deployment not found: {deployment_name}")
            if int(current["revision"]) != request.expected_revision:
                raise TabularModelConflict(
                    f"deployment revision conflict: expected {request.expected_revision}, "
                    f"current {current['revision']}"
                )
            target_revision = request.target_revision or request.expected_revision - 1
            if target_revision < 1 or target_revision >= request.expected_revision:
                raise ValueError("rollback target must be an earlier deployment revision")
            target = conn.execute(
                "SELECT * FROM tabular_model_deployment_history WHERE name=? AND revision=?",
                (deployment_name, target_revision),
            ).fetchone()
            if target is None:
                raise KeyError(f"deployment history not found: {deployment_name}@{target_revision}")
            spec = self._load_version(conn, str(target["model_id"]), int(target["version"]))
            evaluation = conn.execute(
                "SELECT metrics_json FROM tabular_model_evaluations WHERE evaluation_id=?",
                (target["evaluation_id"],),
            ).fetchone()
            now = _utc_now()
            record = {
                "name": deployment_name,
                "revision": request.expected_revision + 1,
                "model_id": target["model_id"],
                "version": int(target["version"]),
                "model_digest": spec["model_digest"],
                "evaluation_id": target["evaluation_id"],
                "metrics": (json.loads(str(evaluation["metrics_json"])) if evaluation else {}),
                "approved_by": request.approved_by,
                "approval_reason": request.approval_reason,
                "action": "rollback",
                "rollback_from_revision": request.expected_revision,
                "rollback_target_revision": target_revision,
                "created_at": now,
                "updated_at": now,
            }
            self._save_deployment(conn, record)
            self._save_idempotency(conn, scope, request.idempotency_key, _sha256(payload), record)
            return {**record, "replayed": False}

    async def predict(
        self, deployment_name: str, request: TabularInferenceRequest
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._predict_sync, deployment_name, request)

    def _predict_sync(
        self, deployment_name: str, request: TabularInferenceRequest
    ) -> dict[str, Any]:
        with self._connect() as conn:
            deployment = self._load_deployment(conn, deployment_name)
            spec = self._load_version(conn, deployment["model_id"], int(deployment["version"]))
            evaluation = conn.execute(
                "SELECT metrics_json,dataset_digest FROM tabular_model_evaluations "
                "WHERE evaluation_id=?",
                (deployment["evaluation_id"],),
            ).fetchone()
        values = self._validate_observation(spec["features"], request)
        names = [feature["name"] for feature in spec["features"]]
        score = float(spec["parameters"]["intercept"])
        for name in names:
            normalized = (values[name] - float(spec["normalization"]["means"][name])) / float(
                spec["normalization"]["scales"][name]
            )
            score += normalized * float(spec["parameters"]["weights"][name])
        probability = self._sigmoid(score)
        threshold = float(spec["threshold"])
        return {
            "deployment_name": deployment_name,
            "deployment_revision": int(deployment["revision"]),
            "model_id": spec["model_id"],
            "version": int(spec["version"]),
            "model_digest": spec["model_digest"],
            "evaluation_id": deployment["evaluation_id"],
            "probability": probability,
            "predicted_label": int(probability >= threshold),
            "confidence": abs(probability - 0.5) * 2,
            "threshold": threshold,
            "features": values,
            "units": request.units,
            "lineage": spec["lineage"],
            "model_card": {
                "model_name": spec["model_name"],
                "task_type": spec["task_type"],
                "route": spec["route"],
                "features": spec["features"],
                "training_dataset_digest": spec["dataset_digest"],
                "source": spec["source"],
                "lineage": spec["lineage"],
                "training_metrics": spec["training_metrics"],
            },
            "evaluation_metrics": (
                json.loads(str(evaluation["metrics_json"])) if evaluation else {}
            ),
            "evaluation_dataset_digest": (
                str(evaluation["dataset_digest"]) if evaluation else ""
            ),
        }

    async def drift(self, deployment_name: str, request: TabularDriftRequest) -> dict[str, Any]:
        return await asyncio.to_thread(self._drift_sync, deployment_name, request)

    def _drift_sync(self, deployment_name: str, request: TabularDriftRequest) -> dict[str, Any]:
        with self._connect() as conn:
            deployment = self._load_deployment(conn, deployment_name)
            spec = self._load_version(conn, deployment["model_id"], int(deployment["version"]))
        validated = [
            self._validate_observation(spec["features"], observation)
            for observation in request.observations
        ]
        feature_results: dict[str, dict[str, Any]] = {}
        overall_score = 0.0
        for feature in spec["features"]:
            name = feature["name"]
            current_mean = sum(row[name] for row in validated) / len(validated)
            baseline = spec["drift_baseline"][name]
            scale = max(float(baseline["scale"]), 1e-12)
            score = abs(current_mean - float(baseline["mean"])) / scale
            if score >= request.critical_threshold:
                status = "critical"
            elif score >= request.warning_threshold:
                status = "warning"
            else:
                status = "stable"
            feature_results[name] = {
                "baseline_mean": float(baseline["mean"]),
                "current_mean": current_mean,
                "mean_shift_z": score,
                "status": status,
            }
            overall_score = max(overall_score, score)
        if overall_score >= request.critical_threshold:
            status = "critical"
        elif overall_score >= request.warning_threshold:
            status = "warning"
        else:
            status = "stable"
        return {
            "deployment_name": deployment_name,
            "deployment_revision": int(deployment["revision"]),
            "model_id": spec["model_id"],
            "version": int(spec["version"]),
            "model_digest": spec["model_digest"],
            "observation_count": len(validated),
            "status": status,
            "score": overall_score,
            "features": feature_results,
            "automatic_training_triggered": False,
        }

    async def list_models(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_models_sync)

    def _list_models_sync(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT m.*, COUNT(v.version) AS version_count, "
                "MAX(v.version) AS latest_version "
                "FROM tabular_models m LEFT JOIN tabular_model_versions v "
                "ON v.model_id=m.model_id GROUP BY m.model_id ORDER BY m.updated_at DESC"
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
            deployment = self._load_deployment(conn, name)
            spec = self._load_version(conn, deployment["model_id"], int(deployment["version"]))
            evaluation = conn.execute(
                "SELECT metrics_json FROM tabular_model_evaluations WHERE evaluation_id=?",
                (deployment["evaluation_id"],),
            ).fetchone()
            return {
                **deployment,
                "model_digest": spec["model_digest"],
                "metrics": (json.loads(str(evaluation["metrics_json"])) if evaluation else {}),
            }

    @staticmethod
    def _validate_rows(
        contracts: list[FeatureContract], rows: list[LabeledObservation]
    ) -> list[str]:
        names = [contract.name for contract in contracts]
        for index, row in enumerate(rows):
            if set(row.features) != set(names):
                raise ValueError(f"row {index} features must exactly match the feature contract")
            if set(row.units) != set(names):
                raise ValueError(f"row {index} units must exactly match the feature contract")
            for contract in contracts:
                value = float(row.features[contract.name])
                if not math.isfinite(value):
                    raise ValueError(f"row {index} feature {contract.name} is not finite")
                if row.units[contract.name] != contract.unit:
                    raise ValueError(
                        f"row {index} feature {contract.name} expected unit "
                        f"{contract.unit}, got {row.units[contract.name]}"
                    )
                if contract.minimum is not None and value < contract.minimum:
                    raise ValueError(f"row {index} feature {contract.name} is below minimum")
                if contract.maximum is not None and value > contract.maximum:
                    raise ValueError(f"row {index} feature {contract.name} is above maximum")
        if len({row.label for row in rows}) < 2:
            raise ValueError("training or evaluation data must contain both labels")
        return names

    @staticmethod
    def _validate_observation(
        raw_contracts: list[dict[str, Any]],
        observation: ModelObservation | TabularInferenceRequest,
    ) -> dict[str, float]:
        names = [str(contract["name"]) for contract in raw_contracts]
        if set(observation.features) != set(names):
            raise ValueError("inference features must exactly match the deployed contract")
        if set(observation.units) != set(names):
            raise ValueError("inference units must exactly match the deployed contract")
        values: dict[str, float] = {}
        for contract in raw_contracts:
            name = str(contract["name"])
            value = float(observation.features[name])
            if not math.isfinite(value):
                raise ValueError(f"feature {name} is not finite")
            if observation.units[name] != contract["unit"]:
                raise ValueError(
                    f"feature {name} expected unit {contract['unit']}, "
                    f"got {observation.units[name]}"
                )
            if contract.get("minimum") is not None and value < float(contract["minimum"]):
                raise ValueError(f"feature {name} is below minimum")
            if contract.get("maximum") is not None and value > float(contract["maximum"]):
                raise ValueError(f"feature {name} is above maximum")
            values[name] = value
        return values

    @staticmethod
    def _normalization(
        names: list[str], rows: list[LabeledObservation]
    ) -> tuple[dict[str, float], dict[str, float]]:
        means = {name: sum(float(row.features[name]) for row in rows) / len(rows) for name in names}
        scales: dict[str, float] = {}
        for name in names:
            variance = sum((float(row.features[name]) - means[name]) ** 2 for row in rows) / len(
                rows
            )
            scales[name] = max(math.sqrt(variance), 1e-12)
        return means, scales

    @staticmethod
    def _drift_baseline(
        names: list[str], rows: list[LabeledObservation]
    ) -> dict[str, dict[str, float]]:
        means, scales = TabularModelService._normalization(names, rows)
        return {name: {"mean": means[name], "scale": scales[name]} for name in names}

    @staticmethod
    def _fit(
        names: list[str],
        rows: list[LabeledObservation],
        means: dict[str, float],
        scales: dict[str, float],
        initial_weights: list[float],
        initial_intercept: float,
        learning_rate: float,
        epochs: int,
    ) -> tuple[list[float], float]:
        weights = list(initial_weights)
        intercept = initial_intercept
        row_count = len(rows)
        for _ in range(epochs):
            gradient = [0.0] * len(names)
            intercept_gradient = 0.0
            for row in rows:
                values = [
                    (float(row.features[name]) - means[name]) / scales[name] for name in names
                ]
                probability = TabularModelService._sigmoid(
                    intercept + sum(w * x for w, x in zip(weights, values, strict=True))
                )
                error = probability - row.label
                intercept_gradient += error
                for index, value in enumerate(values):
                    gradient[index] += error * value
            intercept -= learning_rate * intercept_gradient / row_count
            for index in range(len(weights)):
                weights[index] -= learning_rate * gradient[index] / row_count
        return weights, intercept

    @staticmethod
    def _predict_rows(
        names: list[str],
        rows: list[LabeledObservation],
        means: dict[str, float],
        scales: dict[str, float],
        weights: list[float],
        intercept: float,
    ) -> list[float]:
        probabilities = []
        for row in rows:
            score = intercept
            for index, name in enumerate(names):
                score += (
                    (float(row.features[name]) - float(means[name]))
                    / float(scales[name])
                    * float(weights[index])
                )
            probabilities.append(TabularModelService._sigmoid(score))
        return probabilities

    @staticmethod
    def _metrics(
        labels: list[int], probabilities: list[float], threshold: float
    ) -> dict[str, float | int]:
        predicted = [int(value >= threshold) for value in probabilities]
        true_positive = sum(
            int(actual == 1 and guess == 1) for actual, guess in zip(labels, predicted, strict=True)
        )
        false_positive = sum(
            int(actual == 0 and guess == 1) for actual, guess in zip(labels, predicted, strict=True)
        )
        true_negative = sum(
            int(actual == 0 and guess == 0) for actual, guess in zip(labels, predicted, strict=True)
        )
        false_negative = sum(
            int(actual == 1 and guess == 0) for actual, guess in zip(labels, predicted, strict=True)
        )
        precision = (
            true_positive / (true_positive + false_positive)
            if true_positive + false_positive
            else 0.0
        )
        recall = (
            true_positive / (true_positive + false_negative)
            if true_positive + false_negative
            else 0.0
        )
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        positive_count = sum(labels)
        sorted_pairs = sorted(
            zip(probabilities, labels, strict=True), key=lambda item: item[0], reverse=True
        )
        seen_positive = 0
        precision_sum = 0.0
        for rank, (_, label) in enumerate(sorted_pairs, start=1):
            if label:
                seen_positive += 1
                precision_sum += seen_positive / rank
        auprc = precision_sum / positive_count if positive_count else 0.0
        brier = sum(
            (probability - label) ** 2
            for label, probability in zip(labels, probabilities, strict=True)
        ) / len(labels)
        return {
            "count": len(labels),
            "true_positive": true_positive,
            "false_positive": false_positive,
            "true_negative": true_negative,
            "false_negative": false_negative,
            "accuracy": (true_positive + true_negative) / len(labels),
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "auprc": auprc,
            "brier": brier,
            "threshold": threshold,
        }

    @staticmethod
    def _sigmoid(value: float) -> float:
        if value >= 0:
            inverse = math.exp(-min(value, 700))
            return 1 / (1 + inverse)
        exponential = math.exp(max(value, -700))
        return exponential / (1 + exponential)

    @staticmethod
    def _version_spec(
        *,
        model_id: str,
        version: int,
        model_name: str,
        route: str,
        features: list[FeatureContract],
        weights: dict[str, float],
        intercept: float,
        means: dict[str, float],
        scales: dict[str, float],
        drift_baseline: dict[str, dict[str, float]],
        threshold: float,
        dataset_digest: str,
        source: dict[str, Any],
        lineage: dict[str, Any],
        training_metrics: dict[str, Any] | None,
    ) -> dict[str, Any]:
        created_at = _utc_now()
        spec: dict[str, Any] = {
            "model_id": model_id,
            "version": version,
            "model_name": model_name,
            "task_type": "binary_classification",
            "route": route,
            "features": [item.model_dump(mode="json") for item in features],
            "parameters": {"weights": weights, "intercept": intercept},
            "normalization": {"means": means, "scales": scales},
            "drift_baseline": drift_baseline,
            "threshold": threshold,
            "dataset_digest": dataset_digest,
            "source": source,
            "lineage": lineage,
            "training_metrics": training_metrics,
            "created_at": created_at,
        }
        digest_payload = {key: value for key, value in spec.items() if key != "created_at"}
        spec["model_digest"] = _sha256(digest_payload)
        return spec

    @staticmethod
    def _public_version(spec: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in spec.items() if key not in {"parameters"}}

    @staticmethod
    def _load_version(conn: sqlite3.Connection, model_id: str, version: int) -> dict[str, Any]:
        row = conn.execute(
            "SELECT spec_json FROM tabular_model_versions WHERE model_id=? AND version=?",
            (model_id, version),
        ).fetchone()
        if row is None:
            raise KeyError(f"model version not found: {model_id}@{version}")
        return json.loads(str(row["spec_json"]))

    @staticmethod
    def _load_deployment(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM tabular_model_deployments WHERE name=?", (name,)
        ).fetchone()
        if row is None:
            raise KeyError(f"deployment not found: {name}")
        return dict(row)

    @staticmethod
    def _save_deployment(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
        values = (
            record["name"],
            record["revision"],
            record["model_id"],
            record["version"],
            record["evaluation_id"],
            record["approved_by"],
            record["approval_reason"],
            record["action"],
            record["created_at"],
        )
        conn.execute(
            "INSERT INTO tabular_model_deployment_history VALUES(?,?,?,?,?,?,?,?,?)",
            values,
        )
        conn.execute(
            """
            INSERT INTO tabular_model_deployments
              (name,revision,model_id,version,evaluation_id,approved_by,
               approval_reason,action,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(name) DO UPDATE SET
              revision=excluded.revision,
              model_id=excluded.model_id,
              version=excluded.version,
              evaluation_id=excluded.evaluation_id,
              approved_by=excluded.approved_by,
              approval_reason=excluded.approval_reason,
              action=excluded.action,
              updated_at=excluded.updated_at
            """,
            (*values, record["updated_at"]),
        )

    @staticmethod
    def _idempotent_replay(
        conn: sqlite3.Connection,
        scope: str,
        key: str,
        request_digest: str,
    ) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT request_digest,response_json FROM tabular_model_idempotency "
            "WHERE scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_digest"] != request_digest:
            raise TabularModelConflict("idempotency key was already used with a different request")
        return json.loads(str(row["response_json"]))

    @staticmethod
    def _save_idempotency(
        conn: sqlite3.Connection,
        scope: str,
        key: str,
        request_digest: str,
        response: dict[str, Any],
    ) -> None:
        conn.execute(
            "INSERT INTO tabular_model_idempotency VALUES(?,?,?,?,?)",
            (scope, key, request_digest, _canonical_json(response), _utc_now()),
        )
