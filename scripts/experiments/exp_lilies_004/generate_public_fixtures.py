#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT = (
    ROOT
    / "docs"
    / "experiments"
    / "lilies-collaboration"
    / "EXP-LILIES-004"
    / "1"
    / "fixtures"
    / "public-inputs"
)
FEATURES = [
    {"name": "temperature_c", "unit": "Cel", "minimum": -20, "maximum": 180},
    {
        "name": "vibration_rms_mm_s",
        "unit": "mm/s",
        "minimum": 0,
        "maximum": 50,
    },
    {"name": "current_a", "unit": "A", "minimum": 0, "maximum": 100},
    {"name": "pressure_bar", "unit": "bar", "minimum": 0, "maximum": 30},
    {"name": "rpm", "unit": "rpm", "minimum": 0, "maximum": 5000},
]
UNITS = {item["name"]: item["unit"] for item in FEATURES}
BASE_TIME_MS = int(
    datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc).timestamp() * 1000
)


def observation(
    rng: random.Random,
    *,
    failure_mode: bool,
    row_index: int,
) -> dict[str, Any]:
    if failure_mode:
        temperature = rng.gauss(91, 8)
        vibration = rng.gauss(7.6, 1.7)
        current = rng.gauss(27, 4)
        pressure = rng.gauss(3.4, 0.7)
        rpm = rng.gauss(1390, 85)
    else:
        temperature = rng.gauss(61, 8)
        vibration = rng.gauss(2.3, 0.9)
        current = rng.gauss(15, 3)
        pressure = rng.gauss(5.4, 0.8)
        rpm = rng.gauss(1460, 55)
    features = {
        "temperature_c": round(min(max(temperature, -20), 180), 4),
        "vibration_rms_mm_s": round(min(max(vibration, 0), 50), 4),
        "current_a": round(min(max(current, 0), 100), 4),
        "pressure_bar": round(min(max(pressure, 0), 30), 4),
        "rpm": round(min(max(rpm, 0), 5000), 4),
    }
    return {
        "record_id": f"row-{row_index:04d}",
        "features": features,
        "units": dict(UNITS),
        "label": int(failure_mode),
    }


def dataset(seed: int, count: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    rows = [
        observation(rng, failure_mode=index % 2 == 1, row_index=index)
        for index in range(count)
    ]
    rng.shuffle(rows)
    return rows


def event(
    event_id: str,
    device_name: str,
    offset_seconds: int,
    features: dict[str, float],
    *,
    units: dict[str, str] | None = None,
    business_case: str,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "device_name": device_name,
        "timestamp_ms": BASE_TIME_MS + offset_seconds * 1000,
        "features": features,
        "units": units or dict(UNITS),
        "business_case": business_case,
    }


def debug_events() -> list[dict[str, Any]]:
    low = {
        "temperature_c": 58.0,
        "vibration_rms_mm_s": 1.8,
        "current_a": 14.0,
        "pressure_bar": 5.8,
        "rpm": 1470.0,
    }
    high = {
        "temperature_c": 101.0,
        "vibration_rms_mm_s": 9.8,
        "current_a": 31.0,
        "pressure_bar": 2.6,
        "rpm": 1325.0,
    }
    uncertain = {
        "temperature_c": 77.0,
        "vibration_rms_mm_s": 4.6,
        "current_a": 21.0,
        "pressure_bar": 4.2,
        "rpm": 1425.0,
    }
    invalid_units = dict(UNITS)
    invalid_units["vibration_rms_mm_s"] = "m/s"
    missing_feature = dict(low)
    missing_feature.pop("current_a")
    records = [
        event(
            "pub-low-001",
            "public-pump-low",
            10,
            low,
            business_case="low_risk_no_alarm",
        ),
        event(
            "pub-high-001",
            "public-pump-high",
            20,
            high,
            business_case="high_risk_automatic_alarm",
        ),
        event(
            "pub-review-approve-001",
            "public-pump-review-approve",
            30,
            uncertain,
            business_case="uncertain_human_approve",
        ),
        event(
            "pub-review-reject-001",
            "public-pump-review-reject",
            40,
            uncertain,
            business_case="uncertain_human_reject",
        ),
        event(
            "pub-unit-conflict-001",
            "public-pump-invalid",
            50,
            low,
            units=invalid_units,
            business_case="invalid_unit_safe_stop",
        ),
        event(
            "pub-missing-001",
            "public-pump-invalid",
            60,
            missing_feature,
            business_case="missing_feature_safe_stop",
        ),
        event(
            "pub-stale-001",
            "public-pump-stale",
            -86_500,
            high,
            business_case="stale_event_safe_stop",
        ),
        event(
            "pub-out-of-order-001",
            "public-pump-order",
            70,
            high,
            business_case="out_of_order_safe_stop",
        ),
        event(
            "pub-high-001",
            "public-pump-high",
            20,
            high,
            business_case="duplicate_event_no_duplicate_alarm",
        ),
    ]
    return records


def drift_window() -> list[dict[str, Any]]:
    return [
        {
            "features": {
                "temperature_c": 112.0 + index,
                "vibration_rms_mm_s": 12.0 + index * 0.2,
                "current_a": 36.0 + index * 0.3,
                "pressure_bar": 2.0,
                "rpm": 1250.0 - index * 4,
            },
            "units": dict(UNITS),
        }
        for index in range(12)
    ]


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def generate(output: Path) -> dict[str, Any]:
    train_rows = dataset(4_004, 240)
    validation_rows = dataset(4_104, 80)
    files = {
        "feature-contract.json": {"features": FEATURES},
        "training.json": {"rows": train_rows},
        "validation.json": {"rows": validation_rows},
        "debug-events.json": {
            "clock": {
                "base_time_ms": BASE_TIME_MS,
                "maximum_age_seconds": 86_400,
            },
            "events": debug_events(),
        },
        "drift-window.json": {"observations": drift_window()},
    }
    for filename, value in files.items():
        write_json(output / filename, value)
    return {
        "output": str(output),
        "files": sorted(files),
        "training_rows": len(train_rows),
        "validation_rows": len(validation_rows),
        "debug_events": len(files["debug-events.json"]["events"]),
        "drift_observations": len(files["drift-window.json"]["observations"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(json.dumps(generate(args.output), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
