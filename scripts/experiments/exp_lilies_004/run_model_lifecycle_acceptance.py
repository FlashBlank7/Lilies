#!/usr/bin/env python3
"""Exercise fine-tune, evaluation, promotion, workflow use, and rollback."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import uuid4

from run_sealed_seed import (
    UNITS,
    alarm_count,
    configure_fault,
    create_device,
    ensure_fault_proxy,
    login,
    platform_json,
    publish_event,
    run_workflow,
)


def labeled_rows(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [
        {
            "features": item["features"],
            "units": item["units"],
            "label": item["label"],
        }
        for item in payload["rows"]
    ]


def run_high_event(
    *,
    tag: str,
    expected_version: int,
    platform_base: str,
    platform_token: str,
    application_id: str,
    thingsboard_base: str,
    thingsboard_token: str,
    workspace_root: Path,
    mqtt_host: str,
    mqtt_port: int,
) -> dict:
    device_id, access_token = create_device(
        thingsboard_base,
        thingsboard_token,
        f"EXP004-model-lifecycle-{tag}",
    )
    event_id = f"exp004-model-lifecycle-{tag}-{uuid4().hex[:12]}"
    timestamp_ms = 1_785_333_000_000 + expected_version * 1000
    event = {
        "event_id": event_id,
        "timestamp_ms": timestamp_ms,
        "features": {
            "temperature_c": 101.0,
            "vibration_rms_mm_s": 9.8,
            "current_a": 31.0,
            "pressure_bar": 2.6,
            "rpm": 1325.0,
        },
        "units": dict(UNITS),
    }
    publish_event(
        event,
        access_token,
        mqtt_host=mqtt_host,
        mqtt_port=mqtt_port,
        client_id=f"exp004-model-{uuid4().hex[:10]}",
    )
    relative_workspace = f"exp004-model-lifecycle/{tag}"
    (workspace_root / relative_workspace).mkdir(
        parents=True,
        exist_ok=True,
    )
    run, resumes = run_workflow(
        platform_base,
        platform_token,
        application_id,
        {
            "device_id": device_id,
            "event_id": event_id,
            "units": event["units"],
            "minimum_event_ts": timestamp_ms - 86_400_000,
            "last_accepted_ts": 0,
            "high_risk_threshold": 0.8,
            "review_low_threshold": 0.35,
            "auto_confidence_threshold": 0.8,
            "drift_observations": [
                {
                    "features": event["features"],
                    "units": event["units"],
                }
                for _ in range(12)
            ],
        },
        relative_workspace,
        None,
    )
    prediction = (
        run["state"]["outputs"].get("risk_inference", {}).get("output")
    )
    artifacts = run.get("outputs") or {}
    artifact_count = sum(
        isinstance(artifacts.get(name), dict)
        for name in (
            "decision_artifact",
            "model_evidence_artifact",
            "drift_artifact",
        )
    )
    host_alarm_count = alarm_count(
        thingsboard_base,
        thingsboard_token,
        device_id,
        event_id,
    )
    passed = (
        run["status"] == "succeeded"
        and resumes == 0
        and isinstance(prediction, dict)
        and prediction.get("version") == expected_version
        and host_alarm_count == 1
        and artifact_count == 3
    )
    return {
        "passed": passed,
        "workflow_status": run["status"],
        "model_version": (
            prediction.get("version")
            if isinstance(prediction, dict)
            else None
        ),
        "alarm_count": host_alarm_count,
        "artifact_count": artifact_count,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform-base", default="http://127.0.0.1:8014")
    parser.add_argument("--platform-token", required=True)
    parser.add_argument("--application-id", required=True)
    parser.add_argument(
        "--thingsboard-base",
        default="http://127.0.0.1:19090",
    )
    parser.add_argument("--thingsboard-username", default="tenant@thingsboard.org")
    parser.add_argument("--thingsboard-password", default="tenant")
    parser.add_argument("--mqtt-host", default="127.0.0.1")
    parser.add_argument("--mqtt-port", type=int, default=18884)
    parser.add_argument("--fault-port", type=int, default=19091)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--training-file", type=Path, required=True)
    parser.add_argument("--validation-file", type=Path, required=True)
    parser.add_argument("--summary-file", type=Path, required=True)
    args = parser.parse_args()

    proxy = ensure_fault_proxy(
        Path(__file__).with_name("fault_proxy.py"),
        args.fault_port,
    )
    configure_fault(args.fault_port, "pass")
    tb_token = login(
        args.thingsboard_base,
        args.thingsboard_username,
        args.thingsboard_password,
    )
    platform_json(
        "POST",
        args.platform_base,
        args.platform_token,
        "/api/v1/platform/secrets",
        {
            "owner_id": "exp004-tenant",
            "name": "thingsboard-jwt",
            "value": tb_token,
            "description": "Rotated for model lifecycle acceptance.",
        },
    )

    initial = platform_json(
        "GET",
        args.platform_base,
        args.platform_token,
        "/api/v1/model-deployments/exp004-predictive-risk-debug",
    )
    if initial["revision"] != 1 or initial["version"] != 1:
        raise RuntimeError("model lifecycle acceptance requires deployment revision 1")
    model_id = str(initial["model_id"])
    fine_tuned = platform_json(
        "POST",
        args.platform_base,
        args.platform_token,
        f"/api/v1/tabular-models/{model_id}/versions/1/fine-tune",
        {
            "rows": labeled_rows(args.training_file),
            "learning_rate": 0.01,
            "epochs": 40,
            "source": {
                "kind": "customer_authorized_public_fine_tune",
                "dataset_name": "EXP-LILIES-004 public training",
            },
            "idempotency_key": "exp004-public-fine-tune-version-2",
        },
    )
    evaluated = platform_json(
        "POST",
        args.platform_base,
        args.platform_token,
        f"/api/v1/tabular-models/{model_id}/versions/2/evaluate",
        {
            "rows": labeled_rows(args.validation_file),
            "idempotency_key": "exp004-public-evaluate-version-2",
        },
    )
    promoted = platform_json(
        "POST",
        args.platform_base,
        args.platform_token,
        "/api/v1/model-deployments/exp004-predictive-risk-debug/promote",
        {
            "model_id": model_id,
            "version": 2,
            "evaluation_id": evaluated["evaluation_id"],
            "approved_by": "delegated-experiment-operator",
            "approval_reason": (
                "Public fine-tuned candidate passed the held-out gate for "
                "promotion and rollback acceptance."
            ),
            "expected_revision": 1,
            "minimum_recall": 0.9,
            "idempotency_key": "exp004-public-promote-version-2",
        },
    )
    version_two_run = run_high_event(
        tag="promoted-v2",
        expected_version=2,
        platform_base=args.platform_base,
        platform_token=args.platform_token,
        application_id=args.application_id,
        thingsboard_base=args.thingsboard_base,
        thingsboard_token=tb_token,
        workspace_root=args.workspace_root,
        mqtt_host=args.mqtt_host,
        mqtt_port=args.mqtt_port,
    )
    rolled_back = platform_json(
        "POST",
        args.platform_base,
        args.platform_token,
        "/api/v1/model-deployments/exp004-predictive-risk-debug/rollback",
        {
            "expected_revision": 2,
            "target_revision": 1,
            "approved_by": "delegated-experiment-operator",
            "approval_reason": (
                "Acceptance rollback confirms production inference follows "
                "the restored approved version."
            ),
            "idempotency_key": "exp004-public-rollback-to-revision-1",
        },
    )
    version_one_run = run_high_event(
        tag="rolled-back-v1",
        expected_version=1,
        platform_base=args.platform_base,
        platform_token=args.platform_token,
        application_id=args.application_id,
        thingsboard_base=args.thingsboard_base,
        thingsboard_token=tb_token,
        workspace_root=args.workspace_root,
        mqtt_host=args.mqtt_host,
        mqtt_port=args.mqtt_port,
    )
    if proxy is not None:
        proxy.terminate()
        proxy.wait(timeout=5)

    summary = {
        "schema_version": "1.0",
        "task_id": "EXP-LILIES-004",
        "base_version": 1,
        "fine_tuned_version": fine_tuned["version"],
        "fine_tuned_base_version": fine_tuned["lineage"]["base_model"][
            "version"
        ],
        "evaluation_id": evaluated["evaluation_id"],
        "evaluation_metrics": evaluated["metrics"],
        "promotion_revision": promoted["revision"],
        "promoted_workflow_run": version_two_run,
        "rollback_revision": rolled_back["revision"],
        "rollback_action": rolled_back["action"],
        "restored_version": rolled_back["version"],
        "rolled_back_workflow_run": version_one_run,
        "passed": (
            fine_tuned["version"] == 2
            and fine_tuned["lineage"]["base_model"]["version"] == 1
            and evaluated["metrics"]["recall"] >= 0.9
            and promoted["revision"] == 2
            and version_two_run["passed"]
            and rolled_back["revision"] == 3
            and rolled_back["action"] == "rollback"
            and rolled_back["version"] == 1
            and version_one_run["passed"]
        ),
    }
    args.summary_file.parent.mkdir(parents=True, exist_ok=True)
    args.summary_file.write_text(
        json.dumps(
            summary,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            summary,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
