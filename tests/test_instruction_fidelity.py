"""业主的指示与答复要带上原话——转述走样就会干错活。

回归背景（2026-08-28 真机）：已证实模型会改写工具参数里的自由文本
（验收那次把「第一行/第二行/第三行」改写成「abc/de/f」）。
同一个模型也在填 resume_build.message —— 那条在搭建方那边被当成
「The owner replied:」并标为最高优先级。
"""
import unittest

from agent_platform.assistant_agent import _keep_owner_words

OWNER = "告诉它净字数不算换行，只数汉字"


class KeepOwnerWordsTest(unittest.TestCase):
    def test_paraphrase_carries_the_original_along(self):
        merged = _keep_owner_words("净字数应排除换行符", OWNER)
        self.assertIn("净字数应排除换行符", merged)
        self.assertIn(OWNER, merged)
        self.assertIn("业主原话", merged)

    def test_faithful_rendition_is_not_duplicated(self):
        # 转述已经把原话完整包住了，再贴一遍只是噪音
        merged = _keep_owner_words(f"请这样改：{OWNER}。改完重新发布。", OWNER)
        self.assertEqual(merged.count("只数汉字"), 1)
        self.assertNotIn("业主原话", merged)

    def test_whitespace_differences_do_not_count_as_new_information(self):
        merged = _keep_owner_words("请这样改： 告诉它净字数不算换行，只数汉字",
                                   "告诉它净字数不算换行，只数汉字")
        self.assertNotIn("业主原话", merged)

    def test_empty_rendition_falls_back_to_the_original(self):
        self.assertEqual(_keep_owner_words("", OWNER), OWNER)

    def test_no_owner_words_leaves_the_rendition_alone(self):
        self.assertEqual(_keep_owner_words("改一下", ""), "改一下")
        self.assertEqual(_keep_owner_words("改一下", None), "改一下")

    def test_both_empty_is_empty(self):
        self.assertEqual(_keep_owner_words("", ""), "")

    def test_specifics_dropped_by_the_paraphrase_are_recoverable(self):
        # 真机那类改写：具体数据被换掉了，原话必须还在
        merged = _keep_owner_words(
            "样例：「abc」「de」「f」，行数 3、净字数 5",
            "样例：「第一行」「第二行」「第三行」，行数 3、净字数 5")
        self.assertIn("第一行", merged)
