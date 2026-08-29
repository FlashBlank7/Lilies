"""客户端靠着活、平台这边一条测试都没有的那几个接口。

数出来的（2026-08-30）：api.py 有 236 条路由，其中 22 条在 2658 条测试里
连关键词都没出现过。挑出**有人真的在调**的补上——测一个没人调的接口，
价值不如把它删掉；测一个客户端天天调的接口，坏了才有人知道。

  /api/v1/applications-archived    guanjia 的「收起来的有哪些」
  /api/v1/applications-archivable  guanjia 的「哪些可以收起来」
两个都出现在客户端的接口契约表里（doctor --contract 会逐个探），
而平台这侧零覆盖：它们要是坏了，2658 条测试照样全绿，
坏消息由用户在终端里替我们发现。

另外两个是**没有任何调用方**的（前端、客户端、脚本里都搜不到）：
  /api/v1/orchestration/advise
  /api/v1/module-protocol/validate
它们能跑（真机 200 验过），但既没人调也没人测。删不删是产品决定，
不是我该替谁做的；在那之前先钉一条"别静默 500"——
活着的接口炸了总该有人知道。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings

HEADERS = {"Authorization": "Bearer endpoint-test"}


class _AppCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.app = create_app(Settings(api_token="endpoint-test",
                                       data_dir=root / "d",
                                       workspace_root=root / "w",
                                       scheduler_poll_seconds=3600))

    def get(self, url: str, **params):
        with TestClient(self.app) as client:
            return client.get(url, headers=HEADERS, params=params or None)


class ArchiveListingsAreServed(_AppCase):
    """guanjia 的收起/拿回那条动线，两头都要在。"""

    # 形状是 {"total": N, "items": [...]}，不是裸列表。
    # 这不是随口写的：客户端老远端 404 时的兜底就是
    # `{"total": 0, "items": [], "unsupported": True}`（guanjia/app.py），
    # 也就是说 total+items 这两个键是两边说好的。裸列表会让页面拿不到东西。
    def test_archived_listing_answers(self):
        response = self.get("/api/v1/applications-archived")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("items", body)
        self.assertIn("total", body)
        self.assertIsInstance(body["items"], list)

    def test_archivable_listing_answers(self):
        response = self.get("/api/v1/applications-archivable")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("items", body)
        self.assertIn("total", body)
        self.assertIsInstance(body["items"], list)

    def test_the_total_is_not_just_the_page_length(self):
        """total 要是"一共几个"。等于 len(items) 时看不出区别，
        所以这里只钉它是个整数、且不小于列出的条数——
        真值口径由归档那几条测试管，这里守的是"这个键别消失"。"""
        body = self.get("/api/v1/applications-archived").json()
        self.assertIsInstance(body["total"], int)
        self.assertGreaterEqual(body["total"], len(body["items"]))

    def test_archivable_takes_the_idle_window(self):
        """客户端传 days_idle（管家默认 3 天）——参数不认就等于没这个功能。"""
        response = self.get("/api/v1/applications-archivable", days_idle=7)
        self.assertEqual(response.status_code, 200, response.text)

    def test_both_need_a_token(self):
        """这两个会列出业主的工作流名字，不能裸奔。"""
        with TestClient(self.app) as client:
            for url in ("/api/v1/applications-archived",
                        "/api/v1/applications-archivable"):
                self.assertIn(client.get(url).status_code, (401, 403), url)


class EndpointsWithNoCallerStillMustNotBlowUp(_AppCase):
    """没人调 ≠ 可以静默炸。删不删是产品决定，在那之前先别 500。"""

    def test_orchestration_advise_answers(self):
        response = self.get("/api/v1/orchestration/advise",
                            requirement="每天统计门店销量并发日报")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("recommended_blocks", body)

    def test_advice_only_names_blocks_that_exist(self):
        """推荐一个注册表里没有的积木，照着做的人（和搭建智能体）必然扑空。"""
        from agent_platform.blocks import build_block_registry

        known = {item.type for item in build_block_registry().list()}
        response = self.get("/api/v1/orchestration/advise",
                            requirement="按门店汇总销量再生成报表")
        for item in response.json().get("recommended_blocks", []):
            self.assertIn(item["block_type"], known, item)
        for sequence in response.json().get("sequences", []):
            for block_type in sequence.get("sequence", []):
                self.assertIn(block_type, known, sequence)

    def test_an_empty_requirement_is_not_a_crash(self):
        response = self.get("/api/v1/orchestration/advise", requirement="")
        self.assertEqual(response.status_code, 200, response.text)

    def test_module_protocol_validate_answers(self):
        response = self.get("/api/v1/module-protocol/validate",
                            data=json.dumps({"ok": True}))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("valid", response.json())

    def test_broken_json_is_a_verdict_not_a_500(self):
        """校验接口收到坏 JSON 是**日常**，不是异常——它就是干这个的。"""
        response = self.get("/api/v1/module-protocol/validate", data="{不是json")
        self.assertLess(response.status_code, 500, response.text)
