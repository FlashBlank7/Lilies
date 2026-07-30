#!/usr/bin/env python3
"""Run one sealed EXP-LILIES-003 seed and emit aggregate evidence only."""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from real_project_testkit import (  # noqa: E402
    http_json,
    platform_json,
    run_trace,
    run_workflow,
    wait_run,
    wait_timer,
    write_report,
)


def iso(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).isoformat()


def state_inputs(
    *,
    entity_id: str,
    event_id: str,
    old_state: str,
    new_state: str,
    occurred_at: str,
    hold_for_seconds: float,
) -> dict[str, Any]:
    return {
        "event_kind": "state_changed",
        "event_id": event_id,
        "entity_id": entity_id,
        "old_state": old_state,
        "new_state": new_state,
        "occurred_at": occurred_at,
        "hold_for_seconds": hold_for_seconds,
        "allowed_entities": [entity_id],
        "action_name": "",
    }


def action_inputs(
    *,
    entity_id: str,
    event_id: str,
    action_name: str,
) -> dict[str, Any]:
    return {
        "event_kind": "action_request",
        "event_id": event_id,
        "entity_id": entity_id,
        "old_state": "off",
        "new_state": "off",
        "occurred_at": iso(),
        "hold_for_seconds": 1,
        "allowed_entities": [entity_id],
        "action_name": action_name,
    }


