#!/usr/bin/env python3
"""Run one post-publication EXP-LILIES-006 Seed and emit aggregates only."""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import urllib.parse
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from real_project_testkit import http_json, platform_json, run_workflow  # noqa: E402


ITEM_CODES = ("ITEM-A", "ITEM-B", "ITEM-C")


def points(values: list[float], start: date) -> list[dict[str, Any]]:
    return [
        {
            "timestamp": (start + timedelta(days=index)).isoformat(),
            "value": value,
        }
        for index, value in enumerate(values)
    ]


def seed_case(seed: int) -> dict[str, Any]:
    rng = random.Random(seed)
    start = date(2026, 4, 1) + timedelta(days=seed % 60)
    histories: list[dict[str, Any]] = []
    training: list[dict[str, Any]] = []
    evaluation: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    inventory = {"ITEM-A": 30.0, "ITEM-B": 20.0, "ITEM-C": 50.0}
    forecasts: dict[str, float] = {}
    expected_orders: dict[str, float] = {}
    total_capacity = 0.0
    total_cost = 0.0
    drift_expected = seed % 3 == 0
    for index, item_code in enumerate(ITEM_CODES):
        weekly = [float(rng.randint(3 + index * 2, 14 + index * 4)) for _ in range(7)]
        stable_values = weekly * 4
        production_values = list(stable_values)
        if drift_expected:
            production_values[-7:] = [round(value * 1.6, 3) for value in weekly]
        histories.append(
            {
                "series_id": item_code,
                "points": points(production_values, start),
            }
        )
        training.append(
            {
                "series_id": item_code,
                "points": points(stable_values[:21], start),
            }
        )
        evaluation.append(
            {
                "series_id": item_code,
                "history": points(stable_values[:21], start),
                "actual": points(stable_values[21:], start + timedelta(days=21)),
            }
        )
        lot_size = float((5, 10, 20)[index])
        moq = lot_size * rng.randint(1, 2)
        inbound = float(rng.randint(0, 2)) * lot_size
        safety = float(rng.randint(1, 3)) * lot_size
        unit_cost = float(rng.randint(5, 35) * 10)
        forecast_total = sum(production_values[-7:])
        ideal = max(0, forecast_total + safety - inventory[item_code] - inbound)
        order = math.ceil(ideal / lot_size - 1e-12) * lot_size
        if order > 0:
            order = max(order, moq)
        forecasts[item_code] = forecast_total
        expected_orders[item_code] = order
        total_capacity += order
        total_cost += order * unit_cost
        items.append(
            {
                "item_code": item_code,
                "warehouse": "Stores - L",
                "inbound": inbound,
                "safety_stock": safety,
                "moq": moq,
                "lot_size": lot_size,
                "unit_cost": unit_cost,
                "minimum_fulfillment": 0.7,
                "priority_weight": float(3 - index),
            }
        )
    return {
        "training": training,
        "evaluation": evaluation,
        "inputs": {
            "unit": "ea/day",
            "forecast_horizon_days": 7,
            "history": histories,
            "items": items,
            "constraints": {
                "capacity": max(total_capacity, 1),
                "budget": max(total_cost + 1000, 1000),
            },
            "company": "Lilies Planning",
            "schedule_date": "2026-09-15",
        },
        "expected_orders": expected_orders,
        "forecast_totals": forecasts,
        "drift_expected": drift_expected,
    }


def erpnext_requests(
    base_url: str,
    token: str,
    title: str,
) -> list[dict[str, Any]]:
    query = (
        "fields=%5B%22name%22%2C%22title%22%2C%22docstatus%22%2C%22status%22%5D"
        "&filters="
        + urllib.parse.quote(
            json.dumps(
                [["Material Request", "title", "=", title]],
                separators=(",", ":"),
            ),
            safe="",
        )
        + "&limit_page_length=10"
    )
    return http_json(
        "GET",
        f"{base_url.rstrip('/')}/api/resource/Material%20Request?{query}",
        headers={"Authorization": f"token {token}"},
    )["data"]


