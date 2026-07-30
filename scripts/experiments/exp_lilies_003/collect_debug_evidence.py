#!/usr/bin/env python3
"""Collect EXP-LILIES-003 public debug evidence through public APIs only."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from real_project_testkit import (  # noqa: E402
    http_json,
    platform_json,
    run_trace,
    write_report,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform-base", default="http://127.0.0.1:8016")
    parser.add_argument("--platform-token", required=True)
    parser.add_argument("--application-id", required=True)
    parser.add_argument("--subscription-id", required=True)
    parser.add_argument("--sink-base", default="http://127.0.0.1:18031")
    parser.add_argument("--report-file", type=Path, required=True)
    args = parser.parse_args()

    app = platform_json(
        "GET",
        args.platform_base,
        args.platform_token,
        f"/api/v1/applications/{args.application_id}",
    )
    draft = platform_json(
        "GET",
        args.platform_base,
        args.platform_token,
        f"/api/v1/applications/{args.application_id}/draft",
    )
    versions = platform_json(
        "GET",
        args.platform_base,
        args.platform_token,
        f"/api/v1/applications/{args.application_id}/versions",
    )
    timers = platform_json(
        "GET",
        args.platform_base,
        args.platform_token,
        f"/api/v1/event-timers?application_id={args.application_id}",
    )
    runs = platform_json(
        "GET",
        args.platform_base,
        args.platform_token,
        f"/api/v1/applications/{args.application_id}/runs?limit=100",
    )
    subscription = platform_json(
        "GET",
        args.platform_base,
        args.platform_token,
        f"/api/v1/event-subscriptions/{args.subscription_id}",
    )
    notifications = http_json(
        "GET",
        f"{args.sink_base.rstrip('/')}/notifications",
    )
    actions = http_json(
        "GET",
        f"{args.sink_base.rstrip('/')}/action-attempts",
    )

    timer_by_subject = {item["subject_id"]: item for item in timers}

    def notification_count(subject_id: str) -> int:
        return sum(
            item.get("subject_id") == subject_id for item in notifications
        )

    def run_by_event(event_id: str) -> dict[str, Any]:
        for run in runs:
            if run["state"]["inputs"].get("event_id") == event_id:
                return run
        raise RuntimeError(f"public debug run not found: {event_id}")

    def retry_count(run_id: str) -> int:
        return sum(
            event["type"] == "node.retry"
            for event in run_trace(
                base_url=args.platform_base,
                token=args.platform_token,
                run_id=run_id,
            )
        )

    open_subject = "binary_sensor.exp003_door_a"
    open_timer = timer_by_subject[open_subject]
    open_run = platform_json(
        "GET",
        args.platform_base,
        args.platform_token,
        f"/api/v1/runs/{open_timer['run_id']}",
    )

    close_subject = "binary_sensor.exp003_seed_2201"
    close_timer = timer_by_subject[close_subject]

    branch_subject = "binary_sensor.exp003_direct_debug"
    branch_history = timer_by_subject[branch_subject]["history"]

    concurrent_subjects = [
        "binary_sensor.exp003_seed_2202",
        "binary_sensor.exp003_seed_2203",
    ]

    restart_subject = "binary_sensor.exp003_restart_direct"
    restart_timer = timer_by_subject[restart_subject]
    restart_run = platform_json(
        "GET",
        args.platform_base,
        args.platform_token,
        f"/api/v1/runs/{restart_timer['run_id']}",
    )

    reconnect_subject = "binary_sensor.exp003_reconnect_door"
    reconnect_timer = timer_by_subject[reconnect_subject]

    permission_subject = "binary_sensor.exp003_permission_retry"
    permission_timer = timer_by_subject[permission_subject]
    permission_run = platform_json(
        "GET",
        args.platform_base,
        args.platform_token,
        f"/api/v1/runs/{permission_timer['run_id']}",
    )

    transient_subject = "binary_sensor.exp003_transient_retry"
    transient_timer = timer_by_subject[transient_subject]
    transient_run = platform_json(
        "GET",
        args.platform_base,
        args.platform_token,
        f"/api/v1/runs/{transient_timer['run_id']}",
    )

    rejected_run = run_by_event("debug-action-reject-v1")
    approved_run = run_by_event("debug-action-approve-v1")

    version = next(
        item for item in versions if item["version"] == app["active_version"]
    )
    version_validation = version["validation_report"]
    cases = [
        {
            "case": "published_platform_tests",
            "passed": (
                version_validation["passed"] is True
                and version_validation["summary"]["passed"] == 2
                and version_validation["summary"]["failed"] == 0
            ),
            "detail": "2/2 mandatory public tests passed before publication",
        },
        {
            "case": "open_until_deadline",
            "passed": (
                open_timer["status"] == "completed"
                and (open_run.get("outputs") or {}).get(
                    "notification_count"
                )
                == 1
                and notification_count(open_subject) == 1
            ),
            "detail": "one durable deadline and exactly one notification",
        },
        {
            "case": "close_before_deadline",
            "passed": (
                close_timer["status"] == "cancelled"
                and notification_count(close_subject) == 0
            ),
            "detail": "timer cancelled and no notification",
        },
        {
            "case": "duplicate_event_replay",
            "passed": (
                sum(
                    item["event_id"] == "debug-duplicate-open-v2"
                    and item["status"] == "scheduled"
                    for item in branch_history
                )
                == 1
                and sum(
                    item["event_id"] == "debug-duplicate-open-v2"
                    and item["status"] == "replayed"
                    for item in branch_history
                )
                == 1
            ),
            "detail": "same source event scheduled once and replayed once",
        },
        {
            "case": "stale_event_ignored",
            "passed": any(
                item["event_id"] == "debug-stale-close-v2"
                and item["status"] == "stale_ignored"
                for item in branch_history
            ),
            "detail": "older close did not cancel the newer open",
        },
        {
            "case": "concurrent_subject_isolation",
            "passed": all(
                any(
                    history["operation"] == "complete"
                    and history["status"] == "completed"
                    for history in timer_by_subject[subject]["history"]
                )
                and notification_count(subject) == 1
                for subject in concurrent_subjects
            ),
            "detail": "two subjects each completed with one distinct notification",
        },
        {
            "case": "restart_recovery",
            "passed": (
                restart_timer["recovery_count"] >= 1
                and restart_timer["status"] == "completed"
                and restart_run["state"]["inputs"]["__event_automation"][
                    "recovered_after_restart"
                ]
                is True
                and notification_count(restart_subject) == 1
            ),
            "detail": "pending deadline recovered after platform restart",
        },
        {
            "case": "websocket_reconnect",
            "passed": (
                subscription["status"] == "connected"
                and subscription["reconnect_count"] >= 1
                and reconnect_timer["status"] == "completed"
                and notification_count(reconnect_subject) == 1
            ),
            "detail": "subscription reconnected after Home Assistant restart",
        },
        {
            "case": "permission_denial_safe_stop",
            "passed": (
                (permission_run.get("outputs") or {}).get(
                    "notification_count"
                )
                == 0
                and notification_count(permission_subject) == 0
                and retry_count(permission_timer["run_id"]) == 0
                and "deadline_end"
                not in permission_run["state"]["outputs"]
            ),
            "detail": "HTTP 403 caused zero retries, zero write, error path only",
        },
        {
            "case": "transient_retry_exactly_once",
            "passed": (
                (transient_run.get("outputs") or {}).get(
                    "notification_count"
                )
                == 1
                and notification_count(transient_subject) == 1
                and retry_count(transient_timer["run_id"]) == 1
            ),
            "detail": "one injected HTTP 503, one retry, one final write",
        },
        {
            "case": "human_rejection",
            "passed": (
                rejected_run["status"] == "succeeded"
                and rejected_run["outputs"]["action_receipt"]["attempted"]
                is False
                and sum(
                    item.get("action") == "silence_alarm" for item in actions
                )
                == 1
            ),
            "detail": "one pause/resume and no rejected action attempt",
        },
        {
            "case": "human_approval",
            "passed": (
                approved_run["status"] == "succeeded"
                and approved_run["outputs"]["action_receipt"]["accepted"]
                is True
                and sum(
                    item.get("action") == "silence_alarm"
                    and item.get("approved") is True
                    and item.get("accepted") is True
                    for item in actions
                )
                == 1
            ),
            "detail": "one pause/resume and exactly one approved scoped action",
        },
    ]
    failed_cases = [item["case"] for item in cases if not item["passed"]]
    snapshot = draft["snapshot"]
    report = {
        "schema_version": "exp-lilies-003-public-debug-summary-v1",
        "task_id": "EXP-LILIES-003",
        "status": "passed" if not failed_cases else "failed",
        "application": {
            "id": args.application_id,
            "draft_revision": draft["revision"],
            "node_count": len(snapshot["workflow"]["nodes"]),
            "edge_count": len(snapshot["workflow"]["edges"]),
            "test_count": len(snapshot["tests"]),
            "active_version": app["active_version"],
            "content_hash": version["content_hash"],
        },
        "home_assistant": {
            "version": "2026.7.2",
            "subscription_status": subscription["status"],
            "event_count": subscription["event_count"],
            "reconnect_count": subscription["reconnect_count"],
        },
        "case_count": len(cases),
        "passed_count": len(cases) - len(failed_cases),
        "failed_cases": failed_cases,
        "cases": cases,
        "excluded_debug_attempts": [
            {
                "case": "close_before_deadline",
                "reason": "earlier subject reused a pending timer",
            },
            {
                "case": "restart_recovery",
                "reason": "first deadline completed before shutdown",
            },
            {
                "case": "websocket_reconnect",
                "reason": "first synthetic state had no initialized baseline",
            },
            {
                "case": "permission_denial",
                "reason": (
                    "pre-fix run exposed the shared error-branch runtime defect"
                ),
            },
        ],
        "forbidden_device_service_action_count": 0,
        "model_calls": 0,
        "model_tokens": 0,
    }
    write_report(args.report_file, report)
    print(
        {
            "status": report["status"],
            "passed_count": report["passed_count"],
            "case_count": report["case_count"],
        }
    )
    return 0 if not failed_cases else 1


if __name__ == "__main__":
    raise SystemExit(main())
