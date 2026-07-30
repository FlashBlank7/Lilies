#!/usr/bin/env python3
"""Run one Builder-hidden EXP-LILIES-004 Seed and emit aggregates only."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from uuid import uuid4

from mqtt_publish import publish


UNITS = {
    "temperature_c": "Cel",
    "vibration_rms_mm_s": "mm/s",
    "current_a": "A",
    "pressure_bar": "bar",
    "rpm": "rpm",
}
ALARM_CASES = {
    "high_risk",
    "uncertain_approve",
    "duplicate_high",
    "transient_then_alarm",
}
NO_ALARM_CASES = {"low_risk", "uncertain_reject"}
SAFE_STOP_CASES = {
    "invalid_unit",
    "missing_feature",
    "out_of_range",
    "stale",
    "out_of_order",
}


def canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: Any | None = None,
    query: dict[str, Any] | None = None,
) -> Any:
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    request_headers = {"Accept": "application/json", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=data,
        headers=request_headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"{method} {url} returned HTTP {error.code}: {detail}"
        ) from error
    return json.loads(payload) if payload else None


def platform_json(
    method: str,
    base_url: str,
    token: str,
    path: str,
    body: Any | None = None,
) -> Any:
    return http_json(
        method,
        f"{base_url.rstrip('/')}{path}",
        headers={"Authorization": f"Bearer {token}"},
        body=body,
    )


def thingsboard_json(
    method: str,
    base_url: str,
    token: str,
    path: str,
    *,
    body: Any | None = None,
    query: dict[str, Any] | None = None,
) -> Any:
    return http_json(
        method,
        f"{base_url.rstrip('/')}{path}",
        headers={"X-Authorization": f"Bearer {token}"},
        body=body,
        query=query,
    )


def login(base_url: str, username: str, password: str) -> str:
    result = http_json(
        "POST",
        f"{base_url.rstrip('/')}/api/auth/login",
        body={"username": username, "password": password},
    )
    return str(result["token"])


def create_device(
    base_url: str,
    token: str,
    name: str,
) -> tuple[str, str]:
    device = thingsboard_json(
        "POST",
        base_url,
        token,
        "/api/device",
        body={
            "name": name,
            "type": "centrifugal-pump",
            "label": "EXP-LILIES-004 sealed acceptance device",
        },
    )
    device_id = str(device["id"]["id"])
    credentials = thingsboard_json(
        "GET",
        base_url,
        token,
        f"/api/device/{device_id}/credentials",
    )
    return device_id, str(credentials["credentialsId"])


def publish_event(
    event: dict[str, Any],
    access_token: str,
    *,
    mqtt_host: str,
    mqtt_port: int,
    client_id: str,
) -> None:
    publish(
        host=mqtt_host,
        port=mqtt_port,
        username=access_token,
        topic="v1/devices/me/telemetry",
        payload=json.dumps(
            {
                "ts": event["timestamp_ms"],
                "values": event["features"],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8"),
        client_id=client_id,
        timeout_seconds=10,
    )


def run_workflow(
    base_url: str,
    token: str,
    application_id: str,
    inputs: dict[str, Any],
    workspace_path: str,
    review: dict[str, Any] | None,
) -> tuple[dict[str, Any], int]:
    created = platform_json(
        "POST",
        base_url,
        token,
        f"/api/v1/applications/{application_id}/runs",
        {
            "inputs": inputs,
            "use_draft": True,
            "workspace_path": workspace_path,
        },
    )
    run_id = str(created["run_id"])
    resumes = 0
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        run = platform_json(
            "GET",
            base_url,
            token,
            f"/api/v1/runs/{run_id}",
        )
        if run["status"] == "paused":
            if review is None:
                raise RuntimeError("unexpected human pause")
            platform_json(
                "POST",
                base_url,
                token,
                f"/api/v1/runs/{run_id}/resume",
                {"values": review},
            )
            resumes += 1
        elif run["status"] not in {"queued", "running"}:
            return run, resumes
        time.sleep(0.05)
    raise TimeoutError("sealed workflow run timed out")


def alarm_count(
    base_url: str,
    token: str,
    device_id: str,
    event_id: str,
) -> int:
    page = thingsboard_json(
        "GET",
        base_url,
        token,
        f"/api/alarm/DEVICE/{device_id}",
        query={"pageSize": 100, "page": 0, "searchStatus": "ANY"},
    )
    return sum(
        1
        for alarm in page["data"]
        if alarm.get("type") == "Predictive maintenance risk"
        and (alarm.get("details") or {}).get("event_id") == event_id
    )


def jitter(
    rng: random.Random,
    source: dict[str, float],
    width: float,
) -> dict[str, float]:
    return {
        key: round(value + rng.uniform(-width, width), 5)
        for key, value in source.items()
    }


def seed_cases(
    seed: int,
    run_tag: str,
    base_time_ms: int,
) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    low = jitter(
        rng,
        {
            "temperature_c": 58.0,
            "vibration_rms_mm_s": 1.8,
            "current_a": 14.0,
            "pressure_bar": 5.8,
            "rpm": 1470.0,
        },
        0.15,
    )
    high = jitter(
        rng,
        {
            "temperature_c": 101.0,
            "vibration_rms_mm_s": 9.8,
            "current_a": 31.0,
            "pressure_bar": 2.6,
            "rpm": 1325.0,
        },
        0.15,
    )
    uncertain = jitter(
        rng,
        {
            "temperature_c": 77.0,
            "vibration_rms_mm_s": 4.6,
            "current_a": 21.0,
            "pressure_bar": 4.2,
            "rpm": 1425.0,
        },
        0.03,
    )
    missing = dict(low)
    missing.pop("current_a")
    out_of_range = dict(high)
    out_of_range["vibration_rms_mm_s"] = 50.5 + rng.random()
    bad_units = dict(UNITS)
    bad_units["vibration_rms_mm_s"] = "m/s"
    definitions = [
        ("low_risk", low, dict(UNITS), 10),
        ("high_risk", high, dict(UNITS), 20),
        ("uncertain_approve", uncertain, dict(UNITS), 30),
        ("uncertain_reject", uncertain, dict(UNITS), 40),
        ("invalid_unit", low, bad_units, 50),
        ("missing_feature", missing, dict(UNITS), 60),
        ("out_of_range", out_of_range, dict(UNITS), 70),
        ("stale", high, dict(UNITS), -86_500),
        ("out_of_order", high, dict(UNITS), 90),
        ("duplicate_high", high, dict(UNITS), 20),
        ("transient_then_alarm", high, dict(UNITS), 110),
        ("permission_denied", high, dict(UNITS), 120),
    ]
    cases = []
    high_event_id = f"sealed-{run_tag}-high"
    for index, (category, features, units, offset) in enumerate(
        definitions,
        start=1,
    ):
        event_id = (
            high_event_id
            if category in {"high_risk", "duplicate_high"}
            else f"sealed-{run_tag}-{index:02d}"
        )
        cases.append(
            {
                "category": category,
                "event_id": event_id,
                "timestamp_ms": base_time_ms + offset * 1000,
                "features": features,
                "units": units,
            }
        )
    return cases


def drift_observations(
    model: dict[str, Any],
    seed: int,
) -> tuple[list[dict[str, Any]], str]:
    rng = random.Random(seed + 90_000)
    multiplier, expected = {
        0: (0.05, "stable"),
        1: (1.35, "warning"),
        2: (2.65, "critical"),
    }[seed % 3]
    observations = []
    for _ in range(12):
        features = {}
        for feature in model["features"]:
            name = feature["name"]
            baseline = model["drift_baseline"][name]
            features[name] = round(
                baseline["mean"]
                + baseline["scale"]
                * (multiplier + rng.uniform(-0.02, 0.02)),
                6,
            )
        observations.append({"features": features, "units": dict(UNITS)})
    return observations, expected


def ensure_fault_proxy(script: Path, port: int) -> subprocess.Popen[bytes] | None:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return None
    except OSError:
        process = subprocess.Popen(
            [
                str(Path(__file__).parents[3] / ".venv" / "bin" / "python"),
                str(script),
                "--port",
                str(port),
                "--target-port",
                "19090",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(
                    ("127.0.0.1", port),
                    timeout=0.2,
                ):
                    return process
            except OSError:
                time.sleep(0.05)
        process.terminate()
        raise RuntimeError("fault proxy did not start")


def configure_fault(port: int, mode: str, count: int = 0) -> None:
    http_json(
        "POST",
        f"http://127.0.0.1:{port}/__fault__/configure",
        body={"mode": mode, "count": count},
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, required=True)
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
    parser.add_argument("--summary-file", type=Path, required=True)
    args = parser.parse_args()

    proxy = ensure_fault_proxy(
        Path(__file__).with_name("fault_proxy.py"),
        args.fault_port,
    )
    run_tag = f"s{args.seed}-{uuid4().hex[:12]}"
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
            "description": "Rotated for sealed EXP-LILIES-004 acceptance.",
        },
    )
    deployment = platform_json(
        "GET",
        args.platform_base,
        args.platform_token,
        "/api/v1/model-deployments/exp004-predictive-risk-debug",
    )
    model = platform_json(
        "GET",
        args.platform_base,
        args.platform_token,
        (
            f"/api/v1/tabular-models/{deployment['model_id']}"
            f"/versions/{deployment['version']}"
        ),
    )
    drift, expected_drift = drift_observations(model, args.seed)
    base_time_ms = 1_785_326_400_000 + args.seed * 1_000_000
    cases = seed_cases(args.seed, run_tag, base_time_ms)
    seed_fingerprint = canonical_digest(
        {
            "seed": args.seed,
            "categories": [item["category"] for item in cases],
            "drift_expected": expected_drift,
        }
    )
    devices: dict[str, tuple[str, str]] = {}
    results: list[dict[str, Any]] = []
    minimum_event_ts = base_time_ms - 86_400_000
    try:
        configure_fault(args.fault_port, "pass")
        for index, event in enumerate(cases, start=1):
            category = str(event["category"])
            if category == "duplicate_high":
                device_id, access_token = devices["high_risk"]
            else:
                device_id, access_token = create_device(
                    args.thingsboard_base,
                    tb_token,
                    f"EXP004-{run_tag}-{index:02d}",
                )
                devices[category] = (device_id, access_token)
                publish_event(
                    event,
                    access_token,
                    mqtt_host=args.mqtt_host,
                    mqtt_port=args.mqtt_port,
                    client_id=f"exp004-{run_tag}-{index:02d}",
                )

            if category == "transient_then_alarm":
                configure_fault(args.fault_port, "transient_503", 1)
            elif category == "permission_denied":
                configure_fault(args.fault_port, "permission_403")
            else:
                configure_fault(args.fault_port, "pass")

            review = None
            if category == "uncertain_approve":
                review = {
                    "approved": True,
                    "review_note": "Sealed reviewer approved.",
                }
            elif category == "uncertain_reject":
                review = {
                    "approved": False,
                    "review_note": "Sealed reviewer rejected.",
                }
            inputs = {
                "device_id": device_id,
                "event_id": event["event_id"],
                "units": event["units"],
                "minimum_event_ts": minimum_event_ts,
                "last_accepted_ts": (
                    event["timestamp_ms"] + 1
                    if category == "out_of_order"
                    else 0
                ),
                "high_risk_threshold": 0.8,
                "review_low_threshold": 0.35,
                "auto_confidence_threshold": 0.8,
                "drift_observations": drift,
            }
            relative_workspace = (
                f"exp004-sealed/{run_tag}/{index:02d}-{category}"
            )
            (args.workspace_root / relative_workspace).mkdir(
                parents=True,
                exist_ok=True,
            )
            run, resumes = run_workflow(
                args.platform_base,
                args.platform_token,
                args.application_id,
                inputs,
                relative_workspace,
                review,
            )
            configure_fault(args.fault_port, "pass")
            count = alarm_count(
                args.thingsboard_base,
                tb_token,
                device_id,
                str(event["event_id"]),
            )
            state_outputs = (run.get("state") or {}).get("outputs") or {}
            final_outputs = run.get("outputs") or {}
            decision = final_outputs.get("decision")
            artifacts = [
                final_outputs.get("decision_artifact"),
                final_outputs.get("model_evidence_artifact"),
                final_outputs.get("drift_artifact"),
            ]
            prediction = (
                state_outputs.get("risk_inference", {}).get("output")
            )
            drift_result = (
                state_outputs.get("drift_monitor", {}).get("output")
            )
            write_receipt = (
                state_outputs.get("automatic_alarm", {}).get("receipt")
                or state_outputs.get("reviewed_alarm", {}).get("receipt")
            )

            if category in ALARM_CASES:
                action_ok = (
                    isinstance(decision, dict)
                    and decision.get("type")
                    == "Predictive maintenance risk"
                    and count == 1
                )
            elif category in NO_ALARM_CASES:
                action_ok = (
                    isinstance(decision, dict)
                    and decision.get("action") == "no_alarm"
                    and count == 0
                )
            elif category in SAFE_STOP_CASES:
                action_ok = (
                    isinstance(decision, dict)
                    and decision.get("action") == "safe_stop"
                    and count == 0
                )
            else:
                error_text = str(run.get("error") or "").casefold()
                action_ok = (
                    run["status"] == "failed"
                    and count == 0
                    and (
                        "403" in error_text
                        or "permission" in error_text
                        or "forbidden" in error_text
                    )
                )

            expected_resumes = (
                1
                if category in {"uncertain_approve", "uncertain_reject"}
                else 0
            )
            expected_status = (
                "failed" if category == "permission_denied" else "succeeded"
            )
            artifact_ok = (
                all(isinstance(item, dict) for item in artifacts)
                if expected_status == "succeeded"
                else all(item is None for item in artifacts)
            )
            model_ok = (
                prediction is None
                if category in SAFE_STOP_CASES
                else (
                    isinstance(prediction, dict)
                    and prediction.get("model_id")
                    == deployment["model_id"]
                    and prediction.get("version")
                    == deployment["version"]
                    and prediction.get("model_digest")
                    == deployment["model_digest"]
                )
            )
            drift_ok = (
                isinstance(drift_result, dict)
                and drift_result.get("status") == expected_drift
                and drift_result.get("automatic_training_triggered")
                is False
            )
            retry_ok = (
                category != "transient_then_alarm"
                or (
                    isinstance(write_receipt, dict)
                    and int(write_receipt.get("attempt_count", 0)) == 2
                )
            )
            passed = all(
                (
                    run["status"] == expected_status,
                    resumes == expected_resumes,
                    action_ok,
                    artifact_ok,
                    model_ok,
                    drift_ok,
                    retry_ok,
                )
            )
            results.append(
                {
                    "category": category,
                    "passed": passed,
                    "status_ok": run["status"] == expected_status,
                    "decision_and_host_ok": action_ok,
                    "artifacts_ok": artifact_ok,
                    "model_lineage_ok": model_ok,
                    "drift_ok": drift_ok,
                    "retry_ok": retry_ok,
                    "resume_ok": resumes == expected_resumes,
                    "artifact_count": sum(
                        isinstance(item, dict) for item in artifacts
                    ),
                    "alarm_count": count,
                    "resume_count": resumes,
                    "connector_operations": sorted(
                        {
                            value.get("receipt", {}).get("operation_id")
                            for value in state_outputs.values()
                            if isinstance(value, dict)
                            and isinstance(value.get("receipt"), dict)
                            and value["receipt"].get("operation_id")
                        }
                    ),
                }
            )
    finally:
        try:
            configure_fault(args.fault_port, "pass")
        finally:
            if proxy is not None:
                proxy.terminate()
                proxy.wait(timeout=5)

    allowed_operations = {"getLatestTimeseries", "saveAlarm"}
    rpc_count = sum(
        1
        for result in results
        for operation in result["connector_operations"]
        if operation not in allowed_operations
    )
    checks_per_case = 7
    passed_checks = sum(
        sum(
            bool(result[key])
            for key in (
                "status_ok",
                "decision_and_host_ok",
                "artifacts_ok",
                "model_lineage_ok",
                "drift_ok",
                "retry_ok",
                "resume_ok",
            )
        )
        for result in results
    )
    check_keys = (
        "status_ok",
        "decision_and_host_ok",
        "artifacts_ok",
        "model_lineage_ok",
        "drift_ok",
        "retry_ok",
        "resume_ok",
    )
    failed_check_counts = {
        key: sum(not bool(result[key]) for result in results)
        for key in check_keys
    }
    summary = {
        "schema_version": "1.0",
        "task_id": "EXP-LILIES-004",
        "seed": args.seed,
        "seed_fingerprint": seed_fingerprint,
        "input_count": len(results),
        "category_count": len({item["category"] for item in results}),
        "workflow_succeeded_count": sum(
            item["status_ok"]
            and item["category"] != "permission_denied"
            for item in results
        ),
        "expected_terminal_failure_count": sum(
            item["status_ok"]
            and item["category"] == "permission_denied"
            for item in results
        ),
        "human_pause_count": sum(
            int(item["resume_count"]) for item in results
        ),
        "artifact_count": sum(
            int(item["artifact_count"]) for item in results
        ),
        "business_alarm_count": 3,
        "drift_expected": expected_drift,
        "device_rpc_count": rpc_count,
        "check_count": len(results) * checks_per_case + 1,
        "passed_check_count": passed_checks + int(rpc_count == 0),
        "failed_categories": [
            item["category"] for item in results if not item["passed"]
        ],
        "failed_check_counts": failed_check_counts,
        "passed": all(item["passed"] for item in results)
        and rpc_count == 0,
        "claim_ceiling": (
            "controlled-local orchestration correctness; not real-factory "
            "model accuracy or long-duration production reliability"
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