def artifact_count(run: dict[str, Any]) -> int:
    return sum(
        1
        for value in run.get("outputs", {}).values()
        if isinstance(value, dict) and str(value.get("sha256", "")).startswith("sha256:")
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--platform-base", default="http://127.0.0.1:8018")
    parser.add_argument("--platform-token", required=True)
    parser.add_argument("--application-id", required=True)
    parser.add_argument("--version", type=int, default=1)
    parser.add_argument("--erpnext-base", default="http://127.0.0.1:18060")
    parser.add_argument("--erpnext-token", required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    args = parser.parse_args()
    case = seed_case(args.seed)
    nonce = uuid4().hex[:12]
    lifecycle_key = f"exp006-seed-{args.seed}-{nonce}"
    trained = platform_json(
        "POST",
        args.platform_base,
        args.platform_token,
        "/api/v1/forecast-models/train",
        {
            "model_name": f"Sealed demand candidate {args.seed}",
            "unit": "ea/day",
            "series": case["training"],
            "seasonal_period": 7,
            "interval_coverage": 0.9,
            "retraining_wape_threshold": 0.25,
            "source": {
                "kind": "protected_customer_history",
                "seed_fingerprint": lifecycle_key,
            },
            "idempotency_key": f"{lifecycle_key}-train",
        },
    )
    evaluated = platform_json(
        "POST",
        args.platform_base,
        args.platform_token,
        (f"/api/v1/forecast-models/{trained['model_id']}/versions/1/evaluate"),
        {
            "series": case["evaluation"],
            "idempotency_key": f"{lifecycle_key}-evaluate",
        },
    )
    deployment = platform_json(
        "GET",
        args.platform_base,
        args.platform_token,
        "/api/v1/forecast-deployments/exp006-demand-production",
    )
    promoted = platform_json(
        "POST",
        args.platform_base,
        args.platform_token,
        "/api/v1/forecast-deployments/exp006-demand-production/promote",
        {
            "model_id": trained["model_id"],
            "version": 1,
            "evaluation_id": evaluated["evaluation_id"],
            "approved_by": "sealed-model-owner",
            "approval_reason": "Protected chronological holdout passed",
            "expected_revision": deployment["revision"],
            "maximum_wape": 0.2,
            "maximum_mase": 0.8,
            "minimum_interval_coverage": 0.8,
            "idempotency_key": f"{lifecycle_key}-promote",
        },
    )
    batch = f"SEED-{args.seed}-{nonce}"
    title = f"Lilies replenishment {batch}"
    inputs = {**case["inputs"], "batch_id": batch}
    workspace = args.workspace_root / batch
    workspace.mkdir(parents=True, exist_ok=True)
    for run_name in ("primary", "replay", "rejected", "infeasible"):
        (workspace / run_name).mkdir(exist_ok=True)
    before = erpnext_requests(args.erpnext_base, args.erpnext_token, title)
    primary = run_workflow(
        base_url=args.platform_base,
        token=args.platform_token,
        application_id=args.application_id,
        version=args.version,
        inputs=inputs,
        workspace_path=str(workspace / "primary"),
        resume_values={
            "decision": "approve",
            "reviewer": "sealed-planner",
            "comment": "Protected plan reviewed",
        },
        timeout_seconds=240,
    )
    after_primary = erpnext_requests(args.erpnext_base, args.erpnext_token, title)
    replay = run_workflow(
        base_url=args.platform_base,
        token=args.platform_token,
        application_id=args.application_id,
        version=args.version,
        inputs=inputs,
        workspace_path=str(workspace / "replay"),
        resume_values={
            "decision": "approve",
            "reviewer": "sealed-planner",
            "comment": "Protected replay reviewed",
        },
        timeout_seconds=240,
    )
    after_replay = erpnext_requests(args.erpnext_base, args.erpnext_token, title)
    rejected_inputs = {**case["inputs"], "batch_id": f"{batch}-REJECT"}
    rejected = run_workflow(
        base_url=args.platform_base,
        token=args.platform_token,
        application_id=args.application_id,
        version=args.version,
        inputs=rejected_inputs,
        workspace_path=str(workspace / "rejected"),
        resume_values={
            "decision": "reject",
            "reviewer": "sealed-planner",
            "comment": "Protected rejection",
        },
        timeout_seconds=240,
    )
    infeasible_inputs = {
        **case["inputs"],
        "batch_id": f"{batch}-INFEASIBLE",
        "constraints": {"capacity": 1, "budget": 1},
    }
    infeasible = run_workflow(
        base_url=args.platform_base,
        token=args.platform_token,
        application_id=args.application_id,
        version=args.version,
        inputs=infeasible_inputs,
        workspace_path=str(workspace / "infeasible"),
        timeout_seconds=240,
    )
    primary_outputs = primary.get("outputs", {})
    replay_outputs = replay.get("outputs", {})
    rejected_outputs = rejected.get("outputs", {})
    infeasible_outputs = infeasible.get("outputs", {})
    readback_lines = primary_outputs.get("result", {}).get("readback", {}).get("items", [])
    physical_quantities = {str(line["item_code"]): float(line["qty"]) for line in readback_lines}
    forecast_totals = {
        str(row["series_id"]): float(row["forecast_total"])
        for row in primary_outputs.get("forecast", {}).get("forecasts", [])
    }
    checks = {
        "chronological_evaluation_passed": (
            evaluated["metrics"]["wape"] <= 0.2
            and evaluated["metrics"]["mase"] <= 0.8
            and evaluated["metrics"]["interval_coverage"] >= 0.8
        ),
        "approved_deployment_used": (
            primary_outputs.get("forecast", {}).get("model_id") == trained["model_id"]
            and primary_outputs.get("forecast", {}).get("deployment_revision")
            == promoted["revision"]
        ),
        "forecast_totals_match_independent_seasonal_expectation": (
            forecast_totals == case["forecast_totals"]
        ),
        "retraining_signal_correct": (
            primary_outputs.get("forecast", {}).get("monitoring", {}).get("retraining_recommended")
            == case["drift_expected"]
        ),
        "no_online_training": (
            primary_outputs.get("forecast", {})
            .get("monitoring", {})
            .get("automatic_training_triggered")
            is False
        ),
        "primary_succeeded": primary["status"] == "succeeded",
        "primary_created_one_draft": (
            not before
            and len(after_primary) == 1
            and primary_outputs.get("result", {}).get("status") == "draft_created"
        ),
        "draft_remains_unsubmitted": (
            primary_outputs.get("result", {}).get("readback", {}).get("docstatus") == 0
        ),
        "draft_quantities_match_independent_balance": (
            physical_quantities == case["expected_orders"]
        ),
        "replay_succeeded": replay["status"] == "succeeded",
        "replay_reused_without_new_write": (
            len(after_replay) == 1
            and replay_outputs.get("result", {}).get("status") == "draft_reused"
            and replay_outputs.get("result", {}).get("host_write") is False
        ),
        "rejected_succeeded_without_write": (
            rejected["status"] == "succeeded"
            and rejected_outputs.get("result", {}).get("status") == "rejected"
            and rejected_outputs.get("result", {}).get("host_write") is False
        ),
        "infeasible_succeeded_without_write": (
            infeasible["status"] == "succeeded"
            and infeasible_outputs.get("result", {}).get("status") == "infeasible"
            and infeasible_outputs.get("result", {}).get("host_write") is False
        ),
        "infeasibility_names_both_shared_resources": (
            set(infeasible_outputs.get("plan", {}).get("binding_constraints", []))
            == {"capacity", "budget"}
        ),
        "all_runs_have_three_artifacts": all(
            artifact_count(run) == 3 for run in (primary, replay, rejected, infeasible)
        ),
        "human_pause_counts_correct": (
            primary["resume_count"] == 1
            and replay["resume_count"] == 1
            and rejected["resume_count"] == 1
            and infeasible["resume_count"] == 0
        ),
    }
    summary = {
        "schema_version": "exp-lilies-006-sealed-seed-summary-v1",
        "seed": args.seed,
        "seed_fingerprint": lifecycle_key,
        "input_details_exposed": False,
        "oracle_details_exposed": False,
        "workflow_version": args.version,
        "deployment_revision": promoted["revision"],
        "model_digest": trained["model_digest"],
        "run_count": 4,
        "succeeded_runs": sum(
            run["status"] == "succeeded" for run in (primary, replay, rejected, infeasible)
        ),
        "human_pauses": sum(run["resume_count"] for run in (primary, replay, rejected, infeasible)),
        "artifact_count": sum(
            artifact_count(run) for run in (primary, replay, rejected, infeasible)
        ),
        "material_request_primary_count": len(after_primary),
        "material_request_replay_count": len(after_replay),
        "checks_passed": sum(checks.values()),
        "checks_total": len(checks),
        "all_passed": all(checks.values()),
        "failed_check_categories": sorted(name for name, passed in checks.items() if not passed),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
