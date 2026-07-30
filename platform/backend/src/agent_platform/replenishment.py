from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from pydantic import BaseModel, Field, field_validator


class ReplenishmentItem(BaseModel):
    item_code: str = Field(min_length=1, max_length=200)
    inventory: float = Field(ge=0)
    inbound: float = Field(default=0, ge=0)
    safety_stock: float = Field(default=0, ge=0)
    moq: float = Field(default=0, ge=0)
    lot_size: float = Field(gt=0)
    unit_cost: float = Field(ge=0)
    minimum_fulfillment: float = Field(default=0, ge=0, le=1)
    priority_weight: float = Field(default=1, gt=0)
    warehouse: str | None = None


class ReplenishmentPlanRequest(BaseModel):
    forecasts: list[dict[str, Any]] = Field(min_length=1, max_length=1_000)
    items: list[ReplenishmentItem] = Field(min_length=1, max_length=50)
    capacity: float = Field(gt=0)
    budget: float = Field(gt=0)
    solver_version: str = Field(default="bounded-planner-v1", pattern=r"^[A-Za-z0-9_.-]+$")
    max_candidates_per_item: int = Field(default=100, ge=2, le=1_000)
    max_states: int = Field(default=100_000, ge=100, le=1_000_000)

    @field_validator("items")
    @classmethod
    def unique_items(cls, value: list[ReplenishmentItem]) -> list[ReplenishmentItem]:
        if len({item.item_code for item in value}) != len(value):
            raise ValueError("replenishment item codes must be unique")
        return value


def _digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _round_up(value: float, lot_size: float) -> float:
    return math.ceil(max(0, value) / lot_size - 1e-12) * lot_size


