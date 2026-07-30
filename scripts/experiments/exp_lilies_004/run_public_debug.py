#!/usr/bin/env python3
"""Run the public EXP-LILIES-004 business cases against real ThingsBoard."""

from __future__ import annotations

import argparse
import json
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
    "high_risk_automatic_alarm",
    "uncertain_human_approve",
    "duplicate_event_no_duplicate_alarm",
}
NO_ALARM_CASES = {
    "low_risk_no_alarm",
    "uncertain_human_reject",
}
SAFE_STOP_CASES = {
    "invalid_unit_safe_stop",
    "missing_feature_safe_stop",
    "stale_event_safe_stop",
    "out_of_order_safe_stop",
}


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


def thingsboard_login(
    base_url: str,
    username: str,
    password: str,
) -> str:
    response = http_json(
        "POST",
        f"{base_url.rstrip('/')}/api/auth/login",
        body={"username": username, "password": password},
    )
    return str(response["token"])


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
            "label": "EXP-LILIES-004 public acceptance device",
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
    *,
    mqtt_host: str,
    mqtt_port: int,
    access_token: str,
    client_id: str,
) -> None:
    payload = json.dumps(
        {
            "ts": int(event["timestamp_ms"]),
            "values": event["features"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    publish(
        host=mqtt_host,
        port=mqtt_port,
        username=access_token,
        topic="v1/devices/me/telemetry",
        payload=payload,
        client_id=client_id,
        timeout_seconds=10,
    )


def run_workflow(
    *,
    base_url: str,
    token: str,
    application_id: str,
    inputs: dict[str, Any],
    workspace_path: str,
    simulated_review: dict[str, Any] | None,
) -> dict[str, Any]:
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
            if simulated_review is None:
                raise RuntimeError(
                    f"run {run_id} paused without a public review response"
                )
            platform_json(
                "POST",
                base_url,
                token,
                f"/api/v1/runs/{run_id}/resume",
                {"values": simulated_review},
            )
            resumes += 1
        elif run["status"] not in {"queued", "running"}:
            run["resume_count"] = resumes
            return run
        time.sleep(0.05)
    raise TimeoutError(f"run {run_id} did not complete")


def event_alarm_count(
    *,
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
        query={
            "pageSize": 100,
            "page": 0,
            "searchStatus": "ANY",
        },
    )
    return sum(
        1
        for alarm in page["data"]
        if alarm.get("type") == "Predictive maintenance risk"
        and (alarm.get("details") or {}).get("event_id") == event_id
    )


def case_result(
    event: dict[str, Any],
    run: dict[str, Any],
    alarm_count: int,
) -> dict[str, Any]:
    business_case = str(event["business_case"])
    outputs = run["state"]["outputs"]
    end_outputs = run.get("outputs") or {}
    decision = end_outputs.get("decision")
    if business_case in ALARM_CASES:
        expected_action = "alarm"
        actual_action = (
            "alarm"
            if isinstance(decision, dict)
            and decision.get("type") == "Predictive maintenance risk"
            else "unexpected"
        )
        alarm_ok = alarm_count == 1
    elif business_case in NO_ALARM_CASES:
        expected_action = "no_alarm"
        actual_action = (
            decision.get("action")
            if isinstance(decision, dict)
            else "unexpected"
        )
        alarm_ok = alarm_count == 0
    else:
        expected_action = "safe_stop"
        actual_action = (
            decision.get("action")
            if isinstance(decision, dict)
            else "unexpected"
        )
        alarm_ok = alarm_count == 0
    expected_resumes = (
        1
        if business_case
        in {"uncertain_human_approve", "uncertain_human_reject"}
        else 0
    )
    artifact_keys = (
        "decision_artifact",
        "model_evidence_artifact",
        "drift_artifact",
    )
    artifacts = {
        key: end_outputs.get(key)
        for key in artifact_keys
    }
    passed = (
        run["status"] == "succeeded"
        and actual_action == expected_action
        and alarm_ok
        and run["resume_count"] == expected_resumes
        and all(isinstance(value, dict) for value in artifacts.values())
    )
    prediction = outputs.get("risk_inference", {}).get("output")
    validation = outputs.get("validate_signal", {}).get("output")
    drift = outputs.get("drift_monitor", {}).get("output")
    write_node = (
        outputs.get("automatic_alarm")
        or outputs.get("reviewed_alarm")
        or {}
    )
    return {
        "business_case": business_case,
        "event_id": event["event_id"],
        "run_id": run["id"],
        "status": run["status"],
        "passed": passed,
        "expected_action": expected_action,
        "actual_action": actual_action,
        "resume_count": run["resume_count"],
        "validation": (
            {
                "valid": validation.get("valid"),
                "error_count": len(validation.get("errors", [])),
                "errors": validation.get("errors", []),
            }
            if isinstance(validation, dict)
            else None
        ),
        "prediction": (
            {
                "probability": prediction.get("probability"),
                "confidence": prediction.get("confidence"),
                "predicted_label": prediction.get("predicted_label"),
                "model_id": prediction.get("model_id"),
                "version": prediction.get("version"),
            }
            if isinstance(prediction, dict)
            else None
        ),
        "branches": {
            "structure": outputs.get("structure_router", {}).get("branch"),
            "time": outputs.get("time_router", {}).get("branch"),
            "risk": outputs.get("risk_router", {}).get("branch"),
            "review": outputs.get("review_router", {}).get("branch"),
        },
        "drift": (
            {
                "status": drift.get("status"),
                "score": drift.get("score"),
                "automatic_training_triggered": drift.get(
                    "automatic_training_triggered"
                ),
            }
            if isinstance(drift, dict)
            else None
        ),
        "write_receipt": write_node.get("receipt"),
        "alarm_count_for_event": alarm_count,
        "artifacts": artifacts,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform-base", default="http://127.0.0.1:8014")
    parser.add_argument("--platform-token", required=True)
    parser.add_argument("--application-id", required=True)
    parser.add_argument("--thingsboard-base", default="http://127.0.0.1:19090")
    parser.add_argument("--thingsboard-username", default="tenant@thingsboard.org")
    parser.add_argument("--thingsboard-password", default="tenant")
    parser.add_argument("--mqtt-host", default="127.0.0.1")
    parser.add_argument("--mqtt-port", type=int, default=18884)
    parser.add_argument("--events-file", type=Path, required=True)
    parser.add_argument("--drift-file", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--report-file", type=Path, required=True)
    parser.add_argument("--replay-report", type=Path)
    args = parser.parse_args()

    package = json.loads(args.events_file.read_text(encoding="utf-8"))
    drift = json.loads(args.drift_file.read_text(encoding="utf-8"))[
        "observations"
    ]
    tb_token = thingsboard_login(
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
            "description": "Rotated for the public EXP-LILIES-004 debug run",
        },
    )
    run_tag = uuid4().hex[:10]
    devices: dict[str, tuple[str, str]] = {}
    results: list[dict[str, Any]] = []
    previous_by_case: dict[str, dict[str, Any]] = {}
    if args.replay_report is not None:
        previous = json.loads(
            args.replay_report.read_text(encoding="utf-8")
        )
        previous_by_case = {
            str(item["business_case"]): item
            for item in previous["cases"]
        }
    minimum_event_ts = int(package["clock"]["base_time_ms"]) - (
        int(package["clock"]["maximum_age_seconds"]) * 1000
    )
    for index, event in enumerate(package["events"], start=1):
        business_case = str(event["business_case"])
        if previous_by_case:
            previous_run = platform_json(
                "GET",
                args.platform_base,
                args.platform_token,
                (
                    "/api/v1/runs/"
                    f"{previous_by_case[business_case]['run_id']}"
                ),
            )
            inputs = dict(previous_run["state"]["inputs"])
            device_id = str(inputs["device_id"])
        else:
            if business_case == "duplicate_event_no_duplicate_alarm":
                device_id, access_token = devices[
                    "high_risk_automatic_alarm"
                ]
            else:
                device_id, access_token = create_device(
                    args.thingsboard_base,
                    tb_token,
                    f"EXP004-{run_tag}-{index:02d}-{business_case}",
                )
                devices[business_case] = (device_id, access_token)
                publish_event(
                    event,
                    mqtt_host=args.mqtt_host,
                    mqtt_port=args.mqtt_port,
                    access_token=access_token,
                    client_id=f"exp004-{run_tag}-{index:02d}",
                )
            last_accepted_ts = (
                int(event["timestamp_ms"]) + 1
                if business_case == "out_of_order_safe_stop"
                else 0
            )
            inputs = {
                "device_id": device_id,
                "event_id": event["event_id"],
                "units": event["units"],
                "minimum_event_ts": minimum_event_ts,
                "last_accepted_ts": last_accepted_ts,
                "high_risk_threshold": 0.8,
                "review_low_threshold": 0.35,
                "auto_confidence_threshold": 0.8,
                "drift_observations": drift,
            }
        review = None
        if business_case == "uncertain_human_approve":
            review = {
                "approved": True,
                "review_note": "Public debug reviewer approved.",
            }
        elif business_case == "uncertain_human_reject":
            review = {
                "approved": False,
                "review_note": "Public debug reviewer rejected.",
            }
        workspace_path = (
            f"exp004-public-debug/{run_tag}/{business_case}"
        )
        (args.workspace_root / workspace_path).mkdir(
            parents=True,
            exist_ok=True,
        )
        run = run_workflow(
            base_url=args.platform_base,
            token=args.platform_token,
            application_id=args.application_id,
            inputs=inputs,
            workspace_path=workspace_path,
            simulated_review=review,
        )
        alarm_count = event_alarm_count(
            base_url=args.thingsboard_base,
            token=tb_token,
            device_id=device_id,
            event_id=str(event["event_id"]),
        )
        results.append(case_result(event, run, alarm_count))

    report = {
        "schema_version": "1.0",
        "application_id": args.application_id,
        "run_tag": run_tag,
        "case_count": len(results),
        "passed_count": sum(bool(item["passed"]) for item in results),
        "failed_count": sum(not bool(item["passed"]) for item in results),
        "passed": all(bool(item["passed"]) for item in results),
        "cases": results,
    }
    args.report_file.parent.mkdir(parents=True, exist_ok=True)
    args.report_file.write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "passed_count": report["passed_count"],
                "failed_count": report["failed_count"],
                "report_file": str(args.report_file),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
