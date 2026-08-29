"""给模型的工具载荷：汇总在前，超长要说出来。

回归背景（2026-08-29）：问「一共有多少个生成任务」，它答 25——实际 75。
返回里明明有「一共几个: 75」，动作行摘要也写着"共 75 个构建"。

排查下来是两件事叠在一起：
· 汇总字段排在长列表**后面**。工具结果送模型前会截到 4000 字，
  limit=25 时载荷 3943 字，离上限只差 57——再多一条就静默丢掉总数。
· 截断本身不吭声。模型看的是半截，却不知道是半截，
  于是回「工具没有提供总量字段」。

把汇总挪到列表前面之后，同一个问题连问三次全部答对（75 / 17 / 57 / 1）。
**字段顺序会影响它怎么读**——模型自上而下看，先看到总数就不会去数行数。
"""
import json
import unittest

from agent_platform.assistant_agent import _capped


class CappedPayloadTest(unittest.TestCase):
    def test_a_short_payload_is_untouched(self):
        payload = {"一共几个": 3, "rows": ["a", "b"]}
        self.assertEqual(json.loads(_capped(payload)), payload)

    def test_a_long_payload_says_it_was_cut(self):
        payload = {"rows": ["x" * 100 for _ in range(60)]}
        out = _capped(payload)
        self.assertIn("被截断", out)
        self.assertIn("完整结果", out)

    def test_the_note_says_how_to_get_less(self):
        """只说"被截断"没用，要告诉它下一步怎么办。"""
        out = _capped({"rows": ["x" * 100 for _ in range(60)]})
        self.assertIn("limit", out)

    def test_the_kept_part_is_the_beginning(self):
        """留前面而不是后面：汇总在前，截断先吃掉列表尾巴。"""
        payload = {"一共几个": 999, "rows": ["y" * 100 for _ in range(60)]}
        out = _capped(payload)
        self.assertIn("一共几个", out[:200])
        self.assertIn("999", out[:200])

    def test_the_cap_is_actually_applied(self):
        out = _capped({"rows": ["z" * 100 for _ in range(200)]})
        self.assertLess(len(out), 4200, "截断没生效，会把上下文撑爆")


class SummaryComesFirstTest(unittest.IsolatedAsyncioTestCase):
    """汇总必须排在长列表前面——否则一超长就先被切掉。"""

    async def test_recent_builds_puts_the_total_before_the_list(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        from agent_platform.assistant_agent import WorkflowConcierge

        agent = WorkflowConcierge.__new__(WorkflowConcierge)
        rows = [{"status": "published", "n": 17}, {"status": "needs_attention", "n": 57}]
        conn = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        conn.execute.return_value.fetchall.return_value = rows
        agent.services = SimpleNamespace(workflow_store=SimpleNamespace(
            storage=SimpleNamespace(_connect=lambda: conn),
            list_recent_builds=AsyncMock(return_value=[])))

        result = await agent._exec("recent_builds", {}, {})
        keys = list(result)
        self.assertEqual(keys[0], "一共几个", f"汇总不在最前面：{keys}")
        self.assertLess(keys.index("一共几个"), keys.index("最近几个（不是全部）"))

    async def test_the_list_key_does_not_read_as_the_whole_set(self):
        """键名叫 builds 的时候，它把 25 行读成了"一共 25 个"。"""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        from agent_platform.assistant_agent import WorkflowConcierge

        agent = WorkflowConcierge.__new__(WorkflowConcierge)
        conn = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        conn.execute.return_value.fetchall.return_value = []
        agent.services = SimpleNamespace(workflow_store=SimpleNamespace(
            storage=SimpleNamespace(_connect=lambda: conn),
            list_recent_builds=AsyncMock(return_value=[])))

        result = await agent._exec("recent_builds", {}, {})
        self.assertNotIn("builds", result)
        self.assertIn("不是全部", " ".join(result))


if __name__ == "__main__":
    unittest.main()
