"""线上真实出现过的构建报错，一条都不许以英文原样见业主。

语料不是编的：从真机库里把所有不同的构建报错取出来，抹掉 uuid 与具体数字
留下形状。加新的错误类型时若忘了配翻译，这里会红。
"""
import re
import unittest

from agent_platform.assistant_agent import _build_situation

# 真机语料（2026-08-28 采集）
REAL_ERRORS = (
    'OpenAI-compatible API returned 400: {"object":"error","message":"This model\'s '
    'maximum context length is 32768 tokens. However..."}',
    "UNIQUE constraint failed: events.stream_id, events.seq",
    "acceptance still failing after 4 repair cycles",
    "builder build timed out after 900s (elapsed 901.2s)",
    "builder stopped before mandatory tests passed",
    "builder stopped with invalid draft: workflow must contain exactly one start or "
    "schedule_trigger node",
    "maximum repair cycles reached (4) for this revision — change the draft before "
    "running tests again",
    'model perseverating: identical rejected proposal 3x — catalog_get:{"type": "sum_by_store"}',
    "model perseverating: identical rejected proposal 3x — run_inspect:blocked",
    "model stream timed out after 600s",
    "platform restarted while building — resume to continue",
    "platform task is not running: 7d5ffa06-f1c7-4937-8ba6-54efb001cb03 status=failed",
    "scaffold budget exhausted with invalid draft: 模型始终未宣布完成",
    "test-author budget exhausted without a mandatory acceptance test",
    "tool call budget exceeded: 201 > 200",
    "turn budget exhausted after 40 proposals",
)

# 英文单词连成串就是没翻译干净；也别把 uuid、花括号、状态码带出来
ENGLISH_RUN = re.compile(r"[a-z]{3,}\s+[a-z]{3,}")
RAW_BITS = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}|[{}]|needs_attention|status=")


class BuildErrorTranslationTest(unittest.TestCase):
    def test_every_real_error_becomes_plain_chinese(self):
        for error in REAL_ERRORS:
            situation, what_to_do = _build_situation("needs_attention", None, error)
            self.assertIn("（", situation, f"没翻出原因：{error[:60]}")
            for text, label in ((situation, "情况"), (what_to_do, "接下来")):
                self.assertIsNone(ENGLISH_RUN.search(text.lower()),
                                  f"{label} 里漏了英文：{text}")
                self.assertIsNone(RAW_BITS.search(text),
                                  f"{label} 里漏了原始片段：{text}")

    def test_unknown_error_degrades_without_leaking(self):
        # 没见过的错误宁可不解释，也不能把原文抬出来
        situation, what_to_do = _build_situation(
            "needs_attention", None, "some brand new failure nobody mapped yet")
        self.assertNotIn("brand", situation)
        self.assertTrue(situation.strip())
        self.assertTrue(what_to_do.strip())
