"""Builder 引擎注册表 — 多套可互换的 builder 实现按名注册，按构建选择。

动机（2026-08）：论文要做"单大模型 builder vs 小模型集群 builder"的对照实验，
builder 必须可替换、可并存。每个构建在创建时选定一套 builder（builds.builder 列，
默认 classic），之后该构建的启动/取消/插话/续跑全部路由到同一套引擎。

设计约定：harness（工具边界执法）暂不抽公共层，跟随各自 builder 实现——
注册表只回答"这个构建归哪套 builder 管"，不约束引擎内部结构。
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol, runtime_checkable

DEFAULT_BUILDER_NAME = "classic"


@runtime_checkable
class BuilderEngine(Protocol):
    """一套 builder 实现必须暴露的最小接口（与 WorkflowBuilder 现有方法同名同义）。

    - start: 在当前进程异步启动构建循环；重复启动应抛 RuntimeError。
    - cancel: 取消进行中的构建；无进行中任务应抛 KeyError。
    - queue_resume_message: 附加一条业主指示，续跑时第一轮读到。
    - post_live_message: 向进行中的构建投递实时插话；收件箱满应抛 RuntimeError。
    - run_claimed_build: worker 租约模式下同步执行整个构建。
    - active: build_id → asyncio.Task 的进行中任务表。
    """

    active: dict[str, asyncio.Task[Any]]

    def start(self, build_id: str) -> None: ...

    def cancel(self, build_id: str) -> None: ...

    def queue_resume_message(self, build_id: str, message: str) -> None: ...

    def post_live_message(self, build_id: str, message: str) -> None: ...

    async def run_claimed_build(self, build_id: str) -> dict[str, Any]: ...


class BuilderRegistry:
    """按名字管理多套 BuilderEngine；构建记录里的 builder 字段决定路由。"""

    def __init__(self) -> None:
        self._engines: dict[str, BuilderEngine] = {}

    def register(self, name: str, engine: BuilderEngine) -> None:
        key = (name or "").strip()
        if not key:
            raise ValueError("builder name must be non-empty")
        if key in self._engines:
            raise ValueError(f"builder already registered: {key}")
        self._engines[key] = engine

    def names(self) -> list[str]:
        return sorted(self._engines)

    def has(self, name: str) -> bool:
        return (name or "").strip() in self._engines

    def get(self, name: str | None = None) -> BuilderEngine:
        key = (name or "").strip() or DEFAULT_BUILDER_NAME
        try:
            return self._engines[key]
        except KeyError as error:
            raise KeyError(
                f"unknown builder: {key} (registered: {', '.join(self.names()) or 'none'})"
            ) from error

    def for_build(self, build: dict[str, Any]) -> BuilderEngine:
        """按构建记录路由。老构建记录没有 builder 字段时回落默认引擎。"""

        return self.get(str(build.get("builder") or DEFAULT_BUILDER_NAME))

    def active_task(self, build_id: str) -> asyncio.Task[Any] | None:
        """跨所有引擎查找 build_id 的进行中任务（清理/巡检用）。"""

        for engine in self._engines.values():
            task = engine.active.get(build_id)
            if task is not None:
                return task
        return None
