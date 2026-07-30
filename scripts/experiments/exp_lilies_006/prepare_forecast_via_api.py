#!/usr/bin/env python3
"""Train, chronologically evaluate, and promote the public forecast model."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def call(
    method: str,
    base_url: str,
    token: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> Any:
    data = (
        json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        if body is not None
        else None
    )
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path}: HTTP {error.code}: {detail}") from error
    return json.loads(payload) if payload else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8018")
    parser.add_argument("--token", required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--deployment-name", default="exp006-demand-production")
    parser.add_argument("--key-prefix", default="exp006-public-r1")
    args = parser.parse_args()
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    training = [
        {"series_id": item["series_id"], "points": item["points"][:21]}
        for item in fixture["history"]
    ]
    held_out = [
        {
            "series_id": item["series_id"],
            "history": item["points"][:21],
            "actual": item["points"][21:],
        }
        for item in fixture["history"]
    ]
    trained = call(
        "POST",
        args.base_url,
        args.token,
        "/api/v1/forecast-models/train",
        {
            "model_name": "EXP006 weekly demand",
            "unit": fixture["unit"],
            "series": training,
            "algorithm": "seasonal_naive",
            "seasonal_period": 7,
            "interval_coverage": 0.9,
            "retraining_wape_threshold": 0.25,
            "source": {
                "kind": "customer_authorized_public_history",
                "split": "first 21 days train; final 7 days holdout",
            },
            "idempotency_key": f"{args.key_prefix}-train",
        },
    )
    evaluated = call(
        "POST",
        args.base_url,
        args.token,
        (f"/api/v1/forecast-models/{trained['model_id']}/versions/{trained['version']}/evaluate"),
        {
            "series": held_out,
            "idempotency_key": f"{args.key_prefix}-evaluate",
        },
    )
    promoted = call(
        "POST",
        args.base_url,
        args.token,
        f"/api/v1/forecast-deployments/{args.deployment_name}/promote",
        {
            "model_id": trained["model_id"],
            "version": trained["version"],
            "evaluation_id": evaluated["evaluation_id"],
            "approved_by": "exp006-model-owner",
            "approval_reason": "Chronological public holdout met the frozen gates",
            "expected_revision": 0,
            "maximum_wape": 0.2,
            "maximum_mase": 0.8,
            "minimum_interval_coverage": 0.8,
            "idempotency_key": f"{args.key_prefix}-promote",
        },
    )
    print(
        json.dumps(
            {
                "trained": trained,
                "evaluated": evaluated,
                "promoted": promoted,
                "production_online_training": False,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