def solve_replenishment(request: ReplenishmentPlanRequest) -> dict[str, Any]:
    forecast_by_item: dict[str, float] = {}
    for row in request.forecasts:
        series_id = str(row.get("series_id", "")).strip()
        if not series_id or "forecast_total" not in row:
            raise ValueError("each forecast needs series_id and forecast_total")
        if series_id in forecast_by_item:
            raise ValueError(f"duplicate forecast series: {series_id}")
        total = float(row["forecast_total"])
        if not math.isfinite(total) or total < 0:
            raise ValueError("forecast totals must be finite and non-negative")
        forecast_by_item[series_id] = total
    missing = sorted(
        item.item_code for item in request.items if item.item_code not in forecast_by_item
    )
    if missing:
        raise ValueError(f"forecasts missing for items: {missing}")

    candidates: list[list[dict[str, float]]] = []
    requirements: list[dict[str, float | str]] = []
    for item in request.items:
        demand = forecast_by_item[item.item_code]
        available = item.inventory + item.inbound
        ideal = max(0, demand + item.safety_stock - available)
        minimum = max(
            0,
            demand * item.minimum_fulfillment + item.safety_stock - available,
        )
        ideal_order = _round_up(ideal, item.lot_size)
        minimum_order = _round_up(minimum, item.lot_size)
        if ideal_order > 0:
            ideal_order = max(ideal_order, item.moq)
        if minimum_order > 0:
            minimum_order = max(minimum_order, item.moq)
        maximum_order = max(ideal_order, minimum_order)
        option_count = int(round(maximum_order / item.lot_size)) + 1
        if option_count > request.max_candidates_per_item:
            raise ValueError(f"item {item.item_code} exceeds the bounded candidate limit")
        options: list[dict[str, float]] = []
        for index in range(option_count):
            quantity = index * item.lot_size
            if 0 < quantity < item.moq:
                continue
            projected = available + quantity - demand
            fulfilled = min(demand, max(0, available + quantity - item.safety_stock))
            fulfillment = 1 if demand == 0 else fulfilled / demand
            if quantity + 1e-9 < minimum_order:
                continue
            shortage = max(0, item.safety_stock - projected)
            excess = max(0, projected - item.safety_stock)
            service_value = item.priority_weight * (fulfilled - shortage * 2 - excess * 0.001)
            options.append(
                {
                    "quantity": quantity,
                    "cost": quantity * item.unit_cost,
                    "service_value": service_value,
                    "projected_inventory": projected,
                    "fulfillment": fulfillment,
                }
            )
        candidates.append(options)
        requirements.append(
            {
                "item_code": item.item_code,
                "forecast_total": demand,
                "available_before_order": available,
                "ideal_order": ideal_order,
                "minimum_order": minimum_order,
            }
        )

    if any(not options for options in candidates):
        return _infeasible_result(
            request,
            requirements,
            "at least one item has no quantity satisfying its minimum fulfillment contract",
        )

    states: dict[tuple[float, float], tuple[float, list[dict[str, float]]]] = {
        (0.0, 0.0): (0.0, [])
    }
    for options in candidates:
        next_states: dict[tuple[float, float], tuple[float, list[dict[str, float]]]] = {}
        for (used_capacity, used_budget), (score, chosen) in states.items():
            for option in options:
                capacity = round(used_capacity + option["quantity"], 9)
                budget = round(used_budget + option["cost"], 9)
                if capacity > request.capacity + 1e-9 or budget > request.budget + 1e-9:
                    continue
                key = (capacity, budget)
                candidate = (score + option["service_value"], [*chosen, option])
                existing = next_states.get(key)
                if existing is None or candidate[0] > existing[0]:
                    next_states[key] = candidate
        if not next_states:
            return _infeasible_result(
                request,
                requirements,
                "shared capacity or budget cannot satisfy all minimum fulfillment contracts",
            )
        if len(next_states) > request.max_states:
            ordered = sorted(
                next_states.items(),
                key=lambda value: (
                    value[1][0],
                    -value[0][1],
                    -value[0][0],
                ),
                reverse=True,
            )
            next_states = dict(ordered[: request.max_states])
        states = next_states

    (capacity_used, budget_used), (objective, chosen) = max(
        states.items(),
        key=lambda value: (
            value[1][0],
            -value[0][1],
            -value[0][0],
        ),
    )
    lines: list[dict[str, Any]] = []
    for item, requirement, option in zip(request.items, requirements, chosen, strict=True):
        lines.append(
            {
                **requirement,
                "warehouse": item.warehouse,
                "order_quantity": option["quantity"],
                "order_cost": option["cost"],
                "projected_inventory": option["projected_inventory"],
                "fulfillment": option["fulfillment"],
                "moq": item.moq,
                "lot_size": item.lot_size,
                "unit_cost": item.unit_cost,
            }
        )
    binding: list[str] = []
    if abs(capacity_used - request.capacity) <= 1e-9:
        binding.append("capacity")
    if abs(budget_used - request.budget) <= 1e-9:
        binding.append("budget")
    for line in lines:
        if float(line["order_quantity"]) == float(line["minimum_order"]):
            binding.append(f"minimum_fulfillment:{line['item_code']}")
        if 0 < float(line["order_quantity"]) == float(line["moq"]):
            binding.append(f"moq:{line['item_code']}")
    result = {
        "status": "feasible",
        "solver_version": request.solver_version,
        "objective": objective,
        "capacity": {
            "limit": request.capacity,
            "used": capacity_used,
            "remaining": request.capacity - capacity_used,
        },
        "budget": {
            "limit": request.budget,
            "used": budget_used,
            "remaining": request.budget - budget_used,
        },
        "binding_constraints": sorted(set(binding)),
        "lines": lines,
        "infeasibility": None,
    }
    return {**result, "plan_digest": _digest(result)}


def _infeasible_result(
    request: ReplenishmentPlanRequest,
    requirements: list[dict[str, float | str]],
    reason: str,
) -> dict[str, Any]:
    minimum_capacity = sum(float(row["minimum_order"]) for row in requirements)
    item_by_code = {item.item_code: item for item in request.items}
    minimum_budget = sum(
        float(row["minimum_order"]) * item_by_code[str(row["item_code"])].unit_cost
        for row in requirements
    )
    deficits = {
        "capacity": max(0, minimum_capacity - request.capacity),
        "budget": max(0, minimum_budget - request.budget),
    }
    binding = [name for name, value in deficits.items() if value > 0]
    result = {
        "status": "infeasible",
        "solver_version": request.solver_version,
        "objective": None,
        "capacity": {
            "limit": request.capacity,
            "minimum_required": minimum_capacity,
        },
        "budget": {
            "limit": request.budget,
            "minimum_required": minimum_budget,
        },
        "binding_constraints": binding,
        "lines": [],
        "infeasibility": {
            "reason": reason,
            "deficits": deficits,
            "requirements": requirements,
        },
    }
    return {**result, "plan_digest": _digest(result)}
