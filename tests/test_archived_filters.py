"""找已收起来的工作流，得答得了「上周收的那些」。

按名字过滤只答得了「叫什么的那个」。业主也会按时间问，
而列表截断在 30 条——不给时间维度，他就只能一条条翻。
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from agent_platform.assistant_agent import WorkflowConcierge


def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


ITEMS = [
    {"id": "a1", "name": "日报基准-一", "archived_at": _ago(0.5)},
    {"id": "a2", "name": "日报基准-二", "archived_at": _ago(3)},
    {"id": "a3", "name": "词频统计-旧", "archived_at": _ago(20)},
]


def _agent():
    services = MagicMock()
    services.workflow_store.list_archived = AsyncMock(return_value=list(ITEMS))
    return WorkflowConcierge(services, MagicMock())


class ArchivedFiltersTest(unittest.IsolatedAsyncioTestCase):
    async def _names(self, args):
        result = await _agent()._exec(
            "tidy_workflows", {"action": "list_archived", **args}, {})
        return [i["name"] for i in result["archived_items"]], result

    async def test_no_filter_lists_everything(self):
        names, result = await self._names({})
        self.assertEqual(len(names), 3)
        self.assertEqual(result["total"], 3)

    async def test_recent_days_filter(self):
        names, _ = await self._names({"days_idle": 7})
        self.assertEqual(names, ["日报基准-一", "日报基准-二"])

    async def test_name_filter_still_works(self):
        names, _ = await self._names({"name_or_id": "词频"})
        self.assertEqual(names, ["词频统计-旧"])

    async def test_name_and_time_combine(self):
        names, _ = await self._names({"name_or_id": "日报", "days_idle": 1})
        self.assertEqual(names, ["日报基准-一"])

    async def test_total_reflects_the_filter_not_the_whole_store(self):
        # total 要是全量的话，模型会说「共 3 个」却只列出 1 个，自相矛盾
        _, result = await self._names({"days_idle": 1})
        self.assertEqual(result["total"], 1)
