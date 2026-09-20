"""积木执行覆盖：目录里登记的每个积木，运行时都必须真的能执行。

`_execute_node` 用一条 37 分支的 `isinstance` 链派发，末尾兜底
`raise RuntimeError("block executor missing: ...")`。这条链和 `BlockRegistry`
之间没有任何机械关联——目录里加一个积木、派发链忘了加分支，只有在客户真跑
到那个节点时才炸，而且报错发生在运行时、不在校验期。

这道测试就是把那层关联补上：对每个已注册积木，用默认配置执行一次，只断言
它**没有掉进兜底分支**。缺输入、缺 provider、缺上游数据导致的其他异常都算通过
——这里只问"有没有执行器"，不问"执行得对不对"。

同时它是拆分 `_execute_node`（908 行）的安全网：把 isinstance 链换成执行器
注册表时，任何一个积木漏登记，这里当场红。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_platform.api import build_services
from agent_platform.blocks import default_config_for
from agent_platform.config import Settings
from agent_platform.workflow_models import ApplicationSnapshot, NodeSpec, WorkflowSpec

MISSING_EXECUTOR_MARKER = "block executor missing"


def _registered_block_types(tmp_path: Path) -> list[str]:
    from agent_platform.blocks import build_block_registry

    return sorted(definition.type for definition in build_block_registry().list())


def test_every_registered_block_has_an_executor(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", workspace_root=tmp_path / "ws")
    settings.prepare()
    services = build_services(settings, None)
    registry, runtime = services.blocks, services.workflow_runtime

    async def scenario() -> list[str]:
        await services.storage.initialize()
        await services.workflow_store.initialize()
        without_executor: list[str] = []
        for definition in registry.list():
            config = default_config_for(definition.type, registry.config_model(definition.type)) or {}
            node = NodeSpec(id="n1", type=definition.type, title=definition.title, config=config)
            snapshot = ApplicationSnapshot(workflow=WorkflowSpec(nodes=[node], edges=[]))
            try:
                await runtime._execute_node(snapshot, node, {}, {}, ".", "run-1", "n1", None)
            except Exception as error:  # noqa: BLE001 — 只关心兜底分支这一种失败
                if MISSING_EXECUTOR_MARKER in str(error):
                    without_executor.append(definition.type)
        return without_executor

    without_executor = asyncio.run(scenario())
    assert not without_executor, (
        "这些积木登记在目录里、会被莉莉丝选用，但运行时没有执行器，"
        f"客户跑到该节点才会炸：{without_executor}"
    )


def test_registry_is_the_single_source_of_block_types(tmp_path: Path) -> None:
    """目录非空且无重复登记——派发表要对齐的就是这份清单。"""
    types = _registered_block_types(tmp_path)
    assert len(types) > 40, f"积木目录只剩 {len(types)} 个，注册表可能没建全"
    assert len(types) == len(set(types)), "同一积木类型被登记了两次"

def test_executor_registry_and_block_catalog_agree() -> None:
    """结构不变量：执行器注册表与积木目录必须是同一份清单。

    这是 `_execute_node` 从 isinstance 链换成注册表之后才可能存在的断言——
    链的覆盖只能靠"逐个跑一遍看会不会掉进兜底"来探测，注册表可以直接对账。
    两个方向都要查：
      • 目录有、注册表没有 → 客户跑到那个节点才炸（soft_block 就是这么漏的）
      • 注册表有、目录没有 → 登记了一个谁也选不到的执行器（多半是类型名拼错）
    """
    from agent_platform.blocks import build_block_registry
    from agent_platform.workflow_runtime import _NODE_EXECUTORS

    catalog = {definition.type for definition in build_block_registry().list()}
    registered = set(_NODE_EXECUTORS)

    assert not (catalog - registered), (
        f"目录里有、运行时没有执行器：{sorted(catalog - registered)}"
    )
    assert not (registered - catalog), (
        f"登记了执行器但目录里没有这个积木（类型名拼错？）：{sorted(registered - catalog)}"
    )
