"""Builder 注册表：多套 builder 按名注册、按构建路由、老记录回落默认引擎。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_platform.builder_registry import DEFAULT_BUILDER_NAME, BuilderRegistry


class StubEngine:
    def __init__(self) -> None:
        self.active: dict[str, asyncio.Task[Any]] = {}
        self.started: list[str] = []

    def start(self, build_id: str) -> None:
        self.started.append(build_id)

    def cancel(self, build_id: str) -> None:  # pragma: no cover - 协议占位
        raise KeyError("active build not found")

    def queue_resume_message(self, build_id: str, message: str) -> None:  # pragma: no cover
        pass

    def post_live_message(self, build_id: str, message: str) -> None:  # pragma: no cover
        pass

    async def run_claimed_build(self, build_id: str) -> dict[str, Any]:  # pragma: no cover
        return {"build_id": build_id}


def test_register_and_route_by_name() -> None:
    registry = BuilderRegistry()
    classic = StubEngine()
    ensemble = StubEngine()
    registry.register("classic", classic)
    registry.register("small-ensemble", ensemble)

    assert registry.names() == ["classic", "small-ensemble"]
    assert registry.get("small-ensemble") is ensemble
    assert registry.get() is classic  # 不给名字 = 默认引擎


def test_for_build_routes_by_record_and_falls_back_for_legacy_rows() -> None:
    registry = BuilderRegistry()
    classic = StubEngine()
    ensemble = StubEngine()
    registry.register(DEFAULT_BUILDER_NAME, classic)
    registry.register("small-ensemble", ensemble)

    assert registry.for_build({"id": "b1", "builder": "small-ensemble"}) is ensemble
    # 迁移前的老构建记录没有 builder 字段/为 None：必须回落 classic，不能报错
    assert registry.for_build({"id": "b2"}) is classic
    assert registry.for_build({"id": "b3", "builder": None}) is classic


def test_unknown_and_duplicate_names_fail_loudly() -> None:
    registry = BuilderRegistry()
    registry.register("classic", StubEngine())

    with pytest.raises(KeyError, match="unknown builder: nope"):
        registry.get("nope")
    with pytest.raises(ValueError, match="already registered"):
        registry.register("classic", StubEngine())
    with pytest.raises(ValueError, match="non-empty"):
        registry.register("  ", StubEngine())
    assert registry.has("classic")
    assert not registry.has("nope")


def test_active_task_scans_all_engines() -> None:
    async def scenario() -> None:
        registry = BuilderRegistry()
        classic = StubEngine()
        ensemble = StubEngine()
        registry.register("classic", classic)
        registry.register("small-ensemble", ensemble)

        task = asyncio.create_task(asyncio.sleep(0))
        ensemble.active["b1"] = task
        assert registry.active_task("b1") is task
        assert registry.active_task("missing") is None
        await task

    asyncio.run(scenario())
