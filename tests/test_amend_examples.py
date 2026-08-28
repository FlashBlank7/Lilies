"""样例写错了，只说变化的那一处就行，不该整句重来。

回归背景（2026-08-28）：我自己踩了两次——给的样例里数字算错
（说净字数 5，实际 11），想改就得把「输入是什么、期望是什么」原样重说一遍。
业主真实的说法是「刚才那个净字数应该是 11」。
"""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

from agent_platform import acceptance_pm
from agent_platform.acceptance_pm import AcceptanceSpec
from agent_platform.assistant_agent import WorkflowConcierge

EARLIER = "输入「甲\n乙\n丙」，应该得到行数 3、净字数 5"


def _spec(owner_words: str) -> AcceptanceSpec:
    return AcceptanceSpec.model_validate({
        "summary": "查行数与净字数",
        "cases": [{"name": "三行", "inputs": {"text": "甲"},
                   "expect": {"equals": {"line_count": 3}}}],
        "owner_words": owner_words,
    })


class AmendExamplesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.services = MagicMock()
        self.services.settings.data_dir = Path(self._tmp.name)
        self.agent = WorkflowConcierge(self.services, MagicMock())
        self.agent._resolve_app = AsyncMock(return_value={
            "id": "a1", "name": "统计", "active_version": 1})

    async def _check(self, examples: str, stored: AcceptanceSpec | None):
        seen: dict = {}

        async def fake_generate(services, app, owner_examples, owner_words=""):
            seen["examples"] = owner_examples
            return _spec(owner_words)

        with patch.object(acceptance_pm, "load_spec", return_value=stored), \
             patch.object(acceptance_pm, "generate_spec", fake_generate), \
             patch.object(acceptance_pm, "save_spec", MagicMock()), \
             patch.object(acceptance_pm, "run_acceptance",
                          AsyncMock(return_value={"cases": [], "passed_cases": 0,
                                                  "total_cases": 0, "accepted": True})):
            await self.agent._exec("acceptance_check",
                                   {"name_or_id": "统计", "examples": examples}, {})
        return seen.get("examples", "")

    async def test_correction_carries_the_earlier_words_along(self):
        sent = await self._check("净字数应该是 11 不是 5", _spec(EARLIER))
        self.assertIn("甲", sent)                 # 上次说的输入还在
        self.assertIn("11", sent)                 # 这次的更正也在
        self.assertIn("以这句为准", sent)          # 谁优先说清楚了

    async def test_first_time_has_nothing_to_carry(self):
        sent = await self._check(EARLIER, None)
        self.assertEqual(sent, EARLIER)
        self.assertNotIn("先前说的", sent)

    async def test_restating_everything_is_not_duplicated(self):
        # 业主重说整段时不该把同一句话贴两遍
        sent = await self._check(EARLIER, _spec(EARLIER))
        self.assertEqual(sent.count("净字数 5"), 1)
        self.assertNotIn("先前说的", sent)