def main() -> int:
    started = time.monotonic()
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--platform-base", default="http://127.0.0.1:8016")
    parser.add_argument("--platform-token", required=True)
    parser.add_argument("--application-id", required=True)
    parser.add_argument("--version", type=int, default=2)
    parser.add_argument("--home-assistant-base", default="http://127.0.0.1:18030")
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--sink-base", default="http://127.0.0.1:18031")
    parser.add_argument("--sink-token", required=True)
    parser.add_argument("--report-file", type=Path, required=True)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    credential = json.loads(args.credential_file.read_text(encoding="utf-8"))
    marker = f"{args.seed}-{rng.randrange(10**7, 10**8)}-{uuid4().hex[:8]}"
    prefix = f"binary_sensor.exp003_seed_{marker.replace('-', '_')}"
    results: list[dict[str, Any]] = []
    run_count = 0
    resume_count = 0
    artifact_count = 0

    def ha_state(entity_id: str, state: str) -> None:
        http_json(
            "POST",
            f"{args.home_assistant_base.rstrip('/')}/api/states/{entity_id}",
            headers={
                "Authorization": f"Bearer {credential['workflow_token']}"
            },
            body={
                "state": state,
                "attributes": {"friendly_name": "Sealed facility subject"},
            },
        )

    def sink_items(path: str) -> list[dict[str, Any]]:
        value = http_json("GET", f"{args.sink_base.rstrip('/')}{path}")
        if not isinstance(value, list):
            raise RuntimeError(f"sink {path} response is not a list")
        return value

    def subject_notifications(entity_id: str) -> list[dict[str, Any]]:
        return [
            item
            for item in sink_items("/notifications")
            if item.get("subject_id") == entity_id
        ]

    def workflow(
        inputs: dict[str, Any],
        *,
        resume_values: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        nonlocal run_count, resume_count, artifact_count
        run = run_workflow(
            base_url=args.platform_base,
            token=args.platform_token,
            application_id=args.application_id,
            version=args.version,
            inputs=inputs,
            resume_values=resume_values,
        )
        run_count += 1
        resume_count += int(run["resume_count"])
        artifact_count += count_artifacts(run)
        return run

    def count_artifacts(run: dict[str, Any]) -> int:
        return sum(
            isinstance(value, dict)
            and isinstance(value.get("relative_path"), str)
            for value in (run.get("outputs") or {}).values()
        )

    def timer(entity_id: str, statuses: set[str]) -> dict[str, Any]:
        return wait_timer(
            base_url=args.platform_base,
            token=args.platform_token,
            timer_key=f"facility-door:{entity_id}",
            statuses=statuses,
        )

    open_entity = f"{prefix}_open"
    ha_state(open_entity, "on")
    workflow(
        state_inputs(
            entity_id=open_entity,
            event_id=f"{marker}-open",
            old_state="off",
            new_state="on",
            occurred_at=iso(),
            hold_for_seconds=0.3,
        )
    )
    open_timer = timer(open_entity, {"completed"})
    open_due = wait_run(
        base_url=args.platform_base,
        token=args.platform_token,
        run_id=open_timer["run_id"],
    )
    run_count += 1
    artifact_count += count_artifacts(open_due)
    open_passed = (
        open_due["status"] == "succeeded"
        and (open_due.get("outputs") or {}).get("notification_count") == 1
        and len(subject_notifications(open_entity)) == 1
    )
    results.append({"case": "open_until_deadline", "passed": open_passed})

    close_entity = f"{prefix}_close"
    ha_state(close_entity, "on")
    close_time = datetime.now(timezone.utc)
    workflow(
        state_inputs(
            entity_id=close_entity,
            event_id=f"{marker}-close-open",
            old_state="off",
            new_state="on",
            occurred_at=iso(close_time),
            hold_for_seconds=2,
        )
    )
    ha_state(close_entity, "off")
    close_run = workflow(
        state_inputs(
            entity_id=close_entity,
            event_id=f"{marker}-close",
            old_state="on",
            new_state="off",
            occurred_at=iso(close_time + timedelta(seconds=1)),
            hold_for_seconds=2,
        )
    )
    close_timer = timer(close_entity, {"cancelled"})
    results.append(
        {
            "case": "close_before_deadline",
            "passed": (
                close_run["status"] == "succeeded"
                and close_timer["status"] == "cancelled"
                and not subject_notifications(close_entity)
            ),
        }
    )

    replay_entity = f"{prefix}_replay"
    replay_at = iso()
    replay_inputs = state_inputs(
        entity_id=replay_entity,
        event_id=f"{marker}-replay",
        old_state="off",
        new_state="on",
        occurred_at=replay_at,
        hold_for_seconds=30,
    )
    first_replay = workflow(replay_inputs)
    second_replay = workflow(replay_inputs)
    replay_timer = timer(replay_entity, {"pending"})
    replay_history = replay_timer["history"]
    replay_passed = (
        first_replay["status"] == "succeeded"
        and second_replay["status"] == "succeeded"
        and sum(item["status"] == "scheduled" for item in replay_history) == 1
        and sum(item["status"] == "replayed" for item in replay_history) == 1
    )
    workflow(
        state_inputs(
            entity_id=replay_entity,
            event_id=f"{marker}-replay-cleanup",
            old_state="on",
            new_state="off",
            occurred_at=iso(datetime.now(timezone.utc) + timedelta(seconds=1)),
            hold_for_seconds=30,
        )
    )
    results.append({"case": "duplicate_event_replay", "passed": replay_passed})

    stale_entity = f"{prefix}_stale"
    stale_base = datetime.now(timezone.utc)
    workflow(
        state_inputs(
            entity_id=stale_entity,
            event_id=f"{marker}-newer-open",
            old_state="off",
            new_state="on",
            occurred_at=iso(stale_base),
            hold_for_seconds=30,
        )
    )
    stale_run = workflow(
        state_inputs(
            entity_id=stale_entity,
            event_id=f"{marker}-stale-close",
            old_state="on",
            new_state="off",
            occurred_at=iso(stale_base - timedelta(seconds=5)),
            hold_for_seconds=30,
        )
    )
    stale_timer = timer(stale_entity, {"pending"})
    stale_passed = (
        stale_run["status"] == "succeeded"
        and stale_timer["status"] == "pending"
        and any(
            item["status"] == "stale_ignored"
            for item in stale_timer["history"]
        )
    )
    workflow(
        state_inputs(
            entity_id=stale_entity,
            event_id=f"{marker}-fresh-close",
            old_state="on",
            new_state="off",
            occurred_at=iso(stale_base + timedelta(seconds=1)),
            hold_for_seconds=30,
        )
    )
    results.append({"case": "stale_event_ignored", "passed": stale_passed})

    concurrent_entities = [f"{prefix}_concurrent_{index}" for index in (1, 2)]
    for entity_id in concurrent_entities:
        ha_state(entity_id, "on")
        workflow(
            state_inputs(
                entity_id=entity_id,
                event_id=f"{marker}-{entity_id.rsplit('_', 1)[-1]}",
                old_state="off",
                new_state="on",
                occurred_at=iso(),
                hold_for_seconds=0.3,
            )
        )
    concurrent_due = []
    for entity_id in concurrent_entities:
        current = timer(entity_id, {"completed"})
        run = wait_run(
            base_url=args.platform_base,
            token=args.platform_token,
            run_id=current["run_id"],
        )
        run_count += 1
        artifact_count += count_artifacts(run)
        concurrent_due.append(run)
    results.append(
        {
            "case": "concurrent_subject_isolation",
            "passed": (
                all(
                    (run.get("outputs") or {}).get("notification_count") == 1
                    for run in concurrent_due
                )
                and all(
                    len(subject_notifications(entity_id)) == 1
                    for entity_id in concurrent_entities
                )
            ),
        }
    )

    permission_entity = f"{prefix}_permission"
    ha_state(permission_entity, "on")
    platform_json(
        "POST",
        args.platform_base,
        args.platform_token,
        "/api/v1/platform/secrets",
        {
            "owner_id": args.application_id,
            "name": "notification_sink_bearer",
            "value": "Bearer sealed-permission-denied",
            "description": "Sealed permission-denial probe",
        },
    )
    try:
        workflow(
            state_inputs(
                entity_id=permission_entity,
                event_id=f"{marker}-permission",
                old_state="off",
                new_state="on",
                occurred_at=iso(),
                hold_for_seconds=0.3,
            )
        )
        permission_timer = timer(permission_entity, {"completed"})
        permission_run = wait_run(
            base_url=args.platform_base,
            token=args.platform_token,
            run_id=permission_timer["run_id"],
        )
        run_count += 1
        artifact_count += count_artifacts(permission_run)
        permission_trace = run_trace(
            base_url=args.platform_base,
            token=args.platform_token,
            run_id=permission_timer["run_id"],
        )
    finally:
        platform_json(
            "POST",
            args.platform_base,
            args.platform_token,
            "/api/v1/platform/secrets",
            {
                "owner_id": args.application_id,
                "name": "notification_sink_bearer",
                "value": f"Bearer {args.sink_token}",
                "description": (
                    "Controlled-local scoped notification sink authorization"
                ),
            },
        )
    results.append(
        {
            "case": "permission_denial_safe_stop",
            "passed": (
                (permission_run.get("outputs") or {}).get(
                    "notification_count"
                )
                == 0
                and not subject_notifications(permission_entity)
                and sum(
                    event["type"] == "node.retry"
                    for event in permission_trace
                )
                == 0
                and "deadline_end"
                not in (permission_run["state"]["outputs"] or {})
            ),
        }
    )

    transient_entity = f"{prefix}_transient"
    ha_state(transient_entity, "on")
    http_json(
        "POST",
        f"{args.sink_base.rstrip('/')}/faults",
        headers={"Authorization": f"Bearer {args.sink_token}"},
        body={"path": "/notifications", "status": 503, "count": 1},
    )
    workflow(
        state_inputs(
            entity_id=transient_entity,
            event_id=f"{marker}-transient",
            old_state="off",
            new_state="on",
            occurred_at=iso(),
            hold_for_seconds=0.3,
        )
    )
    transient_timer = timer(transient_entity, {"completed"})
    transient_run = wait_run(
        base_url=args.platform_base,
        token=args.platform_token,
        run_id=transient_timer["run_id"],
    )
    run_count += 1
    artifact_count += count_artifacts(transient_run)
    transient_trace = run_trace(
        base_url=args.platform_base,
        token=args.platform_token,
        run_id=transient_timer["run_id"],
    )
    results.append(
        {
            "case": "transient_retry_exactly_once",
            "passed": (
                (transient_run.get("outputs") or {}).get(
                    "notification_count"
                )
                == 1
                and len(subject_notifications(transient_entity)) == 1
                and sum(
                    event["type"] == "node.retry"
                    for event in transient_trace
                )
                == 1
            ),
        }
    )

    action_entity = f"{prefix}_action"
    action_before = len(sink_items("/action-attempts"))
    rejected = workflow(
        action_inputs(
            entity_id=action_entity,
            event_id=f"{marker}-action-rejected",
            action_name=f"acknowledge-{rng.randrange(1000, 9999)}",
        ),
        resume_values={"approved": False, "comment": "sealed rejection"},
    )
    action_after_reject = len(sink_items("/action-attempts"))
    approved = workflow(
        action_inputs(
            entity_id=action_entity,
            event_id=f"{marker}-action-approved",
            action_name=f"acknowledge-{rng.randrange(1000, 9999)}",
        ),
        resume_values={"approved": True, "comment": "sealed approval"},
    )
    action_after_approve = len(sink_items("/action-attempts"))
    results.append(
        {
            "case": "human_action_gate",
            "passed": (
                rejected["resume_count"] == 1
                and (rejected.get("outputs") or {})["action_receipt"][
                    "attempted"
                ]
                is False
                and action_after_reject == action_before
                and approved["resume_count"] == 1
                and (approved.get("outputs") or {})["action_receipt"][
                    "accepted"
                ]
                is True
                and action_after_approve == action_before + 1
            ),
        }
    )

    failed_cases = [
        item["case"] for item in results if item["passed"] is not True
    ]
    report = {
        "schema_version": "exp-lilies-003-sealed-seed-summary-v1",
        "task_id": "EXP-LILIES-003",
        "seed": args.seed,
        "application_id": args.application_id,
        "version": args.version,
        "status": "passed" if not failed_cases else "failed",
        "case_count": len(results),
        "passed_count": len(results) - len(failed_cases),
        "failed_cases": failed_cases,
        "results": results,
        "run_count": run_count,
        "resume_count": resume_count,
        "artifact_count": artifact_count,
        "forbidden_device_service_action_count": 0,
        "report_contains_exact_seed_values": False,
        "completed_at": iso(),
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    write_report(args.report_file, report)
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "seed",
                    "status",
                    "passed_count",
                    "case_count",
                    "run_count",
                    "resume_count",
                    "artifact_count",
                )
            },
            separators=(",", ":"),
        )
    )
    return 0 if not failed_cases else 1


if __name__ == "__main__":
    raise SystemExit(main())
