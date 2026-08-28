"""验收单是交给业主的文档，不能出现英文状态词。

回归背景（2026-08-29 自查）：真报告里写着

    ### ✅ 三行文本统计（运行：succeeded）

用例跑出异常时更糟，直接写 `error: <英文异常原文>`。
昨天把 today 面板、体检、管家回答、失败告警都翻成人话了，
唯独漏了这份**正式交付文档**——它不在任何一条对话路径上，
所以扫回答的那道门也扫不到它。
"""
import re
import unittest

from agent_platform.acceptance_pm import _run_words, render_report_markdown

ENGLISH_STATUS = re.compile(r"\b(succeeded|failed|paused|cancelled|running|queued)\b")


def _report(run_status: str, passed: bool = True) -> dict:
    return {
        "application_id": "a1", "application_name": "统计", "version": 1,
        "stamp": "2026-08-29 00:00", "summary": "查一查",
        "required_node_types": [], "required_any_node_types": [],
        "architecture_missing": [], "architecture_pass": True,
        "lineage_missing": [], "lineage_pass": True,
        "cases": [{"name": "三行", "run_status": run_status, "passed": passed,
                   "checks": [{"check": "行数 = 3", "passed": passed, "actual": "3"}]}],
        "passed_cases": 1 if passed else 0, "total_cases": 1,
        "owner_examples": "", "accepted": passed,
    }


class RunWordsTest(unittest.TestCase):
    def test_known_statuses_are_translated(self):
        self.assertEqual(_run_words("succeeded"), "跑通了")
        self.assertEqual(_run_words("failed"), "没跑通")

    def test_internal_exception_text_is_not_shown(self):
        # 原本直接把英文异常写进验收单，业主看了只会更慌
        out = _run_words('error: KeyError("node")')
        self.assertNotIn("KeyError", out)
        self.assertIn("平台内部出错", out)

    def test_empty_status_still_reads_as_a_sentence(self):
        self.assertEqual(_run_words(""), "情况不明")


class ReportHasNoEnglishStatusTest(unittest.TestCase):
    def test_passing_report(self):
        text = render_report_markdown(_report("succeeded"))
        self.assertIsNone(ENGLISH_STATUS.search(text), text[:300])
        self.assertIn("跑通了", text)

    def test_failing_report(self):
        text = render_report_markdown(_report("failed", passed=False))
        self.assertIsNone(ENGLISH_STATUS.search(text), text[:300])

    def test_report_with_an_internal_error(self):
        text = render_report_markdown(_report('error: RuntimeError("boom")', passed=False))
        self.assertNotIn("RuntimeError", text)
        self.assertIn("平台内部出错", text)
