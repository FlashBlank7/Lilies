"""软积木：24 个策略各指向哪个真积木，以及家族名写错时会怎样。

soft_block 197 行，被 blocks / api / workflow_runtime 用着，此前零测试。

它是**设计期的宏**：搭建时选一个策略，运行时直接换成对应的离散积木
（workflow_runtime 里 `get_discrete_block_type(strategy)`，映射不到就
当场 RuntimeError）。所以"策略 → 积木"这张表一旦和积木注册表脱节，
症状不是搭不出来，是**搭好、发布、真跑的时候才炸**——
"函数写好了没接在调用点上"这一周撞了四次，这里是同一个形状。

另一件当场量出来的事：`list_strategies("拼错的家族名")` 原来返回**全部 24 个**。
`if family and family in FAMILY_MAP` 匹配不上就掉进"不过滤"那一支，
调用方拿到一个没有任何标注的扁平列表，以为那就是这个家族的。
筛不着就返回全部，比返回空更坏——他拿到了答案，而且是错的。
接口 /api/v1/soft-block/strategies 上是同一处写法，同样改掉了。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_platform import blocks
from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.soft_block import (
    FAMILY_MAP,
    get_discrete_block_type,
    get_family,
    list_strategies,
    strategy_help,
)

EVERY_STRATEGY = [s for family in FAMILY_MAP.values() for s in family]
REAL_BLOCKS = {row[0] for row in blocks._AGENT_ARCHITECTURE_BLOCKS}
TOKEN = "soft-block-test"


@pytest.fixture
def client(tmp_path: Path):
    from tests.test_runtime import ScriptedProvider

    settings = Settings(api_token=TOKEN, data_dir=tmp_path / "d",
                        workspace_root=tmp_path / "w")
    with TestClient(create_app(settings, ScriptedProvider())) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield c


def test_the_registry_is_not_empty():
    """先钉住量的量级。

    下面每条都是拿 EVERY_STRATEGY / REAL_BLOCKS 遍历的——两个集合要是空了，
    整个文件会一路全绿却什么都没验（今天门链里的 ruff 正是这么空转的：
    路径写错、扫了 0 个文件、报"全部通过"）。
    """
    assert len(EVERY_STRATEGY) >= 20, EVERY_STRATEGY
    assert len(REAL_BLOCKS) >= 20, REAL_BLOCKS


@pytest.mark.parametrize("strategy", EVERY_STRATEGY)
def test_every_strategy_points_at_a_block_that_exists(strategy):
    """指向的积木必须真在注册表里，否则发布之后第一次跑才会炸。"""
    target = get_discrete_block_type(strategy)
    assert target is not None, f"{strategy} 映射不到任何积木"
    assert target in REAL_BLOCKS, f"{strategy} 指向 {target}，注册表里没有"


@pytest.mark.parametrize("strategy", EVERY_STRATEGY)
def test_every_strategy_has_a_family_and_a_real_help_line(strategy):
    """说明不能是把策略名原样退回来——那是"看着有文档"的假象。"""
    assert get_family(strategy) in FAMILY_MAP
    help_text = strategy_help(strategy)
    assert help_text and help_text != strategy, f"{strategy} 没有真正的说明"


def test_an_unknown_strategy_maps_to_nothing():
    """认不出来就答"没有"，别猜一个最像的给运行时。"""
    assert get_discrete_block_type("context_assmble") is None


class TestAMistypedFamilyIsNotSilentlyIgnored:
    """筛不着就返回全部——他拿到了答案，而且是错的。"""

    def test_it_raises_instead_of_returning_everything(self):
        with pytest.raises(KeyError) as caught:
            list_strategies("modl")
        assert "modl" in str(caught.value)
        # 报错要带上正确选项，否则调用方只知道错了、不知道该写什么
        assert "model" in str(caught.value)

    @pytest.mark.parametrize("family", sorted(FAMILY_MAP))
    def test_a_real_family_returns_only_its_own(self, family):
        got = list_strategies(family)
        assert set(got) == set(FAMILY_MAP[family])
        assert 0 < len(got) < len(EVERY_STRATEGY), "过滤要真的过滤掉一些"

    def test_no_family_still_means_everything(self):
        """不传家族名＝要全部，这一支不能被上面那条误伤。"""
        assert set(list_strategies()) == set(EVERY_STRATEGY)


class TestTheEndpointSaysWhenTheFamilyIsWrong:
    def test_a_mistyped_family_is_rejected(self, client):
        response = client.get("/api/v1/soft-block/strategies?family=modl")
        assert response.status_code == 400, response.text
        assert "modl" in response.text
        assert "model" in response.text, "要告诉他正确的家族名有哪些"

    def test_a_real_family_returns_only_its_own(self, client):
        response = client.get("/api/v1/soft-block/strategies?family=model")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["family"] == "model"
        assert set(body["strategies"]) == set(FAMILY_MAP["model"])

    def test_no_family_returns_all_of_them(self, client):
        body = client.get("/api/v1/soft-block/strategies").json()
        assert set(body["families"]) == set(FAMILY_MAP)
        listed = {s for group in body["strategies"].values() for s in group}
        assert listed == set(EVERY_STRATEGY)
